# -*- coding: utf-8 -*-
"""
DGS N++ 文档桥接器 —— 通过 Notepad++ 的 Scintilla 控件跨进程读取和写入文档内容。

原理:
  DGS 是一种特殊文档编码格式，由 Notepad++ 负责打开和保存。
  本脚本跨进程操作 Notepad++ 的 Scintilla 编辑缓冲区:
    读: VirtualAllocEx(在 N++ 进程分配) -> SCI_GETTEXT 写入目标内存 -> ReadProcessMemory 读回
    写: 把文档内容写进 N++ 进程内存 -> SCI_SETTEXT 替换缓冲区 -> 请求 N++ 保存

踩坑(已验证):
  1. Notepad++ 8.x 下 SCI_GETLENGTH(2003) 返回 0 不可靠, 必须用 SCI_GETTEXTLENGTH(2183)。
  2. N++ 有多个 Scintilla 子窗口, 要遍历找返回非零 TEXTLENGTH 的那个。
  3. SCI_GETTEXT/SCI_SETTEXT 的 lParam 必须指向 *目标进程* 内存, 不能用本进程指针。
  4. SCI_GETTEXT/SCI_SETTEXT 传输的是 Scintilla 文档字节, 不能强制当 UTF-8 重编码。

依赖: pywin32 不必需 (本脚本纯 ctypes)。需 Python 3.8+, 64 位与 Notepad++ 位数一致。
用法见 SKILL.md。
"""
import ctypes
import sys
import codecs
import json
from pathlib import Path
from ctypes import wintypes

# ---------- Windows API ----------
u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
MEM_RESERVE = 0x2000
MEM_COMMIT = 0x1000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
SMTO_NORMAL = 0x0000
SMTO_BLOCK = 0x0001
SMTO_ABORTIFHUNG = 0x0002
WM_USER = 0x0400
RUNCOMMAND_USER = WM_USER + 3000
SW_HIDE = 0

u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.IsWindowVisible.argtypes = [wintypes.HWND]; u32.IsWindowVisible.restype = wintypes.BOOL
u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.GetWindowThreadProcessId.restype = wintypes.DWORD
u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u32.IsWindow.argtypes = [wintypes.HWND]; u32.IsWindow.restype = wintypes.BOOL
u32.SetForegroundWindow.argtypes = [wintypes.HWND]
u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
u32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
u32.SendMessageW.restype = ctypes.c_ssize_t
u32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
u32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_ssize_t),
]

k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.VirtualAllocEx.restype = wintypes.LPVOID
k32.VirtualAllocEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
k32.VirtualFreeEx.restype = wintypes.BOOL
k32.VirtualFreeEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD]
k32.ReadProcessMemory.restype = wintypes.BOOL
k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.LPVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.WriteProcessMemory.restype = wintypes.BOOL
k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.LPVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.GetACP.restype = wintypes.UINT

_RAW_SEND_MESSAGE_W = u32.SendMessageW

def _send_message_w_timeout(hwnd, msg, wparam, lparam):
    """Send a window message without letting a hung Notepad++ block the MCP."""
    timeout_ms = int(_os.environ.get("DGS_NPP_SEND_TIMEOUT_MS", "5000"))
    result = ctypes.c_ssize_t(0)
    ok = u32.SendMessageTimeoutW(
        hwnd,
        msg,
        wparam,
        lparam,
        SMTO_BLOCK | SMTO_ABORTIFHUNG,
        timeout_ms,
        ctypes.byref(result),
    )
    if not ok:
        raise TimeoutError(f"SendMessageTimeoutW msg={msg} hwnd={int(hwnd)} after {timeout_ms}ms")
    return result.value

u32.SendMessageW = _send_message_w_timeout

# Scintilla 消息
SCI_GETTEXTLENGTH = 2183
SCI_GETTEXT = 2182
SCI_SETTEXT = 2181
SCI_GETLENGTH = 2003  # 这版 N++ 上不可靠, 仅作诊断
SCI_GETCODEPAGE = 2137
SCI_GETMODIFY = 2159
SCI_SETSAVEPOINT = 2014
NPPMSG = WM_USER + 1000
NPPM_GETCURRENTSCINTILLA = NPPMSG + 4
NPPM_ACTIVATEDOC = NPPMSG + 28
NPPM_GETPOSFROMBUFFERID = NPPMSG + 57
NPPM_GETCURRENTBUFFERID = NPPMSG + 60
NPPM_RELOADBUFFERID = NPPMSG + 61
NPPM_GETFULLCURRENTPATH = RUNCOMMAND_USER + 1
NPPM_SWITCHTOFILE = NPPMSG + 37
NPPM_SAVECURRENTFILEAS = NPPMSG + 78
NPPM_DOOPEN = NPPMSG + 77

MAIN_VIEW = 0
SUB_VIEW = 1
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_BORDER = 0x00800000
WS_EX_CLIENTEDGE = 0x00000200

# ---------- 独占实例状态 ----------
import os as _os
_STATE_DIR = _os.path.join(_os.environ.get("LOCALAPPDATA") or _os.path.expanduser("~"), "dgs-npp-bridge")
_STATE_FILE = _os.path.join(_STATE_DIR, "npp-instance.json")

_HEADLESS_ENV = "DGS_NPP_HEADLESS"
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
_FALSE_ENV_VALUES = {"0", "false", "no", "off", ""}


def _env_flag(name, default=False):
    """Read a boolean bridge switch without treating typos as enabled."""
    raw = _os.environ.get(name)
    if raw is None:
        return bool(default)
    value = raw.strip().lower()
    if value in _TRUE_ENV_VALUES:
        return True
    if value in _FALSE_ENV_VALUES:
        return False
    raise ValueError(
        f"{name} must be one of {sorted(_TRUE_ENV_VALUES | _FALSE_ENV_VALUES)}, got {raw!r}"
    )


def build_npp_args(exe):
    """Build the managed Notepad++ command line for both bridge entry points."""
    args = [str(exe), "-multiInst", "-nosession"]
    # The bundled executable is always isolated from the user's interactive N++.
    # The environment switch remains available for an explicitly overridden EXE.
    if _env_flag(_HEADLESS_ENV) or _is_bundled_exe(exe):
        args.append("-headless")
    return args


def _normal_exe_path(path):
    return _os.path.normcase(_os.path.realpath(_os.path.abspath(str(path))))


_MCP_DIR = Path(__file__).resolve().parent
_BUNDLED_NPP_DIR = _MCP_DIR / "runtime" / "notepad-plus-plus-headless"
BUNDLED_NPP_EXE = str(_BUNDLED_NPP_DIR / "notepad++.exe")


