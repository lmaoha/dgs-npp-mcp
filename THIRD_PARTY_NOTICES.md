# Third-party notices

The top-level MIT License applies to the DGS Notepad++ Bridge MCP source code.
It does not relicense the bundled Notepad++, Scintilla, Lexilla, plugins, or
updater components.

## Modified Notepad++ 8.5.7 x64 runtime

The runtime under `runtime/notepad-plus-plus-headless/` is based on Notepad++
8.5.7 and remains licensed under the GNU General Public License version 3.
Its GPL license is included at
`runtime/notepad-plus-plus-headless/license.txt`.

Complete corresponding source for the distributed executable:

- Source tag: https://github.com/lmaoha/notepad-plus-plus/tree/dgs-headless-8.5.7-2
- Modified source commit: `92bfba7d06fc7ece814d639e65291be9352afb15`
- Git submodule: `third_party/notepad-plus-plus`, pinned to the modified source commit
- Official base tag: https://github.com/notepad-plus-plus/notepad-plus-plus/tree/v8.5.7
- Official base commit: `5008b8a0cccfff255c5f48b5782ef993b6f9b631`
- Upstream build guide: https://github.com/lmaoha/notepad-plus-plus/blob/dgs-headless-8.5.7-2/BUILD.md

The headless change is limited to these source files:

- `PowerEditor/src/winmain.cpp`
- `PowerEditor/src/Parameters.h`
- `PowerEditor/src/Notepad_plus_Window.h`
- `PowerEditor/src/Notepad_plus_Window.cpp`

Distributed executable:

- Path: `runtime/notepad-plus-plus-headless/notepad++.exe`
- Version: Notepad++ 8.5.7 x64
- SHA-256: `FD4DFD2A2F9677796498F4693C60EEC4101243C070EDAF24A1FDF891568FD2E0`

The remaining files in the portable runtime retain their respective upstream
licenses and notices. In particular, the updater license is preserved at
`runtime/notepad-plus-plus-headless/updater/LICENSE`.

No enterprise encryption plugin, key, or proprietary decryption implementation
is distributed by this repository.
