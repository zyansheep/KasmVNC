# The `dither` branch

This fork's `dither` branch is KasmVNC's latest stable release tag with a few commits on top. It is built from source
with nix, instead of repackaging upstream's .deb. The carried commits are:

- **"Open FFmpeg's libraries by soname instead of walking /usr/lib".** Xvnc used to walk `/usr/lib` and `/usr/lib64`
  at every start to find FFmpeg, which took seconds. [UPSTREAM.md](UPSTREAM.md) drafts the issue.
- **"Configure with CMake 4".** nixpkgs only has CMake 4.
- **This directory and `.github/workflows/dither.yml`.** These hold the tracking, the build, the smoke test and the
  releases.

## What is built

[package.nix](package.nix) builds `Xkasmvnc` (KasmVNC's Xvnc, under the .deb's name) and `kasmvncpasswd`. It builds
them the way `builder/build.sh` does in Docker, but against nixpkgs' libraries and nixpkgs' X server sources, as
nixpkgs' tigervnc builds its Xvnc. The keyboard compiler, keyboard maps and fonts are compiled in at nixpkgs' paths.
So the .deb's binary patches (`/usr/bin` for xkbcomp, `/usr/lib` for FFmpeg) and its wrapper are gone.

Not built: the web client (`kasmweb`, an npm build that only the websocket listener serves) and the `vncserver` Perl
wrapper. FFmpeg's libraries aren't on the package's library path, so the video encoders stay off, as they were with
the .deb on NixOS. `withFFmpeg = true` adds them.

## Why from source

KasmVNC's own builds run in a Docker image per distribution, with libjpeg-turbo, libwebp, TBB, libcpuid, fmt and
libyuv built from source. nixpkgs has all of them (libjpeg-turbo with its static library, which KasmVNC links), and
the X server part is the same patch-and-configure step nixpkgs' tigervnc already runs. So a source build is one nix
file. It also lets the FFmpeg fix be a real source patch, not a byte rewrite of the binary.

## Releases and tracking

Releases, tracking, failure handling and pushing work exactly as in the TigerVNC fork's
[contrib/dither/README.md](https://github.com/zyansheep/tigervnc/blob/dither/contrib/dither/README.md). The scripts
here are the same files, and [dither.conf](dither.conf) holds what differs. Each release `vX.Y.Z-dither.<n>` carries
`kasmvnc.nix`, a self-contained nix file that builds the tag. Stable tags here are every `vX.Y.Z`: KasmVNC tags no
betas.

The smoke test ([smoke.py](smoke.py)) starts `Xkasmvnc` with the flags a sandbox gives it: a Unix socket, no TCP
port, no password and no STUN lookup. It completes an RFB handshake, gets a full framebuffer update, and sends a key
event. Under strace, it also checks that the server opens no library directory and looks FFmpeg up by soname.

## By hand

```
nix-build contrib/dither -A kasmvnc
nix-build contrib/dither -A smoke && ./result/bin/kasmvnc-smoke [--keep]
DRY_RUN=1 contrib/dither/ci.sh track     # everything but the push and the release
```
