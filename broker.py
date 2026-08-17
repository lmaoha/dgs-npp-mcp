#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Single local broker for DGS-format files via one Notepad++ process.

Multiple stdio MCP adapters may connect to this broker. The broker is the only
process that talks to Notepad++, so reads/writes are serialized through one
MCP-managed Notepad++ instance.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from tool_contract import TOOL_SPECS, tools_list

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


SERVER_NAME = "dgs_npp_broker"
SERVER_VERSION = "0.8.4"
BROKER_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "dgs-npp-mcp"
STATE_FILE = STATE_DIR / "npp-instance-broker.json"
OPEN_FILES_FILE = STATE_DIR / "open-files.json"
BROKER_HOST = "127.0.0.1"
BROKER_PORT = int(os.environ.get("DGS_NPP_BROKER_PORT", "57931"))
WORKER_TIMEOUT = float(os.environ.get("DGS_NPP_WORKER_TIMEOUT", "20"))
BRIDGE_PATH = Path(os.environ.get(
    "DGS_NPP_BRIDGE",
    str(Path(__file__).with_name("npp_bridge.py")),
))
SW_HIDE = 0
SW_SHOW = 5
SW_RESTORE = 9
WM_CLOSE = 0x0010
MAX_SAFE_JSON_INTEGER = (1 << 53) - 1
OWNERSHIP_STATE_VERSION = 4


