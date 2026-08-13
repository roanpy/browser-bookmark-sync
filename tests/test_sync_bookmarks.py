import importlib.util
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
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
    @staticmethod
    def write_synthetic_chromium_bookmarks(home: Path, browser_dir: str) -> None:
        bookmark_file = home / "Library" / "Application Support" / browser_dir / "Default" / "Bookmarks"
        bookmark_file.parent.mkdir(parents=True)
        root = {
            "children": [
                {
                    "date_added": "13200000000000000",
                    "guid": "synthetic-example-guid",
                    "id": "3",
                    "name": "Synthetic Example",
                    "type": "url",
                    "url": "https://example.invalid/",
                }
            ],
            "date_added": "13200000000000000",
            "date_modified": "13200000000000000",
            "guid": "synthetic-root-guid",
            "id": "1",
            "name": "Bookmarks bar",
            "type": "folder",
        }
        empty_root = {
            **root,
            "children": [],
            "guid": f"{browser_dir}-empty-root-guid",
            "id": "2",
            "name": "Other bookmarks",
        }
        raw = {
            "checksum": "",
            "roots": {"bookmark_bar": root, "other": empty_root, "synced": empty_root},
            "version": 1,
        }
        bookmark_file.write_text(json.dumps(raw), encoding="utf-8")

    def test_default_target_ids_excludes_source(self) -> None:
        self.assertEqual(sync_bookmarks.default_target_ids("chrome:Default"), ["edge:Default", "safari"])
        self.assertEqual(sync_bookmarks.default_target_ids("chrome:Profile 1"), ["edge:Default", "safari"])

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

    def test_build_sync_command_preserves_named_profiles_and_deduplicates_targets(self) -> None:
        parser = sync_bookmarks.build_parser()
        args = parser.parse_args(
            ["--from", "Chrome:Profile 1", "--to", "Edge:Work", "edge:Work", "safari"]
        )

        command = sync_bookmarks.build_sync_command(args)

        self.assertEqual(
            command[2:6],
            ["--source", "chrome:Profile 1", "--targets", "edge:Work,safari"],
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

    def test_build_passthrough_command_for_recovery(self) -> None:
        parser = sync_bookmarks.build_parser()
        args = parser.parse_args(["--recover", "--auto-close"])

        self.assertEqual(
            sync_bookmarks.build_passthrough_command(args),
            [sys.executable, str(sync_bookmarks.ENGINE), "--recover", "--auto-close"],
        )

    def test_recovery_actions_are_mutually_exclusive(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            sync_bookmarks.build_parser().parse_args(["--recover", "--discard-recovery"])

    def test_version_matches_package_version(self) -> None:
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--version"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), f"sync_bookmarks.py {sync_bookmarks.__version__}")

    def test_json_parser_is_available_without_changing_engine_command(self) -> None:
        parser = sync_bookmarks.build_parser()
        args = parser.parse_args(["--from", "chrome", "--to", "edge", "--json"])

        command = sync_bookmarks.build_sync_command(args)

        self.assertTrue(args.json)
        self.assertNotIn("--json", command)

    def test_restore_json_metadata_redacts_home_directory(self) -> None:
        parser = sync_bookmarks.build_parser()
        args = parser.parse_args(
            ["--restore-backup", str(Path.home() / "Downloads" / "private.bak"), "--restore-target", "edge", "--json"]
        )

        metadata = sync_bookmarks.json_metadata(args)

        self.assertEqual(metadata["backup"], "~/Downloads/private.bak")
        self.assertNotIn(str(Path.home()), metadata["backup"])

    def test_json_cli_reports_argument_validation_errors(self) -> None:
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--from", "unknown", "--to", "edge", "--json"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["operation"], "sync")
        self.assertIn("Unsupported browser alias or id", payload["error"])
        self.assertNotIn("Traceback", result.stderr)

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
        self.assertEqual(payload["version"], sync_bookmarks.__version__)
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

    def test_doctor_json_is_structured(self) -> None:
        parser = sync_bookmarks.build_parser()
        args = parser.parse_args(["--doctor", "edge", "--json"])
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "[edge]\n"
                "Current sync state: enabled (bookmarks=on, setup=done)\n"
                "Known cloud issue: yes (cloud reinjection)\n"
                "Stabilization profile: settle=8.0s poll=2.0s stable_passes=2\n"
                "Repair profile: max_attempts=2 retry_wait=3.0s\n"
                "Observations: 3, exact_match=2, external_changes=1, repaired=1, unstable=0\n"
                "Max verification passes: 4\n"
                "Last run: target=edge:Default mode=strict matches=True repairs=1 at=2026-08-11T09:50:13\n"
            ),
            stderr="",
        )

        payload = sync_bookmarks.build_json_result(args, result, sync_bookmarks.json_metadata(args))

        report = payload["doctor"][0]
        self.assertEqual(report["browser"], "edge")
        self.assertTrue(report["sync_enabled"])
        self.assertTrue(report["cloud_issue"])
        self.assertEqual(report["observations"]["external_changes"], 1)
        self.assertEqual(report["last_run"]["target"], "edge:Default")

    def test_list_backups_json_works_without_browser_stores(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            backup_dir = home / "backups"
            backup_dir.mkdir()
            (backup_dir / "20260810-100000-000000-safari.plist").write_bytes(b"safari")
            (backup_dir / "20260811-100000-000000-edge_Default.bak").write_bytes(b"edge")
            environment = os.environ.copy()
            environment.update(
                {
                    "BOOKMARK_SYNC_HOME": str(home),
                    "BOOKMARK_SYNC_BACKUP_DIR": str(backup_dir),
                }
            )

            result = subprocess.run(
                [sys.executable, str(MODULE_PATH), "--list-backups", "--json"],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["operation"], "list_backups")
        self.assertEqual([item["target"] for item in payload["backup_files"]], ["edge:Default", "safari"])
        self.assertEqual(payload["backup_files"][0]["size"], 4)

    def test_json_cli_keeps_stdout_machine_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            self.write_synthetic_chromium_bookmarks(home, "Google/Chrome")
            self.write_synthetic_chromium_bookmarks(home, "Microsoft Edge")
            environment = os.environ.copy()
            environment.update(
                {
                    "BOOKMARK_SYNC_HOME": str(home),
                    "BOOKMARK_SYNC_DATA_DIR": str(home / "state"),
                    "BOOKMARK_SYNC_BACKUP_DIR": str(home / "backups"),
                }
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "--from",
                    "chrome",
                    "--to",
                    "edge",
                    "--mode",
                    "preview",
                    "--json",
                ],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["schema"], 1)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["source"], "chrome:Default")
            self.assertEqual(payload["targets"], ["edge:Default"])
            self.assertNotIn("Synthetic Example", result.stdout)
            self.assertNotIn("https://example.invalid/", result.stdout)
            self.assertIn("Synthetic Example", result.stderr)


if __name__ == "__main__":
    unittest.main()
