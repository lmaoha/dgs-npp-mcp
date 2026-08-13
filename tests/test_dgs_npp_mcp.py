from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import broker  # noqa: E402


def active_binding(
    path: str = r"C:\work\target.cpp",
    *,
    pid: int = 4242,
    top: int = 101,
    scin: int = 201,
    buffer_id: int = 123456,
    view: int = 0,
) -> dict:
    return {
        "pid": pid,
        "top_hwnd": top,
        "active_view": view,
        "scintilla_hwnd": scin,
        "buffer_id": buffer_id,
        "path": path,
        "active_binding_verified": True,
    }


class BridgeContractTests(unittest.TestCase):
    def test_public_tool_names_are_stable(self) -> None:
        self.assertEqual(
            set(broker.TOOLS),
            {
                "dgs_open_file",
                "dgs_read_file",
                "dgs_write_file",
                "dgs_search",
                "dgs_apply_patch",
                "dgs_list_open_files",
                "dgs_shutdown",
            },
        )

    def test_unexpected_binary_profile_detection_is_preserved(self) -> None:
        self.assertFalse(broker.bridge.has_unexpected_binary_profile(b"ordinary document text\n"))
        self.assertTrue(broker.bridge.has_unexpected_binary_profile(bytes(range(256)) * 256))


