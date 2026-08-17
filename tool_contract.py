#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Static MCP tool schemas shared by the stdio adapter and the broker."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


TOOL_SPECS: dict[str, dict[str, Any]] = {
    "dgs_open_file": {
        "description": "Open a DGS-format file through the MCP-managed Notepad++ instance and verify the active tab by full path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or workspace-relative file path to open."},
                "wait_timeout": {"type": "number", "default": 15},
                "worker_timeout": {"type": "number", "default": 20, "description": "Hard timeout in seconds for the broker worker process."},
                "auto_reload": {"type": "boolean", "default": True, "description": "Reload if an already-tracked clean buffer changed on disk."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    "dgs_read_file": {
        "description": "Read document text and bytes from the Notepad++ Scintilla buffer. Does not create .dat snapshots.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "wait_timeout": {"type": "number", "default": 15},
                "auto_reload": {"type": "boolean", "default": True},
                "include_base64": {"type": "boolean", "default": False},
                "max_text_chars": {"type": "integer", "default": 200000, "description": "Maximum decoded text chars to include; set -1 for full text."},
                "worker_timeout": {"type": "number", "default": 20, "description": "Hard timeout in seconds for the broker worker process."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    "dgs_write_file": {
        "description": "Replace the Notepad++ Scintilla buffer with the provided document content and save it through Notepad++.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "text": {"type": "string", "description": "Document text to write using the current Scintilla code page."},
                "bytes_base64": {"type": "string", "description": "Raw Scintilla document bytes as base64. Mutually exclusive with text."},
                "expected_mtime_ns": {
                    "oneOf": [
                        {"type": "integer"},
                        {"type": "string", "pattern": "^[0-9]+$"},
                    ],
                    "description": "Optional optimistic-lock mtime. Prefer source.mtime_ns_exact (string); integer is accepted for backward compatibility.",
                },
                "expected_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-fA-F]{64}$",
                    "description": "Optional SHA-256 of the complete current Scintilla document returned by dgs_read_file.",
                },
                "allow_stale": {"type": "boolean", "default": False},
                "wait_timeout": {"type": "number", "default": 15},
                "worker_timeout": {"type": "number", "default": 20, "description": "Hard timeout in seconds for the broker worker process."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    },
    "dgs_search": {
        "description": "Search a DGS document inside the MCP-managed Scintilla buffer and return only matching lines and bounded context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or workspace-relative DGS file path."},
                "query": {"type": "string", "description": "Literal text or regular expression to search for."},
                "regex": {"type": "boolean", "default": False},
                "case_sensitive": {"type": "boolean", "default": True},
                "context_lines": {"type": "integer", "minimum": 0, "maximum": 20, "default": 2},
                "max_matches": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
                "max_output_chars": {"type": "integer", "minimum": 1000, "maximum": 200000, "default": 20000},
                "wait_timeout": {"type": "number", "default": 15},
                "auto_reload": {"type": "boolean", "default": True},
                "worker_timeout": {"type": "number", "default": 20},
            },
            "required": ["path", "query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    "dgs_apply_patch": {
        "description": "Apply an exact small text patch inside the complete DGS Scintilla buffer, then save and reopen-verify it without receiving the whole file from the caller.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string", "description": "Exact existing text to replace; it must occur the expected number of times."},
                "new_text": {"type": "string", "description": "Replacement text. Newlines are normalized to the source style by default."},
                "expected_occurrences": {"type": "integer", "minimum": 1, "maximum": 100, "default": 1},
                "expected_mtime_ns": {"oneOf": [{"type": "integer"}, {"type": "string", "pattern": "^[0-9]+$"}], "description": "Exact mtime returned by dgs_search or dgs_read_file."},
                "expected_sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$", "description": "SHA-256 returned by dgs_search or dgs_read_file for the complete current buffer."},
                "normalize_newlines": {"type": "boolean", "default": True},
                "dry_run": {"type": "boolean", "default": False, "description": "Validate the patch and return hashes without saving."},
                "wait_timeout": {"type": "number", "default": 15},
                "worker_timeout": {"type": "number", "default": 20},
            },
            "required": ["path", "old_text", "new_text", "expected_mtime_ns", "expected_sha256"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    },
    "dgs_list_open_files": {
        "description": "List files tracked by this MCP server and whether their source mtime changed externally.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    "dgs_shutdown": {
        "description": "Close the MCP-managed Notepad++ instance and clear server state.",
        "inputSchema": {
            "type": "object",
            "properties": {"wait_timeout": {"type": "number", "default": 5}},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
}


def tools_list() -> list[dict[str, Any]]:
    """Return an isolated JSON-serializable copy of the public tool contract."""
    return [
        {
            "name": name,
            "description": spec["description"],
            "inputSchema": deepcopy(spec["inputSchema"]),
            "annotations": deepcopy(spec["annotations"]),
        }
        for name, spec in TOOL_SPECS.items()
    ]
