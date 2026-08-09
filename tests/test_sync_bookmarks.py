import importlib.util
import subprocess
import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "sync_bookmarks.py"
SPEC = importlib.util.spec_from_loader("sync_bookmarks", SourceFileLoader("sync_bookmarks", str(MODULE_PATH)))
sync_bookmarks = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = sync_bookmarks
SPEC.loader.exec_module(sync_bookmarks)


class SyncBookmarksWrapperTests(unittest.TestCase):
    def test_default_target_ids_excludes_source(self) -> None:
        self.assertEqual(sync_bookmarks.default_target_ids("chrome:Default"), ["edge:Default", "safari"])

    def test_build_sync_command_uses_default_targets_and_defaults(self) -> None:
        parser = sync_bookmarks.build_parser()
        args = parser.parse_args(["chrome", "--no-backup"])

        command = sync_bookmarks.build_sync_command(args)

        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1], str(sync_bookmarks.ENGINE))
        self.assertEqual(
            command[2:],
            [
                "--source",
                "chrome:Default",
                "--targets",
                "edge:Default,safari",
                "--mode",
                "strict",
                "--sync-strategy",
                "auto",
                "--no-backup",
            ],
        )

    def test_build_sync_command_passes_explicit_safety_flags(self) -> None:
        parser = sync_bookmarks.build_parser()
        args = parser.parse_args(["chrome", "edge", "--auto-close", "--allow-cloud-purge"])

        command = sync_bookmarks.build_sync_command(args)

        self.assertIn("--auto-close", command)
        self.assertIn("--allow-cloud-purge", command)

    def test_build_restore_command_requires_target_and_normalizes_alias(self) -> None:
        parser = sync_bookmarks.build_parser()
        args = parser.parse_args(["--restore-backup", "/tmp/edge.bak", "--restore-target", "edge"])

        command = sync_bookmarks.build_restore_command(args)

        self.assertEqual(
            command[2:],
            ["--restore-backup", "/tmp/edge.bak", "--restore-target", "edge:Default"],
        )

    def test_build_sync_command_accepts_explicit_targets(self) -> None:
        parser = sync_bookmarks.build_parser()
        args = parser.parse_args(["safari", "chrome", "edge", "--mode", "preview", "--sync-strategy", "direct"])

        command = sync_bookmarks.build_sync_command(args)

        self.assertIn("chrome:Default,edge:Default", command)
        self.assertIn("preview", command)
        self.assertIn("direct", command)

    def test_build_sync_command_accepts_additional_chromium_targets(self) -> None:
        parser = sync_bookmarks.build_parser()
        args = parser.parse_args(["chrome", "brave", "vivaldi", "opera"])

        command = sync_bookmarks.build_sync_command(args)

        self.assertIn("brave:Default,vivaldi:Default,opera:Default", command)

    def test_build_sync_command_accepts_direction_flags(self) -> None:
        parser = sync_bookmarks.build_parser()
        args = parser.parse_args(["--from", "chrome", "--to", "edge", "safari", "--mode", "strict"])

        command = sync_bookmarks.build_sync_command(args)

        self.assertEqual(command[2:6], ["--source", "chrome:Default", "--targets", "edge:Default,safari"])

    def test_build_sync_command_accepts_full_store_ids_and_comma_targets(self) -> None:
        parser = sync_bookmarks.build_parser()
        args = parser.parse_args(
            ["--from", "chrome:Default", "--to", "edge:Default,safari", "--mode", "preview"]
        )

        command = sync_bookmarks.build_sync_command(args)

        self.assertEqual(
            command[2:6],
            ["--source", "chrome:Default", "--targets", "edge:Default,safari"],
        )

    def test_build_sync_command_conflicts_source_and_from(self) -> None:
        parser = sync_bookmarks.build_parser()
        args = parser.parse_args(["chrome", "--from", "safari", "--to", "edge"])

        with self.assertRaises(SystemExit):
            sync_bookmarks.build_sync_command(args)

    def test_build_sync_command_conflicts_targets_and_to(self) -> None:
        parser = sync_bookmarks.build_parser()
        args = parser.parse_args(["chrome", "edge", "--to", "safari"])

        with self.assertRaises(SystemExit):
            sync_bookmarks.build_sync_command(args)

    def test_build_passthrough_command_for_doctor(self) -> None:
        parser = sync_bookmarks.build_parser()
        args = parser.parse_args(["--doctor", "edge"])

        command = sync_bookmarks.build_passthrough_command(args)

        self.assertEqual(command, [sys.executable, str(sync_bookmarks.ENGINE), "--doctor", "edge"])

    def test_json_parser_is_available_without_changing_engine_command(self) -> None:
        parser = sync_bookmarks.build_parser()
        args = parser.parse_args(["--from", "chrome", "--to", "edge", "--json"])

        command = sync_bookmarks.build_sync_command(args)

        self.assertTrue(args.json)
        self.assertNotIn("--json", command)

    def test_json_summary_is_structured_without_raw_bookmark_names(self) -> None:
        parser = sync_bookmarks.build_parser()
        args = parser.parse_args(["--from", "chrome", "--to", "edge", "--json"])
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "Strategy: edge:Default -> direct (sync not active)\n"
                "Synced chrome:Default -> edge:Default\n"
                "Backup: ~/Downloads/bookmark-sync-backups/edge.bak\n"
                "Result: 229 bookmarks\n"
                "Verification: target matches source after the stabilization window.\n"
            ),
            stderr="",
        )

        payload = sync_bookmarks.build_json_result(
            args,
            result,
            sync_bookmarks.json_metadata(args),
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source"], "chrome:Default")
        self.assertEqual(payload["targets"], ["edge:Default"])
        self.assertEqual(
            payload["results"],
            [{
                "target": "edge:Default",
                "bookmarks": 229,
                "verification": "target matches source after the stabilization window.",
            }],
        )
        self.assertEqual(payload["backups"], ["~/Downloads/bookmark-sync-backups/edge.bak"])
        self.assertNotIn("bookmark title", payload)


if __name__ == "__main__":
    unittest.main()
