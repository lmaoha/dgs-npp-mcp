#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-shot worker for DGS Notepad++ bridge operations.

The broker launches this process for operations that may block inside Windows
window messaging. If Notepad++ hangs, the broker can kill this worker without
losing the broker process.
"""

from __future__ import annotations

import json
import sys

import broker

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


WORKER_TOOLS = {
    "dgs_open_file": broker.dgs_open_file,
    "dgs_read_file": broker.dgs_read_file,
    "dgs_write_file": broker.dgs_write_file,
    "dgs_search": broker.dgs_search,
    "dgs_apply_patch": broker.dgs_apply_patch,
}


def _read_arguments() -> dict:
    if len(sys.argv) == 2:
        payload = sys.stdin.buffer.read()
        if not payload:
            raise ValueError("worker received an empty stdin payload")
        return json.loads(payload.decode("utf-8"))
    if len(sys.argv) == 3:
        # Backward compatibility for an older broker. New brokers use stdin so
        # Unicode and source files larger than the Windows command-line limit work.
        return json.loads(sys.argv[2])
    raise ValueError("usage: worker.py <tool> with JSON arguments on stdin")


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print(json.dumps({"ok": False, "error": "usage: worker.py <tool> with JSON arguments on stdin"}))
        return 2
    name = sys.argv[1]
    try:
        args = _read_arguments()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"invalid worker arguments: {type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2
    handler = WORKER_TOOLS.get(name)
    if handler is None:
        print(json.dumps({"ok": False, "error": f"unsupported worker tool: {name}"}, ensure_ascii=False))
        return 2
    try:
        print(json.dumps(handler(args), ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
