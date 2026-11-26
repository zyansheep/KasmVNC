# What this fork could send upstream

Nothing here has been sent. These are drafts for whoever decides to.

---

## Draft issue: Xvnc walks all of /usr/lib and /usr/lib64 at every start to find FFmpeg

**Version:** 1.5.2. The code is the same on master.

### What happens

`FFmpeg::FFmpeg()` (`common/rfb/ffmpeg.cxx`) finds each of libavformat, libavutil, libswscale and libavcodec in the
same way. It walks `/usr/lib/` and `/usr/lib64` recursively with `std::filesystem::recursive_directory_iterator`, looking
for a file whose name contains `libavformat.so` (and so on). Then it passes that file's bare name to `dlopen()`.

The constructor runs at every server start, before the server answers on its sockets. On Debian and Ubuntu machines
with a full `/usr/lib` (Python's and Perl's trees, firmware, LLVM...), we measured the walk at 5 to 18 seconds before
the first client could connect. There is no option to skip it, so it happens even when the video encoders are never
used.

### Why the walk isn't needed

- `dlopen()` gets only the bare file name, so the library is then found by the dynamic loader's normal search: its
  cache, `LD_LIBRARY_PATH`, RUNPATH. The walk only decides which name to ask for, and the loader would have found the
  library without it.
- It takes the first matching file it meets, in directory order. That need not be the major version the server was
  compiled against. With `libavformat.so.59` and `libavformat.so.61` both installed, it can pick 59 for a 61 build.
- It never looks where the loader looks, so FFmpeg installed under another prefix and reached through
  `LD_LIBRARY_PATH` or RUNPATH is skipped, even though `dlopen()` would find it.

### Suggested fix

Ask the loader for `<lib>.so.<major>`, with the major version from the FFmpeg headers the server was built with
(`LIBAVFORMAT_VERSION_MAJOR` and the others). Fall back to the unversioned `<lib>.so`. That keeps every case the
walk handled and adds none of its cost. The patch is the fork's commit
["Open FFmpeg's libraries by soname instead of walking /usr/lib"](https://github.com/zyansheep/KasmVNC/commits/dither),
about 40 lines in `common/rfb/ffmpeg.cxx`.

### How it was checked

The fork's smoke test (`contrib/dither/smoke.py`) runs `Xkasmvnc` under `strace -e openat`. It checks that the server
opens neither `/usr/lib` nor `/usr/lib64` as a directory, and that it asks for `libavformat.so.<major>` by name. It
also checks the RFB handshake and a framebuffer update.

---

## Draft PR: the FFmpeg fix above, plus building with CMake 4

The FFmpeg commit, plus one more:

- **Configure with CMake 4.** `CMakeLists.txt` sets policy CMP0022 to OLD. CMake 4 no longer has that old behaviour
  and stops at configure with an error. Asking for it only on CMake versions below 4.0 lets 4.x configure, given
  `-DCMAKE_POLICY_VERSION_MINIMUM=3.5`, because the minimum version is still 2.8. Raising `cmake_minimum_required` to
  3.5 or later would remove the need for that flag. It is upstream's call, since the Docker builds pin their own
  CMake.
