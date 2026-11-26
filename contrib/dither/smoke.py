"""Smoke test for KasmVNC's X server (contrib/dither/default.nix wraps it as kasmvnc-smoke).

What it checks, each by waiting for the exact thing it expects:
  1. Xkasmvnc starts with the flags dither-accounts gives it (a Unix socket, no TCP port, no password, no STUN
     lookup) and listens on its socket. It compiles its keymap with xkbcomp at start, which needs the keyboard compiler
     and maps at the paths this build gave it.
  2. It does not walk /usr/lib or /usr/lib64 for FFmpeg (the old loader read both trees at every start): run under
     strace, it opens neither directory, and it asks the dynamic loader for libavformat.so.<major> by name.
  3. An RFB 3.8 client completes the handshake (security None), gets the screen's size in ServerInit, receives a full
     framebuffer update, sends a key event and gets a further update: the connection stays up.
  4. A client that does not announce KasmVNC's own pseudo-encodings is spoken to in standard RFB (this fork's change):
     a 6-byte pointer event is read as one (an update request right after it is answered), a second viewer joining
     sends the first no message 253, ClientCutText reaches X's CLIPBOARD, and an X client's copy reaches the viewer as
     ServerCutText. A standard client that announces the extended clipboard (as noVNC does) gets its capabilities, and
     text beyond latin-1 (€, an emoji) goes both ways as UTF-8: notify, request, provide.
  5. Xkasmvnc is still running at the end.

Usage: kasmvnc-smoke [--keep] (--keep leaves the temporary directory and its logs).
Needs on PATH: Xkasmvnc, strace, xclip.
"""

import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zlib

WIDTH, HEIGHT = 1024, 768
TIMEOUT = 60.0


class Fail(Exception):
    pass


def log(msg):
    print(f"smoke: {msg}", flush=True)