def _load_bridge() -> Any:
    if not BRIDGE_PATH.exists():
        raise RuntimeError(f"npp bridge module not found: {BRIDGE_PATH}")
    spec = importlib.util.spec_from_file_location("dgs_npp_bridge", BRIDGE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load npp bridge module: {BRIDGE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge()
NPPM_GETCURRENTBUFFERID = bridge.NPPM_GETCURRENTBUFFERID
NPPM_RELOADBUFFERID = bridge.NPPM_RELOADBUFFERID
OPEN_FILES: dict[str, dict[str, Any]] = {}
LOCK = threading.RLock()
STOP_EVENT = threading.Event()

BINDING_FIELDS = (
    "pid",
    "top_hwnd",
    "active_view",
    "scintilla_hwnd",
    "buffer_id",
    "path",
    "active_binding_verified",
)


def _json_line(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _log(message: str) -> None:
    sys.stderr.write(f"[{SERVER_NAME}] {message}\n")
    sys.stderr.flush()


def _ok(request_id: Any, result: dict[str, Any]) -> None:
    _json_line({"jsonrpc": "2.0", "id": request_id, "result": result})


def _err(request_id: Any, code: int, message: str, data: Any = None) -> None:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    _json_line({"jsonrpc": "2.0", "id": request_id, "error": error})


def _tool_result(data: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
        "structuredContent": data,
        "isError": is_error,
    }


def _broker_response(request_id: Any, result: dict[str, Any] | None = None, error: str | None = None) -> dict[str, Any]:
    if error is not None:
        return {"id": request_id, "ok": False, "error": error}
    return {"id": request_id, "ok": True, "result": result or {}}


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _stat(path: str) -> dict[str, Any]:
    st = os.stat(path)
    return {
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "mtime_ns_exact": str(st.st_mtime_ns),
        "mtime": st.st_mtime,
    }


def _binding_result(binding: dict[str, Any] | None) -> dict[str, Any]:
    binding = binding or {}
    return {
        "pid": binding.get("pid"),
        "top_hwnd": binding.get("top_hwnd"),
        "active_view": binding.get("active_view"),
        "scintilla_hwnd": binding.get("scintilla_hwnd"),
        "buffer_id": binding.get("buffer_id"),
        "path": binding.get("path", ""),
        "active_binding_verified": bool(binding.get("active_binding_verified", False)),
    }


def _capture_binding(
    top: int,
    *,
    expected_path: str | None = None,
    expected_scin: int | None = None,
    expected_buffer_id: int | None = None,
) -> dict[str, Any]:
    binding = bridge.get_active_document_snapshot(top)
    if not binding.get("active_binding_verified"):
        raise RuntimeError(f"active Notepad++ binding was not verified: {binding}")
    if int(binding["top_hwnd"]) != int(top):
        raise RuntimeError(
            f"active Notepad++ top HWND changed: expected={int(top)} actual={binding['top_hwnd']}"
        )
    if expected_scin is not None and int(binding["scintilla_hwnd"]) != int(expected_scin):
        raise RuntimeError(
            "active Scintilla HWND changed: "
            f"expected={int(expected_scin)} actual={binding['scintilla_hwnd']}"
        )
    if expected_buffer_id is not None and int(binding["buffer_id"]) != int(expected_buffer_id):
        raise RuntimeError(
            "active Notepad++ BufferID changed: "
            f"expected={int(expected_buffer_id)} actual={binding['buffer_id']}"
        )
    if expected_path is not None:
        actual_path = str(binding.get("path") or "")
        matches = (
            _norm(actual_path) == _norm(expected_path)
            if _is_real_file_path(actual_path) and _is_real_file_path(expected_path)
            else actual_path == expected_path
        )
        if not matches:
            raise RuntimeError(
                f"active Notepad++ path changed: expected={expected_path!r} actual={actual_path!r}"
            )
    return binding


def _mtime_matches(expected: Any, actual: int) -> bool:
    """Compare an mtime while tolerating legacy JSON-number precision loss."""
    if isinstance(expected, bool):
        return False
    if isinstance(expected, str):
        try:
            return int(expected) == actual
        except ValueError:
            return False
    if isinstance(expected, int):
        if expected == actual:
            return True
        if abs(expected) > MAX_SAFE_JSON_INTEGER or abs(actual) > MAX_SAFE_JSON_INTEGER:
            return float(expected) == float(actual)
    return False


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_fragment_newlines(text: str, newline_style: str) -> str:
    if newline_style not in {"crlf", "lf", "cr"}:
        return text
    separator = {"crlf": "\r\n", "lf": "\n", "cr": "\r"}[newline_style]
    return re.sub(r"\r\n|\r|\n", separator, text)


def _search_text(
    text: str,
    query: str,
    *,
    regex: bool = False,
    case_sensitive: bool = True,
    context_lines: int = 2,
    max_matches: int = 50,
    max_output_chars: int = 20_000,
) -> dict[str, Any]:
    if not query:
        raise ValueError("query must not be empty")
    if not 0 <= context_lines <= 20:
        raise ValueError("context_lines must be between 0 and 20")
    if not 1 <= max_matches <= 500:
        raise ValueError("max_matches must be between 1 and 500")
    if not 1_000 <= max_output_chars <= 200_000:
        raise ValueError("max_output_chars must be between 1000 and 200000")

    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(query if regex else re.escape(query), flags)
    if regex and pattern.search("") is not None:
        raise ValueError("regular expressions that match empty text are not supported")

    lines = text.splitlines()
    matches: list[dict[str, Any]] = []
    total_matches = 0
    returned_chars = 0
    output_truncated = False

    for line_index, line_text in enumerate(lines):
        for match in pattern.finditer(line_text):
            total_matches += 1
            before = [
                {"line": index + 1, "text": lines[index]}
                for index in range(max(0, line_index - context_lines), line_index)
            ]
            after = [
                {"line": index + 1, "text": lines[index]}
                for index in range(line_index + 1, min(len(lines), line_index + context_lines + 1))
            ]
            entry = {
                "line": line_index + 1,
                "column": match.start() + 1,
                "end_column": match.end() + 1,
                "text": line_text,
                "before": before,
                "after": after,
            }
            entry_chars = len(line_text) + sum(len(item["text"]) for item in before + after)
            if len(matches) >= max_matches or returned_chars + entry_chars > max_output_chars:
                output_truncated = True
                continue
            matches.append(entry)
            returned_chars += entry_chars

    return {
        "matches": matches,
        "returned_matches": len(matches),
        "total_matches": total_matches,
        "truncated": output_truncated,
        "returned_chars": returned_chars,
    }


def _build_exact_patch(
    original_text: str,
    old_text: str,
    new_text: str,
    *,
    expected_occurrences: int = 1,
    newline_style: str = "none",
    normalize_newlines: bool = True,
) -> dict[str, Any]:
    if not isinstance(old_text, str) or not isinstance(new_text, str):
        raise ValueError("old_text and new_text must be strings")
    if not old_text:
        raise ValueError("old_text must not be empty")
    if not 1 <= expected_occurrences <= 100:
        raise ValueError("expected_occurrences must be between 1 and 100")

    normalized_old = _normalize_fragment_newlines(old_text, newline_style) if normalize_newlines else old_text
    normalized_new = _normalize_fragment_newlines(new_text, newline_style) if normalize_newlines else new_text
    occurrences = original_text.count(normalized_old)
    if occurrences != expected_occurrences:
        raise RuntimeError(
            "patch anchor occurrence mismatch: "
            f"expected={expected_occurrences}, actual={occurrences}"
        )

    first_start = original_text.find(normalized_old)
    last_start = original_text.rfind(normalized_old)
    prefix = original_text[:first_start]
    suffix = original_text[last_start + len(normalized_old):]
    updated_text = original_text.replace(normalized_old, normalized_new)
    expected_chars = len(original_text) + occurrences * (len(normalized_new) - len(normalized_old))
    if len(updated_text) != expected_chars:
        raise RuntimeError(
            f"patch length invariant failed: expected={expected_chars}, actual={len(updated_text)}"
        )
    if not updated_text.startswith(prefix) or not updated_text.endswith(suffix):
        raise RuntimeError("patch changed text outside the matched regions")

    return {
        "text": updated_text,
        "old_text": normalized_old,
        "new_text": normalized_new,
        "occurrences": occurrences,
        "first_start_char": first_start,
        "last_end_char": last_start + len(normalized_old),
        "before_chars": len(original_text),
        "after_chars": len(updated_text),
        "prefix_sha256": _sha256_hex(prefix.encode("utf-8")),
        "suffix_sha256": _sha256_hex(suffix.encode("utf-8")),
        "newlines_normalized": normalize_newlines and newline_style in {"crlf", "lf", "cr"},
    }


def _is_real_file_path(value: str) -> bool:
    return bool(value) and os.path.isabs(value)


def _read_state() -> dict[str, Any] | None:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_state_payload(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _write_state(
    pid: int,
    exe: str,
    *,
    headless: bool,
    startup_buffer_id: int | None = None,
    startup_path: str | None = None,
) -> None:
    state: dict[str, Any] = {
        "state_version": OWNERSHIP_STATE_VERSION,
        "pid": int(pid),
        "exe": bridge._normal_exe_path(exe),
        "headless": bool(headless),
        "launch_mode": "headless" if headless else "normal",
        "window_class": bridge.expected_window_class(headless),
        "runtime_version": bridge.get_file_version(exe),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if startup_buffer_id is not None:
        state["startup_buffer_id"] = int(startup_buffer_id)
        state["startup_path"] = startup_path or ""
    _save_state_payload(state)


def _state_is_headless(state: dict[str, Any]) -> bool:
    if "headless" in state:
        return bool(state["headless"])
    exe = state.get("exe")
    if not exe:
        return False
    try:
        return bool(bridge._is_bundled_exe(exe))
    except Exception:
        return False


def _state_identity_filters(state: dict[str, Any] | None) -> dict[str, Any]:
    if not state:
        return {}
    return {
        "expected_exe": state.get("exe"),
        "expected_headless": _state_is_headless(state),
    }


def _managed_pairs(pid: int, *, include_hidden: bool = True) -> list[tuple[int, int]]:
    state = _read_state()
    filters = (
        _state_identity_filters(state)
        if state and int(state.get("pid") or 0) == int(pid)
        else {}
    )
    return bridge._get_pid_notepadpp(
        int(pid),
        include_hidden=include_hidden,
        **filters,
    )


def _find_managed_top(pid: int) -> tuple[int | None, int | None]:
    state = _read_state()
    filters = (
        _state_identity_filters(state)
        if state and int(state.get("pid") or 0) == int(pid)
        else {}
    )
    return bridge._find_pid_top(int(pid), **filters)


def _validate_managed_window(state: dict[str, Any], top: int) -> dict[str, Any]:
    identity = bridge.validate_window_identity(
        top,
        expected_pid=int(state.get("pid") or 0),
        expected_exe=state.get("exe"),
        expected_headless=_state_is_headless(state),
    )
    updated = dict(state)
    updated["state_version"] = OWNERSHIP_STATE_VERSION
    updated["exe"] = identity["exe"]
    updated["headless"] = bool(identity["headless"])
    updated["launch_mode"] = "headless" if identity["headless"] else "normal"
    updated["window_class"] = identity["window_class"]
    updated["runtime_version"] = bridge.get_file_version(identity["exe"])
    _save_state_payload(updated)
    return identity


def _record_startup_placeholder_if_safe(top: int) -> dict[str, Any] | None:
    """Persist the managed process's original empty unnamed BufferID."""
    state = _read_state()
    if not state or not _state_is_headless(state) or "startup_buffer_id" in state:
        return state
    if int(state.get("pid") or 0) != bridge.window_process_id(top):
        return state

    binding = _capture_binding(top)
    path = str(binding.get("path") or "")
    length = bridge.get_document_length(int(binding["scintilla_hwnd"]))
    confirmed = _capture_binding(
        top,
        expected_path=path,
        expected_scin=int(binding["scintilla_hwnd"]),
        expected_buffer_id=int(binding["buffer_id"]),
    )
    if _is_real_file_path(path) or length != 0:
        return state

    state = dict(state)
    state["state_version"] = OWNERSHIP_STATE_VERSION
    state["startup_buffer_id"] = int(confirmed["buffer_id"])
    state["startup_path"] = path
    _save_state_payload(state)
    return state


def _clear_state() -> None:
    state = _read_state()
    if state:
        pid = int(state.get("pid") or 0)
        if bridge._is_pid_alive(pid):
            pairs = _managed_pairs(pid, include_hidden=True)
            if not pairs or any(not _window_visible(top) for top, _ in pairs):
                raise RuntimeError(
                    f"refusing to clear ownership of live hidden Notepad++ pid={pid}"
                )
    try:
        STATE_FILE.unlink()
    except FileNotFoundError:
        pass


def _load_open_files() -> dict[str, dict[str, Any]]:
    try:
        return json.loads(OPEN_FILES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_open_files(files: dict[str, dict[str, Any]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OPEN_FILES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(files, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(OPEN_FILES_FILE)


def _ensure_npp(wait_timeout: float = 15.0) -> tuple[int, int]:
    state = _read_state()
    if state and bridge._is_pid_alive(state.get("pid")):
        top, _ = _find_managed_top(int(state["pid"]))
        if not top:
            raise RuntimeError(
                f"managed Notepad++ pid={state.get('pid')} is alive without a window matching "
                "its owned EXE, class, and launch mode; "
                "refusing to overwrite its ownership state"
            )
        _validate_managed_window(state, top)
        try:
            _record_startup_placeholder_if_safe(top)
        except Exception as exc:
            _log(f"could not record startup buffer for pid={state.get('pid')}: {type(exc).__name__}: {exc}")
        current = _capture_binding(top).get("path") or ""
        if (
            not _state_is_headless(state)
            and _is_real_file_path(current)
            and _norm(current) not in _load_open_files()
        ):
            released = _release_managed_npp(
                "managed instance owns an untracked user file",
                wait_timeout=0.0,
                close_clean=False,
                preserve_untracked=True,
            )
            if not released["safe_to_forget"]:
                raise RuntimeError(
                    f"could not safely release managed Notepad++ pid={state.get('pid')}: {released}"
                )
        else:
            _hide_npp(top)
            return int(state["pid"]), top
    elif state:
        _clear_state()

    exe = bridge.find_npp_exe()
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = SW_HIDE
    npp_args = bridge.build_npp_args(exe)
    headless = any(str(arg).lower() in {"-headless", "--headless"} for arg in npp_args[1:])
    cwd = bridge.npp_working_directory(exe)
    _log(f"starting managed Notepad++: exe={exe!r} cwd={cwd!r} args={' '.join(npp_args[1:])}")
    proc = subprocess.Popen(npp_args, shell=False, startupinfo=startupinfo, cwd=cwd)
    _write_state(proc.pid, exe, headless=headless)
    deadline = time.time() + wait_timeout
    while time.time() < deadline:
        time.sleep(0.15)
        top, _ = _find_managed_top(proc.pid)
        if top:
            state = _read_state() or {}
            _validate_managed_window(state, top)
            _hide_npp(top)
            try:
                _record_startup_placeholder_if_safe(top)
            except Exception as exc:
                _log(f"could not record startup buffer for pid={proc.pid}: {type(exc).__name__}: {exc}")
            return proc.pid, top
    raise TimeoutError(f"Notepad++ MCP instance did not create a window in {wait_timeout}s (pid={proc.pid})")


def _hide_npp(top: int) -> None:
    try:
        bridge.u32.ShowWindow(top, SW_HIDE)
    except Exception as exc:
        _log(f"could not hide Notepad++ hwnd={int(top)}: {type(exc).__name__}: {exc}")


def _window_visible(top: int) -> bool:
    try:
        return bool(bridge.u32.IsWindowVisible(top))
    except Exception:
        return False


def _show_npp(top: int) -> bool:
    try:
        bridge.u32.ShowWindow(top, SW_RESTORE)
        bridge.u32.ShowWindow(top, SW_SHOW)
        return _window_visible(top)
    except Exception as exc:
        _log(f"could not restore Notepad++ hwnd={int(top)}: {type(exc).__name__}: {exc}")
        return False


def _post_close(top: int) -> bool:
    try:
        return bool(bridge.u32.PostMessageW(top, WM_CLOSE, 0, 0))
    except Exception as exc:
        _log(f"could not post WM_CLOSE to Notepad++ hwnd={int(top)}: {type(exc).__name__}: {exc}")
        return False


def _restore_active_buffer(top: int, original: dict[str, Any] | None) -> None:
    if not original:
        return
    try:
        current = _capture_binding(top)
        if int(current["buffer_id"]) == int(original["buffer_id"]):
            return
        bridge.activate_buffer_id(top, int(original["buffer_id"]), wait_timeout=0.5)
    except Exception as exc:
        _log(
            f"could not restore BufferID {original.get('buffer_id')} on hwnd={int(top)}: "
            f"{type(exc).__name__}: {exc}"
        )


def _cleanup_startup_placeholder(state: dict[str, Any], top: int) -> dict[str, Any]:
    """Close only the recorded, unnamed, zero-byte startup buffer."""
    result: dict[str, Any] = {"status": "not_recorded", "changed": False}
    if not _state_is_headless(state):
        result["status"] = "not_headless"
        return result
    startup_buffer_id = int(state.get("startup_buffer_id") or 0)
    if not startup_buffer_id:
        return result

    result["startup_buffer_id"] = startup_buffer_id
    original: dict[str, Any] | None = None
    cleanup_complete = False
    try:
        original = _capture_binding(top)
        binding = bridge.activate_buffer_id(top, startup_buffer_id, wait_timeout=0.5)
        if binding is None:
            result["status"] = "already_absent"
            return result
        binding = _capture_binding(
            top,
            expected_path=str(binding.get("path") or ""),
            expected_scin=int(binding["scintilla_hwnd"]),
            expected_buffer_id=startup_buffer_id,
        )
        result.update(_binding_result(binding))
        path = str(binding.get("path") or "")
        if _is_real_file_path(path):
            result["status"] = "protected_real_file"
            return result

        scin = int(binding["scintilla_hwnd"])
        length = bridge.get_document_length(scin)
        binding = _capture_binding(
            top,
            expected_path=path,
            expected_scin=scin,
            expected_buffer_id=startup_buffer_id,
        )
        result["bytes"] = length
        result["modified"] = _dirty(scin)
        if length != 0:
            result["status"] = "protected_nonempty"
            return result

        if result["modified"]:
            if not bridge.set_document_save_point(scin):
                result["status"] = "savepoint_failed"
                return result
            binding = _capture_binding(
                top,
                expected_path=path,
                expected_scin=scin,
                expected_buffer_id=startup_buffer_id,
            )
            if bridge.get_document_length(scin) != 0 or _dirty(scin):
                result["status"] = "post_savepoint_validation_failed"
                return result
            result["savepoint_cleared"] = True

        if not _close_current_tab_if_clean(
            top,
            scin,
            expected_path=path,
            expected_buffer_id=startup_buffer_id,
        ):
            retained = _capture_binding(
                top,
                expected_path=path,
                expected_scin=scin,
                expected_buffer_id=startup_buffer_id,
            )
            retained_scin = int(retained["scintilla_hwnd"])
            retained_length = bridge.get_document_length(retained_scin)
            retained_modified = _dirty(retained_scin)
            _capture_binding(
                top,
                expected_path=path,
                expected_scin=retained_scin,
                expected_buffer_id=startup_buffer_id,
            )
            result["bytes"] = retained_length
            result["modified"] = retained_modified
            if retained_length == 0 and not retained_modified:
                result["status"] = "retained_empty_startup_buffer"
                return result
            result["status"] = "close_refused"
            return result
        result["status"] = "closed_empty_startup_buffer"
        result["changed"] = True
        cleanup_complete = True
        return result
    except Exception as exc:
        result["status"] = "inspection_failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        if original and int(original.get("buffer_id") or 0) != startup_buffer_id:
            _restore_active_buffer(top, original)
        if cleanup_complete:
            _log(f"closed empty startup BufferID {startup_buffer_id} on hwnd={int(top)}")


def _release_managed_npp(
    reason: str,
    *,
    wait_timeout: float = 2.0,
    close_clean: bool = True,
    preserve_untracked: bool = True,
) -> dict[str, Any]:
    """Release the managed instance without violating its visibility policy."""
    state = _read_state()
    if not state:
        _save_open_files({})
        return {"status": "no_state", "safe_to_forget": True}

    pid = int(state.get("pid") or 0)
    headless = _state_is_headless(state)
    if not bridge._is_pid_alive(pid):
        _clear_state()
        _save_open_files({})
        return {
            "status": "already_exited",
            "pid": pid,
            "headless": headless,
            "safe_to_forget": True,
        }

    pairs = _managed_pairs(pid, include_hidden=True)
    if not pairs:
        return {
            "status": "live_process_without_window",
            "pid": pid,
            "headless": headless,
            "safe_to_forget": False,
            "reason": reason,
        }

    placeholder_actions: list[dict[str, Any]] = []
    if headless:
        for top, _ in pairs:
            try:
                state = _record_startup_placeholder_if_safe(top) or state
            except Exception as exc:
                _log(f"could not migrate startup buffer state for pid={pid}: {type(exc).__name__}: {exc}")
            placeholder_actions.append(_cleanup_startup_placeholder(state, top))
        pairs = _managed_pairs(pid, include_hidden=True)

    tracked = _load_open_files()
    dirty_paths: list[str] = []
    untracked_paths: list[str] = []
    protected_startup_buffers: list[str] = []
    window_states: list[dict[str, Any]] = []
    unsafe_placeholder_statuses = {
        "protected_real_file",
        "protected_nonempty",
        "savepoint_failed",
        "post_savepoint_validation_failed",
        "close_refused",
        "inspection_failed",
    }
    for action in placeholder_actions:
        if action.get("status") in unsafe_placeholder_statuses:
            protected_startup_buffers.append(
                str(action.get("path") or f"<startup BufferID {action.get('startup_buffer_id')}>")
            )
    uncertain = any(action.get("status") == "inspection_failed" for action in placeholder_actions)
    for top, _ in pairs:
        try:
            binding = _capture_binding(top)
            scin = int(binding["scintilla_hwnd"])
            current = str(binding.get("path") or "")
            modified = _dirty(scin)
            title_dirty = _title_dirty(top)
            window_states.append({
                **_binding_result(binding),
                "modified": modified,
                "title_dirty": title_dirty,
            })
            if modified:
                dirty_paths.append(current or "<unknown>")
            if _is_real_file_path(current) and _norm(current) not in tracked:
                untracked_paths.append(current)
        except Exception as exc:
            uncertain = True
            _log(f"could not inspect Notepad++ hwnd={int(top)} during release: {type(exc).__name__}: {exc}")

    for entry in tracked.values():
        if entry.get("dirty"):
            path = str(entry.get("path") or "<unknown>")
            dirty_paths.append(path)

    dirty_paths = list(dict.fromkeys(dirty_paths))
    untracked_paths = list(dict.fromkeys(untracked_paths))
    dirty = bool(dirty_paths)
    must_retain = (
        dirty
        or uncertain
        or bool(protected_startup_buffers)
        or (preserve_untracked and bool(untracked_paths))
        or not close_clean
    )
    close_posted: list[bool] = []
    if not must_retain:
        close_posted = [_post_close(top) for top, _ in pairs]
        deadline = time.time() + max(0.0, wait_timeout)
        while time.time() < deadline and bridge._is_pid_alive(pid):
            time.sleep(0.1)
        if not bridge._is_pid_alive(pid):
            _clear_state()
            _save_open_files({})
            return {
                "status": "closed",
                "pid": pid,
                "headless": headless,
                "dirty": dirty,
                "dirty_paths": dirty_paths,
                "uncertain": uncertain,
                "untracked_paths": untracked_paths,
                "window_states": window_states,
                "placeholder_actions": placeholder_actions,
                "protected_startup_buffers": protected_startup_buffers,
                "close_posted": close_posted,
                "safe_to_forget": True,
                "reason": reason,
            }
        must_retain = True

    live_pairs = _managed_pairs(pid, include_hidden=True)
    if headless:
        for top, _ in live_pairs:
            _hide_npp(top)
        return {
            "status": "quarantined_hidden",
            "pid": pid,
            "headless": True,
            "dirty": dirty,
            "dirty_paths": dirty_paths,
            "uncertain": uncertain,
            "untracked_paths": untracked_paths,
            "window_states": window_states,
            "placeholder_actions": placeholder_actions,
            "protected_startup_buffers": protected_startup_buffers,
            "close_posted": close_posted,
            "close_timeout": bool(close_posted),
            "safe_to_forget": False,
            "reason": reason,
        }

    visible = [_show_npp(top) for top, _ in live_pairs]
    if visible and all(visible):
        _clear_state()
        _save_open_files({})
        return {
            "status": "restored_visible",
            "pid": pid,
            "headless": False,
            "dirty": dirty,
            "dirty_paths": dirty_paths,
            "uncertain": uncertain,
            "untracked_paths": untracked_paths,
            "window_states": window_states,
            "placeholder_actions": placeholder_actions,
            "protected_startup_buffers": protected_startup_buffers,
            "close_posted": close_posted,
            "safe_to_forget": True,
            "reason": reason,
        }

    return {
        "status": "release_failed_state_retained",
        "pid": pid,
        "headless": False,
        "dirty": dirty,
        "dirty_paths": dirty_paths,
        "uncertain": uncertain,
        "untracked_paths": untracked_paths,
        "window_states": window_states,
        "placeholder_actions": placeholder_actions,
        "protected_startup_buffers": protected_startup_buffers,
        "close_posted": close_posted,
        "visible": visible,
        "safe_to_forget": False,
        "reason": reason,
    }


def _close_managed_npp_if_idle(top: int | None = None, wait_timeout: float = 2.0) -> dict[str, Any]:
    return _release_managed_npp(
        "managed operation completed",
        wait_timeout=wait_timeout,
        close_clean=True,
        preserve_untracked=False,
    )


def _activate(path: str, wait_timeout: float = 15.0) -> tuple[int, int, str]:
    path_abs = os.path.abspath(path)
    if not os.path.exists(path_abs):
        raise FileNotFoundError(path_abs)
    expected = _norm(path_abs)
    pid, top = _ensure_npp(wait_timeout)

    last_path = ""

    def active_target() -> tuple[int, int] | None:
        nonlocal last_path
        seen: set[int] = set()
        for top_hwnd, _ in _managed_pairs(pid, include_hidden=True):
            if int(top_hwnd) in seen:
                continue
            seen.add(int(top_hwnd))
            try:
                binding = _capture_binding(top_hwnd)
            except Exception:
                continue
            current = str(binding.get("path") or "")
            last_path = current or last_path
            if current and _norm(current) == expected:
                _hide_npp(top_hwnd)
                return top_hwnd, int(binding["scintilla_hwnd"])
        return None

    active = active_target()
    if active:
        return active[0], active[1], path_abs

    # Switch to an existing tab before opening. NPPM_DOOPEN on an already-open
    # dirty path can create a second clean tab and leave the dirty tab behind.
    for top_hwnd, _ in _managed_pairs(pid, include_hidden=True):
        if bridge.switch_to_file_by_path(top_hwnd, path_abs):
            time.sleep(0.1)
            active = active_target()
            if active:
                return active[0], active[1], path_abs

    bridge._npp_doopen(top, path_abs)
    deadline = time.time() + wait_timeout
    while time.time() < deadline:
        time.sleep(0.1)
        for top_hwnd, _ in _managed_pairs(pid, include_hidden=True):
            bridge.switch_to_file_by_path(top_hwnd, path_abs)
        active = active_target()
        if active:
            return active[0], active[1], path_abs
    raise TimeoutError(f"Notepad++ did not activate target file. expected={path_abs!r} current={last_path!r}")


def _title_dirty(top: int) -> bool:
    return bridge._get_title(top).lstrip().startswith("*")


def _dirty(scin: int) -> bool:
    return bool(bridge.is_document_modified(scin))


def _dirty_diagnostics(
    top: int,
    scin: int,
    *,
    expected_path: str | None = None,
    expected_buffer_id: int | None = None,
) -> dict[str, Any]:
    if expected_path is None and expected_buffer_id is None:
        return {
            "modified": _dirty(scin),
            "title_dirty": _title_dirty(top),
        }
    binding = _capture_binding(
        top,
        expected_path=expected_path,
        expected_scin=scin,
        expected_buffer_id=expected_buffer_id,
    )
    modified = _dirty(int(binding["scintilla_hwnd"]))
    title_dirty = _title_dirty(top)
    binding = _capture_binding(
        top,
        expected_path=expected_path,
        expected_scin=scin,
        expected_buffer_id=int(binding["buffer_id"]),
    )
    return {
        "modified": modified,
        "title_dirty": title_dirty,
        "binding": binding,
    }


def _reload_current(top: int, path: str, expected_buffer_id: int | None = None) -> int:
    ok, current = bridge.ensure_current_path(top, path)
    if not ok:
        raise RuntimeError(f"cannot reload a non-current path: expected={path!r} current={current!r}")
    buffer_id = bridge.get_current_buffer_id(top)
    if expected_buffer_id is not None and buffer_id != int(expected_buffer_id):
        raise RuntimeError(
            f"current BufferID changed before reload: expected={int(expected_buffer_id)} actual={buffer_id}"
        )
    ret = int(bridge.u32.SendMessageW(top, NPPM_RELOADBUFFERID, buffer_id, 0))
    time.sleep(0.25)
    return ret


def _record(path: str, top: int, scin: int, meta: dict[str, Any]) -> None:
    files = _load_open_files()
    entry = {
        "path": os.path.abspath(path),
        "pid": bridge.window_process_id(top),
        "top_hwnd": int(top),
        "scintilla_hwnd": int(scin),
        "dirty": meta.get("dirty", False),
        "buffer_modified": meta.get("buffer_modified", meta.get("dirty", False)),
        "title_dirty": meta.get("title_dirty", False),
        "source": meta.get("source", _stat(path)),
        "last_seen": time.time(),
    }
    binding = meta.get("binding")
    if binding:
        entry.update({name: binding.get(name) for name in BINDING_FIELDS})
    for name in (
        "operation",
        "dirty_origin",
        "reload_allowed",
        "mutation_started",
        "quarantine_reason",
    ):
        if name in meta:
            entry[name] = meta[name]
    files[_norm(path)] = entry
    _save_open_files(files)


def _record_hidden_quarantine(
    path: str,
    top: int,
    scin: int,
    *,
    operation: str,
    dirty_origin: str,
    reload_allowed: bool,
    mutation_started: bool,
    reason: str,
) -> dict[str, Any]:
    try:
        diagnostics = _dirty_diagnostics(top, scin, expected_path=path)
    except Exception as exc:
        diagnostics = {"modified": True, "title_dirty": False}
        reason = f"{reason}; dirty inspection failed: {type(exc).__name__}: {exc}"
    try:
        source = _stat(path)
    except OSError as exc:
        source = {"stat_error": f"{type(exc).__name__}: {exc}"}
    _record(path, top, scin, {
        "dirty": True,
        "buffer_modified": diagnostics["modified"],
        "title_dirty": diagnostics["title_dirty"],
        "source": source,
        "operation": operation,
        "dirty_origin": dirty_origin,
        "reload_allowed": reload_allowed,
        "mutation_started": mutation_started,
        "quarantine_reason": reason,
        "binding": diagnostics.get("binding"),
    })
    _hide_npp(top)
    return _release_managed_npp(
        reason,
        wait_timeout=0.0,
        close_clean=True,
        preserve_untracked=False,
    )


def _reload_and_validate(
    path: str,
    top: int,
    scin: int,
    source_before: dict[str, Any],
    binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    binding = binding or _capture_binding(top, expected_path=path, expected_scin=scin)
    binding = _capture_binding(
        top,
        expected_path=path,
        expected_scin=scin,
        expected_buffer_id=int(binding["buffer_id"]),
    )
    if not _reload_current(top, path, expected_buffer_id=int(binding["buffer_id"])):
        raise RuntimeError("Notepad++ rejected NPPM_RELOADBUFFERID")
    binding = _capture_binding(
        top,
        expected_path=path,
        expected_scin=scin,
        expected_buffer_id=int(binding["buffer_id"]),
    )
    source_after = _stat(path)
    time.sleep(0.05)
    source_stable = _stat(path)
    if (
        source_before["mtime_ns"] != source_after["mtime_ns"]
        or source_before["size"] != source_after["size"]
        or source_after["mtime_ns"] != source_stable["mtime_ns"]
        or source_after["size"] != source_stable["size"]
    ):
        raise RuntimeError("source changed while the managed buffer was being reloaded")
    diagnostics = _dirty_diagnostics(
        top,
        scin,
        expected_path=path,
        expected_buffer_id=int(binding["buffer_id"]),
    )
    if diagnostics["modified"]:
        raise RuntimeError("Scintilla remained modified after NPPM_RELOADBUFFERID")
    return source_stable


def _prepare_document(
    path: str,
    top: int,
    scin: int,
    auto_reload: bool,
    *,
    read_only: bool,
    operation: str,
) -> dict[str, Any]:
    key = _norm(path)
    binding = _capture_binding(top, expected_path=path, expected_scin=scin)
    current_stat = _stat(path)
    previous = _load_open_files().get(key)
    external_changed = bool(previous and previous.get("source", {}).get("mtime_ns") != current_stat["mtime_ns"])
    diagnostics = _dirty_diagnostics(
        top,
        scin,
        expected_path=path,
        expected_buffer_id=int(binding["buffer_id"]),
    )
    binding = diagnostics.get("binding", binding)
    dirty = diagnostics["modified"]
    reloaded = False
    dirty_recovered = False
    previous_blocks_reload = bool(
        previous
        and previous.get("dirty")
        and (
            previous.get("reload_allowed") is False
            or "reload_allowed" not in previous
        )
    )

    if previous_blocks_reload:
        reason = str(previous.get("quarantine_reason") or "managed buffer has an unresolved quarantine state")
        _record_hidden_quarantine(
            path,
            top,
            scin,
            operation=operation,
            dirty_origin=str(previous.get("dirty_origin") or "unknown"),
            reload_allowed=False,
            mutation_started=bool(previous.get("mutation_started", False)),
            reason=reason,
        )
        raise RuntimeError(reason)

    if dirty:
        prior_write_quarantine = bool(
            previous
            and previous.get("reload_allowed") is False
            and previous.get("dirty_origin") in {"write", "unknown"}
        )
        if read_only and auto_reload and not prior_write_quarantine:
            try:
                current_stat = _reload_and_validate(path, top, scin, current_stat, binding)
            except Exception as exc:
                reason = f"read-only dirty recovery failed: {type(exc).__name__}: {exc}"
                _record_hidden_quarantine(
                    path,
                    top,
                    scin,
                    operation=operation,
                    dirty_origin="read_only",
                    reload_allowed=True,
                    mutation_started=False,
                    reason=reason,
                )
                raise RuntimeError(reason) from exc
            reloaded = True
            dirty_recovered = True
            dirty = False
            binding = _capture_binding(
                top,
                expected_path=path,
                expected_scin=scin,
                expected_buffer_id=int(binding["buffer_id"]),
            )
            diagnostics = _dirty_diagnostics(
                top,
                scin,
                expected_path=path,
                expected_buffer_id=int(binding["buffer_id"]),
            )
            binding = diagnostics.get("binding", binding)
            _forget(path)
        else:
            reason = (
                "managed buffer is quarantined after an incomplete write"
                if prior_write_quarantine
                else "managed buffer is dirty and automatic reload is disabled"
            )
            _record_hidden_quarantine(
                path,
                top,
                scin,
                operation=operation,
                dirty_origin=str(previous.get("dirty_origin") if previous else "unknown"),
                reload_allowed=bool(previous.get("reload_allowed", False)) if previous else False,
                mutation_started=bool(previous.get("mutation_started", False)) if previous else False,
                reason=reason,
            )
            raise RuntimeError(reason)
    elif external_changed and auto_reload:
        try:
            current_stat = _reload_and_validate(path, top, scin, current_stat, binding)
        except Exception as exc:
            reason = f"external-change reload failed: {type(exc).__name__}: {exc}"
            _record_hidden_quarantine(
                path,
                top,
                scin,
                operation=operation,
                dirty_origin="unknown",
                reload_allowed=False,
                mutation_started=False,
                reason=reason,
            )
            raise RuntimeError(reason) from exc
        reloaded = True
        binding = _capture_binding(
            top,
            expected_path=path,
            expected_scin=scin,
            expected_buffer_id=int(binding["buffer_id"]),
        )
        diagnostics = _dirty_diagnostics(
            top,
            scin,
            expected_path=path,
            expected_buffer_id=int(binding["buffer_id"]),
        )
        binding = diagnostics.get("binding", binding)

    return {
        "dirty": dirty,
        "title_dirty": diagnostics["title_dirty"],
        "external_changed": external_changed,
        "auto_reloaded": reloaded,
        "dirty_recovered": dirty_recovered,
        "operation": operation,
        "dirty_origin": "read_only" if read_only else "write",
        "reload_allowed": read_only,
        "mutation_started": False,
        "source": current_stat,
        "binding": binding,
    }


def _read_buffer_snapshot(
    scin: int,
    *,
    top: int | None = None,
    expected_path: str | None = None,
    expected_buffer_id: int | None = None,
) -> dict[str, Any]:
    binding = None
    if top is not None:
        binding = _capture_binding(
            top,
            expected_path=expected_path,
            expected_scin=scin,
            expected_buffer_id=expected_buffer_id,
        )
        scin = int(binding["scintilla_hwnd"])
    data = bridge.read_document_bytes(scin)
    code_page = bridge.get_code_page(scin)
    text, encoding, had_decode_errors = bridge.decode_document_bytes(data, code_page)
    if top is not None:
        binding = _capture_binding(
            top,
            expected_path=expected_path,
            expected_scin=scin,
            expected_buffer_id=int(binding["buffer_id"]),
        )
    result = {
        "data": data,
        "text": text,
        "code_page": code_page,
        "encoding": encoding,
        "had_decode_errors": had_decode_errors,
        "byte_info": bridge.inspect_document_bytes(data),
        "sha256": _sha256_hex(data),
    }
    if binding:
        result["binding"] = binding
    return result


def _finish_read_tab(path_abs: str, top: int, scin: int, meta: dict[str, Any]) -> dict[str, Any]:
    meta["source"] = _stat(path_abs)
    binding = meta.get("binding")
    if binding:
        diagnostics = _dirty_diagnostics(
            top,
            scin,
            expected_path=path_abs,
            expected_buffer_id=int(binding["buffer_id"]),
        )
        binding = diagnostics.get("binding", binding)
        meta["binding"] = binding
    else:
        diagnostics = _dirty_diagnostics(top, scin)
    meta["dirty"] = diagnostics["modified"]
    meta["buffer_modified"] = diagnostics["modified"]
    meta["title_dirty"] = diagnostics["title_dirty"]
    tab_closed = _close_current_tab_if_clean(
        top,
        scin,
        expected_path=path_abs if binding else None,
        expected_buffer_id=int(binding["buffer_id"]) if binding else None,
    )
    if tab_closed:
        _forget(path_abs)
        lifecycle = _close_managed_npp_if_idle(top)
    else:
        _record(path_abs, top, scin, meta)
        _hide_npp(top)
        lifecycle = _release_managed_npp(
            f"{meta.get('operation', 'read-only operation')} left a dirty tab",
            wait_timeout=0.0,
            close_clean=True,
            preserve_untracked=False,
        )
    return {
        "tab_closed": tab_closed,
        "lifecycle": lifecycle,
        **_binding_result(binding),
    }


def _persist_and_verify(
    path_abs: str,
    top: int,
    scin: int,
    data: bytes,
    before: dict[str, Any],
    *,
    wait_timeout: float,
    code_page: int,
    byte_info: dict[str, Any],
) -> dict[str, Any]:
    """Save a validated buffer, reopen it through Notepad++, and compare bytes."""
    binding = _capture_binding(top, expected_path=path_abs, expected_scin=scin)
    buffer_id = int(binding["buffer_id"])
    binding = _capture_binding(
        top,
        expected_path=path_abs,
        expected_scin=scin,
        expected_buffer_id=buffer_id,
    )
    written = bridge.write_document_bytes(scin, data)
    binding = _capture_binding(
        top,
        expected_path=path_abs,
        expected_scin=scin,
        expected_buffer_id=buffer_id,
    )
    buffer_verified = bridge.read_document_bytes(scin) == data
    binding = _capture_binding(
        top,
        expected_path=path_abs,
        expected_scin=scin,
        expected_buffer_id=buffer_id,
    )
    if not buffer_verified:
        raise RuntimeError(
            f"Scintilla buffer verification failed after writing {written} bytes"
        )

    pre_save = _stat(path_abs)
    if pre_save["mtime_ns"] != before["mtime_ns"] or pre_save["size"] != before["size"]:
        raise RuntimeError(
            "source changed after Scintilla buffer preparation and before save; "
            "refusing to overwrite the external change"
        )
    binding = _capture_binding(
        top,
        expected_path=path_abs,
        expected_scin=scin,
        expected_buffer_id=buffer_id,
    )

    save_ok = False
    save_error = None
    try:
        save_ok = bool(bridge.save_current(top, scin=scin))
    except Exception as exc:
        save_error = f"{type(exc).__name__}: {exc}"
    time.sleep(0.2)
    after = _stat(path_abs)
    diagnostics = _dirty_diagnostics(
        top,
        scin,
        expected_path=path_abs,
        expected_buffer_id=buffer_id,
    )
    binding = diagnostics.get("binding", binding)
    buffer_clean = not diagnostics["modified"]
    title_clean = not diagnostics["title_dirty"]
    disk_changed = (after["mtime_ns"] != before["mtime_ns"]) or (after["size"] != before["size"])
    save_confirmed = bool(save_ok and buffer_clean and disk_changed)
    result: dict[str, Any] = {
        "ok": False,
        "path": path_abs,
        "tab_closed": False,
        "written_bytes": written,
        "buffer_verified": buffer_verified,
        "code_page": code_page,
        "encoding": bridge.code_page_to_encoding(code_page),
        "bom": byte_info["bom"],
        "newline": byte_info["newline"],
        "newline_counts": byte_info["newline_counts"],
        "save_command_ok": save_ok,
        "save_error": save_error,
        "buffer_clean": buffer_clean,
        "title_clean": title_clean,
        "disk_changed": disk_changed,
        "save_confirmed": save_confirmed,
        "before": before,
        "after": after,
        "content_sha256": _sha256_hex(data),
        "readback_exact": False,
        "source_stable": False,
        "verification_error": None,
        **_binding_result(binding),
    }

    if not save_confirmed:
        reason = "save was not fully confirmed after the Scintilla buffer was changed"
        result["lifecycle"] = _record_hidden_quarantine(
            path_abs,
            top,
            scin,
            operation="write_save",
            dirty_origin="write",
            reload_allowed=False,
            mutation_started=True,
            reason=reason,
        )
        result["warning"] = "Save was not fully confirmed; the managed buffer remains quarantined."
        return result

    if not _close_current_tab_if_clean(
        top,
        scin,
        expected_path=path_abs,
        expected_buffer_id=buffer_id,
    ):
        result["verification_error"] = "saved buffer could not be closed cleanly for readback"
        result["lifecycle"] = _record_hidden_quarantine(
            path_abs,
            top,
            scin,
            operation="write_close_for_verify",
            dirty_origin="write",
            reload_allowed=False,
            mutation_started=True,
            reason=result["verification_error"],
        )
        return result
    _forget(path_abs)

    verify_top = None
    verify_scin = None
    try:
        verify_top, verify_scin, _ = _activate(path_abs, wait_timeout)
        verify_binding = _capture_binding(
            verify_top,
            expected_path=path_abs,
            expected_scin=verify_scin,
        )
        verify_snapshot = _read_buffer_snapshot(
            verify_scin,
            top=verify_top,
            expected_path=path_abs,
            expected_buffer_id=int(verify_binding["buffer_id"]),
        )
        verify_binding = verify_snapshot["binding"]
        verify_source = _stat(path_abs)
        time.sleep(0.2)
        stable_source = _stat(path_abs)
        result["readback_exact"] = verify_snapshot["data"] == data
        result["source_stable"] = (
            verify_source["mtime_ns"] == stable_source["mtime_ns"]
            and verify_source["size"] == stable_source["size"]
        )
        result["readback_source"] = verify_source
        result["readback_sha256"] = verify_snapshot["sha256"]
        if not result["readback_exact"]:
            result["verification_error"] = "reopened DGS buffer does not match the bytes that were saved"
        elif not result["source_stable"]:
            result["verification_error"] = "source mtime changed during post-save verification"

        result["ok"] = bool(
            result["save_confirmed"]
            and result["readback_exact"]
            and result["source_stable"]
        )
        verify_meta = {
            "dirty": _dirty(verify_scin),
            "title_dirty": _title_dirty(verify_top),
            "external_changed": False,
            "auto_reloaded": False,
            "operation": "write_verify",
            "dirty_origin": "write",
            "reload_allowed": False,
            "mutation_started": True,
            "source": stable_source,
            "binding": verify_binding,
        }
        if result["ok"]:
            result.update(_finish_read_tab(path_abs, verify_top, verify_scin, verify_meta))
        else:
            result["lifecycle"] = _record_hidden_quarantine(
                path_abs,
                verify_top,
                verify_scin,
                operation="write_verify",
                dirty_origin="write",
                reload_allowed=False,
                mutation_started=True,
                reason=str(result["verification_error"]),
            )
    except Exception as exc:
        result["verification_error"] = f"{type(exc).__name__}: {exc}"
        result["ok"] = False
        if verify_top and verify_scin:
            result["lifecycle"] = _record_hidden_quarantine(
                path_abs,
                verify_top,
                verify_scin,
                operation="write_verify",
                dirty_origin="write",
                reload_allowed=False,
                mutation_started=True,
                reason=result["verification_error"],
            )
    return result


def _forget(path: str) -> None:
    files = _load_open_files()
    files.pop(_norm(path), None)
    _save_open_files(files)


def _close_current_tab_if_clean(
    top: int,
    scin: int,
    *,
    expected_path: str | None = None,
    expected_buffer_id: int | None = None,
    close_timeout: float = 1.0,
) -> bool:
    binding = _capture_binding(
        top,
        expected_path=expected_path,
        expected_scin=scin,
        expected_buffer_id=expected_buffer_id,
    )
    scin = int(binding["scintilla_hwnd"])
    buffer_id = int(binding["buffer_id"])
    pid = int(binding["pid"])
    if _dirty(scin):
        return False
    _capture_binding(
        top,
        expected_path=expected_path,
        expected_scin=scin,
        expected_buffer_id=buffer_id,
    )
    WM_COMMAND = 0x0111
    IDM_FILE_CLOSE = 41003
    bridge.u32.SendMessageW(top, WM_COMMAND, IDM_FILE_CLOSE, 0)

    deadline = time.monotonic() + max(0.0, float(close_timeout))
    while True:
        if not bridge.u32.IsWindow(top) or bridge.window_process_id(top) != pid:
            return True
        try:
            if bridge.get_buffer_position(top, buffer_id) is None:
                post_close = _capture_binding(top)
                if int(post_close["buffer_id"]) != buffer_id:
                    return True
        except Exception:
            if not bridge.u32.IsWindow(top) or not bridge._is_pid_alive(pid):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _reset_npp_state() -> None:
    result = _release_managed_npp(
        "recovering after Notepad++ communication failure",
        wait_timeout=1.0,
        close_clean=True,
        preserve_untracked=True,
    )
    if not result["safe_to_forget"]:
        raise RuntimeError(f"could not safely reset managed Notepad++ state: {result}")


def _is_npp_loss_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    markers = [
        "Notepad++ did not activate target file",
        "did not create a window",
        "SendMessageTimeoutW",
        "OpenProcess",
        "active Notepad++ tab mismatch",
    ]
    return any(marker in text for marker in markers)


def _with_npp_recovery(action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return action()
    except Exception as exc:
        if not _is_npp_loss_error(exc):
            raise
        _log(f"recovering MCP Notepad++ after {type(exc).__name__}: {exc}")
        _reset_npp_state()
        return action()


def dgs_open_file(args: dict[str, Any]) -> dict[str, Any]:
    path = args["path"]
    wait_timeout = float(args.get("wait_timeout", 15.0))
    auto_reload = bool(args.get("auto_reload", True))
    def action() -> dict[str, Any]:
        top, scin, path_abs = _activate(path, wait_timeout)
        meta = _prepare_document(
            path_abs,
            top,
            scin,
            auto_reload,
            read_only=True,
            operation="dgs_open_file",
        )
        ok, current = bridge.ensure_current_path(top, path_abs)
        if not ok:
            raise RuntimeError(f"active Notepad++ tab mismatch: expected={path_abs!r} current={current!r}")
        meta["source"] = _stat(path_abs)
        binding = _capture_binding(
            top,
            expected_path=path_abs,
            expected_scin=scin,
            expected_buffer_id=int(meta["binding"]["buffer_id"]),
        )
        code_page = bridge.get_code_page(int(binding["scintilla_hwnd"]))
        meta["binding"] = _capture_binding(
            top,
            expected_path=path_abs,
            expected_scin=scin,
            expected_buffer_id=int(binding["buffer_id"]),
        )
        result = {
            "ok": True,
            "path": path_abs,
            "dirty": meta["dirty"],
            "title_dirty": meta["title_dirty"],
            "external_changed": meta["external_changed"],
            "auto_reloaded": meta["auto_reloaded"],
            "dirty_recovered": meta["dirty_recovered"],
            "source": meta["source"],
            "code_page": code_page,
            "encoding": bridge.code_page_to_encoding(code_page),
        }
        result.update(_finish_read_tab(path_abs, top, scin, meta))
        return result
    with LOCK:
        return _with_npp_recovery(action)


def dgs_read_file(args: dict[str, Any]) -> dict[str, Any]:
    path = args["path"]
    wait_timeout = float(args.get("wait_timeout", 15.0))
    auto_reload = bool(args.get("auto_reload", True))
    include_base64 = bool(args.get("include_base64", False))
    max_text_chars = int(args.get("max_text_chars", 200000))
    def action() -> dict[str, Any]:
        top, scin, path_abs = _activate(path, wait_timeout)
        meta = _prepare_document(
            path_abs,
            top,
            scin,
            auto_reload,
            read_only=True,
            operation="dgs_read_file",
        )
        snapshot = _read_buffer_snapshot(
            scin,
            top=top,
            expected_path=path_abs,
            expected_buffer_id=int(meta["binding"]["buffer_id"]),
        )
        meta["binding"] = snapshot["binding"]
        data = snapshot["data"]
        text = snapshot["text"]
        code_page = snapshot["code_page"]
        encoding = snapshot["encoding"]
        had_decode_errors = snapshot["had_decode_errors"]
        byte_info = snapshot["byte_info"]
        truncated = max_text_chars >= 0 and len(text) > max_text_chars
        shown_text = text[:max_text_chars] if truncated else text
        result = {
            "ok": True,
            "path": path_abs,
            "bytes": len(data),
            "chars": len(text),
            "text": shown_text,
            "truncated": truncated,
            "code_page": code_page,
            "encoding": encoding,
            "had_decode_errors": had_decode_errors,
            "bom": byte_info["bom"],
            "newline": byte_info["newline"],
            "newline_counts": byte_info["newline_counts"],
            "dirty": meta["dirty"],
            "external_changed": meta["external_changed"],
            "auto_reloaded": meta["auto_reloaded"],
            "source": meta["source"],
            "unexpected_binary_profile": bridge.has_unexpected_binary_profile(data),
            "content_sha256": snapshot["sha256"],
            "title_dirty": meta["title_dirty"],
            "dirty_recovered": meta["dirty_recovered"],
        }
        if include_base64:
            result["bytes_base64"] = base64.b64encode(data).decode("ascii")
        result.update(_finish_read_tab(path_abs, top, scin, meta))
        result["source"] = meta["source"]
        return result
    with LOCK:
        return _with_npp_recovery(action)


def dgs_write_file(args: dict[str, Any]) -> dict[str, Any]:
    path = args["path"]
    wait_timeout = float(args.get("wait_timeout", 15.0))
    allow_stale = bool(args.get("allow_stale", False))
    expected_mtime_ns = args.get("expected_mtime_ns")
    expected_sha256 = args.get("expected_sha256")
    text = args.get("text")
    bytes_base64 = args.get("bytes_base64")
    if (text is None) == (bytes_base64 is None):
        raise ValueError("provide exactly one of text or bytes_base64")

    def action() -> dict[str, Any]:
        top, scin, path_abs = _activate(path, wait_timeout)
        meta = _prepare_document(
            path_abs,
            top,
            scin,
            auto_reload=True,
            read_only=False,
            operation="dgs_write_file",
        )
        if meta["dirty"]:
            raise RuntimeError("managed Notepad++ buffer is dirty; inspect or reload it before a full replacement")
        before = _stat(path_abs)
        if expected_mtime_ns is not None and not _mtime_matches(expected_mtime_ns, before["mtime_ns"]) and not allow_stale:
            raise RuntimeError(
                "source changed before write: "
                f"expected_mtime_ns={expected_mtime_ns}, current_mtime_ns={before['mtime_ns_exact']}"
            )

        original = _read_buffer_snapshot(
            scin,
            top=top,
            expected_path=path_abs,
            expected_buffer_id=int(meta["binding"]["buffer_id"]),
        )
        meta["binding"] = original["binding"]
        if expected_sha256 is not None and str(expected_sha256).lower() != original["sha256"]:
            raise RuntimeError(
                "document content changed before write: "
                f"expected_sha256={expected_sha256}, current_sha256={original['sha256']}"
            )

        code_page = original["code_page"]
        if bytes_base64 is not None:
            data = base64.b64decode(bytes_base64, validate=True)
        else:
            data = str(text).encode(bridge.code_page_to_encoding(code_page))

        byte_info = bridge.inspect_document_bytes(data)
        if data == original["data"]:
            result = {
                "ok": True,
                "path": path_abs,
                "changed": False,
                "written_bytes": 0,
                "buffer_verified": True,
                "save_confirmed": True,
                "readback_exact": True,
                "source_stable": True,
                "before": before,
                "after": before,
                "before_content_sha256": original["sha256"],
                "after_content_sha256": original["sha256"],
                "warning": None,
            }
            result.update(_finish_read_tab(path_abs, top, scin, meta))
            return result

        prewrite = _stat(path_abs)
        if prewrite["mtime_ns"] != before["mtime_ns"] or prewrite["size"] != before["size"]:
            raise RuntimeError("source changed during write preparation; refusing full replacement")
        current = _read_buffer_snapshot(
            scin,
            top=top,
            expected_path=path_abs,
            expected_buffer_id=int(meta["binding"]["buffer_id"]),
        )
        meta["binding"] = current["binding"]
        if current["sha256"] != original["sha256"]:
            raise RuntimeError("Scintilla buffer changed during write preparation; refusing full replacement")

        try:
            result = _persist_and_verify(
                path_abs,
                top,
                scin,
                data,
                before,
                wait_timeout=wait_timeout,
                code_page=code_page,
                byte_info=byte_info,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            lifecycle = _record_hidden_quarantine(
                path_abs,
                top,
                scin,
                operation="dgs_write_file",
                dirty_origin="write",
                reload_allowed=False,
                mutation_started=True,
                reason=error,
            )
            return {
                "ok": False,
                "error": error,
                "path": path_abs,
                "changed": False,
                "buffer_mutated": True,
                "mutation_started": True,
                "lifecycle": lifecycle,
                **_binding_result(meta.get("binding")),
            }
        result["changed"] = True
        result["before_content_sha256"] = original["sha256"]
        result["after_content_sha256"] = _sha256_hex(data)
        return result
    with LOCK:
        # A mutation that reaches Scintilla is never retried automatically.
        return action()


def dgs_search(args: dict[str, Any]) -> dict[str, Any]:
    path = args["path"]
    query = args["query"]
    wait_timeout = float(args.get("wait_timeout", 15.0))
    auto_reload = bool(args.get("auto_reload", True))

    def action() -> dict[str, Any]:
        top, scin, path_abs = _activate(path, wait_timeout)
        meta = _prepare_document(
            path_abs,
            top,
            scin,
            auto_reload,
            read_only=True,
            operation="dgs_search",
        )
        snapshot = _read_buffer_snapshot(
            scin,
            top=top,
            expected_path=path_abs,
            expected_buffer_id=int(meta["binding"]["buffer_id"]),
        )
        meta["binding"] = snapshot["binding"]
        search = _search_text(
            snapshot["text"],
            query,
            regex=bool(args.get("regex", False)),
            case_sensitive=bool(args.get("case_sensitive", True)),
            context_lines=int(args.get("context_lines", 2)),
            max_matches=int(args.get("max_matches", 50)),
            max_output_chars=int(args.get("max_output_chars", 20_000)),
        )
        result = {
            "ok": True,
            "path": path_abs,
            "query": query,
            "regex": bool(args.get("regex", False)),
            "case_sensitive": bool(args.get("case_sensitive", True)),
            "bytes": len(snapshot["data"]),
            "chars": len(snapshot["text"]),
            "code_page": snapshot["code_page"],
            "encoding": snapshot["encoding"],
            "had_decode_errors": snapshot["had_decode_errors"],
            "content_sha256": snapshot["sha256"],
            "source": meta["source"],
            "dirty": meta["dirty"],
            "external_changed": meta["external_changed"],
            "auto_reloaded": meta["auto_reloaded"],
            "title_dirty": meta["title_dirty"],
            "dirty_recovered": meta["dirty_recovered"],
            **search,
        }
        result.update(_finish_read_tab(path_abs, top, scin, meta))
        result["source"] = meta["source"]
        return result

    with LOCK:
        return _with_npp_recovery(action)


def dgs_apply_patch(args: dict[str, Any]) -> dict[str, Any]:
    path = args["path"]
    old_text = args["old_text"]
    new_text = args["new_text"]
    expected_mtime_ns = args.get("expected_mtime_ns")
    expected_sha256 = args.get("expected_sha256")
    if expected_mtime_ns is None:
        raise ValueError("expected_mtime_ns is required for dgs_apply_patch")
    if expected_sha256 is None:
        raise ValueError("expected_sha256 is required for dgs_apply_patch")
    wait_timeout = float(args.get("wait_timeout", 15.0))
    expected_occurrences = int(args.get("expected_occurrences", 1))
    normalize_newlines = bool(args.get("normalize_newlines", True))
    dry_run = bool(args.get("dry_run", False))

    def action() -> dict[str, Any]:
        top, scin, path_abs = _activate(path, wait_timeout)
        meta = _prepare_document(
            path_abs,
            top,
            scin,
            auto_reload=True,
            read_only=dry_run,
            operation="dgs_apply_patch",
        )
        if meta["dirty"]:
            raise RuntimeError("managed Notepad++ buffer is dirty; inspect or reload it before applying a patch")
        before = _stat(path_abs)
        if not _mtime_matches(expected_mtime_ns, before["mtime_ns"]):
            raise RuntimeError(
                "source changed before patch: "
                f"expected_mtime_ns={expected_mtime_ns}, current_mtime_ns={before['mtime_ns_exact']}"
            )

        original = _read_buffer_snapshot(
            scin,
            top=top,
            expected_path=path_abs,
            expected_buffer_id=int(meta["binding"]["buffer_id"]),
        )
        meta["binding"] = original["binding"]
        if str(expected_sha256).lower() != original["sha256"]:
            raise RuntimeError(
                "document content changed before patch: "
                f"expected_sha256={expected_sha256}, current_sha256={original['sha256']}"
            )
        if original["had_decode_errors"]:
            raise RuntimeError("document decoding produced replacement characters; patch was refused")

        patch = _build_exact_patch(
            original["text"],
            old_text,
            new_text,
            expected_occurrences=expected_occurrences,
            newline_style=original["byte_info"]["newline"],
            normalize_newlines=normalize_newlines,
        )
        new_data = patch["text"].encode(original["encoding"])
        new_info = bridge.inspect_document_bytes(new_data)
        if new_info["bom"] != original["byte_info"]["bom"]:
            raise RuntimeError("patch would change the document BOM")
        if (
            original["byte_info"]["newline"] in {"crlf", "lf", "cr"}
            and new_info["newline"] != original["byte_info"]["newline"]
        ):
            raise RuntimeError("patch would change the document newline style")

        summary = {
            "old_chars": len(patch["old_text"]),
            "new_chars": len(patch["new_text"]),
            "occurrences": patch["occurrences"],
            "before_chars": len(original["text"]),
            "after_chars": len(patch["text"]),
            "before_bytes": len(original["data"]),
            "after_bytes": len(new_data),
            "before_content_sha256": original["sha256"],
            "after_content_sha256": _sha256_hex(new_data),
            "prefix_sha256": patch["prefix_sha256"],
            "suffix_sha256": patch["suffix_sha256"],
            "newlines_normalized": patch["newlines_normalized"],
            "dry_run": dry_run,
        }
        if dry_run:
            summary["ok"] = True
            summary["changed"] = new_data != original["data"]
            summary["path"] = path_abs
            summary["source"] = meta["source"]
            summary.update(_finish_read_tab(path_abs, top, scin, meta))
            summary["source"] = meta["source"]
            return summary

        if new_data == original["data"]:
            summary.update({
                "ok": True,
                "changed": False,
                "path": path_abs,
                "save_confirmed": True,
                "readback_exact": True,
                "source_stable": True,
            })
            summary.update(_finish_read_tab(path_abs, top, scin, meta))
            summary["source"] = meta["source"]
            return summary

        prewrite = _stat(path_abs)
        if prewrite["mtime_ns"] != before["mtime_ns"] or prewrite["size"] != before["size"]:
            raise RuntimeError("source changed during patch preparation; refusing to write")
        current = _read_buffer_snapshot(
            scin,
            top=top,
            expected_path=path_abs,
            expected_buffer_id=int(meta["binding"]["buffer_id"]),
        )
        meta["binding"] = current["binding"]
        if current["sha256"] != original["sha256"]:
            raise RuntimeError("Scintilla buffer changed during patch preparation; refusing to write")

        try:
            persisted = _persist_and_verify(
                path_abs,
                top,
                scin,
                new_data,
                before,
                wait_timeout=wait_timeout,
                code_page=original["code_page"],
                byte_info=new_info,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            summary.update({
                "ok": False,
                "error": error,
                "path": path_abs,
                "changed": False,
                "buffer_mutated": True,
                "mutation_started": True,
                **_binding_result(meta.get("binding")),
                "lifecycle": _record_hidden_quarantine(
                    path_abs,
                    top,
                    scin,
                    operation="dgs_apply_patch",
                    dirty_origin="write",
                    reload_allowed=False,
                    mutation_started=True,
                    reason=error,
                ),
            })
            return summary
        summary.update(persisted)
        summary["changed"] = True
        return summary

    with LOCK:
        # A patch is derived from the current buffer and must never be retried after a timeout.
        return action()


def dgs_list_open_files(args: dict[str, Any]) -> dict[str, Any]:
    with LOCK:
        items = []
        for entry in _load_open_files().values():
            path = entry["path"]
            live = dict(entry)
            pid = int(live.get("pid") or 0)
            live["npp_alive"] = bridge._is_pid_alive(pid)
            try:
                live["current_source"] = _stat(path)
                live["external_changed"] = live["current_source"]["mtime_ns"] != entry.get("source", {}).get("mtime_ns")
            except OSError as exc:
                live["stat_error"] = f"{type(exc).__name__}: {exc}"
            items.append(live)
        state = _read_state()
        ownership = None
        binding = None
        if state:
            pid = int(state.get("pid") or 0)
            alive = bridge._is_pid_alive(pid)
            ownership = {
                "pid": pid,
                "exe": state.get("exe"),
                "headless": _state_is_headless(state),
                "launch_mode": state.get("launch_mode"),
                "window_class": state.get("window_class"),
                "runtime_version": state.get("runtime_version"),
                "alive": alive,
                "started_at": state.get("started_at"),
                "startup_buffer_id": state.get("startup_buffer_id"),
            }
            if alive:
                try:
                    top, _ = _find_managed_top(pid)
                    if top:
                        binding = _capture_binding(top)
                        ownership["binding"] = _binding_result(binding)
                except Exception as exc:
                    ownership["binding_error"] = f"{type(exc).__name__}: {exc}"
        return {
            "ok": True,
            "count": len(items),
            "files": items,
            "ownership": ownership,
            **_binding_result(binding),
        }


def dgs_shutdown(args: dict[str, Any]) -> dict[str, Any]:
    with LOCK:
        result = _release_managed_npp(
            "explicit dgs_shutdown",
            wait_timeout=float(args.get("wait_timeout", 5.0)),
            close_clean=True,
            preserve_untracked=True,
        )
        binding = None
        window_states = result.get("window_states") or []
        if window_states:
            binding = window_states[0]
        elif result.get("placeholder_actions"):
            binding = next(
                (
                    action
                    for action in result["placeholder_actions"]
                    if action.get("active_binding_verified")
                ),
                None,
            )
        if binding is None and result.get("pid"):
            binding = {"pid": result["pid"]}
        return {
            "ok": result["safe_to_forget"],
            "safe_to_stop": result["safe_to_forget"],
            "lifecycle": result,
            **_binding_result(binding),
        }


def _terminate_pid(pid: int) -> bool:
    PROCESS_TERMINATE = 0x0001
    ok = False
    h = bridge.k32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if h:
        try:
            ok = bool(bridge.k32.TerminateProcess(h, 0))
        finally:
            bridge.k32.CloseHandle(h)
    time.sleep(0.1)
    if bridge._is_pid_alive(pid):
        try:
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            _log(f"taskkill fallback failed for pid={int(pid)}: {type(exc).__name__}: {exc}")
    time.sleep(0.1)
    return ok or not bridge._is_pid_alive(pid)


_TOOL_HANDLERS = {
    "dgs_open_file": dgs_open_file,
    "dgs_read_file": dgs_read_file,
    "dgs_write_file": dgs_write_file,
    "dgs_search": dgs_search,
    "dgs_apply_patch": dgs_apply_patch,
    "dgs_list_open_files": dgs_list_open_files,
    "dgs_shutdown": dgs_shutdown,
}

TOOLS: dict[str, dict[str, Any]] = {
    name: {**TOOL_SPECS[name], "handler": handler}
    for name, handler in _TOOL_HANDLERS.items()
}


def _tools_list() -> list[dict[str, Any]]:
    return tools_list()


def _handle_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    spec = TOOLS.get(name)
    if not spec:
        raise ValueError(f"unknown tool: {name}")
    _log(f"tool start {name}")
    if name in {"dgs_open_file", "dgs_read_file", "dgs_write_file", "dgs_search", "dgs_apply_patch"}:
        data = _run_worker_tool(name, arguments or {})
        _log(f"tool end {name} ok={data.get('ok')}")
        return data
    data = spec["handler"](arguments or {})
    if name == "dgs_shutdown" and data.get("safe_to_stop", data.get("ok", False)):
        STOP_EVENT.set()
    _log(f"tool end {name} ok={data.get('ok')}")
    return data


def _run_worker_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    timeout = float(arguments.get("worker_timeout", WORKER_TIMEOUT))
    args_json = json.dumps(arguments or {}, ensure_ascii=False, separators=(",", ":"))
    args_bytes = args_json.encode("utf-8")
    with LOCK:
        proc = subprocess.Popen(
            [sys.executable, str(BROKER_DIR / "worker.py"), name],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            stdout_bytes, stderr_bytes = proc.communicate(input=args_bytes, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_bytes, stderr_bytes = proc.communicate(timeout=5)
            stderr = _decode_worker_stream(stderr_bytes)
            _log(f"worker timeout {name} after {timeout}s")
            lifecycle = _release_managed_npp(
                f"{name} worker timed out",
                wait_timeout=1.0,
                close_clean=True,
                preserve_untracked=False,
            )
            return {
                "ok": False,
                "error": f"{name} timed out after {timeout}s; Notepad++ may be busy or showing a modal dialog.",
                "worker_stderr": stderr,
                "lifecycle": lifecycle,
            }
        stdout = _decode_worker_stream(stdout_bytes)
        stderr = _decode_worker_stream(stderr_bytes)
        if stderr:
            _log(f"worker {name} stderr: {stderr.strip()}")
        if not stdout.strip():
            return {
                "ok": False,
                "error": f"{name} worker produced no output",
                "worker_stderr": stderr,
                "lifecycle": _release_managed_npp(
                    f"{name} worker produced no output",
                    wait_timeout=1.0,
                    preserve_untracked=False,
                ),
            }
        try:
            data = json.loads(stdout.strip().splitlines()[-1])
        except json.JSONDecodeError as exc:
            return {
                "ok": False,
                "error": f"{name} worker returned invalid JSON: {exc}",
                "worker_stdout": stdout,
                "worker_stderr": stderr,
                "lifecycle": _release_managed_npp(
                    f"{name} worker returned invalid JSON",
                    wait_timeout=1.0,
                    preserve_untracked=False,
                ),
            }
        if not data.get("ok", False):
            if "lifecycle" not in data:
                data["lifecycle"] = _release_managed_npp(
                    f"{name} worker returned an error",
                    wait_timeout=1.0,
                    close_clean=True,
                    preserve_untracked=False,
                )
        return data


def _decode_worker_stream(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    for encoding in ("utf-8", "gbk", "cp936"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


def _handle_broker_request(message: dict[str, Any]) -> dict[str, Any]:
    request_id = message.get("id")
    op = message.get("op")
    try:
        if op == "ping":
            return _broker_response(request_id, {"name": SERVER_NAME, "version": SERVER_VERSION, "pid": os.getpid()})
        if op == "tools/list":
            return _broker_response(request_id, {"tools": _tools_list()})
        if op == "tools/call":
            return _broker_response(
                request_id,
                _handle_tool_call(str(message.get("name", "")), message.get("arguments") or {}),
            )
        return _broker_response(request_id, error=f"unknown broker op: {op}")
    except Exception as exc:
        return _broker_response(request_id, error=f"{type(exc).__name__}: {exc}")


def _handle_client(conn: socket.socket, addr: tuple[str, int]) -> None:
    try:
        with conn:
            reader = conn.makefile("r", encoding="utf-8", newline="\n")
            writer = conn.makefile("w", encoding="utf-8", newline="\n")
            for raw in reader:
                if not raw.strip():
                    continue
                try:
                    response = _handle_broker_request(json.loads(raw))
                except json.JSONDecodeError as exc:
                    response = _broker_response(None, error=f"JSONDecodeError: {exc}")
                writer.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                writer.flush()
                if STOP_EVENT.is_set():
                    break
    except Exception as exc:
        _log(f"client {addr} failed: {type(exc).__name__}: {exc}")


def serve() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _log(f"broker listening on {BROKER_HOST}:{BROKER_PORT}; bridge={BRIDGE_PATH}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((BROKER_HOST, BROKER_PORT))
        sock.listen(16)
        sock.settimeout(0.5)
        while not STOP_EVENT.is_set():
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            threading.Thread(target=_handle_client, args=(conn, addr), daemon=True).start()
    _log("broker stopped")
    return 0


def main() -> int:
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