def _is_bundled_exe(exe):
    return _normal_exe_path(exe) == _normal_exe_path(BUNDLED_NPP_EXE)

def _read_state():
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _write_state(pid, exe):
    try:
        _os.makedirs(_STATE_DIR, exist_ok=True)
        import time as _t
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"pid": int(pid), "exe": exe, "started_at": _t.strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _clear_state():
    try:
        _os.remove(_STATE_FILE)
    except Exception:
        pass

def _is_pid_alive(pid):
    if not pid:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return False
    STILL_ACTIVE = 259
    code = wintypes.DWORD()
    try:
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    except AttributeError:
        k32.CloseHandle(h); return True
    k32.CloseHandle(h)
    return bool(ok) and code.value == STILL_ACTIVE


SOURCE_EXTS = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
    ".inl", ".ipp", ".qml", ".qrc", ".pro", ".pri",
}

def normalize_path_for_compare(path):
    import os
    return os.path.normcase(os.path.abspath(path))

# ---------- 窗口查找 ----------
def _get_class(hwnd):
    b = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(hwnd, b, 256)
    return b.value

def _enum_toplevel():
    out = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _): out.append(h); return True
    u32.EnumWindows(WNDENUMPROC(cb), 0)
    return out

def _enum_children(parent):
    out = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _): out.append(h); return True
    u32.EnumChildWindows(parent, WNDENUMPROC(cb), 0)
    return out

def _get_title(hwnd):
    n = u32.GetWindowTextLengthW(hwnd)
    b = ctypes.create_unicode_buffer(n + 2)
    u32.GetWindowTextW(hwnd, b, n + 2)
    return b.value


_EDITOR_VIEW_CACHE = {}


def _scintilla_children(top):
    top_pid = window_process_id(top)
    return [
        child
        for child in _enum_children(top)
        if _get_class(child) == "Scintilla" and window_process_id(child) == top_pid
    ]


def _has_editor_border(hwnd):
    style = int(u32.GetWindowLongPtrW(hwnd, GWL_STYLE))
    exstyle = int(u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))
    return bool((style & WS_BORDER) or (exstyle & WS_EX_CLIENTEDGE))


def _discover_editor_view_map(top):
    scins = _scintilla_children(top)
    if len(scins) < 2:
        raise RuntimeError(f"expected at least 2 Scintilla controls, got {len(scins)}")

    bordered = [scin for scin in scins if _has_editor_border(scin)]
    if len(bordered) == 2:
        editors = bordered
    else:
        # Notepad++ 8.5.7 creates main then sub before its auxiliary Scintillas.
        editors = scins[:2]

    if editors[0] == editors[1]:
        raise RuntimeError("Notepad++ returned duplicate editor Scintilla handles")
    return {MAIN_VIEW: int(editors[0]), SUB_VIEW: int(editors[1])}


def _editor_view_map(top):
    pid = window_process_id(top)
    key = (int(pid), int(top))
    current = set(_scintilla_children(top))
    cached = _EDITOR_VIEW_CACHE.get(key)
    if cached and all(
        hwnd in current
        and u32.IsWindow(hwnd)
        and window_process_id(hwnd) == pid
        for hwnd in cached.values()
    ):
        return cached

    discovered = _discover_editor_view_map(top)
    _EDITOR_VIEW_CACHE[key] = discovered
    return discovered


def _pick_scintilla(top):
    """Compatibility wrapper; core operations use the official active view."""
    try:
        return get_active_scintilla(top)[1]
    except Exception:
        try:
            return _editor_view_map(top)[MAIN_VIEW]
        except Exception:
            scins = _scintilla_children(top)
            return scins[0] if scins else None

def iter_notepadpp(include_hidden=False):
    """枚举 Notepad++ 顶层窗口, 返回 (top_hwnd, scintilla_hwnd)。"""
    for top in _enum_toplevel():
        if not include_hidden and not u32.IsWindowVisible(top):
            continue
        if _get_class(top) != "Notepad++":
            continue
        scin = _pick_scintilla(top)
        if scin:
            yield top, scin

def find_notepadpp(include_hidden=False):
    """优先回 skill 独占实例的 (top, scin), 没有才回退到全局搜索。"""
    st = _read_state()
    if st and _is_pid_alive(st.get("pid")):
        for top, scin in _get_pid_notepadpp(st["pid"], include_hidden=True):
            if scin and u32.SendMessageW(scin, SCI_GETTEXTLENGTH, 0, 0) > 0:
                return top, scin
        for top, scin in _get_pid_notepadpp(st["pid"], include_hidden=True):
            return top, scin
    for top, scin in iter_notepadpp(include_hidden=include_hidden):
        if u32.SendMessageW(scin, SCI_GETTEXTLENGTH, 0, 0) > 0:
            return top, scin
    for top, scin in iter_notepadpp(include_hidden=include_hidden):
        return top, scin
    return None, None

def find_notepadpp_by_path(file_path, include_hidden=False):
    expected = normalize_path_for_compare(file_path)
    st = _read_state()
    if st and _is_pid_alive(st.get("pid")):
        for top, scin in _get_pid_notepadpp(st["pid"], include_hidden=True):
            current_path = get_current_file_path(top)
            if current_path and normalize_path_for_compare(current_path) == expected:
                return top, scin
        return None, None
    for top, scin in iter_notepadpp(include_hidden=include_hidden):
        current_path = get_current_file_path(top)
        if current_path and normalize_path_for_compare(current_path) == expected:
            return top, scin
    return None, None

def window_process_id(hwnd):
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value

def hide_window_if_pid(top_hwnd, pid):
    if pid and window_process_id(top_hwnd) == pid:
        u32.ShowWindow(top_hwnd, SW_HIDE)
        return True
    return False

# ---------- 启动 Notepad++ 打开文件 ----------
NPP_EXE_CANDIDATES = [
    BUNDLED_NPP_EXE,
    r"C:\Program Files\Notepad++\notepad++.exe",
    r"C:\Program Files (x86)\Notepad++\notepad++.exe",
]


def find_npp_exe():
    """Resolve the bundled runtime first, with an explicit override for tests."""
    override = _os.environ.get("NPP_EXE")
    if override:
        path = _os.path.abspath(_os.path.expandvars(_os.path.expanduser(override)))
        if _os.path.isfile(path):
            return path
        raise FileNotFoundError(f"NPP_EXE points to a missing notepad++.exe: {path}")
    for path in NPP_EXE_CANDIDATES:
        if _os.path.isfile(path):
            return _os.path.abspath(path)
    searched = "; ".join(NPP_EXE_CANDIDATES)
    raise FileNotFoundError(f"找不到 bundled or installed notepad++.exe; searched: {searched}")