class ActiveBindingBridgeTests(unittest.TestCase):
    def test_official_active_view_selects_empty_sub_view_without_length_heuristic(self) -> None:
        with (
            mock.patch.object(
                broker.bridge,
                "send_npp_int_out_message",
                return_value=broker.bridge.SUB_VIEW,
            ),
            mock.patch.object(
                broker.bridge,
                "_editor_view_map",
                return_value={broker.bridge.MAIN_VIEW: 201, broker.bridge.SUB_VIEW: 202},
            ),
            mock.patch.object(broker.bridge, "window_process_id", return_value=4242),
            mock.patch.object(broker.bridge.u32, "IsWindowVisible") as visible,
            mock.patch.object(broker.bridge.u32, "SendMessageW") as send,
        ):
            view, scin = broker.bridge.get_active_scintilla(101)

        self.assertEqual((view, scin), (broker.bridge.SUB_VIEW, 202))
        visible.assert_not_called()
        send.assert_not_called()

    def test_editor_view_discovery_ignores_two_auxiliary_scintillas(self) -> None:
        with (
            mock.patch.object(broker.bridge, "_scintilla_children", return_value=[201, 202, 203, 204]),
            mock.patch.object(
                broker.bridge,
                "_has_editor_border",
                side_effect=lambda hwnd: hwnd in {201, 202},
            ),
        ):
            result = broker.bridge._discover_editor_view_map(101)

        self.assertEqual(
            result,
            {broker.bridge.MAIN_VIEW: 201, broker.bridge.SUB_VIEW: 202},
        )

    def test_active_document_snapshot_retries_when_path_and_buffer_change(self) -> None:
        with (
            mock.patch.object(
                broker.bridge,
                "get_current_file_path_strict",
                side_effect=["old.cpp", "new.cpp", "new.cpp", "new.cpp"],
            ),
            mock.patch.object(
                broker.bridge,
                "get_current_buffer_id",
                side_effect=[11, 12, 12, 12],
            ),
            mock.patch.object(broker.bridge, "get_active_scintilla", return_value=(0, 201)),
            mock.patch.object(broker.bridge, "window_process_id", return_value=4242),
            mock.patch("time.sleep") as sleep,
        ):
            result = broker.bridge.get_active_document_snapshot(101, retries=2)

        self.assertEqual(result["path"], "new.cpp")
        self.assertEqual(result["buffer_id"], 12)
        self.assertTrue(result["active_binding_verified"])
        sleep.assert_called_once_with(0.05)

    def test_active_scintilla_rejects_a_child_from_another_pid(self) -> None:
        with (
            mock.patch.object(broker.bridge, "send_npp_int_out_message", return_value=0),
            mock.patch.object(
                broker.bridge,
                "_editor_view_map",
                return_value={broker.bridge.MAIN_VIEW: 201, broker.bridge.SUB_VIEW: 202},
            ),
            mock.patch.object(
                broker.bridge,
                "window_process_id",
                side_effect=lambda hwnd: 4242 if hwnd == 101 else 9999,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "PID mismatch"):
                broker.bridge.get_active_scintilla(101)

    def test_buffer_read_stops_if_buffer_id_changes(self) -> None:
        before = active_binding(buffer_id=11)
        after = active_binding(buffer_id=12)
        with (
            mock.patch.object(
                broker.bridge,
                "get_active_document_snapshot",
                side_effect=[before, after],
            ),
            mock.patch.object(broker.bridge, "read_document_bytes", return_value=b"text"),
            mock.patch.object(broker.bridge, "get_code_page", return_value=65001),
        ):
            with self.assertRaisesRegex(RuntimeError, "BufferID changed"):
                broker._read_buffer_snapshot(
                    201,
                    top=101,
                    expected_path=r"C:\work\target.cpp",
                    expected_buffer_id=11,
                )

    def test_clean_close_is_confirmed_when_target_buffer_disappears(self) -> None:
        before = active_binding(buffer_id=11)
        after = active_binding(buffer_id=12)
        with (
            mock.patch.object(broker, "_capture_binding", side_effect=[before, before, after]) as capture,
            mock.patch.object(broker, "_dirty", return_value=False),
            mock.patch.object(broker.bridge.u32, "SendMessageW") as send,
            mock.patch.object(broker.bridge.u32, "IsWindow", return_value=True),
            mock.patch.object(broker.bridge, "window_process_id", return_value=before["pid"]),
            mock.patch.object(broker.bridge, "get_buffer_position", return_value=None),
        ):
            closed = broker._close_current_tab_if_clean(
                before["top_hwnd"],
                before["scintilla_hwnd"],
                expected_path=before["path"],
                expected_buffer_id=before["buffer_id"],
                close_timeout=0,
            )

        self.assertTrue(closed)
        self.assertEqual(capture.call_count, 3)
        send.assert_called_once_with(before["top_hwnd"], 0x0111, 41003, 0)

    def test_clean_close_is_refused_when_target_buffer_remains_open(self) -> None:
        binding = active_binding(buffer_id=11)
        with (
            mock.patch.object(broker, "_capture_binding", side_effect=[binding, binding]),
            mock.patch.object(broker, "_dirty", return_value=False),
            mock.patch.object(broker.bridge.u32, "SendMessageW"),
            mock.patch.object(broker.bridge.u32, "IsWindow", return_value=True),
            mock.patch.object(broker.bridge, "window_process_id", return_value=binding["pid"]),
            mock.patch.object(broker.bridge, "get_buffer_position", return_value=(binding["active_view"], 0)),
        ):
            closed = broker._close_current_tab_if_clean(
                binding["top_hwnd"],
                binding["scintilla_hwnd"],
                expected_path=binding["path"],
                expected_buffer_id=binding["buffer_id"],
                close_timeout=0,
            )

        self.assertFalse(closed)


class MtimeTests(unittest.TestCase):
    def test_exact_string_mtime_is_strict(self) -> None:
        actual = 1_783_661_392_316_368_300
        self.assertTrue(broker._mtime_matches(str(actual), actual))
        self.assertFalse(broker._mtime_matches(str(actual + 1), actual))

    def test_legacy_json_number_rounding_is_tolerated(self) -> None:
        actual = 1_783_661_392_316_368_300
        rounded_by_javascript = 1_783_661_392_316_368_400
        self.assertTrue(broker._mtime_matches(rounded_by_javascript, actual))
        self.assertFalse(broker._mtime_matches(rounded_by_javascript + 10_000, actual))


class SearchTests(unittest.TestCase):
    def test_literal_search_returns_bounded_line_context(self) -> None:
        result = broker._search_text(
            "first\nalpha one\nmiddle\nalpha two\nlast\n",
            "alpha",
            context_lines=1,
        )

        self.assertEqual(result["total_matches"], 2)
        self.assertEqual(result["returned_matches"], 2)
        self.assertEqual(result["matches"][0]["line"], 2)
        self.assertEqual(result["matches"][0]["column"], 1)
        self.assertEqual(result["matches"][0]["before"], [{"line": 1, "text": "first"}])
        self.assertEqual(result["matches"][0]["after"], [{"line": 3, "text": "middle"}])
        self.assertNotIn("first\nalpha one", result)

    def test_regex_search_can_ignore_case_and_limit_results(self) -> None:
        result = broker._search_text(
            "Alpha 10\nalpha 20\nALPHA 30\n",
            r"alpha\s+\d+",
            regex=True,
            case_sensitive=False,
            context_lines=0,
            max_matches=2,
        )

        self.assertEqual(result["total_matches"], 3)
        self.assertEqual(result["returned_matches"], 2)
        self.assertTrue(result["truncated"])

    def test_regex_that_matches_empty_text_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty text"):
            broker._search_text("abc", r".*", regex=True)


class ExactPatchTests(unittest.TestCase):
    def test_patch_is_derived_from_original_and_normalizes_newlines(self) -> None:
        original = "head\r\nold line\r\ntail\r\n"
        result = broker._build_exact_patch(
            original,
            "old line\n",
            "new one\nnew two\n",
            newline_style="crlf",
        )

        self.assertEqual(result["text"], "head\r\nnew one\r\nnew two\r\ntail\r\n")
        self.assertEqual(
            len(result["text"]),
            len(original) - len(result["old_text"]) + len(result["new_text"]),
        )
        self.assertTrue(result["newlines_normalized"])

    def test_patch_rejects_ambiguous_anchor(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "expected=1, actual=2"):
            broker._build_exact_patch("A target B target C", "target", "changed")

    def test_patch_can_replace_an_explicit_number_of_occurrences(self) -> None:
        result = broker._build_exact_patch(
            "A target B target C",
            "target",
            "changed",
            expected_occurrences=2,
        )
        self.assertEqual(result["text"], "A changed B changed C")
        self.assertEqual(result["occurrences"], 2)

    def test_patch_allows_large_intentional_shortening(self) -> None:
        original = "prefix\n" + ("remove-me\n" * 100) + "suffix\n"
        result = broker._build_exact_patch(
            original,
            "remove-me\n" * 100,
            "kept short\n",
        )

        self.assertEqual(result["text"], "prefix\nkept short\nsuffix\n")
        self.assertLess(result["after_chars"], result["before_chars"] // 5)


class PersistConcurrencyTests(unittest.TestCase):
    def test_external_change_after_buffer_write_blocks_save(self) -> None:
        data = b"updated\n"
        before = {"mtime_ns": 100, "size": 8}
        with (
            mock.patch.object(
                broker,
                "_capture_binding",
                return_value=active_binding(path="target.cpp"),
            ),
            mock.patch.object(broker.bridge, "write_document_bytes", return_value=len(data)),
            mock.patch.object(broker.bridge, "read_document_bytes", return_value=data),
            mock.patch.object(broker, "_stat", return_value={"mtime_ns": 101, "size": 9}),
            mock.patch.object(broker.bridge, "save_current") as save_current,
        ):
            with self.assertRaisesRegex(RuntimeError, "before save"):
                broker._persist_and_verify(
                    "target.cpp",
                    101,
                    201,
                    data,
                    before,
                    wait_timeout=1,
                    code_page=65001,
                    byte_info={"bom": "none", "newline": "lf", "newline_counts": {}},
                )

        save_current.assert_not_called()


class BackgroundSaveTests(unittest.TestCase):
    def test_save_current_uses_window_message_without_foreground_activation(self) -> None:
        with (
            mock.patch.object(broker.bridge.u32, "SendMessageW") as send_message,
            mock.patch.object(broker.bridge.u32, "SetForegroundWindow") as foreground,
            mock.patch.object(broker.bridge, "_pick_scintilla", return_value=201),
            mock.patch.object(broker.bridge, "is_document_modified", return_value=False) as modified,
        ):
            self.assertTrue(broker.bridge.save_current(101, timeout=0))

        send_message.assert_called_once_with(101, 0x0111, 41006, 0)
        modified.assert_called_once_with(201)
        foreground.assert_not_called()

    def test_save_current_timeout_does_not_activate_notepad(self) -> None:
        with (
            mock.patch.object(broker.bridge.u32, "SendMessageW") as send_message,
            mock.patch.object(broker.bridge.u32, "SetForegroundWindow") as foreground,
            mock.patch.object(broker.bridge, "_pick_scintilla", return_value=201),
            mock.patch.object(broker.bridge, "is_document_modified", return_value=True),
        ):
            self.assertFalse(broker.bridge.save_current(101, timeout=0))

        send_message.assert_called_once_with(101, 0x0111, 41006, 0)
        foreground.assert_not_called()


class MutationRetryTests(unittest.TestCase):
    def test_full_write_does_not_enter_automatic_recovery_retry(self) -> None:
        with (
            mock.patch.object(broker, "_activate", side_effect=TimeoutError("probe timeout")),
            mock.patch.object(broker, "_with_npp_recovery") as recovery,
        ):
            with self.assertRaisesRegex(TimeoutError, "probe timeout"):
                broker.dgs_write_file({"path": "target.cpp", "text": "replacement"})
        recovery.assert_not_called()

    def test_patch_requires_both_concurrency_tokens(self) -> None:
        with self.assertRaisesRegex(ValueError, "expected_mtime_ns"):
            broker.dgs_apply_patch({"path": "target.cpp", "old_text": "a", "new_text": "b"})
        with self.assertRaisesRegex(ValueError, "expected_sha256"):
            broker.dgs_apply_patch({
                "path": "target.cpp",
                "old_text": "a",
                "new_text": "b",
                "expected_mtime_ns": "1",
            })

    def test_write_failure_after_scintilla_mutation_is_quarantined(self) -> None:
        source = {"mtime_ns": 100, "mtime_ns_exact": "100", "size": 3, "mtime": 0.1}
        snapshot = {
            "data": b"old",
            "text": "old",
            "code_page": 65001,
            "encoding": "utf-8",
            "had_decode_errors": False,
            "byte_info": {"bom": "none", "newline": "none", "newline_counts": {}},
            "sha256": broker._sha256_hex(b"old"),
            "binding": active_binding(),
        }
        lifecycle = {"status": "quarantined_hidden", "safe_to_forget": False}
        meta = {
            "dirty": False,
            "title_dirty": False,
            "external_changed": False,
            "auto_reloaded": False,
            "dirty_recovered": False,
            "source": source,
            "binding": active_binding(),
        }
        with (
            mock.patch.object(broker, "_activate", return_value=(101, 201, r"C:\work\target.cpp")),
            mock.patch.object(broker, "_prepare_document", return_value=meta),
            mock.patch.object(broker, "_stat", return_value=source),
            mock.patch.object(broker, "_read_buffer_snapshot", return_value=snapshot),
            mock.patch.object(broker.bridge, "inspect_document_bytes", return_value={
                "bom": "none", "newline": "none", "newline_counts": {}
            }),
            mock.patch.object(broker, "_persist_and_verify", side_effect=RuntimeError("save failed")),
            mock.patch.object(broker, "_record_hidden_quarantine", return_value=lifecycle) as quarantine,
        ):
            result = broker.dgs_write_file({"path": "target.cpp", "text": "new"})

        self.assertFalse(result["ok"])
        self.assertTrue(result["buffer_mutated"])
        self.assertEqual(result["lifecycle"], lifecycle)
        quarantine.assert_called_once()


class DirtyRecoveryTests(unittest.TestCase):
    def test_dirty_uses_scintilla_modify_state_instead_of_window_title(self) -> None:
        with (
            mock.patch.object(broker.bridge, "is_document_modified", return_value=False) as modified,
            mock.patch.object(broker.bridge, "_get_title", return_value="* target.cpp - Notepad++") as title,
        ):
            self.assertFalse(broker._dirty(201))

        modified.assert_called_once_with(201)
        title.assert_not_called()

    def test_read_only_dirty_buffer_reloads_once_and_continues(self) -> None:
        source = {"mtime_ns": 100, "size": 3}
        with (
            mock.patch.object(broker, "_capture_binding", return_value=active_binding()),
            mock.patch.object(broker, "_stat", return_value=source),
            mock.patch.object(broker, "_load_open_files", return_value={}),
            mock.patch.object(
                broker,
                "_dirty_diagnostics",
                side_effect=[
                    {"modified": True, "title_dirty": True},
                    {"modified": False, "title_dirty": False},
                ],
            ),
            mock.patch.object(broker, "_reload_and_validate", return_value=source) as reload_once,
            mock.patch.object(broker, "_forget") as forget,
        ):
            result = broker._prepare_document(
                r"C:\work\target.cpp",
                101,
                201,
                True,
                read_only=True,
                operation="dgs_search",
            )

        self.assertFalse(result["dirty"])
        self.assertTrue(result["dirty_recovered"])
        self.assertTrue(result["auto_reloaded"])
        reload_once.assert_called_once()
        forget.assert_called_once()

    def test_reloadbuffer_result_is_verified_with_scintilla_state(self) -> None:
        source = {"mtime_ns": 100, "size": 3}
        with (
            mock.patch.object(broker, "_capture_binding", return_value=active_binding()),
            mock.patch.object(broker, "_reload_current", return_value=1) as reload_current,
            mock.patch.object(broker, "_stat", return_value=source),
            mock.patch.object(broker, "_dirty", return_value=False),
            mock.patch.object(broker, "_title_dirty", return_value=False),
        ):
            result = broker._reload_and_validate(r"C:\work\target.cpp", 101, 201, source)

        self.assertEqual(result, source)
        reload_current.assert_called_once()

    def test_reload_current_targets_active_buffer_id_instead_of_path_lookup(self) -> None:
        target = r"C:\work\target.cpp"
        with (
            mock.patch.object(broker.bridge, "ensure_current_path", return_value=(True, target)),
            mock.patch.object(broker.bridge.u32, "SendMessageW", side_effect=[123456, 1]) as send,
            mock.patch.object(broker.time, "sleep"),
        ):
            result = broker._reload_current(101, target)

        self.assertEqual(result, 1)
        self.assertEqual(send.call_args_list, [
            mock.call(101, broker.NPPM_GETCURRENTBUFFERID, 0, 0),
            mock.call(101, broker.NPPM_RELOADBUFFERID, 123456, 0),
        ])

    def test_read_only_reload_failure_retains_hidden_quarantine(self) -> None:
        source = {"mtime_ns": 100, "size": 3}
        lifecycle = {"status": "quarantined_hidden", "safe_to_forget": False}
        with (
            mock.patch.object(broker, "_capture_binding", return_value=active_binding()),
            mock.patch.object(broker, "_stat", return_value=source),
            mock.patch.object(broker, "_load_open_files", return_value={}),
            mock.patch.object(
                broker,
                "_dirty_diagnostics",
                return_value={"modified": True, "title_dirty": True},
            ),
            mock.patch.object(broker, "_reload_and_validate", side_effect=RuntimeError("reload failed")),
            mock.patch.object(broker, "_record_hidden_quarantine", return_value=lifecycle) as quarantine,
        ):
            with self.assertRaisesRegex(RuntimeError, "read-only dirty recovery failed"):
                broker._prepare_document(
                    r"C:\work\target.cpp",
                    101,
                    201,
                    True,
                    read_only=True,
                    operation="dgs_search",
                )

        quarantine.assert_called_once()

    def test_incomplete_write_quarantine_is_never_auto_reloaded(self) -> None:
        source = {"mtime_ns": 100, "size": 3}
        previous = {
            broker._norm(r"C:\work\target.cpp"): {
                "path": r"C:\work\target.cpp",
                "dirty": True,
                "dirty_origin": "write",
                "reload_allowed": False,
                "mutation_started": True,
                "quarantine_reason": "save failed",
                "source": source,
            }
        }
        with (
            mock.patch.object(broker, "_capture_binding", return_value=active_binding()),
            mock.patch.object(broker, "_stat", return_value=source),
            mock.patch.object(broker, "_load_open_files", return_value=previous),
            mock.patch.object(
                broker,
                "_dirty_diagnostics",
                return_value={"modified": False, "title_dirty": False},
            ),
            mock.patch.object(broker, "_reload_and_validate") as reload_once,
            mock.patch.object(broker, "_record_hidden_quarantine") as quarantine,
        ):
            with self.assertRaisesRegex(RuntimeError, "save failed"):
                broker._prepare_document(
                    r"C:\work\target.cpp",
                    101,
                    201,
                    True,
                    read_only=True,
                    operation="dgs_read_file",
                )

        reload_once.assert_not_called()
        quarantine.assert_called_once()


class ActivationTests(unittest.TestCase):
    def test_activate_reuses_current_dirty_path_without_doopen(self) -> None:
        target = os.path.abspath("target.cpp")
        with (
            mock.patch.object(broker.os.path, "exists", return_value=True),
            mock.patch.object(broker, "_ensure_npp", return_value=(4242, 101)),
            mock.patch.object(broker.bridge, "_get_pid_notepadpp", return_value=[(101, 201)]),
            mock.patch.object(broker, "_capture_binding", return_value=active_binding(path=target)),
            mock.patch.object(broker, "_hide_npp"),
            mock.patch.object(broker.bridge, "_npp_doopen") as doopen,
        ):
            result = broker._activate(target, wait_timeout=0)

        self.assertEqual(result, (101, 201, target))
        doopen.assert_not_called()

    def test_activate_switches_existing_tab_before_doopen(self) -> None:
        target = os.path.abspath("target.cpp")
        other = os.path.abspath("other.cpp")
        with (
            mock.patch.object(broker.os.path, "exists", return_value=True),
            mock.patch.object(broker, "_ensure_npp", return_value=(4242, 101)),
            mock.patch.object(broker.bridge, "_get_pid_notepadpp", return_value=[(101, 201)]),
            mock.patch.object(
                broker,
                "_capture_binding",
                side_effect=[
                    active_binding(path=other, scin=201, view=broker.bridge.MAIN_VIEW),
                    active_binding(path=target, scin=202, view=broker.bridge.SUB_VIEW),
                ],
            ),
            mock.patch.object(broker.bridge, "switch_to_file_by_path", return_value=True) as switch,
            mock.patch.object(broker, "_hide_npp"),
            mock.patch.object(broker.bridge, "_npp_doopen") as doopen,
        ):
            result = broker._activate(target, wait_timeout=0)

        self.assertEqual(result, (101, 202, target))
        switch.assert_called_once_with(101, target)
        doopen.assert_not_called()


class LaunchArgumentTests(unittest.TestCase):
    def test_headless_switch_appends_private_flag(self) -> None:
        with mock.patch.dict(os.environ, {"DGS_NPP_HEADLESS": "1"}):
            self.assertEqual(
                broker.bridge.build_npp_args(r"C:\tools\notepad++.exe"),
                [r"C:\tools\notepad++.exe", "-multiInst", "-nosession", "-headless"],
            )

    def test_headless_switch_is_opt_in(self) -> None:
        with mock.patch.dict(os.environ, {"DGS_NPP_HEADLESS": "0"}):
            self.assertEqual(
                broker.bridge.build_npp_args(r"C:\tools\notepad++.exe"),
                [r"C:\tools\notepad++.exe", "-multiInst", "-nosession"],
            )

    def test_invalid_headless_switch_is_rejected(self) -> None:
        with mock.patch.dict(os.environ, {"DGS_NPP_HEADLESS": "maybe"}):
            with self.assertRaisesRegex(ValueError, "DGS_NPP_HEADLESS"):
                broker.bridge.build_npp_args("notepad++.exe")

    def test_bundled_executable_is_always_headless(self) -> None:
        bundled = r"C:\mcp\runtime\notepad-plus-plus-headless\notepad++.exe"
        with (
            mock.patch.object(broker.bridge, "BUNDLED_NPP_EXE", bundled),
            mock.patch.dict(os.environ, {"DGS_NPP_HEADLESS": "0"}),
        ):
            self.assertEqual(
                broker.bridge.build_npp_args(bundled),
                [bundled, "-multiInst", "-nosession", "-headless"],
            )

    def test_bundled_executable_is_first_default_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bundled = Path(temp_dir, "runtime", "notepad++.exe")
            installed = Path(temp_dir, "installed", "notepad++.exe")
            bundled.parent.mkdir()
            installed.parent.mkdir()
            bundled.touch()
            installed.touch()
            with (
                mock.patch.object(broker.bridge, "NPP_EXE_CANDIDATES", [str(bundled), str(installed)]),
                mock.patch.dict(os.environ, {"NPP_EXE": ""}),
            ):
                self.assertEqual(broker.bridge.find_npp_exe(), str(bundled.resolve()))

    def test_npp_exe_override_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            override = Path(temp_dir, "override", "notepad++.exe")
            override.parent.mkdir()
            override.touch()
            with mock.patch.dict(os.environ, {"NPP_EXE": str(override)}):
                self.assertEqual(broker.bridge.find_npp_exe(), str(override.resolve()))

    def test_missing_npp_exe_override_does_not_fall_back(self) -> None:
        missing = str(Path(tempfile.gettempdir(), "missing-npp", "notepad++.exe"))
        with mock.patch.dict(os.environ, {"NPP_EXE": missing}):
            with self.assertRaisesRegex(FileNotFoundError, "NPP_EXE"):
                broker.bridge.find_npp_exe()

    def test_ownership_state_persists_headless_launch_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir, "state.json")
            with (
                mock.patch.object(broker, "STATE_DIR", Path(temp_dir)),
                mock.patch.object(broker, "STATE_FILE", state_file),
                mock.patch.object(broker.bridge, "get_file_version", return_value="8.5.7"),
            ):
                broker._write_state(4242, r"C:\mcp\notepad++.exe", headless=True)
                state = broker._read_state()

        self.assertEqual(state["state_version"], 4)
        self.assertTrue(state["headless"])
        self.assertEqual(state["launch_mode"], "headless")
        self.assertEqual(state["window_class"], broker.bridge.DGS_NPP_WINDOW_CLASS)
        self.assertEqual(state["runtime_version"], "8.5.7")


class WindowOwnershipTests(unittest.TestCase):
    def test_pid_enumeration_selects_only_the_expected_launch_mode(self) -> None:
        expected_exe = r"C:\mcp\notepad++.exe"
        with (
            mock.patch.object(broker.bridge, "process_exe_path", return_value=broker.bridge._normal_exe_path(expected_exe)),
            mock.patch.object(broker.bridge, "_enum_toplevel", return_value=[101, 102]),
            mock.patch.object(
                broker.bridge,
                "_get_class",
                side_effect=lambda hwnd: (
                    broker.bridge.DGS_NPP_WINDOW_CLASS
                    if hwnd == 101
                    else broker.bridge.NPP_WINDOW_CLASS
                ),
            ),
            mock.patch.object(broker.bridge, "window_process_id", return_value=4242),
            mock.patch.object(broker.bridge, "_pick_scintilla", side_effect=lambda hwnd: hwnd + 100),
        ):
            result = broker.bridge._get_pid_notepadpp(
                4242,
                expected_exe=expected_exe,
                expected_headless=True,
            )

        self.assertEqual(result, [(101, 201)])

    def test_pid_enumeration_rejects_an_executable_path_mismatch(self) -> None:
        with (
            mock.patch.object(broker.bridge, "process_exe_path", return_value=r"c:\other\notepad++.exe"),
            mock.patch.object(broker.bridge, "_enum_toplevel") as enum_windows,
        ):
            result = broker.bridge._get_pid_notepadpp(
                4242,
                expected_exe=r"C:\mcp\notepad++.exe",
                expected_headless=True,
            )

        self.assertEqual(result, [])
        enum_windows.assert_not_called()

    def test_window_identity_rejects_normal_class_for_headless_ownership(self) -> None:
        expected_exe = broker.bridge._normal_exe_path(r"C:\mcp\notepad++.exe")
        identity = {
            "pid": 4242,
            "top_hwnd": 101,
            "window_class": broker.bridge.NPP_WINDOW_CLASS,
            "headless": False,
            "exe": expected_exe,
        }
        with mock.patch.object(broker.bridge, "get_window_identity", return_value=identity):
            with self.assertRaisesRegex(RuntimeError, "launch-mode mismatch"):
                broker.bridge.validate_window_identity(
                    101,
                    expected_pid=4242,
                    expected_exe=expected_exe,
                    expected_headless=True,
                )


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp_dir.name, "state.json")
        self.open_files_file = Path(self.temp_dir.name, "open-files.json")
        self.path_patches = [
            mock.patch.object(broker, "STATE_FILE", self.state_file),
            mock.patch.object(broker, "OPEN_FILES_FILE", self.open_files_file),
        ]
        for patcher in self.path_patches:
            patcher.start()
        self.state_file.write_text(json.dumps({"pid": 4242, "exe": "notepad++.exe"}), encoding="utf-8")
        self.open_files_file.write_text("{}", encoding="utf-8")

    def tearDown(self) -> None:
        for patcher in reversed(self.path_patches):
            patcher.stop()
        self.temp_dir.cleanup()

    def set_headless_state(self) -> None:
        self.state_file.write_text(
            json.dumps({
                "state_version": 3,
                "pid": 4242,
                "exe": "notepad++.exe",
                "headless": True,
                "startup_buffer_id": None,
            }),
            encoding="utf-8",
        )

    def test_clear_state_refuses_live_hidden_window(self) -> None:
        with (
            mock.patch.object(broker.bridge, "_is_pid_alive", return_value=True),
            mock.patch.object(broker.bridge, "_get_pid_notepadpp", return_value=[(101, 201)]),
            mock.patch.object(broker, "_window_visible", return_value=False),
        ):
            with self.assertRaisesRegex(RuntimeError, "refusing to clear ownership"):
                broker._clear_state()
        self.assertTrue(self.state_file.exists())

    def test_dirty_window_is_restored_before_state_is_cleared(self) -> None:
        with (
            mock.patch.object(broker.bridge, "_is_pid_alive", return_value=True),
            mock.patch.object(broker.bridge, "_get_pid_notepadpp", return_value=[(101, 201)]),
            mock.patch.object(
                broker,
                "_capture_binding",
                return_value=active_binding(path=r"C:\work\user.cpp"),
            ),
            mock.patch.object(broker, "_dirty", return_value=True),
            mock.patch.object(broker, "_show_npp", return_value=True) as show_npp,
            mock.patch.object(broker, "_window_visible", return_value=True),
            mock.patch.object(broker, "_post_close") as post_close,
        ):
            result = broker._release_managed_npp("test dirty release", wait_timeout=0)

        self.assertEqual(result["status"], "restored_visible")
        self.assertTrue(result["safe_to_forget"])
        self.assertFalse(self.state_file.exists())
        show_npp.assert_called_once_with(101)
        post_close.assert_not_called()

    def test_release_uses_verified_active_scintilla_not_enumerated_guess(self) -> None:
        binding = active_binding(path=r"C:\work\target.cpp", scin=201)
        with (
            mock.patch.object(broker.bridge, "_is_pid_alive", return_value=True),
            mock.patch.object(broker.bridge, "_get_pid_notepadpp", return_value=[(101, 999)]),
            mock.patch.object(broker, "_capture_binding", return_value=binding),
            mock.patch.object(broker, "_dirty", side_effect=lambda scin: scin == 999) as dirty,
            mock.patch.object(broker, "_title_dirty", return_value=False),
            mock.patch.object(broker, "_show_npp", return_value=True),
            mock.patch.object(broker, "_window_visible", return_value=True),
        ):
            result = broker._release_managed_npp(
                "binding probe",
                wait_timeout=0,
                close_clean=False,
                preserve_untracked=False,
            )

        self.assertEqual(result["dirty_paths"], [])
        dirty.assert_called_once_with(201)

    def test_headless_dirty_window_is_quarantined_without_showing_gui(self) -> None:
        self.set_headless_state()
        with (
            mock.patch.object(broker.bridge, "_is_pid_alive", return_value=True),
            mock.patch.object(broker.bridge, "_get_pid_notepadpp", return_value=[(101, 201)]),
            mock.patch.object(broker, "_capture_binding", return_value=active_binding()),
            mock.patch.object(broker, "_dirty", return_value=True),
            mock.patch.object(broker, "_title_dirty", return_value=True),
            mock.patch.object(broker, "_hide_npp") as hide_npp,
            mock.patch.object(broker, "_show_npp") as show_npp,
            mock.patch.object(broker, "_post_close") as post_close,
        ):
            result = broker._release_managed_npp(
                "test headless dirty release",
                wait_timeout=0,
                preserve_untracked=False,
            )

        self.assertEqual(result["status"], "quarantined_hidden")
        self.assertFalse(result["safe_to_forget"])
        self.assertEqual(result["dirty_paths"], [r"C:\work\target.cpp"])
        self.assertTrue(self.state_file.exists())
        hide_npp.assert_called_once_with(101)
        show_npp.assert_not_called()
        post_close.assert_not_called()

    def test_headless_clean_close_timeout_stays_hidden(self) -> None:
        self.set_headless_state()
        with (
            mock.patch.object(broker.bridge, "_is_pid_alive", return_value=True),
            mock.patch.object(broker.bridge, "_get_pid_notepadpp", return_value=[(101, 201)]),
            mock.patch.object(broker, "_capture_binding", return_value=active_binding(path="")),
            mock.patch.object(broker, "_dirty", return_value=False),
            mock.patch.object(broker, "_title_dirty", return_value=False),
            mock.patch.object(broker, "_post_close", return_value=True) as post_close,
            mock.patch.object(broker, "_hide_npp") as hide_npp,
            mock.patch.object(broker, "_show_npp") as show_npp,
        ):
            result = broker._release_managed_npp(
                "test headless close timeout",
                wait_timeout=0,
                preserve_untracked=False,
            )

        self.assertEqual(result["status"], "quarantined_hidden")
        self.assertTrue(result["close_timeout"])
        self.assertFalse(result["safe_to_forget"])
        post_close.assert_called_once_with(101)
        hide_npp.assert_called_once_with(101)
        show_npp.assert_not_called()

    def test_headless_release_honors_tracked_dirty_background_tab(self) -> None:
        self.set_headless_state()
        tracked_path = r"C:\work\MRMeshFwd.h"
        self.open_files_file.write_text(
            json.dumps({
                broker._norm(tracked_path): {
                    "path": tracked_path,
                    "dirty": True,
                    "dirty_origin": "read_only",
                    "reload_allowed": True,
                }
            }),
            encoding="utf-8",
        )
        with (
            mock.patch.object(broker.bridge, "_is_pid_alive", return_value=True),
            mock.patch.object(broker.bridge, "_get_pid_notepadpp", return_value=[(101, 201)]),
            mock.patch.object(
                broker,
                "_capture_binding",
                return_value=active_binding(path=r"C:\work\MRMesh.h"),
            ),
            mock.patch.object(broker, "_dirty", return_value=False),
            mock.patch.object(broker, "_title_dirty", return_value=False),
            mock.patch.object(broker, "_hide_npp"),
            mock.patch.object(broker, "_show_npp") as show_npp,
            mock.patch.object(broker, "_post_close") as post_close,
        ):
            result = broker._release_managed_npp(
                "test tracked dirty tab",
                wait_timeout=0,
                preserve_untracked=False,
            )

        self.assertEqual(result["status"], "quarantined_hidden")
        self.assertIn(tracked_path, result["dirty_paths"])
        show_npp.assert_not_called()
        post_close.assert_not_called()

    def test_headless_instance_reuses_clean_untracked_tab_after_close_timeout(self) -> None:
        self.set_headless_state()
        with (
            mock.patch.object(broker.bridge, "_is_pid_alive", return_value=True),
            mock.patch.object(broker.bridge, "_find_pid_top", return_value=(101, 201)),
            mock.patch.object(
                broker.bridge,
                "validate_window_identity",
                return_value={
                    "pid": 4242,
                    "top_hwnd": 101,
                    "window_class": broker.bridge.DGS_NPP_WINDOW_CLASS,
                    "headless": True,
                    "exe": broker.bridge._normal_exe_path("notepad++.exe"),
                },
            ),
            mock.patch.object(broker.bridge, "get_file_version", return_value="8.5.7"),
            mock.patch.object(broker, "_capture_binding", return_value=active_binding()),
            mock.patch.object(broker, "_load_open_files", return_value={}),
            mock.patch.object(broker, "_hide_npp") as hide_npp,
            mock.patch.object(broker, "_release_managed_npp") as release,
        ):
            pid, top = broker._ensure_npp(wait_timeout=0)

        self.assertEqual((pid, top), (4242, 101))
        hide_npp.assert_called_once_with(101)
        release.assert_not_called()

    def test_clean_close_timeout_restores_window_without_terminating(self) -> None:
        with (
            mock.patch.object(broker.bridge, "_is_pid_alive", return_value=True),
            mock.patch.object(broker.bridge, "_get_pid_notepadpp", return_value=[(101, 201)]),
            mock.patch.object(broker, "_capture_binding", return_value=active_binding(path="")),
            mock.patch.object(broker, "_dirty", return_value=False),
            mock.patch.object(broker, "_post_close", return_value=True) as post_close,
            mock.patch.object(broker, "_show_npp", return_value=True) as show_npp,
            mock.patch.object(broker, "_window_visible", return_value=True),
            mock.patch.object(broker, "_terminate_pid") as terminate_pid,
        ):
            result = broker._release_managed_npp(
                "test close timeout",
                wait_timeout=0,
                preserve_untracked=False,
            )

        self.assertEqual(result["status"], "restored_visible")
        self.assertTrue(result["safe_to_forget"])
        self.assertFalse(self.state_file.exists())
        post_close.assert_called_once_with(101)
        show_npp.assert_called_once_with(101)
        terminate_pid.assert_not_called()

    def test_clean_process_exit_clears_state(self) -> None:
        with (
            mock.patch.object(broker.bridge, "_is_pid_alive", side_effect=[True, False, False]),
            mock.patch.object(broker.bridge, "_get_pid_notepadpp", return_value=[(101, 201)]),
            mock.patch.object(broker, "_capture_binding", return_value=active_binding(path="")),
            mock.patch.object(broker, "_dirty", return_value=False),
            mock.patch.object(broker, "_post_close", return_value=True),
        ):
            result = broker._release_managed_npp(
                "test clean exit",
                wait_timeout=0,
                preserve_untracked=False,
            )

        self.assertEqual(result["status"], "closed")
        self.assertTrue(result["safe_to_forget"])
        self.assertFalse(self.state_file.exists())

    def test_finish_read_tab_returns_complete_lifecycle(self) -> None:
        lifecycle = {"status": "closed", "safe_to_forget": True}
        source = {"mtime_ns": 100, "size": 3}
        meta = {"operation": "dgs_search", "dirty_origin": "read_only", "reload_allowed": True}
        with (
            mock.patch.object(broker, "_stat", return_value=source),
            mock.patch.object(
                broker,
                "_dirty_diagnostics",
                return_value={"modified": False, "title_dirty": False},
            ),
            mock.patch.object(broker, "_close_current_tab_if_clean", return_value=True),
            mock.patch.object(broker, "_forget"),
            mock.patch.object(broker, "_close_managed_npp_if_idle", return_value=lifecycle),
        ):
            result = broker._finish_read_tab(r"C:\work\target.cpp", 101, 201, meta)

        self.assertTrue(result["tab_closed"])
        self.assertEqual(result["lifecycle"], lifecycle)

    def test_shutdown_returns_the_verified_binding_fields(self) -> None:
        binding = active_binding()
        lifecycle = {
            "status": "closed",
            "safe_to_forget": True,
            "window_states": [{**binding, "modified": False, "title_dirty": False}],
        }
        with mock.patch.object(broker, "_release_managed_npp", return_value=lifecycle):
            result = broker.dgs_shutdown({})

        for name in broker.BINDING_FIELDS:
            self.assertEqual(result[name], binding[name])
        self.assertTrue(result["safe_to_stop"])


class StartupPlaceholderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = {
            "state_version": 3,
            "pid": 4242,
            "exe": "notepad++.exe",
            "headless": True,
            "startup_buffer_id": 777,
            "startup_path": "new 1",
        }
        self.binding = active_binding(
            path="new 1",
            scin=202,
            buffer_id=777,
            view=broker.bridge.SUB_VIEW,
        )

    def test_empty_dirty_recorded_startup_buffer_is_cleaned_and_closed(self) -> None:
        with (
            mock.patch.object(broker, "_capture_binding", return_value=self.binding),
            mock.patch.object(broker.bridge, "activate_buffer_id", return_value=self.binding),
            mock.patch.object(broker.bridge, "get_document_length", return_value=0),
            mock.patch.object(broker, "_dirty", side_effect=[True, False]),
            mock.patch.object(broker.bridge, "set_document_save_point", return_value=True) as savepoint,
            mock.patch.object(broker, "_close_current_tab_if_clean", return_value=True) as close,
        ):
            result = broker._cleanup_startup_placeholder(self.state, 101)

        self.assertEqual(result["status"], "closed_empty_startup_buffer")
        self.assertTrue(result["changed"])
        self.assertTrue(result["savepoint_cleared"])
        savepoint.assert_called_once_with(202)
        close.assert_called_once_with(
            101,
            202,
            expected_path="new 1",
            expected_buffer_id=777,
        )

    def test_nonempty_startup_buffer_remains_protected(self) -> None:
        with (
            mock.patch.object(broker, "_capture_binding", return_value=self.binding),
            mock.patch.object(broker.bridge, "activate_buffer_id", return_value=self.binding),
            mock.patch.object(broker.bridge, "get_document_length", return_value=1),
            mock.patch.object(broker, "_dirty", return_value=True),
            mock.patch.object(broker.bridge, "set_document_save_point") as savepoint,
            mock.patch.object(broker, "_close_current_tab_if_clean") as close,
        ):
            result = broker._cleanup_startup_placeholder(self.state, 101)

        self.assertEqual(result["status"], "protected_nonempty")
        self.assertFalse(result["changed"])
        savepoint.assert_not_called()
        close.assert_not_called()

    def test_startup_buffer_id_mismatch_never_clears_dirty_state(self) -> None:
        with (
            mock.patch.object(
                broker,
                "_capture_binding",
                side_effect=[self.binding, RuntimeError("active Notepad++ BufferID changed")],
            ),
            mock.patch.object(
                broker.bridge,
                "activate_buffer_id",
                return_value=active_binding(path="new 1", scin=202, buffer_id=888),
            ),
            mock.patch.object(broker.bridge, "set_document_save_point") as savepoint,
            mock.patch.object(broker, "_close_current_tab_if_clean") as close,
        ):
            result = broker._cleanup_startup_placeholder(self.state, 101)

        self.assertEqual(result["status"], "inspection_failed")
        savepoint.assert_not_called()
        close.assert_not_called()


