#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stdio MCP adapter for the singleton DGS Notepad++ bridge."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from tool_contract import tools_list

try:
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


SERVER_NAME = "dgs_npp_mcp"
SERVER_VERSION = "0.9.0"
BROKER_HOST = "127.0.0.1"
BROKER_PORT = int(os.environ.get("DGS_NPP_BROKER_PORT", "57931"))
PYTHON_EXE = os.environ.get("DGS_NPP_PYTHON", sys.executable)
BROKER_PATH = Path(__file__).with_name("broker.py")
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "dgs-npp-mcp"
BROKER_PID_FILE = STATE_DIR / "broker.pid"
START_LOCK_FILE = STATE_DIR / "broker-start.lock"
BROKER_LOG_FILE = STATE_DIR / "broker.log"


def _log(message: str) -> None:
    sys.stderr.write(f"[{SERVER_NAME}] {message}\n")
    sys.stderr.flush()


def _json_line(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _ok(request_id: Any, result: dict[str, Any]) -> None:
    _json_line({"jsonrpc": "2.0", "id": request_id, "result": result})


def _err(request_id: Any, code: int, message: str, data: Any = None) -> None:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    _json_line({"jsonrpc": "2.0", "id": request_id, "error": error})


def _compact_content(data: dict[str, Any]) -> str:
    path = str(data.get("path", ""))
    matches = data.get("matches")
    if isinstance(matches, list):
        if not matches:
            return f"{path}: no matches"
        return "\n".join(
            f"{path}:{int(match.get('line', 0))}:{match.get('text', '')}"
            for match in matches
        )

    text = data.get("text")
    if isinstance(text, str):
        start = int(data.get("line_start", 1))
        lines = text.splitlines()
        if not lines:
            return f"{path}:{start}:"
        return "\n".join(
            f"{path}:{start + index}:{line}" for index, line in enumerate(lines)
        )

    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _tool_result(data: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": _compact_content(data)}],
        "structuredContent": data,
        "isError": is_error,
    }


def _broker_request(payload: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
    with socket.create_connection((BROKER_HOST, BROKER_PORT), timeout=timeout) as sock:
        sock.settimeout(timeout)
        reader = sock.makefile("r", encoding="utf-8", newline="\n")
        writer = sock.makefile("w", encoding="utf-8", newline="\n")
        writer.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        writer.flush()
        line = reader.readline()
        if not line:
            raise RuntimeError("broker closed connection without a response")
        response = json.loads(line)
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error", "broker request failed")))
        return response.get("result") or {}


def _broker_alive() -> bool:
    try:
        _broker_request({"id": "ping", "op": "ping"}, timeout=1.0)
        return True
    except Exception:
        return False


def _start_broker() -> None:
    if _broker_alive():
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_handle = None
    try:
        import msvcrt
        lock_handle = open(START_LOCK_FILE, "a+b")
        _acquire_start_lock(lock_handle)
        if _broker_alive():
            return
        existing_pid = _read_broker_pid()
        if existing_pid and _pid_alive(existing_pid):
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if _broker_alive():
                    return
                time.sleep(0.2)
            _terminate_pid(existing_pid)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            [PYTHON_EXE, str(BROKER_PATH)],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=open(BROKER_LOG_FILE, "a", encoding="utf-8"),
            creationflags=creationflags,
        )
        BROKER_PID_FILE.write_text(str(proc.pid), encoding="ascii")
        deadline = time.time() + 10.0
        last_error = ""
        while time.time() < deadline:
            try:
                _broker_request({"id": "ping", "op": "ping"}, timeout=1.0)
                _log(f"connected to broker pid={proc.pid} {BROKER_HOST}:{BROKER_PORT}")
                return
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if proc.poll() is not None:
                    raise RuntimeError(f"broker exited early with code {proc.returncode}: {last_error}; log={_read_log_tail()}")
                time.sleep(0.2)
        raise TimeoutError(f"broker did not become ready: {last_error}; log={_read_log_tail()}")
    finally:
        if lock_handle is not None:
            try:
                import msvcrt
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
                lock_handle.close()
            except Exception:
                pass


def _acquire_start_lock(lock_handle: Any, timeout: float = 10.0) -> None:
    import msvcrt

    deadline = time.time() + timeout
    last_error: OSError | None = None
    while time.time() < deadline:
        try:
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            last_error = exc
            if _broker_alive():
                return
            time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for broker start lock: {last_error}")


def _read_broker_pid() -> int | None:
    try:
        return int(BROKER_PID_FILE.read_text(encoding="ascii").strip())
    except Exception:
        return None


def _read_log_tail(max_chars: int = 2000) -> str:
    try:
        text = BROKER_LOG_FILE.read_text(encoding="utf-8", errors="replace")
        return text[-max_chars:]
    except Exception:
        return ""


def _pid_alive(pid: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        code = wintypes.DWORD()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259
    except Exception:
        return False


def _terminate_pid(pid: int) -> bool:
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x0001, False, int(pid))
        if not h:
            return False
        try:
            return bool(k32.TerminateProcess(h, 1))
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def _tools_list() -> list[dict[str, Any]]:
    return tools_list()


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    _start_broker()
    default_timeout = float(os.environ.get("DGS_NPP_MCP_TIMEOUT", "35"))
    data = _broker_request(
        {"id": "call", "op": "tools/call", "name": name, "arguments": arguments or {}},
        timeout=float(arguments.get("mcp_timeout", default_timeout)) if isinstance(arguments, dict) else default_timeout,
    )
    return _tool_result(data, is_error=not bool(data.get("ok", True)))


def _dispatch(message: dict[str, Any]) -> None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if request_id is None and method and method.startswith("notifications/"):
        return
    if method == "initialize":
        _ok(request_id, {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "DGS search and reads always use compact output. Use dgs_search with a small max_matches for normal source discovery, then dgs_read_file with line_start/line_end for local context and dgs_apply_patch for focused edits. Calls are serialized by one local broker and routed through the bundled headless Notepad++ runtime by default. Use dgs_write_file only for intentional complete-document replacement. NPP_EXE is an optional explicit runtime override. Do not use .dat snapshots.",
        })
    elif method == "ping":
        _ok(request_id, {})
    elif method == "tools/list":
        try:
            _ok(request_id, {"tools": _tools_list()})
        except Exception as exc:
            _err(request_id, -32603, f"broker unavailable: {type(exc).__name__}: {exc}")
    elif method == "tools/call":
        try:
            _ok(request_id, _call_tool(str(params.get("name", "")), params.get("arguments") or {}))
        except Exception as exc:
            _ok(request_id, _tool_result({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, is_error=True))
    else:
        _err(request_id, -32601, f"method not found: {method}")


def main() -> int:
    _log(f"starting stdio adapter for broker {BROKER_HOST}:{BROKER_PORT}")
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            _dispatch(json.loads(raw))
        except json.JSONDecodeError as exc:
            _err(None, -32700, f"parse error: {exc}")
        except Exception as exc:
            _err(None, -32603, f"internal error: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