def read_file(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return ""


def wait_for(what, check, timeout=TIMEOUT, procs=()):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        for name, p in procs:
            if p.poll() is not None:
                raise Fail(f"{name} exited ({p.returncode}) while waiting for {what}")
        if time.monotonic() > deadline:
            raise Fail(f"timed out after {timeout:.0f} s waiting for {what}")
        time.sleep(0.05)


def recv(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise Fail("server closed the connection")
        buf += chunk
    return bytes(buf)


EXT_CLIPBOARD = -1063131698  # pseudo-encoding 0xC0A1E5CE, as a signed 32-bit number
CLIP_UTF8 = 1 << 0
CLIP_CAPS, CLIP_REQUEST, CLIP_PEEK, CLIP_NOTIFY, CLIP_PROVIDE = (1 << 24), (1 << 25), (1 << 26), (1 << 27), (1 << 28)


def read_update(sock, bpp, cut=None, ext=None):
    """Read server messages until a FramebufferUpdate; return the raw pixel area it carried. ServerCutText goes to
    `cut` (a list) when given, and an extended clipboard message (a negative length) to `ext` as (flags, payload);
    any message a standard RFB client does not know fails."""
    while True:
        (kind,) = struct.unpack("B", recv(sock, 1))
        if kind == 0:
            (n,) = struct.unpack(">xH", recv(sock, 3))
            area = 0
            for _ in range(n):
                x, y, w, h, enc = struct.unpack(">HHHHi", recv(sock, 12))
                if enc == 0:
                    recv(sock, w * h * bpp // 8)
                    area += w * h
                elif enc == -223:  # DesktopSize
                    pass
                else:
                    raise Fail(f"unexpected encoding {enc}")
            return area
        if kind == 2:
            continue
        if kind == 3:
            (n,) = struct.unpack(">xxxi", recv(sock, 7))
            if n < 0:
                payload = recv(sock, -n)
                if ext is None:
                    raise Fail("an extended clipboard message for a client that did not announce it")
                ext.append((struct.unpack(">I", payload[:4])[0], payload[4:]))
                continue
            text = recv(sock, n)
            if cut is not None:
                cut.append(text.decode("latin-1"))
            continue
        raise Fail(f"unexpected server message type {kind}")


def handshake(sock):
    """RFB 3.8 with security None, shared; returns (width, height, bpp)."""
    banner = recv(sock, 12)
    if not banner.startswith(b"RFB 003."):
        raise Fail(f"unexpected banner {banner!r}")
    sock.sendall(b"RFB 003.008\n")
    (count,) = struct.unpack("B", recv(sock, 1))
    if count == 0:
        (n,) = struct.unpack(">I", recv(sock, 4))
        raise Fail(f"server refused: {recv(sock, n).decode(errors='replace')}")
    if 1 not in list(recv(sock, count)):
        raise Fail("no security type None offered")
    sock.sendall(b"\x01")
    (result,) = struct.unpack(">I", recv(sock, 4))
    if result != 0:
        raise Fail(f"security result {result}")
    sock.sendall(b"\x01")
    w, h = struct.unpack(">HH", recv(sock, 4))
    pf = recv(sock, 16)
    (nl,) = struct.unpack(">I", recv(sock, 4))
    recv(sock, nl)
    return w, h, pf[0]


def free_display():
    for n in range(90, 200):
        if not os.path.exists(f"/tmp/.X{n}-lock") and not os.path.exists(f"/tmp/.X11-unix/X{n}"):
            return n
    raise Fail("no free X display number in 90-199")


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def stop_group(p, grace=10):
    """Stop a process started with start_new_session=True, and everything left in its group."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(p.pid, sig)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if p.poll() is not None and not group_alive(p.pid):
                return
            time.sleep(0.05)
    p.wait()


def main():
    keep = "--keep" in sys.argv[1:]
    for tool in ("Xkasmvnc", "strace", "xclip"):
        if not shutil.which(tool):
            raise Fail(f"{tool} not on PATH")

    tmp = tempfile.mkdtemp(prefix="kasmvnc-smoke-")
    procs = []
    ok = False
    try:
        display = free_display()
        sock_path = os.path.join(tmp, "vnc.sock")
        trace = os.path.join(tmp, "strace.log")
        xlog = os.path.join(tmp, "Xkasmvnc.log")
        env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        env["HOME"] = tmp
        argv = [
            "strace", "-f", "-qq", "-e", "trace=openat,open", "-o", trace,
            "Xkasmvnc", f":{display}", "-geometry", f"{WIDTH}x{HEIGHT}", "-depth", "24", "-nolisten", "tcp",
            "-dpi", "96", "-desktop", "smoke", "-rfbunixpath", sock_path, "-rfbport", "-1", "-SecurityTypes", "None",
            "-AlwaysShared", "-AcceptSetDesktopSize=0", "-publicIP", "127.0.0.1", "-DisableBasicAuth",
            "-Log", "*:stderr:100",
        ]
        started = time.monotonic()
        with open(xlog, "wb") as out:
            x = subprocess.Popen(argv, env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 start_new_session=True)
        procs.append(("Xkasmvnc", x))
        wait_for("Xkasmvnc's socket", lambda: os.path.exists(sock_path), procs=procs)

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(TIMEOUT)
        wait_for("Xkasmvnc to accept on its socket", lambda: s.connect_ex(sock_path) == 0, procs=procs)
        banner = recv(s, 12)
        log(f"banner {banner!r} {time.monotonic() - started:.2f} s after start")
        if not banner.startswith(b"RFB 003."):
            raise Fail(f"unexpected banner {banner!r}")
        s.sendall(b"RFB 003.008\n")
        (count,) = struct.unpack("B", recv(s, 1))
        if count == 0:
            (n,) = struct.unpack(">I", recv(s, 4))
            raise Fail(f"server refused: {recv(s, n).decode(errors='replace')}")
        types = list(recv(s, count))
        if 1 not in types:
            raise Fail(f"no security type None offered: {types}")
        s.sendall(b"\x01")
        (result,) = struct.unpack(">I", recv(s, 4))
        if result != 0:
            raise Fail(f"security result {result}")
        s.sendall(b"\x01")
        w, h = struct.unpack(">HH", recv(s, 4))
        pf = recv(s, 16)
        (nl,) = struct.unpack(">I", recv(s, 4))
        name = recv(s, nl).decode(errors="replace")
        bpp = pf[0]
        log(f"ServerInit: {w}x{h}, {bpp} bpp, name {name!r}")
        if (w, h) != (WIDTH, HEIGHT):
            raise Fail(f"ServerInit says {w}x{h}, asked for {WIDTH}x{HEIGHT}")

        s.sendall(struct.pack(">BxHi", 2, 1, 0))  # SetEncodings: Raw
        covered = 0
        s.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
        while covered < w * h:
            covered += read_update(s, bpp)
            if covered < w * h:
                s.sendall(struct.pack(">BBHHHH", 3, 1, 0, 0, w, h))
        log(f"framebuffer update: {covered} pixels")

        for down in (1, 0):
            s.sendall(struct.pack(">BBxxI", 4, down, 0x61))
        s.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
        read_update(s, bpp)
        log("key event sent, connection still up")

        # Standard RFB's pointer event, 6 bytes, then an update request: read as 11 bytes, the request would be
        # swallowed and no update would come.
        s.sendall(struct.pack(">BBHH", 5, 0, 10, 10))
        s.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
        read_update(s, bpp)
        log("6-byte pointer event read as one")

        # A second viewer: the first is sent no message 253 (read_update fails on any type it does not know).
        s2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s2.settimeout(TIMEOUT)
        s2.connect(sock_path)
        handshake(s2)
        s.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
        read_update(s, bpp)
        s2.close()
        s.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
        read_update(s, bpp)
        log("a second viewer came and went; the first got no KasmVNC session message")

        # The clipboard both ways, as standard RFB carries it (latin-1).
        xenv = {**env, "DISPLAY": f":{display}"}
        sent = "from the viewer \xe9"
        data = sent.encode("latin-1")
        s.sendall(struct.pack(">BxxxI", 6, len(data)) + data)
        def clipboard():
            out = subprocess.run(["xclip", "-o", "-selection", "clipboard"], env=xenv, capture_output=True, timeout=10)
            return out.returncode == 0 and out.stdout.decode("utf-8", "replace") == sent
        wait_for("ClientCutText in X's CLIPBOARD", clipboard, procs=procs)
        log("clipboard reached X")
        owner = subprocess.Popen(["xclip", "-i", "-selection", "clipboard", "-loops", "0"], env=xenv,
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        procs.append(("xclip", owner))
        owner.stdin.write("from an app \u00fc".encode("utf-8"))
        owner.stdin.close()
        cut = []
        deadline = time.monotonic() + TIMEOUT
        while "from an app \u00fc" not in cut:
            if time.monotonic() > deadline:
                raise Fail(f"no ServerCutText with the app's copy (got {cut!r})")
            # A full update each time (an incremental one waits for a change that may never come); the cut text
            # arrives between them.
            s.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
            read_update(s, bpp, cut)
        log("clipboard reached the viewer")
        s.close()

        # The extended clipboard, as noVNC speaks it: UTF-8 both ways.
        s3 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s3.settimeout(TIMEOUT)
        s3.connect(sock_path)
        handshake(s3)
        s3.sendall(struct.pack(">BxHii", 2, 2, 0, EXT_CLIPBOARD))  # SetEncodings: Raw, ExtendedClipboard
        ext = []

        def ext_until(what, match):
            deadline = time.monotonic() + TIMEOUT
            while True:
                for m in ext:
                    if match(m):
                        ext.remove(m)
                        return m
                if time.monotonic() > deadline:
                    raise Fail(f"no extended clipboard {what} (got {[hex(f) for f, _ in ext]})")
                s3.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
                read_update(s3, bpp, None, ext)

        def client_ext(flags, payload=b""):
            s3.sendall(struct.pack(">Bxxxi", 6, -(4 + len(payload))) + struct.pack(">I", flags) + payload)

        caps = ext_until("caps", lambda m: m[0] & CLIP_CAPS)
        if not caps[0] & CLIP_UTF8 or not caps[0] & CLIP_PROVIDE:
            raise Fail(f"the server's clipboard caps lack text or provide: {hex(caps[0])}")
        client_ext(CLIP_CAPS | CLIP_UTF8 | CLIP_REQUEST | CLIP_PEEK | CLIP_NOTIFY | CLIP_PROVIDE, struct.pack(">I", 1 << 20))
        sent = "from the viewer \u20ac \U0001f600"
        client_ext(CLIP_NOTIFY | CLIP_UTF8)
        ext_until("request after the viewer's notify", lambda m: m[0] & CLIP_REQUEST and m[0] & CLIP_UTF8)
        body = sent.encode("utf-8") + b"\0"
        client_ext(CLIP_PROVIDE | CLIP_UTF8, zlib.compress(struct.pack(">I", len(body)) + body))
        def clipboard3():
            out = subprocess.run(["xclip", "-o", "-selection", "clipboard"], env=xenv, capture_output=True, timeout=10)
            return out.returncode == 0 and out.stdout.decode("utf-8", "replace") == sent
        wait_for("the extended clipboard's UTF-8 in X's CLIPBOARD", clipboard3, procs=procs)
        log("extended clipboard reached X, as UTF-8")
        owner2 = subprocess.Popen(["xclip", "-i", "-selection", "clipboard", "-loops", "0"], env=xenv,
                                  stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  start_new_session=True)
        procs.append(("xclip", owner2))
        copied = "from an app \u20ac \U0001f600"
        owner2.stdin.write(copied.encode("utf-8"))
        owner2.stdin.close()
        ext_until("notify of the app's copy", lambda m: m[0] & CLIP_NOTIFY and m[0] & CLIP_UTF8)
        client_ext(CLIP_REQUEST | CLIP_UTF8)
        provided = ext_until("provide of the app's copy", lambda m: m[0] & CLIP_PROVIDE)
        # Sync-flushed, not finished, as TigerVNC's server sends it: what noVNC's inflator reads.
        raw = zlib.decompressobj().decompress(provided[1])
        (n,) = struct.unpack(">I", raw[:4])
        got = raw[4:4 + n].rstrip(b"\0").decode("utf-8", "replace")
        if got != copied:
            raise Fail(f"the extended clipboard gave {got!r}, the app copied {copied!r}")
        log("extended clipboard reached the viewer, as UTF-8")
        s3.close()

        if x.poll() is not None:
            raise Fail(f"Xkasmvnc exited ({x.returncode})")

        # The FFmpeg lookup: no walk of the library trees, a lookup by soname instead.
        traced = read_file(trace)
        walked = [l for l in traced.splitlines() if re.search(r'"/usr/lib(64)?/?"', l) and "O_DIRECTORY" in l]
        if walked:
            raise Fail("Xkasmvnc opened a library directory to walk it:\n" + "\n".join(walked[:5]))
        if not re.search(r"libavformat\.so\.\d+", traced):
            raise Fail("Xkasmvnc never looked for libavformat.so.<major> (FFmpeg loader not reached?)")
        looked = sorted(set(re.findall(r"libav\w+\.so\.\d+", traced)))
        log(f"FFmpeg looked up by soname: {', '.join(looked)}; no library directory walked")

        ok = True
        log("PASS")
    finally:
        # Each process leads its own group: strace leaves its tracee running when it is stopped alone, so stop the
        # whole group.
        for name, p in procs:
            stop_group(p)
        if not ok or keep:
            text = read_file(os.path.join(tmp, "Xkasmvnc.log"))
            print("----- Xkasmvnc.log (last 60 lines) -----")
            print("\n".join(text.splitlines()[-60:]))
        if keep:
            log(f"kept {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Fail as e:
        print(f"smoke: FAIL: {e}", flush=True)
        sys.exit(1)