@unittest.skipUnless(
    os.environ.get("DGS_NPP_LIVE_TESTS") == "1",
    "set DGS_NPP_LIVE_TESTS=1 to exercise the bundled Notepad++ runtime",
)
class LiveBindingIntegrationTests(unittest.TestCase):
    def test_same_path_two_pids_100_reads_and_managed_only_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "same-path.cpp"
            data = b"line one\r\nline two\r\n" * 16
            path.write_bytes(data)
            state_dir = root / "state"
            state_file = state_dir / "npp-instance-broker.json"
            open_files_file = state_dir / "open-files.json"
            user_settings = root / "user-settings"
            managed_settings = root / "managed-settings"
            user_settings.mkdir()
            managed_settings.mkdir()
            test_exe = broker.bridge.find_npp_exe()
            original_build_npp_args = broker.bridge.build_npp_args
            user_proc = None
            user_top = None
            managed_pid = None

            with (
                mock.patch.object(broker, "STATE_DIR", state_dir),
                mock.patch.object(broker, "STATE_FILE", state_file),
                mock.patch.object(broker, "OPEN_FILES_FILE", open_files_file),
                mock.patch.object(
                    broker.bridge,
                    "build_npp_args",
                    side_effect=lambda exe: original_build_npp_args(exe)
                    + [f"-settingsDir={managed_settings}"],
                ),
            ):
                try:
                    top, main_scin, path_abs = broker._activate(str(path), 15)
                    managed_state = broker._read_state() or {}
                    managed_pid = int(managed_state.get("pid") or 0)
                    self.assertTrue(managed_pid)
                    self.assertEqual(
                        broker.bridge._get_class(top),
                        broker.bridge.DGS_NPP_WINDOW_CLASS,
                    )
                    self.assertFalse(broker.bridge.u32.IsWindowVisible(top))

                    # This intentionally omits -multiInst. It models a normal
                    # user launch after the DGS headless process is already up.
                    user_proc = subprocess.Popen(
                        [
                            test_exe,
                            "-nosession",
                            f"-settingsDir={user_settings}",
                            str(path),
                        ],
                        cwd=broker.bridge.npp_working_directory(test_exe),
                    )
                    user_binding = None
                    for _ in range(300):
                        user_top, _ = broker.bridge._find_pid_top(
                            user_proc.pid,
                            expected_exe=test_exe,
                            expected_headless=False,
                        )
                        if user_top:
                            try:
                                candidate = broker.bridge.get_active_document_snapshot(user_top)
                                if broker._norm(candidate["path"]) == broker._norm(str(path)):
                                    user_binding = candidate
                                    break
                            except Exception:
                                pass
                        time.sleep(0.05)
                    self.assertIsNotNone(user_binding)
                    self.assertNotEqual(user_proc.pid, managed_pid)
                    self.assertEqual(
                        broker.bridge._get_class(user_top),
                        broker.bridge.NPP_WINDOW_CLASS,
                    )
                    self.assertTrue(broker.bridge.u32.IsWindowVisible(user_top))
                    broker.bridge.u32.ShowWindow(user_top, broker.SW_HIDE)

                    broker.bridge.u32.SendMessageW(top, 0x0111, 10002, 0)
                    time.sleep(0.15)
                    top, scin, path_abs = broker._activate(str(path), 15)
                    meta = broker._prepare_document(
                        path_abs,
                        top,
                        scin,
                        True,
                        read_only=True,
                        operation="live_100_read_probe",
                    )
                    self.assertEqual(int(meta["binding"]["pid"]), managed_pid)
                    buffer_id = int(meta["binding"]["buffer_id"])
                    self.assertNotEqual(main_scin, scin)
                    self.assertEqual(meta["binding"]["active_view"], broker.bridge.SUB_VIEW)
                    hashes = []
                    for _ in range(100):
                        snapshot = broker._read_buffer_snapshot(
                            scin,
                            top=top,
                            expected_path=path_abs,
                            expected_buffer_id=buffer_id,
                        )
                        self.assertEqual(snapshot["data"], data)
                        self.assertEqual(snapshot["binding"]["pid"], managed_pid)
                        hashes.append(snapshot["sha256"])
                        meta["binding"] = snapshot["binding"]

                    self.assertFalse(
                        broker._close_current_tab_if_clean(
                            top,
                            scin,
                            expected_path=path_abs,
                            expected_buffer_id=buffer_id,
                            close_timeout=0.25,
                        )
                    )
                    remaining = broker._capture_binding(
                        top,
                        expected_path=path_abs,
                        expected_buffer_id=buffer_id,
                    )
                    self.assertEqual(remaining["active_view"], broker.bridge.MAIN_VIEW)
                    self.assertTrue(
                        broker._close_current_tab_if_clean(
                            top,
                            int(remaining["scintilla_hwnd"]),
                            expected_path=path_abs,
                            expected_buffer_id=buffer_id,
                        )
                    )
                    self.assertIsNone(broker.bridge.get_buffer_position(top, buffer_id))

                    top, scin, path_abs = broker._activate(str(path), 15)
                    reopened = broker._capture_binding(
                        top,
                        expected_path=path_abs,
                        expected_scin=scin,
                    )
                    meta["binding"] = reopened
                    broker._record(path_abs, top, scin, {
                        **meta,
                        "dirty": False,
                        "buffer_modified": False,
                        "title_dirty": False,
                    })
                    shutdown = broker.dgs_shutdown({"wait_timeout": 5})
                    self.assertEqual(len(set(hashes)), 1)
                    self.assertNotEqual(user_proc.pid, managed_pid)
                    self.assertTrue(shutdown["safe_to_stop"])
                    self.assertTrue(broker.bridge._is_pid_alive(user_proc.pid))
                    self.assertFalse(broker.bridge._is_pid_alive(managed_pid))
                finally:
                    state = broker._read_state() or {}
                    cleanup_pid = managed_pid or int(state.get("pid") or 0)
                    if (
                        cleanup_pid
                        and (not user_proc or cleanup_pid != user_proc.pid)
                        and broker.bridge._is_pid_alive(cleanup_pid)
                    ):
                        broker.dgs_shutdown({"wait_timeout": 2})
                        if broker.bridge._is_pid_alive(cleanup_pid):
                            broker._terminate_pid(cleanup_pid)
                    if user_top:
                        broker.bridge.u32.PostMessageW(user_top, broker.WM_CLOSE, 0, 0)
                    if user_proc:
                        try:
                            user_proc.wait(5)
                        except subprocess.TimeoutExpired:
                            user_proc.kill()


