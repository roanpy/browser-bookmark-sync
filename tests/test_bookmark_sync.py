import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "bookmark_sync.py"
SPEC = importlib.util.spec_from_file_location("bookmark_sync", MODULE_PATH)
bookmark_sync = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = bookmark_sync
SPEC.loader.exec_module(bookmark_sync)


def make_store(store_id: str, browser: str) -> bookmark_sync.BrowserStore:
    return bookmark_sync.BrowserStore(
        id=store_id,
        browser=browser,
        profile="Default",
        format="chromium" if browser != "safari" else "safari",
        path=Path(f"/tmp/{store_id.replace(':', '_')}"),
        label=store_id,
    )


def make_snapshot(store: bookmark_sync.BrowserStore, portable: dict[str, list[dict]]) -> bookmark_sync.BookmarkSnapshot:
    bookmark_count, folder_count = bookmark_sync.count_portable(portable)
    return bookmark_sync.BookmarkSnapshot(
        store=store,
        portable=portable,
        raw={},
        modified_ts=0.0,
        bookmark_count=bookmark_count,
        folder_count=folder_count,
    )


class BookmarkSyncTests(unittest.TestCase):
    def test_display_path_redacts_home_directory(self) -> None:
        self.assertEqual(bookmark_sync.display_path(bookmark_sync.HOME / "Downloads" / "backup.bak"), "~/Downloads/backup.bak")

    def test_supported_browsers_come_from_registry(self) -> None:
        self.assertEqual(
            bookmark_sync.supported_browsers(),
            ["chrome", "edge", "brave", "vivaldi", "opera", "safari"],
        )

    def test_backup_target_never_reuses_a_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target_path = Path(tmpdir) / "Bookmarks"
            target_path.write_text("bookmarks")
            store = bookmark_sync.BrowserStore(
                id="brave:Default",
                browser="brave",
                profile="Default",
                format="chromium",
                path=target_path,
                label="brave:Default",
            )
            backup_dir = Path(tmpdir) / "backups"
            with mock.patch.object(bookmark_sync, "DOWNLOAD_BACKUP_DIR", backup_dir):
                first = bookmark_sync.backup_target(store)
                second = bookmark_sync.backup_target(store)

        self.assertNotEqual(first.name, second.name)

    def test_atomic_write_removes_temporary_file_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "Bookmarks"
            target.write_bytes(b"original")
            with (
                mock.patch.object(bookmark_sync.os, "replace", side_effect=OSError("replace failed")),
                self.assertRaises(OSError),
            ):
                bookmark_sync.atomic_write_bytes(target, b"replacement")

            self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual(list(Path(tmpdir).iterdir()), [target])

    def test_restore_store_backup_verifies_and_keeps_a_rollback(self) -> None:
        def chromium_raw(name: str, url: str) -> dict:
            return {
                "roots": {
                    "bookmark_bar": {
                        "children": [{"type": "url", "name": name, "url": url}],
                        "type": "folder",
                    },
                    "other": {"children": [], "type": "folder"},
                    "synced": {"children": [], "type": "folder"},
                },
                "version": 1,
            }

        with tempfile.TemporaryDirectory() as tmpdir:
            target_path = Path(tmpdir) / "Bookmarks"
            backup_path = Path(tmpdir) / "restore.bak"
            current_bytes = json.dumps(chromium_raw("Current", "https://current")).encode()
            target_path.write_bytes(current_bytes)
            backup_path.write_text(json.dumps(chromium_raw("Restored", "https://restored")))
            store = bookmark_sync.BrowserStore(
                id="brave:Default",
                browser="brave",
                profile="Default",
                format="chromium",
                path=target_path,
                label="brave:Default",
            )
            with mock.patch.object(bookmark_sync, "DOWNLOAD_BACKUP_DIR", Path(tmpdir) / "backups"):
                rollback_path, restored = bookmark_sync.restore_store_backup(backup_path, store)

            self.assertEqual(rollback_path.read_bytes(), current_bytes)
            self.assertEqual(rollback_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(restored.portable["bar"][0]["name"], "Restored")

    def test_cloud_purge_requires_explicit_consent(self) -> None:
        with self.assertRaises(SystemExit):
            bookmark_sync.require_cloud_purge_consent(False)

        bookmark_sync.require_cloud_purge_consent(True)

    def test_cloud_purge_requires_backup(self) -> None:
        with self.assertRaises(SystemExit):
            bookmark_sync.require_cloud_purge_backup(False)

        bookmark_sync.require_cloud_purge_backup(True)

    def test_failed_cloud_purge_restores_local_backup(self) -> None:
        raw = {
            "roots": {
                "bookmark_bar": {"children": [], "type": "folder"},
                "other": {"children": [], "type": "folder"},
                "synced": {"children": [], "type": "folder"},
            },
            "version": 1,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            target_path = Path(tmpdir) / "Bookmarks"
            backup_path = Path(tmpdir) / "before-purge.bak"
            target_path.write_text("{}")
            backup_path.write_text(json.dumps(raw))
            store = bookmark_sync.BrowserStore(
                id="edge:Default",
                browser="edge",
                profile="Default",
                format="chromium",
                path=target_path,
                label="edge:Default",
            )
            with mock.patch.object(bookmark_sync, "ensure_browsers_closed"):
                bookmark_sync.rollback_failed_cloud_purge(store, backup_path)

            self.assertEqual(target_path.read_bytes(), backup_path.read_bytes())

    def test_attach_recorded_baseline_uses_previous_signature(self) -> None:
        source = make_store("chrome:Default", "chrome")
        target = make_store("edge:Default", "edge")
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            with mock.patch.object(bookmark_sync, "STATE_FILE", state_path):
                bookmark_sync.save_state(
                    {
                        bookmark_sync.SYNC_BASELINES_STATE_KEY: {
                            bookmark_sync.state_key(source, target, "strict"): {
                                "target_signature": "previous-signature"
                            }
                        }
                    }
                )
                verification = bookmark_sync.SyncVerification(
                    reloaded=make_snapshot(target, {"bar": [], "menu": [], "synced": []}),
                    same_count=True,
                    same_portable=False,
                    source_signature="source-signature",
                    target_signature="new-signature",
                )

                record = bookmark_sync.get_recorded_baseline(source, target, "strict")
                bookmark_sync.attach_recorded_baseline(record, verification)

                self.assertEqual(verification.baseline_signature, "previous-signature")
                self.assertFalse(verification.matches_recorded_baseline)

    def test_print_order_warning_is_specific_to_actual_difference(self) -> None:
        source_store = make_store("chrome:Default", "chrome")
        target_store = make_store("safari", "safari")
        portable = {"bar": [{"type": "url", "name": "A", "url": "https://a"}], "menu": [], "synced": []}
        source = make_snapshot(source_store, portable)
        reloaded = make_snapshot(target_store, portable)
        verification = bookmark_sync.SyncVerification(
            reloaded=reloaded,
            same_count=True,
            same_portable=False,
            source_signature="one",
            target_signature="two",
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bookmark_sync.print_order_warning(source, verification)

        self.assertIn("target still differs from source", output.getvalue())
        self.assertNotIn("bookmark bar order still differs", output.getvalue())

    def test_edge_diagnostics_do_not_print_bookmark_data(self) -> None:
        source = make_snapshot(make_store("chrome:Default", "chrome"), {"bar": [], "menu": [], "synced": []})
        target = make_store("edge:Default", "edge")
        reloaded = make_snapshot(
            target,
            {"bar": [{"type": "url", "name": "Private title", "url": "https://private.invalid/path"}], "menu": [], "synced": []},
        )
        verification = bookmark_sync.SyncVerification(
            reloaded=reloaded,
            same_count=False,
            same_portable=False,
            source_signature="source",
            target_signature="target",
        )
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            bookmark_sync.explain_edge_cloud_reinjection(source, verification)

        self.assertNotIn("Private title", output.getvalue())
        self.assertNotIn("private.invalid", output.getvalue())
        self.assertIn("omitted", output.getvalue())

    def test_edge_repair_retries_after_mismatch(self) -> None:
        source_store = make_store("chrome:Default", "chrome")
        target_store = make_store("edge:Default", "edge")
        portable = {"bar": [{"type": "url", "name": "A", "url": "https://a"}], "menu": [], "synced": []}
        source = make_snapshot(source_store, portable)
        mismatch = bookmark_sync.SyncVerification(
            reloaded=make_snapshot(target_store, {"bar": [], "menu": [], "synced": []}),
            same_count=False,
            same_portable=False,
            source_signature="source",
            target_signature="target-1",
        )
        repaired = bookmark_sync.SyncVerification(
            reloaded=make_snapshot(target_store, portable),
            same_count=True,
            same_portable=True,
            source_signature="source",
            target_signature="target-2",
        )

        with (
            mock.patch.object(bookmark_sync, "write_target_store") as write_target_store,
            mock.patch.object(bookmark_sync, "verify_snapshot_with_stabilization", side_effect=[repaired]) as verify_snapshot,
            mock.patch.object(bookmark_sync.time, "sleep"),
        ):
            final = bookmark_sync.repair_sync_if_needed(source, target_store, "strict", mismatch)

        write_target_store.assert_called_once_with(source, target_store, "strict")
        verify_snapshot.assert_called_once_with(target_store, source)
        self.assertTrue(final.same_portable)
        self.assertEqual(final.repair_attempts, 1)

    def test_normalize_browser_filter_accepts_supported_values(self) -> None:
        self.assertEqual(bookmark_sync.normalize_browser_filter(None), "all")
        self.assertEqual(bookmark_sync.normalize_browser_filter("EDGE"), "edge")
        self.assertEqual(bookmark_sync.normalize_browser_filter(" safari "), "safari")

    def test_calibrate_browser_profiles_writes_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            with mock.patch.object(bookmark_sync, "STATE_FILE", state_path):
                bookmark_sync.save_state(
                    {
                        bookmark_sync.SYNC_OBSERVATIONS_STATE_KEY: [
                            {
                                "target_browser": "edge",
                                "verification_passes": 4,
                                "observed_external_changes": True,
                                "stabilized": True,
                                "repair_attempts": 1,
                            }
                        ]
                    }
                )
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    bookmark_sync.calibrate_browser_profiles("edge")
                state = bookmark_sync.load_state()
                overrides = state[bookmark_sync.BROWSER_PROFILE_OVERRIDES_STATE_KEY]

                self.assertIn("edge", overrides["stabilization"])
                self.assertGreaterEqual(overrides["stabilization"]["edge"]["settle_seconds"], 8.0)
                self.assertEqual(overrides["repair"]["edge"]["max_attempts"], 2)
                self.assertIn("[edge] calibrated", output.getvalue())

    def test_apply_portable_to_chromium_preserves_matching_ids_and_guids(self) -> None:
        source_store = make_store("chrome:Default", "chrome")
        source = make_snapshot(
            source_store,
            {
                "bar": [
                    {"type": "url", "name": "A", "url": "https://a"},
                    {
                        "type": "folder",
                        "name": "Work",
                        "children": [{"type": "url", "name": "B", "url": "https://b"}],
                    },
                ],
                "menu": [],
                "synced": [],
            },
        )
        target_raw = {
            "roots": {
                "bookmark_bar": {
                    "children": [
                        {"type": "url", "name": "A", "url": "https://a", "id": "10", "guid": "guid-a", "date_added": "1"},
                        {
                            "type": "folder",
                            "name": "Work",
                            "id": "11",
                            "guid": "guid-work",
                            "date_added": "2",
                            "children": [
                                {"type": "url", "name": "B", "url": "https://b", "id": "12", "guid": "guid-b", "date_added": "3"}
                            ],
                        },
                    ]
                },
                "other": {"children": []},
                "synced": {"children": []},
            }
        }

        result = bookmark_sync.apply_portable_to_chromium(source, target_raw, "strict")
        bar_children = result["roots"]["bookmark_bar"]["children"]

        self.assertEqual(bar_children[0]["id"], "10")
        self.assertEqual(bar_children[0]["guid"], "guid-a")
        self.assertEqual(bar_children[1]["id"], "11")
        self.assertEqual(bar_children[1]["guid"], "guid-work")
        self.assertEqual(bar_children[1]["children"][0]["id"], "12")
        self.assertEqual(bar_children[1]["children"][0]["guid"], "guid-b")

    def test_apply_portable_to_safari_preserves_matching_uuids(self) -> None:
        source_store = make_store("safari", "safari")
        source = make_snapshot(
            source_store,
            {
                "bar": [
                    {"type": "url", "name": "A", "url": "https://a"},
                    {
                        "type": "folder",
                        "name": "Work",
                        "children": [{"type": "url", "name": "B", "url": "https://b"}],
                    },
                ],
                "menu": [],
                "synced": [],
            },
        )
        target_raw = {
            "Children": [
                {
                    "Title": "BookmarksBar",
                    "WebBookmarkType": "WebBookmarkTypeList",
                    "Children": [
                        {
                            "WebBookmarkType": "WebBookmarkTypeLeaf",
                            "URIDictionary": {"title": "A"},
                            "URLString": "https://a",
                            "WebBookmarkUUID": "UUID-A",
                        },
                        {
                            "WebBookmarkType": "WebBookmarkTypeList",
                            "Title": "Work",
                            "WebBookmarkUUID": "UUID-WORK",
                            "Children": [
                                {
                                    "WebBookmarkType": "WebBookmarkTypeLeaf",
                                    "URIDictionary": {"title": "B"},
                                    "URLString": "https://b",
                                    "WebBookmarkUUID": "UUID-B",
                                }
                            ],
                        },
                    ],
                },
                {
                    "Title": "BookmarksMenu",
                    "WebBookmarkType": "WebBookmarkTypeList",
                    "Children": [],
                },
            ]
        }

        result = bookmark_sync.apply_portable_to_safari(source, target_raw, "strict")
        bar_children = bookmark_sync.find_safari_root(result, "BookmarksBar")["Children"]

        self.assertEqual(bar_children[0]["WebBookmarkUUID"], "UUID-A")
        self.assertEqual(bar_children[1]["WebBookmarkUUID"], "UUID-WORK")
        self.assertEqual(bar_children[1]["Children"][0]["WebBookmarkUUID"], "UUID-B")

    def test_clear_chromium_bookmark_file_clears_bookmark_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_store("edge:Default", "edge")
            store.path = Path(tmpdir) / "Bookmarks"
            store.path.write_text(
                """
{
  "roots": {
    "bookmark_bar": {"children": [{"type": "url", "name": "A", "url": "https://a"}]},
    "other": {"children": [{"type": "url", "name": "B", "url": "https://b"}]},
    "synced": {"children": [{"type": "url", "name": "C", "url": "https://c"}]}
  },
  "version": 1
}
""".strip()
            )

            bookmark_sync.clear_chromium_bookmark_file(store)

            raw = json.loads(store.path.read_text())
            self.assertEqual(raw["roots"]["bookmark_bar"]["children"], [])
            self.assertEqual(raw["roots"]["other"]["children"], [])
            self.assertEqual(raw["roots"]["synced"]["children"], [])
            self.assertEqual(raw["version"], 1)

    def test_chromium_sync_state_detects_enabled_edge_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir) / "Default"
            profile_dir.mkdir(parents=True)
            store = bookmark_sync.BrowserStore(
                id="edge:Default",
                browser="edge",
                profile="Default",
                format="chromium",
                path=profile_dir / "Bookmarks",
                label="edge:Default",
            )
            (profile_dir / "Preferences").write_text(
                json.dumps({"sync": {"bookmarks": True, "has_setup_completed": True}})
            )
            (Path(tmpdir) / "Local State").write_text(
                json.dumps({"profile": {"info_cache": {"Default": {"edge_sync_enabled": True}}}})
            )

            enabled, reason = bookmark_sync.chromium_sync_state(store)

        self.assertTrue(enabled)
        self.assertIn("bookmarks=on", reason)
        self.assertIn("setup=done", reason)

    def test_choose_sync_strategy_uses_direct_for_edge_with_sync_without_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir) / "Default"
            profile_dir.mkdir(parents=True)
            store = bookmark_sync.BrowserStore(
                id="edge:Default",
                browser="edge",
                profile="Default",
                format="chromium",
                path=profile_dir / "Bookmarks",
                label="edge:Default",
            )
            (profile_dir / "Preferences").write_text(
                json.dumps({"sync": {"bookmarks": True, "has_setup_completed": True}})
            )
            (Path(tmpdir) / "Local State").write_text(
                json.dumps({"profile": {"info_cache": {"Default": {"edge_sync_enabled": True}}}})
            )
            state_path = Path(tmpdir) / "state.json"
            with mock.patch.object(bookmark_sync, "STATE_FILE", state_path):
                decision = bookmark_sync.choose_sync_strategy(store, "auto")

        self.assertEqual(decision.strategy, "direct")
        self.assertIn("sync enabled but no issue recorded yet", decision.reason)

    def test_choose_sync_strategy_uses_cloud_safe_for_edge_with_recorded_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir) / "Default"
            profile_dir.mkdir(parents=True)
            store = bookmark_sync.BrowserStore(
                id="edge:Default",
                browser="edge",
                profile="Default",
                format="chromium",
                path=profile_dir / "Bookmarks",
                label="edge:Default",
            )
            (profile_dir / "Preferences").write_text(
                json.dumps({"sync": {"bookmarks": True, "has_setup_completed": True}})
            )
            (Path(tmpdir) / "Local State").write_text(
                json.dumps({"profile": {"info_cache": {"Default": {"edge_sync_enabled": True}}}})
            )
            state_path = Path(tmpdir) / "state.json"
            with mock.patch.object(bookmark_sync, "STATE_FILE", state_path):
                bookmark_sync.mark_cloud_issue(store, "post-open drift detected")
                decision = bookmark_sync.choose_sync_strategy(store, "auto")

        self.assertEqual(decision.strategy, "cloud-safe")
        self.assertIn("known cloud reinjection issue", decision.reason)

    def test_choose_sync_strategy_uses_direct_for_edge_without_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir) / "Default"
            profile_dir.mkdir(parents=True)
            store = bookmark_sync.BrowserStore(
                id="edge:Default",
                browser="edge",
                profile="Default",
                format="chromium",
                path=profile_dir / "Bookmarks",
                label="edge:Default",
            )
            (profile_dir / "Preferences").write_text(
                json.dumps({"sync": {"bookmarks": False, "has_setup_completed": True}})
            )
            (Path(tmpdir) / "Local State").write_text(
                json.dumps({"profile": {"info_cache": {"Default": {"edge_sync_enabled": False}}}})
            )
            state_path = Path(tmpdir) / "state.json"
            with mock.patch.object(bookmark_sync, "STATE_FILE", state_path):
                decision = bookmark_sync.choose_sync_strategy(store, "auto")

        self.assertEqual(decision.strategy, "direct")
        self.assertIn("sync not active", decision.reason)

    def test_choose_sync_strategy_uses_direct_for_chrome_without_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir) / "Default"
            profile_dir.mkdir(parents=True)
            store = bookmark_sync.BrowserStore(
                id="chrome:Default",
                browser="chrome",
                profile="Default",
                format="chromium",
                path=profile_dir / "Bookmarks",
                label="chrome:Default",
            )
            (profile_dir / "Preferences").write_text(
                json.dumps(
                    {
                        "google": {"services": {"consented_to_sync": False}},
                        "sync": {
                            "data_type_status_for_sync_to_signin": {"bookmarks": False},
                            "feature_status_for_sync_to_signin": 1,
                            "has_setup_completed": False,
                        },
                    }
                )
            )
            state_path = Path(tmpdir) / "state.json"
            with mock.patch.object(bookmark_sync, "STATE_FILE", state_path):
                decision = bookmark_sync.choose_sync_strategy(store, "auto")

        self.assertEqual(decision.strategy, "direct")
        self.assertIn("sync not active", decision.reason)

    def test_create_chromium_bookmark_purge_extension_writes_manifest_and_worker(self) -> None:
        with bookmark_sync.create_chromium_bookmark_purge_extension(30.0) as extension_dir:
            extension_path = Path(extension_dir)
            manifest = json.loads((extension_path / "manifest.json").read_text())
            worker = (extension_path / "service-worker.js").read_text()

        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(manifest["name"], "Bookmark Sync Temporary Purge")
        self.assertIn("bookmarks", manifest["permissions"])
        self.assertIn("alarms", manifest["permissions"])
        self.assertIn("chrome.bookmarks.getTree", worker)
        self.assertIn("chrome.bookmarks.onCreated", worker)

    def test_chromium_api_clear_then_sync_runs_purge_then_sync(self) -> None:
        source_store = make_store("chrome:Default", "chrome")
        target_store = make_store("edge:Default", "edge")
        source = make_snapshot(source_store, {"bar": [{"type": "url", "name": "A", "url": "https://a"}], "menu": [], "synced": []})
        cleared = make_snapshot(target_store, {"bar": [], "menu": [], "synced": []})
        verification = bookmark_sync.SyncVerification(
            reloaded=source,
            same_count=True,
            same_portable=True,
            source_signature="source",
            target_signature="target",
        )

        with tempfile.TemporaryDirectory() as extdir:
            with (
                mock.patch.object(bookmark_sync, "backup_target", return_value=Path("/tmp/clear-backup")),
                mock.patch.object(
                    bookmark_sync,
                    "create_chromium_bookmark_purge_extension",
                    return_value=contextlib.nullcontext(extdir),
                ),
                mock.patch.object(bookmark_sync, "open_browser") as open_browser,
                mock.patch.object(bookmark_sync, "wait_for_bookmark_count", return_value=cleared) as wait_for_count,
                mock.patch.object(bookmark_sync, "load_snapshot_with_retry", return_value=cleared),
                mock.patch.object(bookmark_sync.time, "sleep"),
                mock.patch.object(bookmark_sync, "ensure_browsers_closed") as close_browser,
                mock.patch.object(
                    bookmark_sync,
                    "sync_store",
                    return_value=(Path("/tmp/sync-backup"), verification),
                ) as sync_store,
            ):
                clear_backup, sync_backup, result = bookmark_sync.reset_chromium_cloud_via_api_then_sync(
                    source,
                    target_store,
                    "strict",
                    purge_window_seconds=60.0,
                    settle_wait_seconds=10.0,
                )

        self.assertEqual(clear_backup, Path("/tmp/clear-backup"))
        self.assertEqual(sync_backup, Path("/tmp/sync-backup"))
        self.assertTrue(result.same_portable)
        open_browser.assert_called_once()
        self.assertIn(f"--profile-directory={target_store.profile}", open_browser.call_args.kwargs["extra_args"])
        self.assertIn(f"--load-extension={extdir}", open_browser.call_args.kwargs["extra_args"])
        wait_for_count.assert_called_once_with(
            target_store,
            expected_count=0,
            timeout_seconds=60.0,
            poll_interval_seconds=bookmark_sync.EDGE_API_POLL_INTERVAL_SECONDS,
        )
        close_browser.assert_called_once_with({"edge"}, auto_close=True)
        sync_store.assert_called_once_with(source, target_store, "strict", backup_enabled=True)

    def test_chromium_api_purge_rolls_back_after_failure(self) -> None:
        source_store = make_store("chrome:Default", "chrome")
        target_store = make_store("edge:Default", "edge")
        source = make_snapshot(source_store, {"bar": [], "menu": [], "synced": []})
        not_cleared = make_snapshot(
            target_store,
            {"bar": [{"type": "url", "name": "A", "url": "https://a"}], "menu": [], "synced": []},
        )
        backup_path = Path("/tmp/clear-backup")

        with tempfile.TemporaryDirectory() as extdir:
            with (
                mock.patch.object(bookmark_sync, "backup_target", return_value=backup_path),
                mock.patch.object(
                    bookmark_sync,
                    "create_chromium_bookmark_purge_extension",
                    return_value=contextlib.nullcontext(extdir),
                ),
                mock.patch.object(bookmark_sync, "open_browser"),
                mock.patch.object(bookmark_sync, "wait_for_bookmark_count", return_value=not_cleared),
                mock.patch.object(bookmark_sync, "rollback_failed_cloud_purge") as rollback,
                self.assertRaises(SystemExit),
            ):
                bookmark_sync.reset_chromium_cloud_via_api_then_sync(
                    source,
                    target_store,
                    "strict",
                    purge_window_seconds=1.0,
                    settle_wait_seconds=0.0,
                )

        rollback.assert_called_once_with(target_store, backup_path)

    def test_should_auto_probe_after_direct_requires_sync_without_recorded_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir) / "Default"
            profile_dir.mkdir(parents=True)
            store = bookmark_sync.BrowserStore(
                id="chrome:Default",
                browser="chrome",
                profile="Default",
                format="chromium",
                path=profile_dir / "Bookmarks",
                label="chrome:Default",
            )
            (profile_dir / "Preferences").write_text(
                json.dumps(
                    {
                        "google": {"services": {"consented_to_sync": True}},
                        "sync": {
                            "has_setup_completed": True,
                            "feature_status_for_sync_to_signin": 3,
                            "data_type_status_for_sync_to_signin": {"bookmarks": True},
                        },
                    }
                )
            )

            self.assertTrue(bookmark_sync.should_auto_probe_after_direct(store, "auto"))
            self.assertFalse(bookmark_sync.should_auto_probe_after_direct(store, "direct"))

    def test_post_open_check_waits_full_window_before_repair(self) -> None:
        source_store = make_store("chrome:Default", "chrome")
        target_store = make_store("edge:Default", "edge")
        source = make_snapshot(source_store, {"bar": [], "menu": [], "synced": []})
        drift = bookmark_sync.SyncVerification(
            reloaded=make_snapshot(target_store, {"bar": [{"type": "url", "name": "X", "url": "https://x"}], "menu": [], "synced": []}),
            same_count=False,
            same_portable=False,
            source_signature="s",
            target_signature="t1",
        )
        repaired = bookmark_sync.SyncVerification(
            reloaded=make_snapshot(target_store, {"bar": [], "menu": [], "synced": []}),
            same_count=True,
            same_portable=True,
            source_signature="s",
            target_signature="t2",
        )

        with (
            mock.patch.dict(bookmark_sync.POST_OPEN_CHECK_PROFILES, {"edge": {"delay_seconds": 1.0, "poll_interval": 1.0, "max_checks": 3}}, clear=False),
            mock.patch.object(bookmark_sync, "open_browser"),
            mock.patch.object(bookmark_sync.time, "sleep") as sleep_mock,
            mock.patch.object(bookmark_sync, "verify_snapshot_counts", side_effect=[drift, drift, drift]) as verify_mock,
            mock.patch.object(bookmark_sync, "ensure_browsers_closed") as close_mock,
            mock.patch.object(bookmark_sync, "sync_store", return_value=(Path("/tmp/backup"), repaired)) as sync_mock,
        ):
            drifted, backup_path, verification = bookmark_sync.run_post_open_check(source, target_store, "strict")

        self.assertTrue(drifted)
        self.assertEqual(backup_path, Path("/tmp/backup"))
        self.assertTrue(verification.same_portable)
        self.assertEqual(verify_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 3)
        close_mock.assert_called_once_with({"edge"}, auto_close=True)
        sync_mock.assert_called_once_with(source, target_store, "strict", backup_enabled=True)

    def test_sync_store_skips_backup_when_disabled(self) -> None:
        source_store = make_store("chrome:Default", "chrome")
        target_store = make_store("edge:Default", "edge")
        portable = {"bar": [{"type": "url", "name": "A", "url": "https://a"}], "menu": [], "synced": []}
        source = make_snapshot(source_store, portable)
        verification = bookmark_sync.SyncVerification(
            reloaded=make_snapshot(target_store, portable),
            same_count=True,
            same_portable=True,
            source_signature="source",
            target_signature="target",
        )

        with (
            mock.patch.object(bookmark_sync, "backup_target") as backup_target,
            mock.patch.object(bookmark_sync, "write_target_store"),
            mock.patch.object(bookmark_sync, "verify_snapshot_with_stabilization", return_value=verification),
            mock.patch.object(bookmark_sync, "repair_sync_if_needed", return_value=verification),
            mock.patch.object(bookmark_sync, "record_sync_baseline"),
            mock.patch.object(bookmark_sync, "record_sync_observation"),
        ):
            backup_path, result = bookmark_sync.sync_store(source, target_store, "strict", backup_enabled=False)

        backup_target.assert_not_called()
        self.assertIsNone(backup_path)
        self.assertTrue(result.same_portable)


if __name__ == "__main__":
    unittest.main()
