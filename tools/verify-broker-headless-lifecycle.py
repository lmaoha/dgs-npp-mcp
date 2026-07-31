#!/usr/bin/env python3
"""Exercise read-only dirty recovery against the live singleton broker."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import broker  # noqa: E402


def broker_call(name: str, arguments: dict) -> dict:
    payload = {
        "id": "headless-lifecycle-probe",
        "op": "tools/call",
        "name": name,
        "arguments": arguments,
    }
    with socket.create_connection((broker.BROKER_HOST, broker.BROKER_PORT), timeout=5) as sock:
        sock.settimeout(30)
        reader = sock.makefile("r", encoding="utf-8", newline="\n")
        writer = sock.makefile("w", encoding="utf-8", newline="\n")
        writer.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        writer.flush()
        response = json.loads(reader.readline())
    if not response.get("ok"):
        raise RuntimeError(str(response.get("error") or "broker request failed"))
    return response.get("result") or {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("query")
    args = parser.parse_args()

    top, scin, path_abs = broker._activate(args.path, 15.0)
    before_source = broker._stat(path_abs)
    before = broker._read_buffer_snapshot(scin)

    broker.bridge.write_document_bytes(scin, before["data"] + b" ")
    broker.bridge.write_document_bytes(scin, before["data"])
    after_edit = broker._read_buffer_snapshot(scin)
    same_hash = after_edit["sha256"] == before["sha256"]
    modified_before_search = broker._dirty(scin)
    hidden_before_search = not broker._window_visible(top)
    if not same_hash or not modified_before_search:
        meta = {
            "operation": "probe_cleanup",
            "dirty_origin": "read_only",
            "reload_allowed": True,
            "source": before_source,
        }
        broker._reload_current(top, path_abs)
        broker._finish_read_tab(path_abs, top, scin, meta)
        raise RuntimeError(
            "could not create a same-content modified Scintilla buffer: "
            f"same_hash={same_hash} modified={modified_before_search}"
        )

    broker._record(path_abs, top, scin, {
        "dirty": True,
        "buffer_modified": True,
        "title_dirty": broker._title_dirty(top),
        "source": before_source,
        "operation": "dgs_search",
        "dirty_origin": "read_only",
        "reload_allowed": True,
        "mutation_started": False,
        "quarantine_reason": "synthetic read-only dirty probe",
    })
    state = broker._read_state() or {}
    pid = int(state.get("pid") or 0)
    stop_polling = threading.Event()
    visibility_samples: list[bool] = []

    def poll_visibility() -> None:
        while not stop_polling.is_set():
            for hwnd in broker.bridge._enum_toplevel():
                if (
                    broker.bridge.window_process_id(hwnd) == pid
                    and broker.bridge._get_class(hwnd) == "Notepad++"
                ):
                    visibility_samples.append(broker._window_visible(hwnd))
            time.sleep(0.005)

    poller = threading.Thread(target=poll_visibility, daemon=True)
    poller.start()
    try:
        result = broker_call("dgs_search", {
            "path": path_abs,
            "query": args.query,
            "context_lines": 1,
        })
    finally:
        stop_polling.set()
        poller.join(timeout=2)

    after_source = broker._stat(path_abs)
    source_unchanged = (
        before_source["mtime_ns"] == after_source["mtime_ns"]
        and before_source["size"] == after_source["size"]
    )
    lifecycle = result.get("lifecycle") or {}
    output = {
        "ok": bool(
            result.get("ok")
            and result.get("dirty_recovered")
            and result.get("tab_closed")
            and lifecycle.get("status") == "closed"
            and lifecycle.get("headless") is True
            and hidden_before_search
            and not any(visibility_samples)
            and source_unchanged
        ),
        "same_hash_before_search": same_hash,
        "modified_before_search": modified_before_search,
        "hidden_before_search": hidden_before_search,
        "visibility_samples": len(visibility_samples),
        "ever_visible": any(visibility_samples),
        "dirty_recovered": result.get("dirty_recovered"),
        "tab_closed": result.get("tab_closed"),
        "lifecycle": lifecycle,
        "source_unchanged": source_unchanged,
        "returned_matches": result.get("returned_matches"),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
