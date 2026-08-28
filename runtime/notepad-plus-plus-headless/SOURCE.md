# Corresponding source

This directory contains a modified Notepad++ 8.5.7 x64 runtime. The modification
adds `-headless` and `--headless` launch modes, blocks keyboard and IME input in
that mode, and retains the Notepad++ and Scintilla window hierarchy required by
the local bridge.

- Corresponding source tag: https://github.com/lmaoha/notepad-plus-plus/tree/dgs-headless-8.5.7-3
- Modified source commit: `b01ff1d8d6d36df70ecff62a7f6ad857b97fb07e`
- Official base tag: https://github.com/notepad-plus-plus/notepad-plus-plus/tree/v8.5.7
- Official base commit: `5008b8a0cccfff255c5f48b5782ef993b6f9b631`
- Build instructions: https://github.com/lmaoha/notepad-plus-plus/blob/dgs-headless-8.5.7-3/BUILD.md
- Executable SHA-256: `17DE13A2D3093D2E8D5916DD37E7B18C1941611E5FCD25DE27565581DB832C78`

The distributed executable was rebuilt from the tagged source as `Release|x64`
with Visual Studio 2026/MSBuild 18.8.2, MSVC 14.44.35207, and Windows SDK
10.0.26100.0.
Scintilla and Lexilla were compiled with UTF-8 source input on the zh-CN build
host.

The headless change is limited to:

- `PowerEditor/src/winmain.cpp`
- `PowerEditor/src/Parameters.h`
- `PowerEditor/src/Notepad_plus_Window.h`
- `PowerEditor/src/Notepad_plus_Window.cpp`

Notepad++ remains licensed under GPLv3. See `license.txt` in this directory.
