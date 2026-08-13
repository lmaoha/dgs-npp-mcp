# Corresponding source

This directory contains a modified Notepad++ 8.5.7 x64 runtime. The modification
adds `-headless` and `--headless` launch modes while retaining the normal
Notepad++ and Scintilla window hierarchy required by the local bridge.

- Corresponding source tag: https://github.com/lmaoha/notepad-plus-plus/tree/dgs-headless-8.5.7-2
- Modified source commit: `92bfba7d06fc7ece814d639e65291be9352afb15`
- Official base tag: https://github.com/notepad-plus-plus/notepad-plus-plus/tree/v8.5.7
- Official base commit: `5008b8a0cccfff255c5f48b5782ef993b6f9b631`
- Build instructions: https://github.com/lmaoha/notepad-plus-plus/blob/dgs-headless-8.5.7-2/BUILD.md
- Executable SHA-256: `FD4DFD2A2F9677796498F4693C60EEC4101243C070EDAF24A1FDF891568FD2E0`

The distributed executable was rebuilt from the tagged source as `Release|x64`
with Visual Studio 2026 18.7.3, MSVC 14.51, and Windows SDK 10.0.26100.0.
Scintilla and Lexilla were compiled with UTF-8 source input on the zh-CN build
host.

The headless change is limited to:

- `PowerEditor/src/winmain.cpp`
- `PowerEditor/src/Parameters.h`
- `PowerEditor/src/Notepad_plus_Window.h`
- `PowerEditor/src/Notepad_plus_Window.cpp`

Notepad++ remains licensed under GPLv3. See `license.txt` in this directory.
