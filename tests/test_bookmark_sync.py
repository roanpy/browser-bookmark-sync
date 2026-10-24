import contextlib
import http.client
import importlib.util
import io
import json
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit


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


def run_chromium_worker_harness(worker: str, options: dict | None = None) -> dict:
    if shutil.which("node") is None:
        raise unittest.SkipTest("node is required to execute the generated service worker")
    harness = r"""
const worker = __WORKER__;
const options = __OPTIONS__;
const storage = options.initialState || {};
const calls = [];
const phases = [];
let unauthorized = 0;
let finished = false;
let sequence = 0;
let reversed = false;
const tree = [{ id: "0", children: [
  { id: "1", title: "Bar", children: [{ id: "old", title: "Old", url: "https://old.test" }] },
  { id: "2", title: "Other", children: [] },
  ...(options.noMobile ? [] : [{ id: "3", title: "Mobile", children: [] }]),
] }];
const listener = { addListener() {} };
function checkAuthorization() { if (!storage.startupAuthorized) unauthorized += 1; }
function findNode(id, nodes = tree) {
  for (const node of nodes) {
    if (node.id === id) return node;
    const nested = findNode(id, node.children || []);
    if (nested) return nested;
  }
}
function removeNode(id, nodes = tree) {
  const index = nodes.findIndex((node) => node.id === id);
  if (index >= 0) { nodes.splice(index, 1); return true; }
  return nodes.some((node) => removeNode(id, node.children || []));
}
function finish() {
  if (finished) return;
  finished = true;
  setTimeout(() => {
    process.stdout.write(JSON.stringify({ tree, calls, phases, storage, unauthorized }));
  }, 10);
}
globalThis.fetch = async (_url, request) => {
  if (!request.body) return { ok: options.authorize !== false };
  const status = JSON.parse(request.body);
  phases.push(status);
  if (status.phase === "rebuilt" || status.phase === "failed") finish();
  return { ok: true, json: async () => ({ command: "rebuild" }) };
};
globalThis.chrome = {
  runtime: { lastError: null, onInstalled: listener, onStartup: listener },
  alarms: { create() {}, clear() {}, onAlarm: listener },
  storage: { local: {
    get(keys, done) { done(Object.fromEntries(keys.map((key) => [key, storage[key]]))); },
    set(values, done) {
      Object.assign(storage, values);
      if (values.phase === "failed" && !storage.startupAuthorized) finish();
      if (done) done();
    },
  } },
  bookmarks: {
    getTree(done) {
      checkAuthorization();
      if (options.reverseFinal && calls.length && !reversed) {
        tree[0].children[0].children.reverse();
        reversed = true;
      }
      setImmediate(() => done(JSON.parse(JSON.stringify(tree))));
    },
    remove(id, done) { checkAuthorization(); removeNode(id); setImmediate(done); },
    removeTree(id, done) { checkAuthorization(); removeNode(id); setImmediate(done); },
    create(details, done) {
      checkAuthorization();
      calls.push({ ...details });
      if (options.hangCreate) return;
      if (options.createError) {
        chrome.runtime.lastError = { message: "synthetic sensitive API error" };
        done();
        chrome.runtime.lastError = null;
        return;
      }
      const node = { id: `new-${++sequence}`, title: details.title,
        ...(details.url === undefined ? { children: [] } : { url: details.url }) };
      if (options.dropCreateAt !== calls.length) findNode(details.parentId).children.splice(details.index, 0, node);
      setImmediate(() => done(node));
    },
    onCreated: listener, onChanged: listener, onMoved: listener, onChildrenReordered: listener,
  },
};
new Function(worker)();
setTimeout(() => { if (!finished) { console.error("worker did not finish"); process.exit(2); } }, 4500).unref();
"""
    harness = harness.replace("__WORKER__", json.dumps(worker)).replace("__OPTIONS__", json.dumps(options or {}))
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=6, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class BookmarkSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.runtime_dir.cleanup)
        runtime = Path(self.runtime_dir.name)
        patcher = mock.patch.multiple(
            bookmark_sync,
            DATA_DIR=runtime,
            STATE_FILE=runtime / "state.json",
            LOCK_FILE=runtime / "operation.lock",
            DOWNLOAD_BACKUP_DIR=runtime / "backups",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_display_path_redacts_home_directory(self) -> None:
        self.assertEqual(bookmark_sync.display_path(bookmark_sync.HOME / "Downloads" / "backup.bak"), "~/Downloads/backup.bak")

    def test_supported_browsers_come_from_registry(self) -> None:
        self.assertEqual(
            bookmark_sync.supported_browsers(),
            ["chrome", "edge", "brave", "vivaldi", "opera", "safari"],
        )

    def test_automatic_browser_close_fails_if_main_process_remains(self) -> None:
        with (
            mock.patch.object(bookmark_sync, "running_browsers", return_value=["edge"]),
            mock.patch.object(bookmark_sync, "quit_browser") as quit_browser,
            mock.patch.object(bookmark_sync.time, "sleep"),
            self.assertRaisesRegex(SystemExit, "Could not close browsers automatically"),
        ):
            bookmark_sync.ensure_browsers_closed({"edge"}, auto_close=True)

        quit_browser.assert_called_once_with("edge")

    def test_automatic_browser_close_waits_until_main_process_exits(self) -> None:
        with (
            mock.patch.object(bookmark_sync, "running_browsers", side_effect=[["edge"], ["edge"], []]),
            mock.patch.object(bookmark_sync, "quit_browser") as quit_browser,
            mock.patch.object(bookmark_sync.time, "sleep"),
        ):
            bookmark_sync.ensure_browsers_closed({"edge"}, auto_close=True)

        quit_browser.assert_called_once_with("edge")

    def test_argument_based_browser_launch_returns_process_for_startup_verification(self) -> None:
        executable = Path("/Applications/Edge.app/Contents/MacOS/Edge")
        with (
            mock.patch.object(bookmark_sync, "browser_executable_path", return_value=executable),
            mock.patch.object(bookmark_sync.subprocess, "Popen") as popen,
        ):
            process = bookmark_sync.open_browser("edge", ["--no-first-run"])

        self.assertIs(process, popen.return_value)
        popen.assert_called_once_with(
            [str(executable), "--no-first-run"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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

    def test_snapshot_loaders_reject_missing_required_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            chromium_path = root / "Bookmarks"
            safari_path = root / "Bookmarks.plist"
            chromium_path.write_text("{}")
            safari_path.write_bytes(plistlib.dumps({}))
            chromium = make_store("chrome:Default", "chrome")
            safari = make_store("safari", "safari")
            chromium.path = chromium_path
            safari.path = safari_path

            with self.assertRaisesRegex(SystemExit, "missing required roots"):
                bookmark_sync.load_snapshot(chromium)
            with self.assertRaisesRegex(SystemExit, "missing BookmarksBar or BookmarksMenu roots"):
                bookmark_sync.load_snapshot(safari)

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

    def test_atomic_write_syncs_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "Bookmarks"
            with mock.patch.object(bookmark_sync, "sync_directory") as sync_directory:
                bookmark_sync.atomic_write_bytes(target, b"replacement")

            sync_directory.assert_called_once_with(target.parent)

    def test_atomic_write_refuses_symlink_and_hardlink_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            original = root / "original"
            original.write_bytes(b"private")
            symlink = root / "symlink"
            symlink.symlink_to(original)
            hardlink = root / "hardlink"
            hardlink.hardlink_to(original)

            with self.assertRaisesRegex(SystemExit, "linked or non-regular"):
                bookmark_sync.atomic_write_bytes(symlink, b"replacement")
            with self.assertRaisesRegex(SystemExit, "linked or non-regular"):
                bookmark_sync.atomic_write_bytes(hardlink, b"replacement")
            self.assertEqual(original.read_bytes(), b"private")

    def test_invalid_runtime_state_blocks_writes(self) -> None:
        bookmark_sync.STATE_FILE.write_text("not-json")

        with self.assertRaisesRegex(SystemExit, "Writes are blocked"):
            bookmark_sync.require_no_pending_operation()

    def test_invalid_runtime_state_does_not_block_read_only_list(self) -> None:
        bookmark_sync.STATE_FILE.write_text("not-json")
        snapshot = make_snapshot(make_store("chrome:Default", "chrome"), {"bar": [], "menu": [], "synced": []})
        args = bookmark_sync.build_parser().parse_args(["--list"])
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = bookmark_sync.noninteractive_mode(args, [snapshot])

        self.assertEqual(result, 0)
        self.assertIn("State warning:", output.getvalue())
        self.assertIn("chrome:Default", output.getvalue())

    def test_dangling_runtime_state_link_blocks_writes(self) -> None:
        bookmark_sync.STATE_FILE.symlink_to(Path(self.runtime_dir.name) / "missing-state")

        with self.assertRaisesRegex(SystemExit, "Writes are blocked"):
            bookmark_sync.require_no_pending_operation()

    def test_restore_refuses_linked_backup(self) -> None:
        runtime = Path(self.runtime_dir.name)
        original = runtime / "original.bak"
        original.write_text("{}")
        linked = runtime / "linked.bak"
        linked.symlink_to(original)

        with self.assertRaisesRegex(SystemExit, "Could not open backup file"):
            bookmark_sync.restore_store_backup(linked, make_store("edge:Default", "edge"))

    def test_restore_validates_the_same_bytes_it_writes(self) -> None:
        runtime = Path(self.runtime_dir.name)
        backup = runtime / "restore.bak"
        target = make_store("edge:Default", "edge")
        target.path = runtime / "Bookmarks"
        target.path.write_text("current")

        with (
            mock.patch.object(bookmark_sync, "read_regular_bytes", return_value=b"{}"),
            mock.patch.object(bookmark_sync, "backup_target") as backup_target,
            self.assertRaisesRegex(SystemExit, "missing required roots"),
        ):
            bookmark_sync.restore_store_backup(backup, target)

        backup_target.assert_not_called()
        self.assertEqual(target.path.read_text(), "current")

    def test_backup_target_does_not_leave_a_partial_backup(self) -> None:
        runtime = Path(self.runtime_dir.name)
        source = runtime / "Bookmarks"
        source.write_bytes(b"original")
        store = make_store("edge:Default", "edge")
        store.path = source

        with (
            mock.patch.object(bookmark_sync.os, "replace", side_effect=OSError("replace failed")),
            self.assertRaisesRegex(OSError, "replace failed"),
        ):
            bookmark_sync.backup_target(store)

        self.assertEqual(list(bookmark_sync.DOWNLOAD_BACKUP_DIR.iterdir()), [])

    def test_backup_directory_refuses_dangling_symlink(self) -> None:
        bookmark_sync.DOWNLOAD_BACKUP_DIR.symlink_to(Path(self.runtime_dir.name) / "missing-backups")

        with self.assertRaisesRegex(SystemExit, "Backup directory is not a regular directory"):
            bookmark_sync.print_backup_list()

    def test_operation_lock_rejects_a_second_writer(self) -> None:
        with bookmark_sync.operation_lock():
            with self.assertRaisesRegex(SystemExit, "already running"):
                with bookmark_sync.operation_lock():
                    self.fail("second writer acquired the lock")

        self.assertEqual(bookmark_sync.LOCK_FILE.stat().st_mode & 0o777, 0o600)
        with bookmark_sync.operation_lock():
            pass

    def test_operation_lock_refuses_symlink(self) -> None:
        outside = Path(self.runtime_dir.name) / "outside"
        outside.write_text("unchanged")
        bookmark_sync.LOCK_FILE.symlink_to(outside)

        with self.assertRaisesRegex(SystemExit, "open operation lock safely"):
            with bookmark_sync.operation_lock():
                self.fail("symlink lock was accepted")
        self.assertEqual(outside.read_text(), "unchanged")

    def test_recovery_actions_are_mutually_exclusive(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            bookmark_sync.build_parser().parse_args(["--recover", "--discard-recovery"])

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

    def test_chromium_developer_mode_ready_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir) / "Default"
            profile_dir.mkdir()
            store = bookmark_sync.BrowserStore(
                id="edge:Default",
                browser="edge",
                profile="Default",
                format="chromium",
                path=profile_dir / "Bookmarks",
                label="edge:Default",
            )
            self.assertTrue(bookmark_sync.chromium_developer_mode_ready(store))

            secure = profile_dir / "Secure Preferences"
            secure.write_text(json.dumps({"extensions": {"ui": {"developer_mode": True}}}))
            self.assertTrue(bookmark_sync.chromium_developer_mode_ready(store))

            secure.write_text(json.dumps({"extensions": {"ui": {}}}))
            self.assertFalse(bookmark_sync.chromium_developer_mode_ready(store))

            secure.write_text(json.dumps({"extensions": {"ui": {"developer_mode": False}}}))
            self.assertFalse(bookmark_sync.chromium_developer_mode_ready(store))

    def test_cloud_purge_requires_developer_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir) / "Default"
            profile_dir.mkdir()
            (profile_dir / "Secure Preferences").write_text(json.dumps({"extensions": {"ui": {}}}))
            store = bookmark_sync.BrowserStore(
                id="edge:Default",
                browser="edge",
                profile="Default",
                format="chromium",
                path=profile_dir / "Bookmarks",
                label="edge:Default",
            )
            with self.assertRaises(SystemExit) as caught:
                bookmark_sync.require_chromium_developer_mode(store)
            self.assertIn("edge://extensions", str(caught.exception))

            (profile_dir / "Secure Preferences").write_text(
                json.dumps({"extensions": {"ui": {"developer_mode": True}}})
            )
            bookmark_sync.require_chromium_developer_mode(store)

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

    def test_chromium_sync_state_treats_missing_preferences_as_potentially_enabled(self) -> None:
        store = make_store("edge:Default", "edge")

        enabled, reason = bookmark_sync.chromium_sync_state(store)

        self.assertTrue(enabled)
        self.assertIn("unknown", reason)

    def test_chromium_sync_state_treats_partial_preferences_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir) / "Default"
            profile_dir.mkdir(parents=True)
            store = make_store("chrome:Default", "chrome")
            store.path = profile_dir / "Bookmarks"
            (profile_dir / "Preferences").write_text(json.dumps({"sync": {}}))

            enabled, reason = bookmark_sync.chromium_sync_state(store)

        self.assertTrue(enabled)
        self.assertIn("fields are missing", reason)

    def test_chromium_sync_state_respects_explicit_disabled_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir) / "Default"
            profile_dir.mkdir(parents=True)
            store = make_store("edge:Default", "edge")
            store.path = profile_dir / "Bookmarks"
            (profile_dir / "Preferences").write_text(json.dumps({"sync": {"bookmarks": False}}))

            enabled, reason = bookmark_sync.chromium_sync_state(store)

        self.assertFalse(enabled)
        self.assertIn("bookmarks=off", reason)

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

    def test_choose_sync_strategy_uses_direct_and_post_open_repair_for_recorded_edge_issue(self) -> None:
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

        self.assertEqual(decision.strategy, "direct")
        self.assertIn("known cloud reinjection issue", decision.reason)
        self.assertIn("repair with browser closed", decision.reason)

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
        source = {"bar": [{"type": "url", "name": 'A "quoted"', "url": "https://example.test/a"}], "menu": [], "synced": []}
        with bookmark_sync.create_chromium_bookmark_purge_extension(30.0, "http://127.0.0.1:12345/ready/test", source) as extension_dir:
            extension_path = Path(extension_dir)
            manifest = json.loads((extension_path / "manifest.json").read_text())
            worker = (extension_path / "service-worker.js").read_text()

        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(manifest["name"], "Bookmark Sync Temporary Purge")
        self.assertIn("bookmarks", manifest["permissions"])
        self.assertIn("alarms", manifest["permissions"])
        self.assertIn("chrome.bookmarks.getTree", worker)
        self.assertIn("chrome.bookmarks.onCreated", worker)
        embedded = worker.split("const SOURCE_TREE = ", 1)[1].split(";\n", 1)[0]
        self.assertEqual(json.loads(embedded), source)
        self.assertEqual(manifest["host_permissions"], ["http://127.0.0.1/*"])
        self.assertIn("chrome.bookmarks.create", worker)
        with self.assertRaisesRegex(ValueError, "authorized startup URL"):
            bookmark_sync.create_chromium_bookmark_purge_extension(30.0)
        with self.assertRaisesRegex(ValueError, "authorized startup URL"):
            bookmark_sync.render_chromium_bookmark_purge_worker(30.0, None)

    def test_purge_extension_startup_permission_is_loopback_only(self) -> None:
        startup_url = "http://127.0.0.1:12345/ready/test-capability"
        with bookmark_sync.create_chromium_bookmark_purge_extension(30.0, startup_url) as extension_dir:
            extension_path = Path(extension_dir)
            manifest = json.loads((extension_path / "manifest.json").read_text())
            worker = (extension_path / "service-worker.js").read_text()

        self.assertEqual(manifest["host_permissions"], ["http://127.0.0.1/*"])
        self.assertIn(json.dumps(startup_url), worker)
        for url in ("https://example.com/ready", "http://localhost:12345/ready", "http://user@127.0.0.1:12345/ready"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "loopback HTTP capability"):
                bookmark_sync.create_chromium_bookmark_purge_extension(30.0, url)

    def test_purge_startup_server_accepts_only_the_run_capability(self) -> None:
        responses = []
        errors = []
        with bookmark_sync.chromium_purge_startup_server() as startup:
            parsed = urlsplit(startup.url)

            def send_requests() -> None:
                try:
                    for path in ("/wrong-capability", parsed.path):
                        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
                        connection.request("POST", path, body=b"")
                        response = connection.getresponse()
                        responses.append(response.status)
                        response.read()
                        connection.close()
                except Exception as exc:
                    errors.append(exc)

            sender = threading.Thread(target=send_requests)
            sender.start()
            try:
                startup.wait(None, 3.0)
            finally:
                sender.join(timeout=4)

        self.assertFalse(sender.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(responses, [404, 204])

    def test_purge_startup_server_fails_when_browser_exits_or_ack_times_out(self) -> None:
        with bookmark_sync.chromium_purge_startup_server() as startup:
            process = mock.Mock()
            process.poll.return_value = 1
            with self.assertRaisesRegex(SystemExit, "Browser exited before"):
                startup.wait(process, 1.0)

            with (
                mock.patch.object(bookmark_sync.time, "monotonic", side_effect=[0.0, 2.0]),
                self.assertRaisesRegex(SystemExit, "No API purge was authorized"),
            ):
                startup.wait(None, 1.0)

    def test_purge_startup_server_controls_rebuild_and_accepts_only_count_status(self) -> None:
        errors = []
        with bookmark_sync.chromium_purge_startup_server() as startup:
            parsed = urlsplit(startup.url)

            def post(status=None):
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
                body = b"" if status is None else json.dumps(status).encode()
                connection.request("POST", parsed.path, body=body)
                response = connection.getresponse()
                content = response.read()
                result = response.status, json.loads(content) if response.status == 200 else None
                connection.close()
                return result

            self.assertEqual(post()[0], 204)
            self.assertEqual(post({"phase": "purged", "url": "https://private.test"})[0], 400)

            def rebuild_worker() -> None:
                try:
                    for _ in range(20):
                        status, response = post({"phase": "purged", "bookmarks": 0, "folders": 0})
                        if status == 200 and response["command"] == "rebuild":
                            post({"phase": "rebuilt", "bookmarks": 2, "folders": 1, "mobile_fallback": False})
                            return
                        threading.Event().wait(0.01)
                    raise AssertionError("rebuild command was not received")
                except Exception as exc:
                    errors.append(exc)

            sender = threading.Thread(target=rebuild_worker)
            sender.start()
            try:
                startup.wait(None, 1.0)
                result = startup.rebuild(None, 2.0)
            finally:
                sender.join(timeout=3)

        self.assertEqual(errors, [])
        self.assertEqual(result, {"phase": "rebuilt", "bookmarks": 2, "folders": 1, "mobile_fallback": False})

    def test_chromium_purge_worker_waits_for_startup_authorization(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node is required to execute the generated service worker")

        worker = bookmark_sync.render_chromium_bookmark_purge_worker(1.0, "http://127.0.0.1:12345/ready/test")
        for accepted in (False, True):
            harness = f"""
const worker = {json.dumps(worker)};
const accepted = {json.dumps(accepted)};
const listeners = [];
const storage = {{}};
let requests = 0;
let treeReads = 0;
let authorized = false;
const listener = {{ addListener(callback) {{ listeners.push(callback); }} }};
globalThis.fetch = async (_url, options) => {{
  if (!options.body) requests += 1;
  return {{ ok: accepted, json: async () => ({{ command: "rebuild" }}) }};
}};
globalThis.chrome = {{
  runtime: {{ lastError: null, onInstalled: listener, onStartup: listener }},
  alarms: {{ create() {{}}, clear() {{}}, onAlarm: listener }},
  storage: {{ local: {{
    get(keys, callback) {{ callback(Object.fromEntries(keys.map((key) => [key, storage[key]]))); }},
    set(values, callback) {{
      Object.assign(storage, values);
      authorized = !!storage.startupAuthorized;
      if (callback) callback();
    }},
  }} }},
  bookmarks: {{
    getTree(callback) {{
      if (!authorized) throw new Error("API called before startup authorization");
      treeReads += 1;
      callback([{{ children: [{{ id: "1", children: [] }}, {{ id: "2", children: [] }}, {{ id: "3", children: [] }}] }}]);
    }},
    onCreated: listener, onChanged: listener, onMoved: listener, onChildrenReordered: listener,
  }},
}};
eval(worker);
for (const callback of listeners) callback({{ name: "edgeBookmarkPurge" }});
setTimeout(() => {{
  if (requests !== 1 || (accepted ? treeReads === 0 : treeReads !== 0)) {{
    console.error(JSON.stringify({{ requests, treeReads, storage }}));
    process.exit(1);
  }}
  if (!accepted && !storage.lastError) process.exit(1);
}}, 400);
"""
            with self.subTest(accepted=accepted):
                result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=3, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_chromium_purge_worker_does_not_renew_expired_deletion_window(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node is required to execute the generated service worker")

        worker = bookmark_sync.render_chromium_bookmark_purge_worker(1.0, "http://127.0.0.1:12345/ready/test")
        harness = f"""
const worker = {json.dumps(worker)};
const listeners = {{}};
const deadline = Date.now() - 1000;
const storage = {{ deadlineMs: deadline, startupAuthorized: true }};
let treeReads = 0;
function listener(name) {{
  return {{ addListener(callback) {{ listeners[name] = callback; }} }};
}}
globalThis.chrome = {{
  runtime: {{ onInstalled: listener("installed"), onStartup: listener("startup") }},
  alarms: {{ create() {{}}, clear() {{}}, onAlarm: listener("alarm") }},
  storage: {{ local: {{
    get(keys, callback) {{ callback(Object.fromEntries(keys.map((key) => [key, storage[key]]))); }},
    set(values, callback) {{ Object.assign(storage, values); if (callback) callback(); }},
  }} }},
  bookmarks: {{
    getTree() {{ treeReads += 1; }},
    onCreated: listener("created"), onChanged: listener("changed"),
    onMoved: listener("moved"), onChildrenReordered: listener("reordered"),
  }},
}};
globalThis.fetch = async () => ({{ ok: true, json: async () => ({{ command: "purge" }}) }});
eval(worker);
listeners.installed();
listeners.startup();
listeners.created();
setTimeout(() => {{
  if (treeReads !== 0 || storage.deadlineMs !== deadline) {{
    console.error(JSON.stringify({{ treeReads, storage }}));
    process.exit(1);
  }}
}}, 100);
"""
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=3, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_chromium_purge_worker_retries_delete_errors_and_cloud_reinjections(self) -> None:
        if shutil.which("node") is None:
            self.skipTest("node is required to execute the generated service worker")

        worker = bookmark_sync.render_chromium_bookmark_purge_worker(2.0, "http://127.0.0.1:12345/ready/test")
        harness = f"""
const worker = {json.dumps(worker)};
const listeners = {{}};
const storage = {{}};
const tree = [{{
  id: "0",
  children: [
    {{
      id: "1",
      children: [
        {{ id: "11", children: [] }},
        {{ id: "12", children: [{{ id: "121", children: [] }}] }},
      ],
    }},
    {{ id: "2", children: [{{ id: "21", children: [] }}] }},
  ],
}}];
const failOnce = new Set(["11"]);
let readded = false;

function listenerRegistry(name) {{
  return {{ addListener(callback) {{ (listeners[name] ||= []).push(callback); }} }};
}}

function emit(name, ...args) {{
  for (const callback of listeners[name] || []) callback(...args);
}}

function snapshot(value) {{
  return JSON.parse(JSON.stringify(value));
}}

function countNodes(nodes) {{
  return nodes.reduce((count, node) => count + 1 + countNodes(node.children || []), 0);
}}

function removeById(nodes, id) {{
  const index = nodes.findIndex((node) => node.id === id);
  if (index >= 0) {{
    nodes.splice(index, 1);
    return true;
  }}
  return nodes.some((node) => removeById(node.children || [], id));
}}

function removeBookmark(id, callback) {{
  setImmediate(() => {{
    if (failOnce.delete(id)) {{
      chrome.runtime.lastError = {{ message: "simulated transient delete failure" }};
      callback();
      chrome.runtime.lastError = null;
      return;
    }}
    if (!removeById(tree[0].children, id)) {{
      chrome.runtime.lastError = {{ message: "simulated missing node" }};
      callback();
      chrome.runtime.lastError = null;
      return;
    }}
    chrome.runtime.lastError = null;
    callback();
    if (!readded) {{
      readded = true;
      setTimeout(() => {{
        const node = {{ id: "cloud-1", children: [] }};
        tree[0].children[0].children.push(node);
        emit("created", node.id, node);
        emit("changed", node.id, {{ title: "cloud" }});
      }}, 900);
    }}
  }});
}}

const chrome = {{
  runtime: {{
    lastError: null,
    onInstalled: listenerRegistry("installed"),
    onStartup: listenerRegistry("startup"),
  }},
  storage: {{
    local: {{
      get(keys, callback) {{
        callback(Object.fromEntries(keys.map((key) => [key, storage[key]])));
      }},
      set(values, callback) {{
        Object.assign(storage, values);
        if (callback) callback();
      }},
    }},
  }},
  alarms: {{
    create() {{}},
    clear() {{}},
    onAlarm: listenerRegistry("alarm"),
  }},
  bookmarks: {{
    getTree(callback) {{
      setImmediate(() => {{
        chrome.runtime.lastError = null;
        callback(snapshot(tree));
      }});
    }},
    remove: removeBookmark,
    removeTree: removeBookmark,
    onCreated: listenerRegistry("created"),
    onChanged: listenerRegistry("changed"),
    onMoved: listenerRegistry("moved"),
    onChildrenReordered: listenerRegistry("reordered"),
  }},
}};

globalThis.chrome = chrome;
const started = Date.now();
globalThis.fetch = async () => ({{ ok: true, json: async () => ({{ command: Date.now() - started > 1100 ? "rebuild" : "purge" }}) }});
new Function(worker)();

setTimeout(() => {{
  const remaining = countNodes(tree[0].children.flatMap((node) => node.children || []));
  if (remaining !== 0 || storage.lastRemaining !== 0 || storage.lastError !== "") {{
    console.error(JSON.stringify({{ remaining, storage }}));
    process.exit(1);
  }}
  if (!readded) {{
    console.error("synthetic cloud reinjection did not run");
    process.exit(1);
  }}
}}, 2400);
"""
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=7, check=False)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_chromium_worker_rebuilds_depth_first_with_explicit_sibling_order(self) -> None:
        source = {
            "bar": [
                {"type": "folder", "name": "Folder", "children": [
                    {"type": "url", "name": "First", "url": "https://first.test"},
                    {"type": "url", "name": "Last", "url": "https://last.test"},
                ]},
                {"type": "url", "name": "Bar last", "url": "https://bar.test"},
            ],
            "menu": [{"type": "url", "name": "Other", "url": "https://other.test"}],
            "synced": [{"type": "url", "name": "Mobile", "url": "https://mobile.test"}],
        }
        worker = bookmark_sync.render_chromium_bookmark_purge_worker(2.0, "http://127.0.0.1:12345/ready/test", source)
        result = run_chromium_worker_harness(worker)
        self.assertEqual(result["unauthorized"], 0)
        self.assertEqual(result["storage"]["phase"], "rebuilt")
        self.assertEqual([call["title"] for call in result["calls"]], ["Folder", "First", "Last", "Bar last", "Other", "Mobile"])
        self.assertEqual([call["index"] for call in result["calls"]], [0, 0, 1, 1, 0, 0])
        self.assertEqual([call["parentId"] for call in result["calls"]], ["1", "new-1", "new-1", "1", "2", "3"])
        self.assertEqual(result["phases"][-1], {"phase": "rebuilt", "bookmarks": 5, "folders": 1, "mobile_fallback": False})

    def test_chromium_worker_falls_back_to_existing_root_when_mobile_is_missing(self) -> None:
        source = {"bar": [], "menu": [{"type": "url", "name": "Other", "url": "https://other.test"}],
                  "synced": [{"type": "url", "name": "Mobile", "url": "https://mobile.test"}]}
        worker = bookmark_sync.render_chromium_bookmark_purge_worker(2.0, "http://127.0.0.1:12345/ready/test", source)
        result = run_chromium_worker_harness(worker, {"noMobile": True})
        self.assertEqual([call["parentId"] for call in result["calls"]], ["2", "2"])
        self.assertEqual([call["index"] for call in result["calls"]], [0, 1])
        self.assertTrue(result["phases"][-1]["mobile_fallback"])
        self.assertEqual(result["storage"]["phase"], "rebuilt")

    def test_chromium_worker_rejects_count_order_create_errors_and_rebuild_timeouts(self) -> None:
        source = {"bar": [{"type": "url", "name": "A", "url": "https://a.test"},
                          {"type": "url", "name": "B", "url": "https://b.test"}], "menu": [], "synced": []}
        worker = bookmark_sync.render_chromium_bookmark_purge_worker(1.0, "http://127.0.0.1:12345/ready/test", source)
        for options, error in (
            ({"dropCreateAt": 2}, "rebuild_count_mismatch"),
            ({"reverseFinal": True}, "rebuild_order_mismatch"),
            ({"createError": True}, "rebuild_failed"),
            ({"hangCreate": True}, "rebuild_failed"),
            ({"initialState": {"startupAuthorized": True, "phase": "rebuilding"}}, "rebuild_interrupted"),
        ):
            with self.subTest(options=options):
                result = run_chromium_worker_harness(worker, options)
                self.assertEqual(result["storage"]["phase"], "failed")
                self.assertEqual(result["phases"][-1]["error"], error)
                self.assertNotIn("synthetic sensitive API error", json.dumps(result["phases"]))

    def test_chromium_worker_never_deletes_or_creates_without_authorization(self) -> None:
        worker = bookmark_sync.render_chromium_bookmark_purge_worker(
            1.0, "http://127.0.0.1:12345/ready/test", {"bar": [], "menu": [], "synced": []}
        )
        result = run_chromium_worker_harness(worker, {"authorize": False})
        self.assertEqual(result["unauthorized"], 0)
        self.assertEqual(result["calls"], [])
        self.assertEqual(result["tree"][0]["children"][0]["children"][0]["id"], "old")

    def test_chromium_api_clear_then_sync_runs_purge_then_sync(self) -> None:
        source_store = make_store("chrome:Default", "chrome")
        target_store = make_store("edge:Default", "edge")
        target_store.path = Path("/tmp/isolated-edge/Default/Bookmarks")
        startup = bookmark_sync.ChromiumPurgeStartup(
            "http://127.0.0.1:12345/ready/test", mock.Mock(), mock.Mock(return_value={"bookmarks": 1, "folders": 0})
        )
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
                mock.patch.object(bookmark_sync, "running_browsers", return_value=[]),
                mock.patch.object(bookmark_sync, "chromium_purge_startup_server", return_value=contextlib.nullcontext(startup)),
                mock.patch.object(bookmark_sync, "backup_target", return_value=Path("/tmp/clear-backup")),
                mock.patch.object(
                    bookmark_sync,
                    "create_chromium_bookmark_purge_extension",
                    return_value=contextlib.nullcontext(extdir),
                ),
                mock.patch.object(bookmark_sync, "open_browser") as open_browser,
                mock.patch.object(bookmark_sync, "wait_for_bookmark_count", side_effect=[cleared, source]) as wait_for_count,
                mock.patch.object(bookmark_sync, "load_snapshot_with_retry", return_value=cleared),
                mock.patch.object(bookmark_sync.time, "sleep"),
                mock.patch.object(bookmark_sync, "ensure_browsers_closed") as close_browser,
                mock.patch.object(
                    bookmark_sync,
                    "verify_snapshot_with_stabilization", return_value=verification,
                ) as verify,
                mock.patch.object(bookmark_sync, "sync_store") as sync_store,
            ):
                clear_backup, sync_backup, result = bookmark_sync.reset_chromium_cloud_via_api_then_sync(
                    source,
                    target_store,
                    "strict",
                    purge_window_seconds=60.0,
                    settle_wait_seconds=10.0,
                )

        self.assertEqual(clear_backup, Path("/tmp/clear-backup"))
        self.assertIsNone(sync_backup)
        self.assertTrue(result.same_portable)
        open_browser.assert_called_once()
        self.assertIn("--user-data-dir=/tmp/isolated-edge", open_browser.call_args.kwargs["extra_args"])
        self.assertIn(f"--profile-directory={target_store.profile}", open_browser.call_args.kwargs["extra_args"])
        self.assertIn(f"--load-extension={extdir}", open_browser.call_args.kwargs["extra_args"])
        self.assertEqual(wait_for_count.call_count, 2)
        self.assertEqual(wait_for_count.call_args_list[0].kwargs["expected_count"], 0)
        self.assertEqual(wait_for_count.call_args_list[1].kwargs["expected_count"], source.bookmark_count)
        self.assertEqual(wait_for_count.call_args_list[1].kwargs["expected_folder_count"], source.folder_count)
        startup.wait.assert_called_once_with(open_browser.return_value, bookmark_sync.CHROMIUM_EXTENSION_STARTUP_TIMEOUT_SECONDS)
        close_browser.assert_called_once_with({"edge"}, auto_close=True)
        startup.rebuild.assert_called_once_with(open_browser.return_value, 60.0)
        verify.assert_called_once_with(target_store, source)
        sync_store.assert_not_called()

    def test_chromium_api_purge_rolls_back_after_failure(self) -> None:
        source_store = make_store("chrome:Default", "chrome")
        target_store = make_store("edge:Default", "edge")
        target_store.path = Path("/tmp/isolated-edge/Default/Bookmarks")
        startup = bookmark_sync.ChromiumPurgeStartup("http://127.0.0.1:12345/ready/test", mock.Mock(), mock.Mock())
        source = make_snapshot(source_store, {"bar": [], "menu": [], "synced": []})
        not_cleared = make_snapshot(
            target_store,
            {"bar": [{"type": "url", "name": "A", "url": "https://a"}], "menu": [], "synced": []},
        )
        backup_path = Path("/tmp/clear-backup")

        with tempfile.TemporaryDirectory() as extdir:
            with (
                mock.patch.object(bookmark_sync, "running_browsers", return_value=[]),
                mock.patch.object(bookmark_sync, "chromium_purge_startup_server", return_value=contextlib.nullcontext(startup)),
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

    def test_api_rebuild_errors_count_mismatch_and_final_order_mismatch_roll_back(self) -> None:
        source = make_snapshot(make_store("chrome:Default", "chrome"),
                               {"bar": [{"type": "url", "name": "A", "url": "https://a.test"}], "menu": [], "synced": []})
        target = make_store("edge:Default", "edge")
        target.path = Path("/tmp/isolated-edge/Default/Bookmarks")
        cleared = make_snapshot(target, {"bar": [], "menu": [], "synced": []})
        mismatched = bookmark_sync.SyncVerification(
            reloaded=source, same_count=True, same_portable=False, source_signature="source", target_signature="different-order"
        )
        for outcome in (SystemExit("API rebuild failed"), {"bookmarks": 2, "folders": 0}, {"bookmarks": 1, "folders": 0}):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as extdir:
                rebuild = mock.Mock(side_effect=outcome) if isinstance(outcome, SystemExit) else mock.Mock(return_value=outcome)
                startup = bookmark_sync.ChromiumPurgeStartup("http://127.0.0.1:12345/ready/test", mock.Mock(), rebuild)
                with (
                    mock.patch.object(bookmark_sync, "running_browsers", return_value=[]),
                    mock.patch.object(bookmark_sync, "chromium_purge_startup_server", return_value=contextlib.nullcontext(startup)),
                    mock.patch.object(bookmark_sync, "create_chromium_bookmark_purge_extension", return_value=contextlib.nullcontext(extdir)),
                    mock.patch.object(bookmark_sync, "backup_target", return_value=Path("/tmp/clear-backup")),
                    mock.patch.object(bookmark_sync, "open_browser"),
                    mock.patch.object(bookmark_sync, "wait_for_bookmark_count", side_effect=[cleared, source]),
                    mock.patch.object(bookmark_sync, "load_snapshot_with_retry", return_value=cleared),
                    mock.patch.object(bookmark_sync.time, "sleep"),
                    mock.patch.object(bookmark_sync, "ensure_browsers_closed"),
                    mock.patch.object(bookmark_sync, "verify_snapshot_with_stabilization", return_value=mismatched),
                    mock.patch.object(bookmark_sync, "sync_store") as file_sync,
                    mock.patch.object(bookmark_sync, "rollback_failed_cloud_purge", return_value=True) as rollback,
                    self.assertRaises(SystemExit),
                ):
                    bookmark_sync.reset_chromium_cloud_via_api_then_sync(source, target, "strict", 2.0, 0.0)
                rollback.assert_called_once_with(target, Path("/tmp/clear-backup"))
                file_sync.assert_not_called()

    def test_cloud_purge_rejects_running_browser_before_backup_or_launch(self) -> None:
        source = make_snapshot(make_store("chrome:Default", "chrome"), {"bar": [], "menu": [], "synced": []})
        target = make_store("edge:Default", "edge")
        target.path = Path("/tmp/isolated-edge/Default/Bookmarks")
        with (
            mock.patch.object(bookmark_sync, "running_browsers", return_value=["edge"]),
            mock.patch.object(bookmark_sync, "backup_target") as backup,
            mock.patch.object(bookmark_sync, "open_browser") as launch,
            self.assertRaisesRegex(SystemExit, "must be fully closed"),
        ):
            bookmark_sync.reset_chromium_cloud_via_api_then_sync(source, target, "strict", 1.0, 0.0)

        backup.assert_not_called()
        launch.assert_not_called()

    def test_cloud_purge_rejects_ambiguous_profile_path_before_backup(self) -> None:
        source = make_snapshot(make_store("chrome:Default", "chrome"), {"bar": [], "menu": [], "synced": []})
        target = make_store("edge:Default", "edge")
        with (
            mock.patch.object(bookmark_sync, "backup_target") as backup,
            self.assertRaisesRegex(SystemExit, "profile bookmark file"),
        ):
            bookmark_sync.reset_chromium_cloud_via_api_then_sync(source, target, "strict", 1.0, 0.0)
        backup.assert_not_called()

    def test_cloud_purge_does_not_wait_for_deletion_without_extension_ack(self) -> None:
        source = make_snapshot(make_store("chrome:Default", "chrome"), {"bar": [], "menu": [], "synced": []})
        target = make_store("edge:Default", "edge")
        target.path = Path("/tmp/isolated-edge/Default/Bookmarks")
        startup = bookmark_sync.ChromiumPurgeStartup(
            "http://127.0.0.1:12345/ready/test",
            mock.Mock(side_effect=SystemExit("extension startup blocked")),
            mock.Mock(),
        )
        backup_path = Path("/tmp/clear-backup")
        with tempfile.TemporaryDirectory() as extdir:
            with (
                mock.patch.object(bookmark_sync, "running_browsers", return_value=[]),
                mock.patch.object(bookmark_sync, "chromium_purge_startup_server", return_value=contextlib.nullcontext(startup)),
                mock.patch.object(bookmark_sync, "create_chromium_bookmark_purge_extension", return_value=contextlib.nullcontext(extdir)),
                mock.patch.object(bookmark_sync, "backup_target", return_value=backup_path),
                mock.patch.object(bookmark_sync, "open_browser"),
                mock.patch.object(bookmark_sync, "wait_for_bookmark_count") as wait_for_count,
                mock.patch.object(bookmark_sync, "sync_store") as sync_store,
                mock.patch.object(bookmark_sync, "rollback_failed_cloud_purge", return_value=True) as rollback,
                self.assertRaisesRegex(SystemExit, "extension startup blocked"),
            ):
                bookmark_sync.reset_chromium_cloud_via_api_then_sync(source, target, "strict", 1.0, 0.0)

        wait_for_count.assert_not_called()
        sync_store.assert_not_called()
        rollback.assert_called_once_with(target, backup_path)
        self.assertIsNone(bookmark_sync.get_pending_operation())

    def test_cloud_purge_keeps_recovery_record_when_rollback_cannot_close_browser(self) -> None:
        source = make_snapshot(make_store("chrome:Default", "chrome"), {"bar": [], "menu": [], "synced": []})
        target = make_store("edge:Default", "edge")
        target.path = Path("/tmp/isolated-edge/Default/Bookmarks")
        startup = bookmark_sync.ChromiumPurgeStartup(
            "http://127.0.0.1:12345/ready/test",
            mock.Mock(side_effect=SystemExit("extension startup blocked")),
            mock.Mock(),
        )
        with tempfile.TemporaryDirectory() as extdir:
            with (
                mock.patch.object(bookmark_sync, "running_browsers", return_value=[]),
                mock.patch.object(bookmark_sync, "chromium_purge_startup_server", return_value=contextlib.nullcontext(startup)),
                mock.patch.object(bookmark_sync, "create_chromium_bookmark_purge_extension", return_value=contextlib.nullcontext(extdir)),
                mock.patch.object(bookmark_sync, "backup_target", return_value=Path("/tmp/clear-backup")),
                mock.patch.object(bookmark_sync, "open_browser"),
                mock.patch.object(bookmark_sync, "ensure_browsers_closed", side_effect=SystemExit("still running")),
                mock.patch.object(bookmark_sync, "atomic_write_bytes", wraps=bookmark_sync.atomic_write_bytes) as write,
                self.assertRaisesRegex(SystemExit, "extension startup blocked"),
            ):
                bookmark_sync.reset_chromium_cloud_via_api_then_sync(source, target, "strict", 1.0, 0.0)

        self.assertTrue(all(call.args[0] != target.path for call in write.call_args_list))
        self.assertEqual(bookmark_sync.get_pending_operation()["target"], target.id)

    def test_cloud_purge_rollback_requires_exact_backup_bytes(self) -> None:
        target = make_store("edge:Default", "edge")
        target.path = Path(self.runtime_dir.name) / "Bookmarks"
        backup_path = Path(self.runtime_dir.name) / "before-purge.bak"
        raw = {
            "roots": {
                "bookmark_bar": {"children": [], "type": "folder"},
                "other": {"children": [], "type": "folder"},
                "synced": {"children": [], "type": "folder"},
            },
            "version": 1,
        }
        backup_path.write_text(json.dumps(raw))
        raw["version"] = 2
        target.path.write_text(json.dumps(raw))
        with (
            mock.patch.object(bookmark_sync, "ensure_browsers_closed"),
            mock.patch.object(bookmark_sync, "atomic_write_bytes"),
        ):
            self.assertFalse(bookmark_sync.rollback_failed_cloud_purge(target, backup_path))

    def test_empty_bookmark_wait_rejects_remaining_empty_folders(self) -> None:
        target = make_store("edge:Default", "edge")
        folders_remain = make_snapshot(target, {"bar": [{"type": "folder", "name": "A", "children": []}], "menu": [], "synced": []})
        with mock.patch.object(bookmark_sync, "load_snapshot_with_retry", return_value=folders_remain):
            result = bookmark_sync.wait_for_bookmark_count(target, 0, 0.0, 0.0, expected_folder_count=0)
        self.assertEqual(result.folder_count, 1)

    def test_should_auto_probe_after_direct_for_synced_chromium_even_with_recorded_issue(self) -> None:
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

            state_path = Path(tmpdir) / "state.json"
            with mock.patch.object(bookmark_sync, "STATE_FILE", state_path):
                self.assertTrue(bookmark_sync.should_auto_probe_after_direct(store, "auto"))
                bookmark_sync.mark_cloud_issue(store, "post-open drift detected")
                self.assertTrue(bookmark_sync.should_auto_probe_after_direct(store, "auto"))
            self.assertFalse(bookmark_sync.should_auto_probe_after_direct(store, "direct"))

    def test_auto_probe_repairs_drift_with_closed_browser_sync_without_purge(self) -> None:
        source_store = make_store("chrome:Default", "chrome")
        target_store = make_store("edge:Default", "edge")
        portable = {"bar": [{"type": "url", "name": "A", "url": "https://a"}], "menu": [], "synced": []}
        source = make_snapshot(source_store, portable)
        target = make_snapshot(target_store, portable)
        initial = bookmark_sync.SyncVerification(
            reloaded=target,
            same_count=True,
            same_portable=True,
            source_signature="source",
            target_signature="initial",
        )
        drifted = bookmark_sync.SyncVerification(
            reloaded=make_snapshot(target_store, {"bar": [], "menu": [], "synced": []}),
            same_count=False,
            same_portable=False,
            source_signature="source",
            target_signature="drifted",
        )
        repaired = bookmark_sync.SyncVerification(
            reloaded=target,
            same_count=True,
            same_portable=True,
            source_signature="source",
            target_signature="repaired",
        )
        args = bookmark_sync.build_parser().parse_args(
            ["--source", "chrome:Default", "--targets", "edge:Default", "--auto-close", "--allow-cloud-purge"]
        )
        output = io.StringIO()

        with (
            mock.patch.object(bookmark_sync, "ensure_browsers_closed"),
            mock.patch.object(bookmark_sync, "load_snapshot_with_retry", return_value=source),
            mock.patch.object(
                bookmark_sync,
                "sync_store",
                side_effect=[(Path("/tmp/initial.bak"), initial), (Path("/tmp/repair.bak"), repaired)],
            ) as sync_store,
            mock.patch.object(bookmark_sync, "run_post_open_check", return_value=(True, None, drifted)),
            mock.patch.object(bookmark_sync, "reset_chromium_cloud_via_api_then_sync") as cloud_purge,
            contextlib.redirect_stdout(output),
        ):
            result = bookmark_sync.noninteractive_mode(args, [source, target])

        self.assertEqual(result, 0)
        self.assertEqual(sync_store.call_count, 2)
        cloud_purge.assert_not_called()
        self.assertEqual(
            sync_store.call_args_list[1],
            mock.call(source, target_store, "strict", backup_enabled=True),
        )
        self.assertIn("repairing with the browser closed", output.getvalue())
        self.assertNotIn("Cloud-safe resync", output.getvalue())

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

    def test_post_open_check_closes_browser_without_drift(self) -> None:
        source_store = make_store("chrome:Default", "chrome")
        target_store = make_store("edge:Default", "edge")
        source = make_snapshot(source_store, {"bar": [], "menu": [], "synced": []})
        aligned = bookmark_sync.SyncVerification(
            reloaded=make_snapshot(target_store, {"bar": [], "menu": [], "synced": []}),
            same_count=True,
            same_portable=True,
            source_signature="same",
            target_signature="same",
        )

        with (
            mock.patch.dict(bookmark_sync.POST_OPEN_CHECK_PROFILES, {"edge": {"delay_seconds": 0.0, "poll_interval": 0.0, "max_checks": 1}}, clear=False),
            mock.patch.object(bookmark_sync, "open_browser"),
            mock.patch.object(bookmark_sync.time, "sleep"),
            mock.patch.object(bookmark_sync, "verify_snapshot_counts", return_value=aligned),
            mock.patch.object(bookmark_sync, "ensure_browsers_closed") as close_mock,
        ):
            drifted, backup_path, verification = bookmark_sync.run_post_open_check(source, target_store, "strict")

        self.assertFalse(drifted)
        self.assertIsNone(backup_path)
        self.assertTrue(verification.same_portable)
        close_mock.assert_called_once_with({"edge"}, auto_close=True)

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

    def test_sync_failure_keeps_recovery_record(self) -> None:
        source_store = make_store("chrome:Default", "chrome")
        target_store = make_store("edge:Default", "edge")
        source = make_snapshot(source_store, {"bar": [], "menu": [], "synced": []})
        backup_path = Path(self.runtime_dir.name) / "before-sync.bak"
        backup_path.write_text("backup")

        with (
            mock.patch.object(bookmark_sync, "backup_target", return_value=backup_path),
            mock.patch.object(bookmark_sync, "write_target_store", side_effect=OSError("write failed")),
            self.assertRaisesRegex(OSError, "write failed"),
        ):
            bookmark_sync.sync_store(source, target_store, "strict")

        pending = bookmark_sync.get_pending_operation()
        self.assertEqual(pending["target"], "edge:Default")
        self.assertEqual(pending["backup"], str(backup_path))
        with self.assertRaisesRegex(SystemExit, "Run --recover"):
            bookmark_sync.require_no_pending_operation()

    def test_recover_pending_operation_restores_backup(self) -> None:
        def chromium_raw(name: str) -> dict:
            return {
                "roots": {
                    "bookmark_bar": {
                        "children": [{"type": "url", "name": name, "url": "https://example.invalid"}],
                        "type": "folder",
                    },
                    "other": {"children": [], "type": "folder"},
                    "synced": {"children": [], "type": "folder"},
                },
                "version": 1,
            }

        runtime = Path(self.runtime_dir.name)
        target_path = runtime / "Bookmarks"
        backup_path = runtime / "before-sync.bak"
        target_path.write_text(json.dumps(chromium_raw("Interrupted")))
        backup_path.write_text(json.dumps(chromium_raw("Original")))
        target = make_store("edge:Default", "edge")
        target.path = target_path
        state = bookmark_sync.load_state()
        state[bookmark_sync.PENDING_OPERATION_STATE_KEY] = {
            "operation": "sync",
            "target": target.id,
            "backup": str(backup_path),
        }
        bookmark_sync.save_state(state)

        with mock.patch.object(bookmark_sync, "ensure_browsers_closed"):
            result = bookmark_sync.recover_pending_operation([target], auto_close=True)

        self.assertEqual(result, 0)
        self.assertEqual(bookmark_sync.load_snapshot(target).portable["bar"][0]["name"], "Original")
        self.assertIsNone(bookmark_sync.get_pending_operation())

    def test_strict_sync_fails_when_content_differs_with_same_count(self) -> None:
        source_store = make_store("chrome:Default", "chrome")
        target_store = make_store("edge:Default", "edge")
        source = make_snapshot(
            source_store,
            {"bar": [{"type": "url", "name": "Source", "url": "https://source.invalid"}], "menu": [], "synced": []},
        )
        verification = bookmark_sync.SyncVerification(
            reloaded=make_snapshot(
                target_store,
                {"bar": [{"type": "url", "name": "Target", "url": "https://target.invalid"}], "menu": [], "synced": []},
            ),
            same_count=True,
            same_portable=False,
            source_signature="source",
            target_signature="target",
        )

        with (
            mock.patch.object(bookmark_sync, "write_target_store"),
            mock.patch.object(bookmark_sync, "verify_snapshot_with_stabilization", return_value=verification),
            mock.patch.object(bookmark_sync, "repair_sync_if_needed", return_value=verification),
            mock.patch.object(bookmark_sync, "record_sync_baseline") as record_baseline,
            mock.patch.object(bookmark_sync, "record_sync_observation") as record_observation,
        ):
            with self.assertRaises(SystemExit):
                bookmark_sync.sync_store(source, target_store, "strict", backup_enabled=False)

        record_observation.assert_called_once()
        record_baseline.assert_not_called()


if __name__ == "__main__":
    unittest.main()