def _find_npp_exe():
    """Backward-compatible alias for callers using the older private helper."""
    return find_npp_exe()


def npp_working_directory(exe):
    return _os.path.dirname(_os.path.abspath(str(exe)))

def _get_pid_notepadpp(pid, include_hidden=True):
    """在同一 PID 下枚举 Notepad++ 主窗口, 返回 (top, scintilla) 列表。"""
    if not pid:
        return []
    out = []
    for top in _enum_toplevel():
        if not include_hidden and not u32.IsWindowVisible(top):
            continue
        if _get_class(top) != "Notepad++":
            continue
        if window_process_id(top) != int(pid):
            continue
        scin = _pick_scintilla(top)
        out.append((top, scin))
    return out

def _find_pid_top(pid):
    for top, scin in _get_pid_notepadpp(pid, include_hidden=True):
        return top, scin
    return None, None

def _ensure_dedicated_instance(wait_timeout=15.0, hide_window=True):
    """确保 skill 独占的 N++ 存活; 返回 (pid, top)。"""
    import time, subprocess
    st = _read_state()
    if st and _is_pid_alive(st.get("pid")):
        top, _ = _find_pid_top(st["pid"])
        if top:
            return int(st["pid"]), top
        deadline = time.time() + 3.0
        while time.time() < deadline:
            time.sleep(0.1)
            top, _ = _find_pid_top(st["pid"])
            if top:
                return int(st["pid"]), top
    exe = find_npp_exe()
    npp_args = build_npp_args(exe)
    startupinfo = None
    creationflags = 0
    if hide_window:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = SW_HIDE
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(npp_args, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            startupinfo=startupinfo, creationflags=creationflags,
                            cwd=npp_working_directory(exe))
    _write_state(proc.pid, exe)
    deadline = time.time() + wait_timeout
    while time.time() < deadline:
        time.sleep(0.15)
        top, _ = _find_pid_top(proc.pid)
        if top:
            if hide_window:
                u32.ShowWindow(top, SW_HIDE)
            return proc.pid, top
    raise TimeoutError(f"启动 Notepad++ 独占实例 (pid={proc.pid}) 后 {wait_timeout}s 内未出现窗口")

