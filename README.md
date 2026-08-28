# DGS Notepad++ Bridge MCP

[中文](#中文) | [English](#english)

## 中文

### 这是什么

DGS Notepad++ Bridge MCP 是一个仅在本机运行的 Windows MCP 服务。它让
ChatGPT、Codex 和其他支持 MCP 的 vibe coding 工具，通过 Notepad++ 的
Scintilla 编辑缓冲区检索和修改文档。

它主要解决一种工具兼容性问题：在某些经过授权的企业开发环境中，源码在磁盘上
由透明加密或 DLP 产品保护，`rg.exe`、`cat` 和普通文件 API 只能看到密文或无效
内容，而经过企业批准的 Notepad++ 集成能够正常显示源码。本项目复用这个已经
授权的编辑器视图，让 MCP 客户端不再依赖 `rg.exe` 直接读取磁盘内容。

本项目不是解密器，也不破解或绕过身份认证、访问控制、DRM、DLP 或企业安全
策略。它不包含密钥、企业加密插件或解密算法。只能对你本来就有权在 Notepad++
中打开的文件使用本项目。

> 重要：工具返回的搜索片段、文件内容和修改请求可能会被发送给所配置的 MCP
> 客户端或模型服务。处理公司代码前，请确认公司允许使用对应的 AI 服务和数据
> 路径。

### 系统要求

- Windows x64
- Python 3.10 或更高版本
- 如果文档依赖企业插件或透明解密组件，该组件必须已经由组织批准并正常配置

仓库包含完整的 Notepad++ 8.5.7 x64 headless 便携运行时，无需另外安装
Notepad++。桥接源码使用 MIT 许可证；修改版 Notepad++ 继续使用 GPLv3，其完整
对应源码固定在
[`dgs-headless-8.5.7-3`](https://github.com/lmaoha/notepad-plus-plus/tree/dgs-headless-8.5.7-3)
tag，并通过 `third_party/notepad-plus-plus` submodule 引用同一提交。企业插件和
解密组件不在本项目中分发。

### 安装

克隆仓库：

```powershell
git clone https://github.com/lmaoha/dgs-npp-mcp.git
cd dgs-npp-mcp
```

运行安装脚本。脚本会定位 Python 的绝对路径，在缺少配置时注册 `dgs-npp`，
并用独立的 `server.py` 进程完成 `initialize + tools/list` 探测：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

默认会使用仓库中的 headless 运行时，不需要设置环境变量。如果需要诊断或使用
另一份已获授权的运行时，可用 `NPP_EXE` 显式覆盖；仅当覆盖的运行时明确支持
`-headless` 时才设置 `DGS_NPP_HEADLESS = '1'`。

注册成功只表示 Codex 全局配置中已有 `dgs-npp`，不会热更新当前任务已经加载的
工具快照。安装后请重启 Codex，再创建新任务或 Fork 旧任务。在新任务中先确认
`dgs_list_open_files` 可调用，再用 `dgs_read_file`、`dgs_search` 等工具处理 DGS
源码。安装脚本不会终止现有 Codex、broker 或 Notepad++ 进程。

桥会按以下顺序选择可执行文件：

1. `NPP_EXE` 显式指定的路径（如果设置）。
2. 捆绑的 `runtime/notepad-plus-plus-headless/notepad++.exe`。
3. 标准 64 位或 32 位 Notepad++ 安装路径。

### 工作方式

1. 每个 MCP 会话启动轻量的 UTF-8 stdio 适配器 `server.py`。
2. 适配器连接到本机唯一 broker，或在需要时启动它。
3. broker 只监听 `127.0.0.1:57931`，并串行处理文件操作。
4. broker 启动独立的 Notepad++ 实例，以 Windows 消息操作当前 Scintilla 缓冲区。
5. 保存仍由 Notepad++ 完成，因此继续沿用已有的文档插件和保存流程。

文档正文通过 worker 的 UTF-8 stdin 传输，不依赖 Windows 命令行长度。聚焦修改
推荐使用“搜索后精确替换”，避免把整份源码放进 MCP 参数。

### MCP 工具

- `dgs_open_file`：打开文件，并用完整路径确认活动标签页。
- `dgs_read_file`：从 Scintilla 缓冲区读取文本；可用 `line_start` / `line_end`
  只返回指定行，结果固定使用紧凑结构。
- `dgs_search`：执行文字或正则搜索；固定只返回匹配行和后续 patch 所需的
  完整文件 SHA-256、mtime、size。
- `dgs_apply_patch`：精确替换文本，经 Notepad++ 保存并重新读取验证。
- `dgs_write_file`：有意替换完整文档；常规源码修改优先使用 patch。
- `dgs_list_open_files`：列出桥跟踪的文件和托管实例状态。
- `dgs_shutdown`：关闭桥管理的干净 Notepad++ 实例。

修改操作使用 `mtime` 和内容 SHA-256 做乐观并发检查。如果文件在搜索与保存之间
发生变化，patch 会被拒绝。通信失败、超时或未确认保存时，桥不会盲目重试覆盖。

推荐的低 token 调用顺序：

```json
{"path":"TARGET.cpp","query":"FunctionName","max_matches":3}
```

如果需要查看命中位置附近的源码，再按返回行号读取小范围：

```json
{"path":"TARGET.cpp","line_start":317,"line_end":329}
```

bridge 内部仍读取完整 Scintilla 缓冲区并计算完整 SHA-256；只有返回给 MCP 客户端的
文本和元数据被裁剪。固定的 `content` 使用 `路径:行号:文本`，同时保留精简的
`structuredContent`，因此后续 `dgs_apply_patch` 的并发校验流程保持不变。

从 0.8.4 起，每次缓冲区操作都验证同一份 `PID + path + Buffer ID + active view +
Scintilla HWND` 快照。活动编辑区通过 Notepad++ 官方消息取得，不再根据窗口可见性
或文本长度猜测。用户自己的 Notepad++ 与 headless 托管实例可以同时打开同一路径，
两者仍按 PID 隔离。

broker 会记录托管实例启动时的空白 Buffer ID。shutdown 只会处理同时满足以下条件
的占位标签：Buffer ID 与启动记录一致、路径不是绝对路径、PID、可执行文件和无头
窗口身份仍与托管状态一致。即使这个专用占位标签意外收到键盘输入，broker 也会清空
它、设置并验证保存点，再关闭标签或整个进程。真实文件、Buffer ID 不匹配、绑定或
清空校验失败时仍保持隐藏隔离。

### 测试

```powershell
python -m unittest discover -s tests -v
```

测试覆盖有界搜索、精确 patch、换行保留、并发保护、Unicode MCP 输入、worker
stdin、保存失败恢复以及 Notepad++ 生命周期管理。额外的捆绑运行时集成测试会启动
普通和 headless 两个隔离实例，对同一路径连续读取 100 次：

```powershell
$env:DGS_NPP_LIVE_TESTS = '1'
python -m unittest tests.test_dgs_npp_mcp.LiveBindingIntegrationTests -v
```

### 安全与许可证

请阅读 [SECURITY.md](SECURITY.md)。桥接源码采用 [MIT License](LICENSE)。
Notepad++ 及其他组件的许可边界见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## English

### What it is

DGS Notepad++ Bridge MCP is a local-only Windows MCP server. It lets ChatGPT,
Codex, and other MCP-capable coding tools search and edit documents through a
Notepad++ Scintilla buffer.

It addresses a tooling compatibility gap found in some authorized enterprise
development environments: transparent encryption or DLP software may cause
disk-oriented tools such as `rg.exe`, `cat`, and ordinary file APIs to see
ciphertext or invalid content, while an approved Notepad++ integration can
display the source normally. The bridge reuses that already-authorized editor
view instead of requiring the MCP client to read the on-disk bytes directly.

This project is not a decryptor. It does not defeat authentication, access
controls, DRM, DLP, or organizational policy, and it contains no keys,
enterprise encryption plugins, or decryption algorithms. Use it only with
files you are already authorized to open in Notepad++.

> Search snippets, document content, and edit requests returned through MCP may
> be sent to the configured client or model provider. Before working with
> company source, confirm that the provider and data path are approved by the
> data owner and your organization.

### Requirements

- Windows x64
- Python 3.10 or newer
- Any required document integration already approved and configured locally

The repository includes a complete portable Notepad++ 8.5.7 x64 headless
runtime, so a separate Notepad++ installation is not required. The bridge
source is MIT-licensed. The modified Notepad++ runtime remains GPLv3, with its
complete corresponding source pinned at
[`dgs-headless-8.5.7-3`](https://github.com/lmaoha/notepad-plus-plus/tree/dgs-headless-8.5.7-3).
The same commit is referenced by the `third_party/notepad-plus-plus` submodule.
Enterprise plugins and decryption components are not distributed here.

### Setup

```powershell
git clone https://github.com/lmaoha/dgs-npp-mcp.git
cd dgs-npp-mcp
```

Run the installer. It resolves an absolute Python executable, registers
`dgs-npp` when the configuration is missing, and probes `initialize +
tools/list` through an independent `server.py` process:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The bundled headless runtime is used by default, with no environment variables
required. `NPP_EXE` is an explicit override for diagnostics or another
authorized runtime. Set `DGS_NPP_HEADLESS=1` only if that override explicitly
supports the `-headless` switch.

A successful registration only confirms that `dgs-npp` exists in Codex's
global configuration; it does not hot-refresh the tool snapshot of an existing
task. Restart Codex, then create a new task or fork the old task. Confirm that
`dgs_list_open_files` is available before using `dgs_read_file`, `dgs_search`,
or other DGS source tools. The installer does not terminate existing Codex,
broker, or Notepad++ processes.

Executable resolution order is:

1. The explicit `NPP_EXE` path, when configured.
2. The bundled `runtime/notepad-plus-plus-headless/notepad++.exe`.
3. Standard 64-bit and 32-bit Notepad++ installation paths.

### Design

1. Each MCP session starts a small UTF-8 stdio adapter (`server.py`).
2. The adapter connects to, or starts, one local singleton broker.
3. The broker listens only on `127.0.0.1:57931` and serializes operations.
4. It owns a dedicated Notepad++ instance and accesses the active Scintilla
   buffer through Windows messages.
5. Saves are issued through Notepad++, preserving the configured document and
   plugin workflow.

Document bodies travel over worker UTF-8 stdin rather than Windows command-line
arguments. For focused edits, prefer search followed by exact replacement so a
complete document does not need to enter MCP arguments.

### MCP tools

- `dgs_open_file`: open a file and verify the active tab by full path.
- `dgs_read_file`: read text from the Scintilla buffer; use `line_start` /
  `line_end` to return a narrow range. Results always use compact metadata.
- `dgs_search`: run literal or regular-expression search. It always returns only
  matching lines plus the full-document SHA-256, mtime, and size needed by a
  later patch.
- `dgs_apply_patch`: replace exact text, save through Notepad++, and verify the
  persisted result.
- `dgs_write_file`: intentionally replace a complete document; use patch for
  normal source edits.
- `dgs_list_open_files`: list tracked files and managed-instance state.
- `dgs_shutdown`: close a clean bridge-managed Notepad++ process.

Mutations use exact modification times and a content SHA-256 as optimistic
concurrency tokens. A patch is refused if the source changes between search
and save. Communication failures, timeouts, and unconfirmed saves are not
blindly retried over the source.

Recommended low-token discovery call:

```json
{"path":"TARGET.cpp","query":"FunctionName","max_matches":3}
```

Read a small range only when more local context is needed:

```json
{"path":"TARGET.cpp","line_start":317,"line_end":329}
```

The bridge still reads the complete Scintilla buffer and computes the complete
SHA-256 internally. Only the MCP response text and metadata are reduced.
The fixed compact `content` uses `path:line:text`, while
`structuredContent` retains the concurrency tokens required by
`dgs_apply_patch`.

Since 0.8.4, every buffer operation verifies one stable
`PID + path + BufferID + active view + Scintilla HWND` snapshot. The active
editor is selected through Notepad++'s official message rather than inferred
from visibility or text length. A user's interactive Notepad++ and the managed
headless process remain isolated by PID, executable path, mutex, and window
class even when both open the same path.

The broker records the empty startup BufferID. Shutdown handles that placeholder
only while the BufferID still matches, its path is not absolute, and the PID,
executable, and headless window identity still match managed state. If this
dedicated placeholder receives accidental keyboard input, the broker clears it,
sets and verifies its save point, then closes the tab or process. If Notepad++
reuses that BufferID for a broker-tracked real file, normal tracked-file
lifecycle handling takes over. Untracked real files, mismatched BufferIDs, and
failed binding or clear verification remain quarantined.

### Tests

```powershell
python -m unittest discover -s tests -v
```

The suite covers bounded search, exact patching, newline preservation,
concurrency protection, Unicode MCP input, worker stdin transport, save-failure
recovery, and managed Notepad++ lifecycle behavior. The optional bundled-runtime
test starts isolated normal and headless instances, opens the same path in both,
and performs 100 verified reads:

```powershell
$env:DGS_NPP_LIVE_TESTS = '1'
python -m unittest tests.test_dgs_npp_mcp.LiveBindingIntegrationTests -v
```

### Security and license

Read [SECURITY.md](SECURITY.md) before using the bridge with protected data.
The bridge source is available under the [MIT License](LICENSE). See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for third-party boundaries.