class TransportTests(unittest.TestCase):
    def test_server_decodes_mcp_input_as_utf8(self) -> None:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "unknown-中文-method",
        }
        env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        completed = subprocess.run(
            [sys.executable, str(ROOT / "server.py")],
            input=(json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        response = json.loads(completed.stdout.decode("utf-8"))
        self.assertIn("中文", response["error"]["message"])

    def test_broker_sends_large_unicode_payload_over_worker_stdin(self) -> None:
        fake_worker = """\
import json
import sys

args = json.loads(sys.stdin.buffer.read().decode("utf-8"))
value = args["text"]
print(json.dumps({"ok": True, "chars": len(value), "first": value[:2], "last": value[-1]}, ensure_ascii=False))
"""
        value = "中文" + ("x" * 40_000) + "终"
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "worker.py").write_text(fake_worker, encoding="utf-8")
            with mock.patch.object(broker, "BROKER_DIR", Path(temp_dir)):
                result = broker._run_worker_tool(
                    "dgs_write_file",
                    {"text": value, "worker_timeout": 10},
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["chars"], len(value))
        self.assertEqual(result["first"], "中文")
        self.assertEqual(result["last"], "终")

    def test_worker_accepts_utf8_json_on_stdin(self) -> None:
        payload = {"text": "中文" + ("x" * 40_000)}
        completed = subprocess.run(
            [sys.executable, str(ROOT / "worker.py"), "unsupported-test-tool"],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        response = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(completed.returncode, 2)
        self.assertIn("unsupported worker tool", response["error"])

    def test_worker_timeout_releases_managed_notepad(self) -> None:
        fake_worker = """\
import time

time.sleep(5)
"""
        lifecycle = {"status": "restored_visible", "safe_to_forget": True}
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "worker.py").write_text(fake_worker, encoding="utf-8")
            with (
                mock.patch.object(broker, "BROKER_DIR", Path(temp_dir)),
                mock.patch.object(broker, "_release_managed_npp", return_value=lifecycle) as release,
            ):
                result = broker._run_worker_tool(
                    "dgs_read_file",
                    {"worker_timeout": 0.05},
                )

        self.assertFalse(result["ok"])
        self.assertEqual(result["lifecycle"], lifecycle)
        release.assert_called_once()

    def test_worker_error_with_lifecycle_is_not_released_twice(self) -> None:
        fake_worker = """\
import json

print(json.dumps({
    "ok": False,
    "error": "save failed",
    "lifecycle": {"status": "quarantined_hidden", "safe_to_forget": False}
}))
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "worker.py").write_text(fake_worker, encoding="utf-8")
            with (
                mock.patch.object(broker, "BROKER_DIR", Path(temp_dir)),
                mock.patch.object(broker, "_release_managed_npp") as release,
            ):
                result = broker._run_worker_tool("dgs_write_file", {})

        self.assertFalse(result["ok"])
        self.assertEqual(result["lifecycle"]["status"], "quarantined_hidden")
        release.assert_not_called()


if __name__ == "__main__":
    unittest.main()