def _npp_doopen(top_hwnd, file_path):
    """让指定 N++ 实例在自己进程里打开 file_path。"""
    import os
    file_path = os.path.abspath(file_path)
    pid = window_process_id(top_hwnd)
    h = k32.OpenProcess(PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE, False, pid)
    if not h:
        raise OSError(f"OpenProcess pid={pid} 失败, err={ctypes.get_last_error()}")
    encoded = (file_path + "\0").encode("utf-16-le")
    remote = k32.VirtualAllocEx(h, None, len(encoded), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
    if not remote:
        k32.CloseHandle(h)
        raise OSError(f"VirtualAllocEx 失败, err={ctypes.get_last_error()}")
    written = ctypes.c_size_t(0)
    ok = k32.WriteProcessMemory(h, remote, encoded, len(encoded), ctypes.byref(written))
    if not ok:
        k32.VirtualFreeEx(h, remote, 0, MEM_RELEASE); k32.CloseHandle(h)
        raise OSError(f"WriteProcessMemory 失败, err={ctypes.get_last_error()}")
    ret = u32.SendMessageW(top_hwnd, NPPM_DOOPEN, 0, remote)
    k32.VirtualFreeEx(h, remote, 0, MEM_RELEASE)
    k32.CloseHandle(h)
    return bool(ret)

def open_file(file_path, switch_tab=True, wait_timeout=15.0, systemtray=False, hide_window=False):
    """在 skill 独占的 N++ 实例里打开 file_path。systemtray/hide_window 仅保留兼容。"""
    import os, time
    file_path = os.path.abspath(file_path)
    if not os.path.exists(file_path):
        raise FileNotFoundError(file_path)
    expected_path = normalize_path_for_compare(file_path)
    pid, top = _ensure_dedicated_instance(wait_timeout=wait_timeout, hide_window=True)
    _npp_doopen(top, file_path)
    deadline = time.time() + wait_timeout
    while time.time() < deadline:
        time.sleep(0.1)
        if u32.IsWindowVisible(top):
            u32.ShowWindow(top, SW_HIDE)
        pairs = _get_pid_notepadpp(pid, include_hidden=True)
        for t, _ in pairs:
            switch_to_file_by_path(t, file_path)
            try:
                binding = get_active_document_snapshot(t)
            except Exception:
                continue
            current_path = binding["path"]
            if current_path and normalize_path_for_compare(current_path) == expected_path:
                if u32.IsWindowVisible(t):
                    u32.ShowWindow(t, SW_HIDE)
                return t, int(binding["scintilla_hwnd"])
    raise TimeoutError(f"skill 独占 N++ 打开 {file_path} 后 {wait_timeout}s 内未就绪")

def _switch_to_tab_by_name(top_hwnd, name_lower):
    """靠模拟 Ctrl+Tab 循环切到标题含 name 的标签。best-effort。"""
    import time
    try:
        import win32api, win32con
    except ImportError:
        return False
    try:
        u32.SetForegroundWindow(top_hwnd)
    except Exception:
        pass
    time.sleep(0.1)
    for _ in range(30):  # 最多切 30 次
        title = _get_title(top_hwnd)
        if name_lower in title.lower():
            return True
        win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
        win32api.keybd_event(win32con.VK_TAB, 0, 0, 0)
        time.sleep(0.05)
        win32api.keybd_event(win32con.VK_TAB, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.2)
    return False

# ---------- 跨进程内存操作 ----------
def _open_hwnd_process(hwnd):
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = k32.OpenProcess(PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE, False, pid.value)
    if not h:
        raise OSError(f"OpenProcess 失败 (err={ctypes.get_last_error()})。需管理员或同权限运行。")
    return h, pid.value

def _open_target(scin):
    return _open_hwnd_process(scin)

def send_npp_text_message(top_hwnd, msg, text=None, buffer_chars=4096, wparam=None):
    h, pid = _open_hwnd_process(top_hwnd)
    remote = None
    size_bytes = buffer_chars * ctypes.sizeof(ctypes.c_wchar)
    try:
        remote = k32.VirtualAllocEx(h, None, size_bytes, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE)
        if not remote:
            raise OSError("VirtualAllocEx 失败")
        if text is not None:
            data = (text + "\0").encode("utf-16-le")
            if len(data) > size_bytes:
                raise ValueError("文本太长, 远程缓冲区不足")
            written = ctypes.c_size_t(0)
            if not k32.WriteProcessMemory(h, remote, data, len(data), ctypes.byref(written)):
                raise OSError(f"WriteProcessMemory 失败 (err={ctypes.get_last_error()})")
        ret = u32.SendMessageW(top_hwnd, msg, buffer_chars if wparam is None else wparam, remote)
        local = (ctypes.c_wchar * buffer_chars)()
        nr = ctypes.c_size_t(0)
        if not k32.ReadProcessMemory(h, remote, local, size_bytes, ctypes.byref(nr)):
            raise OSError(f"ReadProcessMemory 失败 (err={ctypes.get_last_error()})")
        return int(ret), local.value
    finally:
        if remote:
            k32.VirtualFreeEx(h, remote, 0, MEM_RELEASE)
        k32.CloseHandle(h)


def send_npp_int_out_message(top_hwnd, msg):
    """Read an int* output parameter written inside the Notepad++ process."""
    h, _ = _open_hwnd_process(top_hwnd)
    remote = None
    size = ctypes.sizeof(ctypes.c_int)
    try:
        remote = k32.VirtualAllocEx(h, None, size, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE)
        if not remote:
            raise OSError("VirtualAllocEx failed for Notepad++ integer output")

        initial = ctypes.c_int(-1)
        written = ctypes.c_size_t(0)
        if not k32.WriteProcessMemory(h, remote, ctypes.byref(initial), size, ctypes.byref(written)):
            raise OSError(f"WriteProcessMemory failed (err={ctypes.get_last_error()})")
        if written.value != size:
            raise OSError(f"short WriteProcessMemory: expected={size} actual={written.value}")

        accepted = int(u32.SendMessageW(top_hwnd, msg, 0, remote))
        result = ctypes.c_int(-1)
        read = ctypes.c_size_t(0)
        if not k32.ReadProcessMemory(h, remote, ctypes.byref(result), size, ctypes.byref(read)):
            raise OSError(f"ReadProcessMemory failed (err={ctypes.get_last_error()})")
        if read.value != size:
            raise OSError(f"short ReadProcessMemory: expected={size} actual={read.value}")
        if not accepted:
            raise RuntimeError(f"Notepad++ rejected integer output message {msg}")
        return int(result.value)
    finally:
        if remote:
            k32.VirtualFreeEx(h, remote, 0, MEM_RELEASE)
        k32.CloseHandle(h)


def get_current_file_path_strict(top_hwnd):
    ret, path = send_npp_text_message(top_hwnd, NPPM_GETFULLCURRENTPATH, buffer_chars=4096)
    if ret < 0:
        raise RuntimeError(f"NPPM_GETFULLCURRENTPATH failed with ret={ret}")
    return path or ""


def get_current_file_path(top_hwnd):
    try:
        return get_current_file_path_strict(top_hwnd)
    except Exception:
        return ""


def get_current_buffer_id(top_hwnd):
    buffer_id = int(u32.SendMessageW(top_hwnd, NPPM_GETCURRENTBUFFERID, 0, 0))
    if buffer_id <= 0:
        raise RuntimeError(f"Notepad++ returned an invalid current BufferID: {buffer_id}")
    return buffer_id


def get_active_scintilla(top_hwnd):
    view = send_npp_int_out_message(top_hwnd, NPPM_GETCURRENTSCINTILLA)
    if view not in (MAIN_VIEW, SUB_VIEW):
        raise RuntimeError(f"invalid active Scintilla view: {view}")
    scin = _editor_view_map(top_hwnd)[view]
    top_pid = window_process_id(top_hwnd)
    if not top_pid or window_process_id(scin) != top_pid:
        raise RuntimeError("Scintilla and Notepad++ PID mismatch")
    return view, scin


def get_active_document_snapshot(top_hwnd, retries=5, retry_delay=0.05):
    """Capture a stable PID/path/BufferID/view/HWND binding."""
    import time

    attempts = max(1, int(retries))
    last = None
    for attempt in range(attempts):
        path1 = get_current_file_path_strict(top_hwnd)
        buffer1 = get_current_buffer_id(top_hwnd)
        view1, scin1 = get_active_scintilla(top_hwnd)
        path2 = get_current_file_path_strict(top_hwnd)
        buffer2 = get_current_buffer_id(top_hwnd)
        view2, scin2 = get_active_scintilla(top_hwnd)
        pid = window_process_id(top_hwnd)
        last = {
            "pid": int(pid),
            "top_hwnd": int(top_hwnd),
            "active_view": int(view2),
            "scintilla_hwnd": int(scin2),
            "buffer_id": int(buffer2),
            "path": path2,
        }
        if (
            pid
            and path1 == path2
            and buffer1 == buffer2
            and view1 == view2
            and scin1 == scin2
            and window_process_id(scin2) == pid
        ):
            last["active_binding_verified"] = True
            return last
        if attempt + 1 < attempts:
            time.sleep(max(0.0, float(retry_delay)))

    raise RuntimeError(f"active Notepad++ document did not stabilize: {last}")


def get_buffer_position(top_hwnd, buffer_id, priority_view=MAIN_VIEW):
    position = int(u32.SendMessageW(top_hwnd, NPPM_GETPOSFROMBUFFERID, int(buffer_id), int(priority_view)))
    if position == -1:
        return None
    view = (position >> 30) & 0x3
    index = position & ((1 << 30) - 1)
    if view not in (MAIN_VIEW, SUB_VIEW):
        raise RuntimeError(f"invalid view encoded for BufferID {int(buffer_id)}: {view}")
    return view, index


def activate_buffer_id(top_hwnd, buffer_id, wait_timeout=2.0):
    import time

    position = get_buffer_position(top_hwnd, buffer_id)
    if position is None:
        return None
    view, index = position
    if not u32.SendMessageW(top_hwnd, NPPM_ACTIVATEDOC, view, index):
        raise RuntimeError(f"Notepad++ rejected activation of BufferID {int(buffer_id)}")
    deadline = time.monotonic() + max(0.0, float(wait_timeout))
    while True:
        snapshot = get_active_document_snapshot(top_hwnd)
        if snapshot["buffer_id"] == int(buffer_id):
            return snapshot
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Notepad++ did not activate BufferID {int(buffer_id)}")
        time.sleep(0.05)

def switch_to_file_by_path(top_hwnd, file_path):
    try:
        ret, _ = send_npp_text_message(
            top_hwnd,
            NPPM_SWITCHTOFILE,
            text=file_path,
            buffer_chars=max(4096, len(file_path) + 16),
            wparam=0,
        )
        return ret != 0
    except Exception:
        return False

def save_current_file_as(top_hwnd, out_path, save_as_copy=True):
    """让 Notepad++ 保存当前文件到 out_path。save_as_copy=True 等价 Save a Copy As。"""
    import os, time
    out_path = os.path.abspath(out_path)
    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    ret, _ = send_npp_text_message(
        top_hwnd,
        NPPM_SAVECURRENTFILEAS,
        text=out_path,
        buffer_chars=max(4096, len(out_path) + 16),
        wparam=1 if save_as_copy else 0,
    )
    for _ in range(50):
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return True, ret
        time.sleep(0.1)
    return False, ret

def ensure_current_path(top_hwnd, expected_path):
    current = get_current_file_path(top_hwnd)
    if not current:
        return False, current
    return normalize_path_for_compare(current) == normalize_path_for_compare(expected_path), current

def get_code_page(scin):
    """返回 Scintilla 当前文档 code page。65001=UTF-8, 0=系统 ANSI。"""
    cp = u32.SendMessageW(scin, SCI_GETCODEPAGE, 0, 0)
    return int(cp)

def code_page_to_encoding(code_page):
    if code_page == 65001:
        return "utf-8"
    if code_page == 0:
        return f"cp{k32.GetACP()}"
    enc = f"cp{code_page}"
    try:
        codecs.lookup(enc)
        return enc
    except LookupError:
        return "utf-8"

def decode_document_bytes(data, code_page):
    encoding = code_page_to_encoding(code_page)
    try:
        return data.decode(encoding), encoding, False
    except UnicodeDecodeError:
        return data.decode(encoding, errors="replace"), encoding, True

def has_unexpected_binary_profile(data):
    if not data:
        return False
    from collections import Counter
    import math
    sample = data[:min(len(data), 65536)]
    counts = Counter(sample)
    n = len(sample)
    entropy = -sum((v / n) * math.log2(v / n) for v in counts.values())
    ascii_text = sum(1 for b in sample if b in (9, 10, 13) or 32 <= b <= 126) / n
    return entropy > 7.2 and ascii_text < 0.65 and len(counts) > 180

def warn_if_risky_out_path(out_abs):
    import os
    ext = os.path.splitext(out_abs)[1].lower()
    if ext in SOURCE_EXTS:
        print(f"WARNING: 输出路径扩展名是 {ext}; DGS 格式目录可能接管该文件。建议把诊断副本放到独立临时目录。")

def metadata_path(path):
    return path + ".npp-encoding.json"

def detect_bom(data):
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8"
    if data.startswith(b"\xff\xfe"):
        return "utf-16-le"
    if data.startswith(b"\xfe\xff"):
        return "utf-16-be"
    return "none"

def newline_stats(data):
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    cr = data.count(b"\r") - crlf
    styles = [name for name, count in (("crlf", crlf), ("lf", lf), ("cr", cr)) if count]
    if not styles:
        style = "none"
    elif len(styles) == 1:
        style = styles[0]
    else:
        style = "mixed"
    return {"newline": style, "newline_counts": {"crlf": crlf, "lf": lf, "cr": cr}}

def inspect_document_bytes(data):
    info = newline_stats(data)
    info["bom"] = detect_bom(data)
    return info

def format_newline_counts(info):
    c = info["newline_counts"]
    return f"crlf={c['crlf']} lf={c['lf']} cr={c['cr']}"

def load_metadata(path):
    import os
    meta_file = metadata_path(path)
    if not os.path.exists(meta_file):
        return None
    with open(meta_file, "r", encoding="utf-8") as f:
        return json.load(f)

def normalize_newlines(data, style):
    if style not in ("crlf", "lf", "cr"):
        raise ValueError(f"不支持的换行类型: {style}")
    import re
    sep = {"crlf": b"\r\n", "lf": b"\n", "cr": b"\r"}[style]
    return sep.join(re.split(br"\r\n|\r|\n", data))

def write_metadata(path, code_page, encoding, byte_count, char_count, had_decode_errors, byte_info, source_path=""):
    meta = {
        "code_page": code_page,
        "encoding": encoding,
        "bytes": byte_count,
        "chars": char_count,
        "had_decode_errors": had_decode_errors,
        "bom": byte_info["bom"],
        "newline": byte_info["newline"],
        "newline_counts": byte_info["newline_counts"],
        "source_path": source_path,
        "note": "npp_bridge stores raw Scintilla document bytes; write them back without forcing UTF-8.",
    }
    with open(metadata_path(path), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

def write_metadata_for_file(path, source_path="", code_page=None, encoding="unknown"):
    with open(path, "rb") as f:
        data = f.read()
    byte_info = inspect_document_bytes(data)
    text = ""
    had_decode_errors = False
    if code_page is not None:
        text, encoding, had_decode_errors = decode_document_bytes(data, code_page)
    write_metadata(
        path,
        code_page if code_page is not None else -1,
        encoding,
        len(data),
        len(text),
        had_decode_errors,
        byte_info,
        source_path,
    )
    return data, byte_info, text, encoding

def copyas_file(target, out, opts):
    import os
    target_abs = os.path.abspath(target)
    out_abs = os.path.abspath(out)
    warn_if_risky_out_path(out_abs)
    top, scin = open_file(
        target_abs,
        wait_timeout=opts["wait_timeout"],
        systemtray=opts["systemtray"],
        hide_window=opts["hide_window"],
    )
    ok, current_path = ensure_current_path(top, target_abs)
    if not ok:
        raise RuntimeError(f"当前 Notepad++ 标签不是目标文件: expected={target_abs!r} current={current_path!r}")
    code_page = get_code_page(scin)
    saved, ret = save_current_file_as(top, out_abs, save_as_copy=True)
    if not saved:
        raise RuntimeError(f"NPPM_SAVECURRENTFILEAS 未生成输出文件或文件为空: ret={ret} out={out_abs}")
    data, byte_info, text, encoding = write_metadata_for_file(out_abs, current_path, code_page, code_page_to_encoding(code_page))
    if has_unexpected_binary_profile(data):
        print("WARNING: copyas 输出内容特征异常; 请确认当前 N++ 标签和输出路径正确。")
    print(f"OK copyas bytes={len(data)} chars={len(text)} lines={len(text.splitlines()) if text else 0} codepage={code_page} encoding={encoding} bom={byte_info['bom']} newline={byte_info['newline']} {format_newline_counts(byte_info)} ret={ret}")
    print(f"OUT: {out_abs}")
    print(f"META: {metadata_path(out_abs)}")
    return out_abs

def parse_write_args(args):
    opts = {
        "normalize_newlines": False,
        "newline": None,
        "allow_mixed_newlines": False,
        "allow_newline_change": False,
        "allow_bom_change": False,
        "target": None,
    }
    in_file = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--normalize-newlines":
            opts["normalize_newlines"] = True
        elif a.startswith("--newline="):
            opts["newline"] = a.split("=", 1)[1].lower()
        elif a == "--newline":
            i += 1
            if i >= len(args):
                raise ValueError("--newline 需要 crlf/lf/cr")
            opts["newline"] = args[i].lower()
        elif a == "--allow-mixed-newlines":
            opts["allow_mixed_newlines"] = True
        elif a == "--allow-newline-change":
            opts["allow_newline_change"] = True
        elif a == "--allow-bom-change":
            opts["allow_bom_change"] = True
        elif a.startswith("--target="):
            opts["target"] = a.split("=", 1)[1]
        elif a == "--target":
            i += 1
            if i >= len(args):
                raise ValueError("--target 需要原始 DGS 格式文件路径")
            opts["target"] = args[i]
        elif in_file is None:
            in_file = a
        else:
            raise ValueError(f"未知或重复参数: {a}")
        i += 1
    if opts["newline"] and opts["newline"] not in ("crlf", "lf", "cr"):
        raise ValueError("--newline 只支持 crlf/lf/cr")
    if not in_file:
        raise ValueError("write 需要指定 in_file")
    return in_file, opts

def parse_open_read_args(args, allow_out=False):
    opts = {
        "systemtray": False,
        "hide_window": False,
        "wait_timeout": 15.0,
        "copyas": False,
    }
    positional = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--systemtray":
            opts["systemtray"] = True
        elif a == "--hide-window":
            opts["hide_window"] = True
        elif a == "--copyas":
            opts["copyas"] = True
        elif a.startswith("--wait="):
            opts["wait_timeout"] = float(a.split("=", 1)[1])
        elif a == "--wait":
            i += 1
            if i >= len(args):
                raise ValueError("--wait 需要秒数")
            opts["wait_timeout"] = float(args[i])
        elif a.startswith("--"):
            raise ValueError(f"未知参数: {a}")
        else:
            positional.append(a)
        i += 1
    if not allow_out and len(positional) > 1:
        raise ValueError("open 只接受一个文件路径")
    return positional, opts

def prepare_write_bytes(in_file, data, opts):
    meta = load_metadata(in_file)
    original_info = inspect_document_bytes(data)
    expected_newline = meta.get("newline") if meta else None
    expected_bom = meta.get("bom") if meta else None

    target_newline = opts["newline"] or expected_newline
    if opts["normalize_newlines"]:
        if target_newline not in ("crlf", "lf", "cr"):
            raise ValueError("无法推断要规范化成哪种换行; 请传 --newline crlf/lf/cr")
        data = normalize_newlines(data, target_newline)

    info = inspect_document_bytes(data)
    errors = []
    if info["newline"] == "mixed" and not opts["allow_mixed_newlines"]:
        errors.append(
            f"输入文件是混合换行({format_newline_counts(info)}); "
            "先规范化, 或明确传 --allow-mixed-newlines"
        )
    if (
        expected_newline in ("crlf", "lf", "cr")
        and info["newline"] in ("crlf", "lf", "cr")
        and info["newline"] != expected_newline
        and not opts["allow_newline_change"]
    ):
        errors.append(
            f"换行类型从 metadata 的 {expected_newline} 变成 {info['newline']}; "
            "请传 --normalize-newlines, 或明确传 --allow-newline-change"
        )
    if expected_bom and expected_bom != info["bom"] and not opts["allow_bom_change"]:
        errors.append(
            f"BOM 从 metadata 的 {expected_bom} 变成 {info['bom']}; "
            "请确认编码后再传 --allow-bom-change"
        )
    if errors:
        detail = "\n".join(f"- {e}" for e in errors)
        raise ValueError(f"写回前检查失败:\n{detail}")
    return data, info, meta, original_info

def read_document_bytes(scin):
    """从 Scintilla 读取完整文档字节，并保留当前文档编码。"""
    length = u32.SendMessageW(scin, SCI_GETTEXTLENGTH, 0, 0)
    if length <= 0:
        return b""
    alloc = length + 16
    h, pid = _open_target(scin)
    remote = None
    try:
        remote = k32.VirtualAllocEx(h, None, alloc, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE)
        if not remote:
            raise OSError("VirtualAllocEx 失败")
        written = u32.SendMessageW(scin, SCI_GETTEXT, alloc, remote)
        if written < 0:
            raise OSError(f"SCI_GETTEXT 失败 (ret={written})")
        n = written if written > 0 else length
        local = (ctypes.c_char * alloc)()
        nr = ctypes.c_size_t(0)
        if not k32.ReadProcessMemory(h, remote, local, alloc, ctypes.byref(nr)):
            raise OSError(f"ReadProcessMemory 失败 (err={ctypes.get_last_error()})")
        return bytes(local[:n])
    finally:
        if remote:
            k32.VirtualFreeEx(h, remote, 0, MEM_RELEASE)
        k32.CloseHandle(h)

def read_text(scin):
    """从 Scintilla 读取完整文档文本。兼容旧调用，按文档 code page 解码。"""
    data = read_document_bytes(scin)
    text, _, _ = decode_document_bytes(data, get_code_page(scin))
    return text


def is_document_modified(scin):
    """Return Scintilla's save-point state for the active document."""
    if not scin:
        raise RuntimeError("Scintilla handle is required to inspect the modify state")
    return bool(u32.SendMessageW(scin, SCI_GETMODIFY, 0, 0))


def get_document_length(scin):
    if not scin:
        raise RuntimeError("Scintilla handle is required to read the document length")
    length = int(u32.SendMessageW(scin, SCI_GETTEXTLENGTH, 0, 0))
    if length < 0:
        raise RuntimeError(f"Scintilla returned an invalid document length: {length}")
    return length


def set_document_save_point(scin):
    """Mark the current Scintilla document clean and verify the result."""
    if not scin:
        raise RuntimeError("Scintilla handle is required to set the save point")
    u32.SendMessageW(scin, SCI_SETSAVEPOINT, 0, 0)
    return not is_document_modified(scin)

def write_document_bytes(scin, data):
    """把原编码文档字节写入 Scintilla 缓冲区 (替换全文)。返回写入字节数。"""
    alloc = len(data) + 16
    h, pid = _open_target(scin)
    remote = None
    try:
        remote = k32.VirtualAllocEx(h, None, alloc, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE)
        if not remote:
            raise OSError("VirtualAllocEx 失败")
        written = ctypes.c_size_t(0)
        if not k32.WriteProcessMemory(h, remote, data, len(data), ctypes.byref(written)):
            raise OSError(f"WriteProcessMemory 失败 (err={ctypes.get_last_error()})")
        ret = u32.SendMessageW(scin, SCI_SETTEXT, 0, remote)
        # SCI_SETTEXT 成功返回 1, 失败返回 0
        if ret == 0:
            raise OSError("SCI_SETTEXT 返回 0, 写入可能失败")
        return len(data)
    finally:
        if remote:
            k32.VirtualFreeEx(h, remote, 0, MEM_RELEASE)
        k32.CloseHandle(h)

def write_text(scin, text):
    """把 text 按当前 Scintilla code page 编码后写入缓冲区。"""
    encoding = code_page_to_encoding(get_code_page(scin))
    return write_document_bytes(scin, text.encode(encoding))

def save_current(top_hwnd, timeout=2.0, scin=None):
    """后台请求 Notepad++ 保存，不改变用户当前的前台窗口。"""
    import time

    WM_COMMAND = 0x0111
    IDM_FILE_SAVE = 41006
    scin = scin or _pick_scintilla(top_hwnd)
    u32.SendMessageW(top_hwnd, WM_COMMAND, IDM_FILE_SAVE, 0)

    deadline = time.monotonic() + timeout
    while True:
        if scin:
            clean = not is_document_modified(scin)
        else:
            clean = not _get_title(top_hwnd).lstrip().startswith("*")
        if clean:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)

# ---------- CLI ----------
def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("用法: python npp_bridge.py <probe|status|open|read|copyas|inspect|write|shutdown> [args]")
        print("  probe [--include-hidden]     检查当前 N++ 状态 (优先 skill 独占实例)")
        print("  status                       看 skill 独占 N++ 实例的 pid 和存活情况")
        print("  open [--systemtray] [--hide-window] <file>")
        print("                               在 skill 独占实例里打开 file (后台隐藏)")
        print("  read [--copyas] [--systemtray] [--hide-window] [file] [out]")
        print("                               自动打开 file 并读取文档内容到 out; --copyas 走 N++ Save a Copy As")
        print("  copyas [--systemtray] [--hide-window] <file> <out>")
        print("                               让 N++ Save a Copy As 到 out")
        print("  inspect <file>               检查文件 BOM/换行, 并与旁边 metadata 对比")
        print("  write <in_file> [--target 原文件] [--normalize-newlines] [--newline crlf|lf|cr]")
        print("                        [--allow-mixed-newlines|--allow-newline-change|--allow-bom-change]")
        print("                               把 in_file 原始字节写回目标标签并保存")
        print("  shutdown                     关闭 skill 独占的 N++ 实例 (不影响用户自己的 N++)")
        sys.exit(0)
    cmd = sys.argv[1]

    if cmd == "probe":
        include_hidden = "--include-hidden" in sys.argv[2:]
        top, scin = find_notepadpp(include_hidden=include_hidden)
        if not top:
            print("NOT_RUNNING")
            sys.exit(1)
        title = _get_title(top)
        length = u32.SendMessageW(scin, SCI_GETTEXTLENGTH, 0, 0) if scin else -1
        code_page = get_code_page(scin) if scin else -1
        current_path = get_current_file_path(top)
        print(f"TITLE: {title}")
        print(f"PATH: {current_path or '(unknown)'}")
        print(f"LENGTH: {length}")
        print(f"CODEPAGE: {code_page} ({code_page_to_encoding(code_page) if code_page >= 0 else 'N/A'})")
        sys.exit(0)

    if cmd == "status":
        st = _read_state()
        if not st:
            print("NO_STATE"); sys.exit(1)
        alive = _is_pid_alive(st.get("pid"))
        top, _ = _find_pid_top(st.get("pid")) if alive else (None, None)
        print(f"STATE_FILE: {_STATE_FILE}")
        print(f"PID: {st.get('pid')}  ALIVE: {alive}")
        print(f"EXE: {st.get('exe')}")
        print(f"STARTED_AT: {st.get('started_at', '?')}")
        if alive and top:
            current_path = get_current_file_path(top)
            print(f"CURRENT_PATH: {current_path or '(unknown)'}")
            print(f"TITLE: {_get_title(top)}")
        sys.exit(0 if alive else 1)

    if cmd == "shutdown":
        st = _read_state()
        if not st:
            print("NO_STATE"); sys.exit(0)
        pid = int(st.get("pid") or 0)
        if not _is_pid_alive(pid):
            _clear_state(); print(f"pid={pid} already gone, cleared state"); sys.exit(0)
        WM_CLOSE = 0x0010
        closed_any = False
        for top in _enum_toplevel():
            if _get_class(top) != "Notepad++":
                continue
            if window_process_id(top) != pid:
                continue
            if hasattr(u32, "PostMessageW"):
                u32.PostMessageW(top, WM_CLOSE, 0, 0)
            else:
                u32.SendMessageW(top, WM_CLOSE, 0, 0)
            closed_any = True
        import time
        deadline = time.time() + 5.0
        while time.time() < deadline and _is_pid_alive(pid):
            time.sleep(0.15)
        if _is_pid_alive(pid):
            PROCESS_TERMINATE = 0x0001
            h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if h and hasattr(k32, "TerminateProcess"):
                k32.TerminateProcess(h, 0)
            if h:
                k32.CloseHandle(h)
        alive = _is_pid_alive(pid)
        _clear_state()
        print(f"SHUTDOWN pid={pid} wm_close_sent={closed_any} still_alive={alive}")
        sys.exit(0 if not alive else 1)

    if cmd == "open":
        try:
            positional, opts = parse_open_read_args(sys.argv[2:], allow_out=False)
        except ValueError as e:
            print(str(e)); sys.exit(2)
        if not positional:
            print("open 需要指定文件"); sys.exit(2)
        top, scin = open_file(
            positional[0],
            wait_timeout=opts["wait_timeout"],
            systemtray=opts["systemtray"],
            hide_window=opts["hide_window"],
        )
        code_page = get_code_page(scin)
        print(f"OK opened: {_get_title(top)}  path={get_current_file_path(top) or '(unknown)'}  length={u32.SendMessageW(scin, SCI_GETTEXTLENGTH,0,0)}  codepage={code_page} ({code_page_to_encoding(code_page)})")
        sys.exit(0)

    if cmd == "read":
        # read [file] [out]  或 read [out]
        target = None
        out = "out.dat"
        try:
            args, opts = parse_open_read_args(sys.argv[2:], allow_out=True)
        except ValueError as e:
            print(str(e)); sys.exit(2)
        if args:
            # 若第一个参数是已存在的文件路径, 当作目标; 否则当输出名
            import os
            if os.path.exists(args[0]) and not args[0].endswith(".txt") is False:
                # 仍可能用户想把输出叫某名; 用启发: 存在则为目标
                pass
            if os.path.exists(args[0]):
                target = args[0]
                if len(args) > 1:
                    out = args[1]
            else:
                out = args[0]
        if opts["copyas"]:
            if not target:
                print("read --copyas 需要指定原始文件和输出文件"); sys.exit(2)
            try:
                copyas_file(target, out, opts)
            except Exception as e:
                print(f"copyas 失败: {e}")
                sys.exit(5)
            sys.exit(0)
        if target:
            top, scin = open_file(
                target,
                wait_timeout=opts["wait_timeout"],
                systemtray=opts["systemtray"],
                hide_window=opts["hide_window"],
            )
        else:
            top, scin = find_notepadpp(include_hidden=opts["hide_window"] or opts["systemtray"])
        if not scin:
            print("未找到 Notepad++ 活动文档。用 'open <file>' 打开, 或手动在 Notepad++ 打开文件。")
            sys.exit(1)
        import os
        out_abs = os.path.abspath(out)
        warn_if_risky_out_path(out_abs)
        data = read_document_bytes(scin)
        code_page = get_code_page(scin)
        text, encoding, had_decode_errors = decode_document_bytes(data, code_page)
        byte_info = inspect_document_bytes(data)
        with open(out_abs, "wb") as f:
            f.write(data)
        current_path = get_current_file_path(top)
        write_metadata(out_abs, code_page, encoding, len(data), len(text), had_decode_errors, byte_info, current_path)
        nlines = len(text.splitlines())
        if has_unexpected_binary_profile(data):
            print("WARNING: 读取内容特征异常; 请确认 Notepad++ 当前标签与目标文件一致。")
        # stdout 只输出元信息 + 绝对输出路径。**故意不打印任何内容预览**,
        # 避免调用方把 stdout 截屏当成全文 (历史踩坑: 看到带行号的前 N 行被误当成
        # 完整文件)。完整内容请用 Read 工具读 OUT 给出的路径。
        print(f"OK bytes={len(data)} chars={len(text)} lines={nlines} codepage={code_page} encoding={encoding} bom={byte_info['bom']} newline={byte_info['newline']} {format_newline_counts(byte_info)}")
        print(f"OUT: {out_abs}")
        print(f"META: {metadata_path(out_abs)}")
        print("NOTE: stdout 仅元信息, 完整内容在 OUT 路径里, 用 Read 工具读它。")
        sys.exit(0)

    if cmd == "copyas":
        try:
            positional, opts = parse_open_read_args(sys.argv[2:], allow_out=True)
        except ValueError as e:
            print(str(e)); sys.exit(2)
        if len(positional) != 2:
            print("copyas 需要指定原始文件和输出文件"); sys.exit(2)
        try:
            copyas_file(positional[0], positional[1], opts)
        except Exception as e:
            print(f"copyas 失败: {e}")
            sys.exit(5)
        sys.exit(0)

    if cmd == "inspect":
        if len(sys.argv) < 3:
            print("inspect 需要指定 file"); sys.exit(2)
        with open(sys.argv[2], "rb") as f:
            data = f.read()
        info = inspect_document_bytes(data)
        meta = load_metadata(sys.argv[2])
        print(f"FILE: {sys.argv[2]}")
        print(f"BOM: {info['bom']}")
        print(f"NEWLINE: {info['newline']} {format_newline_counts(info)}")
        if meta:
            print(f"META: {metadata_path(sys.argv[2])}")
            print(f"META_SOURCE_PATH: {meta.get('source_path', '') or 'unknown'}")
            print(f"META_BOM: {meta.get('bom', 'unknown')}")
            print(f"META_NEWLINE: {meta.get('newline', 'unknown')} counts={meta.get('newline_counts', {})}")
            issues = []
            if meta.get("bom") and meta.get("bom") != info["bom"]:
                issues.append(f"BOM changed: {meta.get('bom')} -> {info['bom']}")
            if meta.get("newline") and meta.get("newline") != info["newline"]:
                issues.append(f"newline changed: {meta.get('newline')} -> {info['newline']}")
            if info["newline"] == "mixed":
                issues.append("input has mixed newlines")
            if issues:
                print("ISSUES:")
                for issue in issues:
                    print(f"- {issue}")
                sys.exit(1)
            print("OK metadata-compatible")
        elif info["newline"] == "mixed":
            print("ISSUES:")
            print("- input has mixed newlines")
            sys.exit(1)
        else:
            print("NO_META")
        sys.exit(0)

    if cmd == "write":
        try:
            in_file, opts = parse_write_args(sys.argv[2:])
        except ValueError as e:
            print(str(e)); sys.exit(2)
        with open(in_file, "rb") as f:
            data = f.read()
        try:
            data, byte_info, meta, original_info = prepare_write_bytes(in_file, data, opts)
        except ValueError as e:
            print(str(e)); sys.exit(3)
        top, scin = find_notepadpp()
        if not scin:
            print("未找到 Notepad++ 活动文档。写回前必须先用 Notepad++ 打开目标文件。"); sys.exit(1)
        expected_target = opts["target"] or (meta.get("source_path") if meta else None)
        if expected_target:
            switch_to_file_by_path(top, expected_target)
            top, scin = find_notepadpp()
            if not scin:
                print("切换目标标签后未找到 Notepad++ 活动文档。"); sys.exit(1)
            ok, current_path = ensure_current_path(top, expected_target)
            if not ok:
                print(f"当前 Notepad++ 标签不是写回目标, 已拒绝写入。expected={expected_target!r} current={current_path!r}")
                sys.exit(4)
        else:
            print("WARNING: 未提供 --target 且 metadata 没有 source_path; 将写入当前活动标签。强烈建议使用 --target 原始文件路径。")
        code_page = get_code_page(scin)
        n = write_document_bytes(scin, data)
        if opts["normalize_newlines"] and original_info["newline"] != byte_info["newline"]:
            print(f"NOTE normalized newlines: {original_info['newline']} -> {byte_info['newline']} {format_newline_counts(byte_info)}")
        if meta:
            print(f"NOTE checked metadata: bom={meta.get('bom', 'unknown')} newline={meta.get('newline', 'unknown')}")
        else:
            print("WARNING: 未找到 metadata; 只检查了输入文件本身是否混合换行。")
        print(f"OK wrote {n} bytes into Scintilla buffer using existing document codepage={code_page} ({code_page_to_encoding(code_page)}) bom={byte_info['bom']} newline={byte_info['newline']} {format_newline_counts(byte_info)}")
        if save_current(top):
            print("已发送 DGS 文档保存命令，Notepad++ 标题已无 * 修改标记。")
        else:
            print("WARNING: 已发送保存命令, 但 Notepad++ 标题仍有 * 修改标记; 请手动确认保存。")
        sys.exit(0)

    print(f"未知命令: {cmd}")
    sys.exit(2)

if __name__ == "__main__":
    main()
