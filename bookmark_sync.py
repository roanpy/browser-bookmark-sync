#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

HOME = Path(os.environ.get("BOOKMARK_SYNC_HOME", Path.home())).expanduser()
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BOOKMARK_SYNC_DATA_DIR", HOME / "Library" / "Application Support" / "Bookmark Sync")).expanduser()
DOWNLOAD_BACKUP_DIR = Path(os.environ.get("BOOKMARK_SYNC_BACKUP_DIR", HOME / "Downloads" / "bookmark-sync-backups")).expanduser()
STATE_FILE = DATA_DIR / "state.json"
PRIMARY_STORE_STATE_KEY = "_primary_store_id"
SYNC_BASELINES_STATE_KEY = "_sync_baselines"
SYNC_OBSERVATIONS_STATE_KEY = "_sync_observations"
BROWSER_PROFILE_OVERRIDES_STATE_KEY = "_browser_profile_overrides"
TARGET_ISSUES_STATE_KEY = "_target_issues"
SAFARI_BOOKMARKS = HOME / "Library" / "Safari" / "Bookmarks.plist"
POST_OPEN_CHECK_PROFILES = {
    "chrome": {"delay_seconds": 20.0, "poll_interval": 5.0, "max_checks": 8},
    "edge": {"delay_seconds": 30.0, "poll_interval": 5.0, "max_checks": 12},
}
EDGE_EMPTY_SYNC_WAIT_SECONDS = 45.0
EDGE_API_PURGE_WINDOW_SECONDS = 90.0
EDGE_API_POLL_INTERVAL_SECONDS = 5.0


@dataclass
class BrowserStore:
    id: str
    browser: str
    profile: str
    format: str
    path: Path
    label: str


@dataclass
class BookmarkSnapshot:
    store: BrowserStore
    portable: dict[str, list[dict[str, Any]]]
    raw: Any
    modified_ts: float
    bookmark_count: int
    folder_count: int


@dataclass
class SyncVerification:
    reloaded: BookmarkSnapshot
    same_count: bool
    same_portable: bool
    source_signature: str
    target_signature: str
    baseline_signature: str | None = None
    matches_recorded_baseline: bool | None = None
    verification_passes: int = 1
    observed_external_changes: bool = False
    stabilized: bool = True
    repair_attempts: int = 0


@dataclass(frozen=True)
class SyncStrategyDecision:
    strategy: str
    reason: str


@dataclass(frozen=True)
class FormatHandler:
    name: str
    load_snapshot: Callable[[BrowserStore], BookmarkSnapshot]
    write_snapshot: Callable[[BookmarkSnapshot, BrowserStore, str], None]


@dataclass(frozen=True)
class BrowserProfile:
    browser: str
    format_name: str
    app_name: str
    executable_name: str
    detect_stores: Callable[[], list[BrowserStore]]
    stabilization_profile: dict[str, Any]
    repair_profile: dict[str, Any] | None = None


FORMAT_HANDLERS: dict[str, FormatHandler] = {}
BROWSER_PROFILES: dict[str, BrowserProfile] = {}


def chromium_timestamp_now() -> str:
    unix_microseconds = int(datetime.now(UTC).timestamp() * 1_000_000)
    return str(unix_microseconds + 11644473600000000)


def now_string(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def display_path(path: str | Path) -> str:
    resolved = Path(path).expanduser()
    try:
        return f"~/{resolved.relative_to(HOME)}"
    except ValueError:
        return str(resolved)


def register_format_handler(handler: FormatHandler) -> None:
    FORMAT_HANDLERS[handler.name] = handler


def register_browser_profile(profile: BrowserProfile) -> None:
    BROWSER_PROFILES[profile.browser] = profile


def get_format_handler(format_name: str) -> FormatHandler:
    try:
        return FORMAT_HANDLERS[format_name]
    except KeyError as exc:
        raise SystemExit(f"Unsupported bookmark format: {format_name}") from exc


def get_browser_profile(browser: str) -> BrowserProfile:
    try:
        return BROWSER_PROFILES[browser]
    except KeyError as exc:
        raise SystemExit(f"Unsupported browser: {browser}") from exc


def supported_browsers() -> list[str]:
    return list(BROWSER_PROFILES)


def make_chromium_detector(browser: str, base_dir: Path) -> Callable[[], list[BrowserStore]]:
    def detect() -> list[BrowserStore]:
        stores: list[BrowserStore] = []
        if not base_dir.exists():
            return stores
        for child in sorted(base_dir.iterdir(), key=lambda item: item.name.lower()):
            if not child.is_dir():
                continue
            bookmark_file = child / "Bookmarks"
            if not bookmark_file.exists():
                continue
            profile = child.name
            label = f"{browser}:{profile}"
            stores.append(
                BrowserStore(
                    id=label,
                    browser=browser,
                    profile=profile,
                    format="chromium",
                    path=bookmark_file,
                    label=label,
                )
            )
        return stores

    return detect


def detect_safari_stores() -> list[BrowserStore]:
    if not SAFARI_BOOKMARKS.exists():
        return []
    return [
        BrowserStore(
            id="safari",
            browser="safari",
            profile="default",
            format="safari",
            path=SAFARI_BOOKMARKS,
            label="safari",
        )
    ]


def detect_browser_stores() -> list[BrowserStore]:
    stores: list[BrowserStore] = []
    for profile in BROWSER_PROFILES.values():
        stores.extend(profile.detect_stores())
    return stores


def chromium_node_to_portable(node: dict[str, Any]) -> dict[str, Any] | None:
    node_type = node.get("type")
    if node_type == "url":
        return {
            "type": "url",
            "name": node.get("name") or node.get("url") or "",
            "url": node.get("url") or "",
        }
    if node_type == "folder":
        return {
            "type": "folder",
            "name": node.get("name") or "",
            "children": [
                converted
                for child in node.get("children", [])
                if (converted := chromium_node_to_portable(child)) is not None
            ],
        }
    return None


def load_chromium_snapshot(store: BrowserStore) -> BookmarkSnapshot:
    raw = json.loads(store.path.read_text())
    if not isinstance(raw, dict):
        raise SystemExit(f"Invalid Chromium bookmark store: {store.label}")
    roots = raw.get("roots")
    if not isinstance(roots, dict) or not all(isinstance(roots.get(key), dict) for key in ("bookmark_bar", "other")):
        raise SystemExit(f"Chromium bookmark store is missing required roots: {store.label}")
    portable = {
        "bar": [
            converted
            for child in roots.get("bookmark_bar", {}).get("children", [])
            if (converted := chromium_node_to_portable(child)) is not None
        ],
        "menu": [
            converted
            for child in roots.get("other", {}).get("children", [])
            if (converted := chromium_node_to_portable(child)) is not None
        ],
        "synced": [
            converted
            for child in roots.get("synced", {}).get("children", [])
            if (converted := chromium_node_to_portable(child)) is not None
        ],
    }
    bookmark_count, folder_count = count_portable(portable)
    return BookmarkSnapshot(
        store=store,
        portable=portable,
        raw=raw,
        modified_ts=store.path.stat().st_mtime,
        bookmark_count=bookmark_count,
        folder_count=folder_count,
    )


def safari_leaf_name(node: dict[str, Any]) -> str:
    uri_dictionary = node.get("URIDictionary") or {}
    return uri_dictionary.get("title") or node.get("Title") or node.get("URLString") or ""


def safari_node_to_portable(node: dict[str, Any]) -> dict[str, Any] | None:
    node_type = node.get("WebBookmarkType")
    if node_type == "WebBookmarkTypeLeaf":
        url = node.get("URLString")
        if not url:
            return None
        return {
            "type": "url",
            "name": safari_leaf_name(node),
            "url": url,
        }
    if node_type == "WebBookmarkTypeList":
        return {
            "type": "folder",
            "name": node.get("Title") or "",
            "children": [
                converted
                for child in node.get("Children", [])
                if (converted := safari_node_to_portable(child)) is not None
            ],
        }
    return None


def find_safari_root(raw: dict[str, Any], title: str) -> dict[str, Any] | None:
    for child in raw.get("Children", []):
        if child.get("Title") == title and child.get("WebBookmarkType") == "WebBookmarkTypeList":
            return child
    return None


def load_safari_snapshot(store: BrowserStore) -> BookmarkSnapshot:
    with store.path.open("rb") as handle:
        raw = plistlib.load(handle)
    if not isinstance(raw, dict):
        raise SystemExit(f"Invalid Safari bookmark store: {store.label}")
    bookmarks_bar = find_safari_root(raw, "BookmarksBar")
    bookmarks_menu = find_safari_root(raw, "BookmarksMenu")
    if bookmarks_bar is None or bookmarks_menu is None:
        raise SystemExit(f"Safari bookmark store is missing BookmarksBar or BookmarksMenu roots: {store.label}")
    portable = {
        "bar": [
            converted
            for child in (bookmarks_bar or {}).get("Children", [])
            if (converted := safari_node_to_portable(child)) is not None
        ],
        "menu": [
            converted
            for child in (bookmarks_menu or {}).get("Children", [])
            if (converted := safari_node_to_portable(child)) is not None
        ],
        "synced": [],
    }
    bookmark_count, folder_count = count_portable(portable)
    return BookmarkSnapshot(
        store=store,
        portable=portable,
        raw=raw,
        modified_ts=store.path.stat().st_mtime,
        bookmark_count=bookmark_count,
        folder_count=folder_count,
    )


def load_snapshot(store: BrowserStore) -> BookmarkSnapshot:
    return get_format_handler(store.format).load_snapshot(store)


def count_nodes(nodes: list[dict[str, Any]]) -> tuple[int, int]:
    bookmarks = 0
    folders = 0
    for node in nodes:
        if node["type"] == "url":
            bookmarks += 1
        else:
            folders += 1
            child_bookmarks, child_folders = count_nodes(node.get("children", []))
            bookmarks += child_bookmarks
            folders += child_folders
    return bookmarks, folders


def count_portable(portable: dict[str, list[dict[str, Any]]]) -> tuple[int, int]:
    bookmarks = 0
    folders = 0
    for key in ("bar", "menu", "synced"):
        child_bookmarks, child_folders = count_nodes(portable.get(key, []))
        bookmarks += child_bookmarks
        folders += child_folders
    return bookmarks, folders


def portable_bar_names(portable: dict[str, list[dict[str, Any]]]) -> list[str]:
    return [node.get("name") or "" for node in portable.get("bar", [])]


def portable_signature(portable: dict[str, list[dict[str, Any]]]) -> str:
    payload = json.dumps(portable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        raw_state = json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {}

    baselines = {
        key: value
        for key, value in raw_state.items()
        if key
        not in {
            PRIMARY_STORE_STATE_KEY,
            SYNC_BASELINES_STATE_KEY,
            SYNC_OBSERVATIONS_STATE_KEY,
            BROWSER_PROFILE_OVERRIDES_STATE_KEY,
            TARGET_ISSUES_STATE_KEY,
        }
    }
    nested_baselines = raw_state.get(SYNC_BASELINES_STATE_KEY)
    if isinstance(nested_baselines, dict):
        baselines.update(nested_baselines)

    state: dict[str, Any] = {SYNC_BASELINES_STATE_KEY: baselines}
    primary_store_id = raw_state.get(PRIMARY_STORE_STATE_KEY)
    if isinstance(primary_store_id, str):
        state[PRIMARY_STORE_STATE_KEY] = primary_store_id
    observations = raw_state.get(SYNC_OBSERVATIONS_STATE_KEY)
    if isinstance(observations, list):
        state[SYNC_OBSERVATIONS_STATE_KEY] = [item for item in observations if isinstance(item, dict)]
    else:
        state[SYNC_OBSERVATIONS_STATE_KEY] = []
    overrides = raw_state.get(BROWSER_PROFILE_OVERRIDES_STATE_KEY)
    if isinstance(overrides, dict):
        state[BROWSER_PROFILE_OVERRIDES_STATE_KEY] = overrides
    else:
        state[BROWSER_PROFILE_OVERRIDES_STATE_KEY] = {}
    target_issues = raw_state.get(TARGET_ISSUES_STATE_KEY)
    if isinstance(target_issues, dict):
        state[TARGET_ISSUES_STATE_KEY] = target_issues
    else:
        state[TARGET_ISSUES_STATE_KEY] = {}
    return state


def save_state(state: dict[str, Any]) -> None:
    payload: dict[str, Any] = {}
    primary_store_id = state.get(PRIMARY_STORE_STATE_KEY)
    if isinstance(primary_store_id, str):
        payload[PRIMARY_STORE_STATE_KEY] = primary_store_id
    payload[SYNC_BASELINES_STATE_KEY] = state.get(SYNC_BASELINES_STATE_KEY, {})
    payload[SYNC_OBSERVATIONS_STATE_KEY] = state.get(SYNC_OBSERVATIONS_STATE_KEY, [])
    payload[BROWSER_PROFILE_OVERRIDES_STATE_KEY] = state.get(BROWSER_PROFILE_OVERRIDES_STATE_KEY, {})
    payload[TARGET_ISSUES_STATE_KEY] = state.get(TARGET_ISSUES_STATE_KEY, {})
    atomic_write_text(STATE_FILE, json.dumps(payload, indent=2, ensure_ascii=False))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def atomic_write_text(path: Path, content: str) -> None:
    atomic_write_bytes(path, content.encode("utf-8"))


def read_json_dict(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def get_primary_store_id() -> str | None:
    state = load_state()
    value = state.get(PRIMARY_STORE_STATE_KEY)
    return value if isinstance(value, str) else None


def set_primary_store_id(store_id: str) -> None:
    state = load_state()
    state[PRIMARY_STORE_STATE_KEY] = store_id
    state.setdefault(SYNC_BASELINES_STATE_KEY, {})
    save_state(state)


def state_key(source: BrowserStore, target: BrowserStore, mode: str) -> str:
    return f"{source.id}->{target.id}:{mode}"


def get_browser_profile_overrides() -> dict[str, dict[str, dict[str, Any]]]:
    state = load_state()
    overrides = state.get(BROWSER_PROFILE_OVERRIDES_STATE_KEY, {})
    return overrides if isinstance(overrides, dict) else {}


def get_stabilization_profile(browser: str) -> dict[str, Any]:
    profile = dict(get_browser_profile(browser).stabilization_profile)
    overrides = get_browser_profile_overrides().get("stabilization", {}).get(browser, {})
    if isinstance(overrides, dict):
        profile.update(overrides)
    return profile


def get_repair_profile(browser: str) -> dict[str, Any] | None:
    profile = get_browser_profile(browser).repair_profile
    if profile is None:
        return None
    resolved = dict(profile)
    overrides = get_browser_profile_overrides().get("repair", {}).get(browser, {})
    if isinstance(overrides, dict):
        resolved.update(overrides)
    return resolved


def summarize_snapshots(snapshots: list[BookmarkSnapshot], preferred_source_id: str | None = None) -> None:
    print("Detected bookmark stores:\n")
    for index, snapshot in enumerate(snapshots, start=1):
        markers: list[str] = []
        if preferred_source_id and snapshot.store.id == preferred_source_id:
            markers.append("primary")
        marker_text = f" [{', '.join(markers)}]" if markers else ""
        print(
            f"[{index}] {snapshot.store.label:<20} "
            f"bookmarks={snapshot.bookmark_count:<5} "
            f"folders={snapshot.folder_count:<4} "
            f"modified={now_string(snapshot.modified_ts)}"
            f"{marker_text}"
        )
    newest = max(snapshots, key=lambda snapshot: snapshot.modified_ts)
    fullest = max(snapshots, key=lambda snapshot: snapshot.bookmark_count)
    print()
    if preferred_source_id:
        print(f"Primary source default: {preferred_source_id}")
    print(f"Newest source candidate: {newest.store.label}")
    print(f"Fullest source candidate: {fullest.store.label}")


def running_browsers() -> list[str]:
    result = subprocess.run(["ps", "-ax", "-o", "comm="], capture_output=True, text=True, check=False)
    processes = [Path(process.strip()).name for process in result.stdout.splitlines() if process.strip()]
    running: list[str] = []
    for browser, profile in BROWSER_PROFILES.items():
        if profile.executable_name in processes:
            running.append(browser)
    return running


def quit_browser(browser: str) -> None:
    subprocess.run(["osascript", "-e", f'tell application "{get_browser_profile(browser).app_name}" to quit'], check=False)


def browser_executable_path(browser: str) -> Path:
    profile = get_browser_profile(browser)
    candidates = [
        Path("/Applications") / f"{profile.app_name}.app" / "Contents" / "MacOS" / profile.executable_name,
        HOME / "Applications" / f"{profile.app_name}.app" / "Contents" / "MacOS" / profile.executable_name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise SystemExit(f"Could not find executable for {profile.app_name}")


def open_browser(browser: str, extra_args: list[str] | None = None) -> None:
    if extra_args:
        subprocess.Popen(
            [str(browser_executable_path(browser)), *extra_args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    subprocess.run(["open", "-a", get_browser_profile(browser).app_name], check=False)


def ensure_browsers_closed(required_browsers: set[str], auto_close: bool) -> None:
    active = [browser for browser in running_browsers() if browser in required_browsers]
    if not active:
        return

    print("The following browsers are still running:", ", ".join(active))
    if auto_close:
        for browser in active:
            quit_browser(browser)
        for _ in range(10):
            time.sleep(0.5)
            active = [browser for browser in running_browsers() if browser in required_browsers]
            if not active:
                return
        raise SystemExit(f"Could not close browsers automatically: {', '.join(active)}")

    while active:
        answer = input("Close them automatically now? [y/N]: ").strip().lower()
        if answer in {"y", "yes"}:
            for browser in active:
                quit_browser(browser)
            for _ in range(10):
                time.sleep(0.5)
                active = [browser for browser in running_browsers() if browser in required_browsers]
                if not active:
                    return
            raise SystemExit(f"Could not close browsers automatically: {', '.join(active)}")
        raise SystemExit("Cancelled. Close the browsers and run again.")


def backup_target(target: BrowserStore) -> Path:
    DOWNLOAD_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    suffix = target.path.suffix or ".bak"
    backup_path = DOWNLOAD_BACKUP_DIR / f"{timestamp}-{target.id.replace(':', '_')}{suffix}"
    shutil.copy2(target.path, backup_path)
    backup_path.chmod(0o600)
    return backup_path


def print_backup_list() -> int:
    backups: list[tuple[datetime, Path, str]] = []
    if DOWNLOAD_BACKUP_DIR.exists():
        for path in DOWNLOAD_BACKUP_DIR.iterdir():
            if not path.is_file() or path.suffix not in {".bak", ".plist"}:
                continue
            if len(path.name) < 24 or path.name[22] != "-":
                continue
            try:
                created = datetime.strptime(path.name[:22], "%Y%m%d-%H%M%S-%f")
            except ValueError:
                continue
            target = path.name[23 : -len(path.suffix)].replace("_", ":", 1)
            backups.append((created, path, target))

    if not backups:
        print(f"No backups found in {display_path(DOWNLOAD_BACKUP_DIR)}")
        return 0

    now = datetime.now()
    for index, (created, path, target) in enumerate(sorted(backups, reverse=True), start=1):
        seconds = max(0, int((now - created).total_seconds()))
        if seconds < 60:
            age = f"{seconds}s"
        elif seconds < 3600:
            age = f"{seconds // 60}m"
        elif seconds < 86400:
            age = f"{seconds // 3600}h"
        else:
            age = f"{seconds // 86400}d"
        print(
            f"[{index}] target={target} created={created.isoformat(timespec='seconds')} "
            f"age={age} size={path.stat().st_size} path={display_path(path)}"
        )
    return 0


def maybe_backup_target(target: BrowserStore, enabled: bool) -> Path | None:
    if not enabled:
        return None
    return backup_target(target)


def chromium_sync_state(target: BrowserStore) -> tuple[bool, str]:
    if target.format != "chromium" or target.browser not in {"chrome", "edge"}:
        return False, "not a supported Chromium sync target"

    prefs = read_json_dict(target.path.parent / "Preferences")
    sync = prefs.get("sync", {})
    if not isinstance(sync, dict):
        sync = {}

    if target.browser == "chrome":
        consented_to_sync = bool(read_json_dict(target.path.parent / "Preferences").get("google", {}).get("services", {}).get("consented_to_sync"))
        data_types = sync.get("data_type_status_for_sync_to_signin", {})
        if not isinstance(data_types, dict):
            data_types = {}
        bookmarks_enabled = bool(data_types.get("bookmarks"))
        setup_completed = bool(sync.get("has_setup_completed"))
        feature_status = sync.get("feature_status_for_sync_to_signin")
        enabled = bookmarks_enabled and setup_completed and consented_to_sync and feature_status in {2, 3}
        reason = (
            f"bookmarks={'on' if bookmarks_enabled else 'off'}, "
            f"setup={'done' if setup_completed else 'pending'}, "
            f"consented_to_sync={consented_to_sync}, "
            f"feature_status={feature_status}"
        )
        return enabled, reason

    local_state = read_json_dict(target.path.parent.parent / "Local State")
    sync = prefs.get("sync", {})
    if not isinstance(sync, dict):
        sync = {}
    profile_info = local_state.get("profile", {}).get("info_cache", {}).get(target.profile, {})
    if not isinstance(profile_info, dict):
        profile_info = {}

    bookmarks_enabled = bool(sync.get("bookmarks"))
    setup_completed = bool(sync.get("has_setup_completed"))
    edge_sync_enabled = profile_info.get("edge_sync_enabled")
    enabled = bookmarks_enabled and setup_completed and edge_sync_enabled is not False
    reason = (
        f"bookmarks={'on' if bookmarks_enabled else 'off'}, "
        f"setup={'done' if setup_completed else 'pending'}, "
        f"edge_sync_enabled={edge_sync_enabled}"
    )
    return enabled, reason


def supports_cloud_safe_strategy(target: BrowserStore) -> bool:
    return target.format == "chromium" and target.browser in {"chrome", "edge"}


def get_target_issues() -> dict[str, Any]:
    state = load_state()
    issues = state.get(TARGET_ISSUES_STATE_KEY, {})
    return issues if isinstance(issues, dict) else {}


def get_cloud_issue_record(target: BrowserStore) -> dict[str, Any] | None:
    record = get_target_issues().get(target.id)
    return record if isinstance(record, dict) and record.get("cloud_reinjection") else None


def mark_cloud_issue(target: BrowserStore, reason: str) -> None:
    state = load_state()
    issues = state.setdefault(TARGET_ISSUES_STATE_KEY, {})
    if not isinstance(issues, dict):
        issues = {}
        state[TARGET_ISSUES_STATE_KEY] = issues
    issues[target.id] = {
        "cloud_reinjection": True,
        "reason": reason,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_state(state)


def choose_sync_strategy(target: BrowserStore, requested_strategy: str) -> SyncStrategyDecision:
    if requested_strategy == "direct":
        return SyncStrategyDecision(strategy="direct", reason="forced by --sync-strategy=direct")

    if requested_strategy == "cloud-safe":
        if not supports_cloud_safe_strategy(target):
            raise SystemExit("--sync-strategy=cloud-safe currently supports Chrome and Edge targets only")
        enabled, reason = chromium_sync_state(target)
        return SyncStrategyDecision(strategy="cloud-safe", reason=f"forced by --sync-strategy=cloud-safe ({reason})")

    if supports_cloud_safe_strategy(target):
        enabled, reason = chromium_sync_state(target)
        issue = get_cloud_issue_record(target)
        if enabled and issue:
            return SyncStrategyDecision(
                strategy="cloud-safe",
                reason=f"known cloud reinjection issue ({issue.get('reason')}); sync state: {reason}",
            )
        if enabled:
            return SyncStrategyDecision(strategy="direct", reason=f"sync enabled but no issue recorded yet ({reason})")
        return SyncStrategyDecision(strategy="direct", reason=f"sync not active ({reason})")

    return SyncStrategyDecision(strategy="direct", reason=f"{target.browser} uses the default direct path")


def scan_max_chromium_id(node: Any) -> int:
    max_id = 0
    if isinstance(node, dict):
        node_id = node.get("id")
        if isinstance(node_id, str) and node_id.isdigit():
            max_id = max(max_id, int(node_id))
        for value in node.values():
            max_id = max(max_id, scan_max_chromium_id(value))
    elif isinstance(node, list):
        for item in node:
            max_id = max(max_id, scan_max_chromium_id(item))
    return max_id


def chromium_portable_match_key(node: dict[str, Any]) -> tuple[str, ...]:
    if node["type"] == "url":
        return ("url", node.get("name") or node.get("url") or "", node.get("url") or "")
    return ("folder", node.get("name") or "")


def chromium_existing_match_key(node: dict[str, Any]) -> tuple[str, ...] | None:
    node_type = node.get("type")
    if node_type == "url":
        return ("url", node.get("name") or node.get("url") or "", node.get("url") or "")
    if node_type == "folder":
        return ("folder", node.get("name") or "")
    return None


def bucket_chromium_children(children: list[dict[str, Any]]) -> dict[tuple[str, ...], list[dict[str, Any]]]:
    buckets: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for child in children:
        key = chromium_existing_match_key(child)
        if key is None:
            continue
        buckets.setdefault(key, []).append(child)
    return buckets


def chromium_builder(target_raw: dict[str, Any]):
    next_id = scan_max_chromium_id(target_raw) + 1

    def allocate_id() -> str:
        nonlocal next_id
        current = str(next_id)
        next_id += 1
        return current

    def build_node(node: dict[str, Any], existing_node: dict[str, Any] | None = None) -> dict[str, Any]:
        if node["type"] == "url":
            result = copy.deepcopy(existing_node) if existing_node else {}
            result.update(
                {
                    "date_added": result.get("date_added") or chromium_timestamp_now(),
                    "guid": result.get("guid") or str(uuid.uuid4()),
                    "id": result.get("id") or allocate_id(),
                    "name": node.get("name") or node.get("url") or "",
                    "type": "url",
                    "url": node.get("url") or "",
                }
            )
            return result

        child_buckets = bucket_chromium_children(existing_node.get("children", [])) if existing_node else {}
        children = build_nodes(node.get("children", []), child_buckets)
        result = copy.deepcopy(existing_node) if existing_node else {}
        result.update(
            {
                "children": children,
                "date_added": result.get("date_added") or chromium_timestamp_now(),
                "date_modified": chromium_timestamp_now(),
                "guid": result.get("guid") or str(uuid.uuid4()),
                "id": result.get("id") or allocate_id(),
                "name": node.get("name") or "",
                "type": "folder",
            }
        )
        return result

    def build_nodes(
        nodes: list[dict[str, Any]],
        existing_buckets: dict[tuple[str, ...], list[dict[str, Any]]] | None = None,
    ) -> list[dict[str, Any]]:
        built: list[dict[str, Any]] = []
        buckets = existing_buckets or {}
        for node in nodes:
            matches = buckets.get(chromium_portable_match_key(node), [])
            existing_node = matches.pop(0) if matches else None
            built.append(build_node(node, existing_node))
        return built

    return build_nodes


def apply_portable_to_chromium(
    source: BookmarkSnapshot,
    target_raw: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    result = copy.deepcopy(target_raw)
    build_nodes = chromium_builder(result)

    roots = result.setdefault("roots", {})
    bookmark_bar_root = roots.setdefault("bookmark_bar", {"children": [], "name": "Bookmarks bar", "type": "folder"})
    other_root = roots.setdefault("other", {"children": [], "name": "Other bookmarks", "type": "folder"})
    synced_root = roots.setdefault("synced", {"children": [], "name": "Mobile bookmarks", "type": "folder"})

    bar_children = build_nodes(source.portable.get("bar", []), bucket_chromium_children(bookmark_bar_root.get("children", [])))
    menu_children = build_nodes(source.portable.get("menu", []), bucket_chromium_children(other_root.get("children", [])))
    source_synced = source.portable.get("synced", [])
    synced_children = build_nodes(source_synced, bucket_chromium_children(synced_root.get("children", [])))

    bookmark_bar_root["children"] = bar_children
    bookmark_bar_root["date_modified"] = chromium_timestamp_now()
    other_root["children"] = menu_children
    other_root["date_modified"] = chromium_timestamp_now()

    if source_synced or mode == "strict":
        synced_root["children"] = synced_children
        synced_root["date_modified"] = chromium_timestamp_now()

    result["checksum"] = ""
    return result


def safari_make_leaf(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "URIDictionary": {"title": node.get("name") or node.get("url") or ""},
        "URLString": node.get("url") or "",
        "WebBookmarkType": "WebBookmarkTypeLeaf",
        "WebBookmarkUUID": str(uuid.uuid4()).upper(),
        "Sync": {},
        "ReadingListNonSync": {"neverFetchMetadata": False},
        "dateAdded": datetime.now(),
    }


def safari_portable_match_key(node: dict[str, Any]) -> tuple[str, ...]:
    if node["type"] == "url":
        return ("url", node.get("name") or node.get("url") or "", node.get("url") or "")
    return ("folder", node.get("name") or "")


def safari_existing_match_key(node: dict[str, Any]) -> tuple[str, ...] | None:
    node_type = node.get("WebBookmarkType")
    if node_type == "WebBookmarkTypeLeaf":
        return ("url", safari_leaf_name(node), node.get("URLString") or "")
    if node_type == "WebBookmarkTypeList":
        return ("folder", node.get("Title") or "")
    return None


def bucket_safari_children(children: list[dict[str, Any]]) -> dict[tuple[str, ...], list[dict[str, Any]]]:
    buckets: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for child in children:
        key = safari_existing_match_key(child)
        if key is None:
            continue
        buckets.setdefault(key, []).append(child)
    return buckets


def safari_builder():
    def build_node(node: dict[str, Any], existing_node: dict[str, Any] | None = None) -> dict[str, Any]:
        if node["type"] == "url":
            result = copy.deepcopy(existing_node) if existing_node else {}
            result.update(
                {
                    "URIDictionary": {"title": node.get("name") or node.get("url") or ""},
                    "URLString": node.get("url") or "",
                    "WebBookmarkType": "WebBookmarkTypeLeaf",
                    "WebBookmarkUUID": result.get("WebBookmarkUUID") or str(uuid.uuid4()).upper(),
                    "Sync": result.get("Sync") or {},
                    "ReadingListNonSync": result.get("ReadingListNonSync") or {"neverFetchMetadata": False},
                    "dateAdded": result.get("dateAdded") or datetime.now(),
                }
            )
            return result

        child_buckets = bucket_safari_children(existing_node.get("Children", [])) if existing_node else {}
        children = build_nodes(node.get("children", []), child_buckets)
        result = copy.deepcopy(existing_node) if existing_node else {}
        result.update(
            {
                "WebBookmarkUUID": result.get("WebBookmarkUUID") or str(uuid.uuid4()).upper(),
                "Children": children,
                "Sync": result.get("Sync") or {},
                "WebBookmarkType": "WebBookmarkTypeList",
                "Title": node.get("name") or "",
            }
        )
        return result

    def build_nodes(
        nodes: list[dict[str, Any]],
        existing_buckets: dict[tuple[str, ...], list[dict[str, Any]]] | None = None,
    ) -> list[dict[str, Any]]:
        built: list[dict[str, Any]] = []
        buckets = existing_buckets or {}
        for node in nodes:
            matches = buckets.get(safari_portable_match_key(node), [])
            existing_node = matches.pop(0) if matches else None
            built.append(build_node(node, existing_node))
        return built

    return build_nodes


def apply_portable_to_safari(source: BookmarkSnapshot, target_raw: dict[str, Any], mode: str) -> dict[str, Any]:
    result = copy.deepcopy(target_raw)
    bookmarks_bar = find_safari_root(result, "BookmarksBar")
    bookmarks_menu = find_safari_root(result, "BookmarksMenu")
    if bookmarks_bar is None or bookmarks_menu is None:
        raise SystemExit("Safari Bookmarks.plist is missing BookmarksBar or BookmarksMenu roots")
    build_nodes = safari_builder()

    menu_nodes = list(source.portable.get("menu", []))
    if source.portable.get("synced"):
        menu_nodes.append(
            {
                "type": "folder",
                "name": "Synced",
                "children": copy.deepcopy(source.portable["synced"]),
            }
        )

    bookmarks_bar["Children"] = build_nodes(source.portable.get("bar", []), bucket_safari_children(bookmarks_bar.get("Children", [])))
    bookmarks_menu["Children"] = build_nodes(menu_nodes, bucket_safari_children(bookmarks_menu.get("Children", [])))

    if mode == "strict" and not source.portable.get("synced"):
        bookmarks_menu["Children"] = build_nodes(
            source.portable.get("menu", []),
            bucket_safari_children(bookmarks_menu.get("Children", [])),
        )

    return result


def write_chromium_snapshot(source: BookmarkSnapshot, target: BrowserStore, mode: str) -> None:
    target_raw = json.loads(target.path.read_text())
    result = apply_portable_to_chromium(source, target_raw, mode)
    atomic_write_text(target.path, json.dumps(result, indent=3, ensure_ascii=False))


def write_safari_snapshot(source: BookmarkSnapshot, target: BrowserStore, mode: str) -> None:
    with target.path.open("rb") as handle:
        target_raw = plistlib.load(handle)
    result = apply_portable_to_safari(source, target_raw, mode)
    atomic_write_bytes(target.path, plistlib.dumps(result, sort_keys=False))


def verify_snapshot_counts(target: BrowserStore, expected_source: BookmarkSnapshot) -> SyncVerification:
    reloaded = load_snapshot(target)
    same_count = reloaded.bookmark_count == expected_source.bookmark_count
    same_portable = reloaded.portable == expected_source.portable
    return SyncVerification(
        reloaded=reloaded,
        same_count=same_count,
        same_portable=same_portable,
        source_signature=portable_signature(expected_source.portable),
        target_signature=portable_signature(reloaded.portable),
    )


def verify_snapshot_with_stabilization(target: BrowserStore, expected_source: BookmarkSnapshot) -> SyncVerification:
    profile = get_stabilization_profile(target.browser)
    verification = verify_snapshot_counts(target, expected_source)
    last_signature = verification.target_signature
    last_modified_ts = verification.reloaded.modified_ts
    stable_passes = 1
    observed_external_changes = False
    verification_passes = 1
    deadline = time.monotonic() + profile["settle_seconds"]

    while time.monotonic() < deadline:
        time.sleep(profile["poll_interval"])
        current = verify_snapshot_counts(target, expected_source)
        verification_passes += 1

        changed = current.target_signature != last_signature or current.reloaded.modified_ts != last_modified_ts
        if changed:
            observed_external_changes = True
            stable_passes = 1
        else:
            stable_passes += 1

        verification = current
        last_signature = current.target_signature
        last_modified_ts = current.reloaded.modified_ts
        if stable_passes >= profile["stable_passes"]:
            break

    verification.verification_passes = verification_passes
    verification.observed_external_changes = observed_external_changes
    verification.stabilized = stable_passes >= profile["stable_passes"]
    return verification


def record_sync_baseline(source: BrowserStore, target: BrowserStore, mode: str, verification: SyncVerification) -> None:
    state = load_state()
    baselines = state.setdefault(SYNC_BASELINES_STATE_KEY, {})
    baselines[state_key(source, target, mode)] = {
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "source": source.id,
        "target": target.id,
        "mode": mode,
        "source_signature": verification.source_signature,
        "target_signature": verification.target_signature,
        "bookmark_count": verification.reloaded.bookmark_count,
    }
    save_state(state)


def record_sync_observation(source: BrowserStore, target: BrowserStore, mode: str, verification: SyncVerification) -> None:
    state = load_state()
    observations = state.setdefault(SYNC_OBSERVATIONS_STATE_KEY, [])
    if not isinstance(observations, list):
        observations = []
        state[SYNC_OBSERVATIONS_STATE_KEY] = observations
    stabilization_profile = get_stabilization_profile(target.browser)
    repair_profile = get_repair_profile(target.browser)
    observations.append(
        {
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
            "source": source.id,
            "target": target.id,
            "target_browser": target.browser,
            "mode": mode,
            "same_count": verification.same_count,
            "same_portable": verification.same_portable,
            "verification_passes": verification.verification_passes,
            "observed_external_changes": verification.observed_external_changes,
            "stabilized": verification.stabilized,
            "repair_attempts": verification.repair_attempts,
            "bookmark_count": verification.reloaded.bookmark_count,
            "settle_seconds": stabilization_profile["settle_seconds"],
            "poll_interval": stabilization_profile["poll_interval"],
            "stable_passes": stabilization_profile["stable_passes"],
            "repair_profile": repair_profile,
        }
    )
    state[SYNC_OBSERVATIONS_STATE_KEY] = observations[-100:]
    save_state(state)


def get_recorded_baseline(source: BrowserStore, target: BrowserStore, mode: str) -> dict[str, Any] | None:
    state = load_state()
    baselines = state.get(SYNC_BASELINES_STATE_KEY, {})
    record = baselines.get(state_key(source, target, mode))
    return record if isinstance(record, dict) else None


def attach_recorded_baseline(record: dict[str, Any] | None, verification: SyncVerification) -> None:
    if not record:
        return
    verification.baseline_signature = record.get("target_signature")
    verification.matches_recorded_baseline = verification.target_signature == verification.baseline_signature


def explain_edge_cloud_reinjection(source: BookmarkSnapshot, verification: SyncVerification) -> None:
    if source.store.browser != "chrome" or verification.reloaded.store.browser != "edge":
        return
    if verification.same_portable:
        return

    print("Verification note: local overwrite succeeded, but the current Edge bookmark tree no longer matches the source exactly.")
    if verification.matches_recorded_baseline is False:
        print("This differs from the last recorded successful strict-sync baseline, which strongly suggests Edge account/cloud sync re-injected bookmarks after restart.")
    else:
        print("This usually means Edge account/cloud sync re-injected extra bookmarks after restart, not that the local write failed.")

    print("Bookmark titles and URLs are omitted from diagnostics to avoid leaking browsing data into logs.")


def write_target_store(source: BookmarkSnapshot, target: BrowserStore, mode: str) -> None:
    get_format_handler(target.format).write_snapshot(source, target, mode)


def clear_chromium_bookmark_file(target: BrowserStore) -> None:
    raw = json.loads(target.path.read_text())
    roots = raw.setdefault("roots", {})
    timestamp = chromium_timestamp_now()
    for key in ("bookmark_bar", "other", "synced"):
        root = roots.setdefault(key, {"children": [], "name": key, "type": "folder"})
        root["children"] = []
        root["date_modified"] = timestamp
    raw["checksum"] = ""
    atomic_write_text(target.path, json.dumps(raw, indent=3, ensure_ascii=False))


def render_chromium_bookmark_purge_worker(active_seconds: float) -> str:
    active_window_ms = max(1, int(active_seconds * 1000))
    return f"""const ACTIVE_WINDOW_MS = {active_window_ms};
const ALARM_NAME = "edgeBookmarkPurge";

function storageGet(keys) {{
  return new Promise((resolve) => chrome.storage.local.get(keys, resolve));
}}

function storageSet(values) {{
  return new Promise((resolve) => chrome.storage.local.set(values, resolve));
}}

function createAlarm() {{
  chrome.alarms.create(ALARM_NAME, {{ delayInMinutes: 0.1, periodInMinutes: 0.5 }});
}}

function clearAlarm() {{
  chrome.alarms.clear(ALARM_NAME);
}}

function getTree() {{
  return new Promise((resolve, reject) => {{
    chrome.bookmarks.getTree((tree) => {{
      if (chrome.runtime.lastError) {{
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }}
      resolve(tree);
    }});
  }});
}}

function removeNode(id, hasChildren) {{
  return new Promise((resolve) => {{
    const callback = () => resolve();
    if (hasChildren) {{
      chrome.bookmarks.removeTree(id, callback);
      return;
    }}
    chrome.bookmarks.remove(id, callback);
  }});
}}

async function purgePermanentNodeChildren(node) {{
  for (const child of node.children || []) {{
    await removeNode(child.id, !!(child.children && child.children.length));
  }}
}}

async function purgeAllBookmarks() {{
  const tree = await getTree();
  const root = tree[0] || {{}};
  for (const child of root.children || []) {{
    await purgePermanentNodeChildren(child);
  }}
}}

async function ensureDeadline(forceReset) {{
  const now = Date.now();
  const state = await storageGet(["deadlineMs"]);
  const existing = Number(state.deadlineMs || 0);
  if (!forceReset && existing > now) {{
    return existing;
  }}
  const deadlineMs = now + ACTIVE_WINDOW_MS;
  await storageSet({{ deadlineMs }});
  return deadlineMs;
}}

async function purgeWhileActive(forceReset) {{
  const deadlineMs = await ensureDeadline(forceReset);
  if (Date.now() > deadlineMs) {{
    clearAlarm();
    return;
  }}
  createAlarm();
  try {{
    await purgeAllBookmarks();
  }} catch (_error) {{
  }}
}}

chrome.runtime.onInstalled.addListener(() => {{
  void purgeWhileActive(true);
}});

chrome.runtime.onStartup.addListener(() => {{
  void purgeWhileActive(false);
}});

chrome.alarms.onAlarm.addListener((alarm) => {{
  if (alarm.name === ALARM_NAME) {{
    void purgeWhileActive(false);
  }}
}});

chrome.bookmarks.onCreated.addListener(() => {{
  void purgeWhileActive(false);
}});

chrome.bookmarks.onChanged.addListener(() => {{
  void purgeWhileActive(false);
}});

chrome.bookmarks.onMoved.addListener(() => {{
  void purgeWhileActive(false);
}});

chrome.bookmarks.onChildrenReordered.addListener(() => {{
  void purgeWhileActive(false);
}});

void purgeWhileActive(false);
"""


def create_chromium_bookmark_purge_extension(active_seconds: float) -> tempfile.TemporaryDirectory[str]:
    extension_dir = tempfile.TemporaryDirectory(prefix="chromium-bookmark-purge-")
    extension_path = Path(extension_dir.name)
    manifest = {
        "manifest_version": 3,
        "name": "Bookmark Sync Temporary Purge",
        "version": "1.0.0",
        "background": {"service_worker": "service-worker.js"},
        "permissions": ["alarms", "bookmarks", "storage"],
    }
    atomic_write_text(extension_path / "manifest.json", json.dumps(manifest, indent=2))
    atomic_write_text(extension_path / "service-worker.js", render_chromium_bookmark_purge_worker(active_seconds))
    return extension_dir


def load_snapshot_with_retry(store: BrowserStore, attempts: int = 5, delay_seconds: float = 0.25) -> BookmarkSnapshot:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return load_snapshot(store)
        except (json.JSONDecodeError, OSError, plistlib.InvalidFileException) as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(delay_seconds)
    if last_error is None:
        raise SystemExit(f"Could not load bookmark store: {store.label}")
    raise SystemExit(f"Could not load bookmark store {store.label}: {last_error}") from last_error


def wait_for_bookmark_count(
    target: BrowserStore,
    expected_count: int,
    timeout_seconds: float,
    poll_interval_seconds: float,
    stable_passes: int = 2,
) -> BookmarkSnapshot:
    deadline = time.time() + timeout_seconds
    matched_passes = 0
    latest = load_snapshot_with_retry(target)
    while True:
        latest = load_snapshot_with_retry(target)
        if latest.bookmark_count == expected_count:
            matched_passes += 1
            if matched_passes >= stable_passes:
                return latest
        else:
            matched_passes = 0
        if time.time() >= deadline:
            return latest
        time.sleep(poll_interval_seconds)


def repair_sync_if_needed(source: BookmarkSnapshot, target: BrowserStore, mode: str, verification: SyncVerification) -> SyncVerification:
    profile = get_repair_profile(target.browser)
    if not profile:
        return verification

    current = verification
    attempts = 0
    while attempts < profile["max_attempts"] and not current.same_portable:
        attempts += 1
        time.sleep(profile["retry_wait_seconds"])
        write_target_store(source, target, mode)
        current = verify_snapshot_with_stabilization(target, source)
        current.repair_attempts = attempts
        if current.same_portable and not current.observed_external_changes:
            break
    return current


def sync_store(source: BookmarkSnapshot, target: BrowserStore, mode: str, backup_enabled: bool = True) -> tuple[Path | None, SyncVerification]:
    backup_path = maybe_backup_target(target, backup_enabled)
    previous_baseline = get_recorded_baseline(source.store, target, mode)
    write_target_store(source, target, mode)
    verification = verify_snapshot_with_stabilization(target, source)
    verification = repair_sync_if_needed(source, target, mode, verification)
    reloaded = verification.reloaded
    attach_recorded_baseline(previous_baseline, verification)
    record_sync_observation(source.store, target, mode, verification)
    if mode == "strict" and not verification.same_portable:
        raise SystemExit(f"Verification mismatch for {target.label}: strict sync did not produce an exact match")
    if not verification.same_count and not (target.format == "safari" and reloaded.bookmark_count >= source.bookmark_count):
        raise SystemExit(
            f"Verification mismatch for {target.label}: expected about {source.bookmark_count} bookmarks, got {reloaded.bookmark_count}"
        )
    record_sync_baseline(source.store, target, mode, verification)
    return backup_path, verification


def restore_store_backup(backup_path: Path, target: BrowserStore) -> tuple[Path, BookmarkSnapshot]:
    if not backup_path.is_file():
        raise SystemExit(f"Backup file not found: {display_path(backup_path)}")
    backup_store = BrowserStore(
        id=target.id,
        browser=target.browser,
        profile=target.profile,
        format=target.format,
        path=backup_path,
        label=f"backup:{backup_path.name}",
    )
    expected = load_snapshot(backup_store)
    rollback_path = backup_target(target)
    atomic_write_bytes(target.path, backup_path.read_bytes())
    restored = load_snapshot(target)
    if restored.portable != expected.portable:
        atomic_write_bytes(target.path, rollback_path.read_bytes())
        raise SystemExit(f"Restore verification failed for {target.label}; current store was rolled back")
    return rollback_path, restored


def require_cloud_purge_consent(allowed: bool) -> None:
    if not allowed:
        raise SystemExit("Cloud bookmark purge requires explicit --allow-cloud-purge consent")


def require_cloud_purge_backup(backup_enabled: bool) -> None:
    if not backup_enabled:
        raise SystemExit("Cloud bookmark purge cannot run with --no-backup")


def rollback_failed_cloud_purge(target: BrowserStore, backup_path: Path) -> None:
    try:
        ensure_browsers_closed({target.browser}, auto_close=True)
    except SystemExit as exc:
        print(f"Cloud purge rollback could not close {target.browser}: {exc}. Backup: {display_path(backup_path)}")
        return

    try:
        atomic_write_bytes(target.path, backup_path.read_bytes())
        restored = load_snapshot(target)
        print(f"Cloud purge failed; restored local {target.label} from {display_path(backup_path)} ({restored.bookmark_count} bookmarks).")
    except (OSError, json.JSONDecodeError, plistlib.InvalidFileException) as exc:
        print(f"Cloud purge rollback failed for {target.label}: {exc}. Backup: {display_path(backup_path)}")


def reset_edge_cloud_via_empty_file_then_sync(
    source: BookmarkSnapshot,
    target: BrowserStore,
    mode: str,
    wait_seconds: float,
    backup_enabled: bool = True,
) -> tuple[Path | None, Path | None, SyncVerification]:
    if target.browser != "edge" or target.format != "chromium":
        raise SystemExit("--edge-empty-then-sync only supports edge Chromium targets")
    require_cloud_purge_backup(backup_enabled)

    clear_backup = backup_target(target)
    try:
        open_browser(target.browser)
        clear_chromium_bookmark_file(target)
        cleared = load_snapshot(target)
        print(f"Edge empty phase: cleared local bookmark file to {cleared.bookmark_count} bookmarks.")
        print(f"Edge empty phase backup: {display_path(clear_backup)}")
        print(f"Waiting {wait_seconds:g}s for Edge to observe and sync the empty bookmark state...")
        time.sleep(wait_seconds)
        after_wait = load_snapshot(target)
        print(f"Edge empty phase after wait: {after_wait.bookmark_count} bookmarks.")

        ensure_browsers_closed({target.browser}, auto_close=True)
        sync_backup, verification = sync_store(source, target, mode, backup_enabled=True)
        return clear_backup, sync_backup, verification
    except BaseException:
        rollback_failed_cloud_purge(target, clear_backup)
        raise


def reset_chromium_cloud_via_api_then_sync(
    source: BookmarkSnapshot,
    target: BrowserStore,
    mode: str,
    purge_window_seconds: float,
    settle_wait_seconds: float,
    backup_enabled: bool = True,
) -> tuple[Path | None, Path | None, SyncVerification]:
    if not supports_cloud_safe_strategy(target):
        raise SystemExit("--edge-api-clear-then-sync only supports Chrome and Edge Chromium targets")
    require_cloud_purge_backup(backup_enabled)

    clear_backup = backup_target(target)
    try:
        with create_chromium_bookmark_purge_extension(purge_window_seconds) as extension_dir:
            print(f"{target.browser.capitalize()} API purge phase: launching the browser with a temporary bookmark-purge extension.")
            open_browser(
                target.browser,
                extra_args=[
                    "--no-first-run",
                    f"--profile-directory={target.profile}",
                    f"--load-extension={extension_dir}",
                ],
            )
            cleared = wait_for_bookmark_count(
                target,
                expected_count=0,
                timeout_seconds=purge_window_seconds,
                poll_interval_seconds=EDGE_API_POLL_INTERVAL_SECONDS,
            )
            print(f"{target.browser.capitalize()} API purge phase reached {cleared.bookmark_count} bookmarks.")
            if cleared.bookmark_count != 0:
                raise SystemExit(f"{target.browser.capitalize()} API purge did not reach an empty bookmark state before timeout")
            print(f"Waiting {settle_wait_seconds:g}s with {target.browser.capitalize()} open so bookmark deletions can propagate through cloud sync...")
            time.sleep(settle_wait_seconds)
            after_wait = load_snapshot_with_retry(target)
            print(f"{target.browser.capitalize()} API purge phase after settle wait: {after_wait.bookmark_count} bookmarks.")
            if after_wait.bookmark_count != 0:
                raise SystemExit(f"{target.browser.capitalize()} API purge did not stay empty through the cloud-settle wait")
            ensure_browsers_closed({target.browser}, auto_close=True)

        sync_backup, verification = sync_store(source, target, mode, backup_enabled=True)
        mark_cloud_issue(target, f"cloud-safe remediation succeeded for {target.id}")
        return clear_backup, sync_backup, verification
    except BaseException:
        rollback_failed_cloud_purge(target, clear_backup)
        raise


def choose_one(prompt: str, items: list[BookmarkSnapshot], default_index: int) -> BookmarkSnapshot:
    while True:
        answer = input(f"{prompt} [{default_index + 1}]: ").strip()
        if not answer:
            return items[default_index]
        if answer.isdigit() and 1 <= int(answer) <= len(items):
            return items[int(answer) - 1]
        print("Please enter a valid number.")


def choose_many(prompt: str, items: list[BookmarkSnapshot], default_indices: list[int]) -> list[BookmarkSnapshot]:
    default_text = ",".join(str(index + 1) for index in default_indices)
    while True:
        answer = input(f"{prompt} [{default_text}]: ").strip()
        if not answer:
            selected = default_indices
        else:
            parts = [part.strip() for part in answer.split(",") if part.strip()]
            if not parts or not all(part.isdigit() for part in parts):
                print("Please enter numbers separated by commas.")
                continue
            selected = [int(part) - 1 for part in parts]
            if any(index < 0 or index >= len(items) for index in selected):
                print("Please enter valid numbers.")
                continue
        deduped = []
        seen = set()
        for index in selected:
            if index not in seen:
                seen.add(index)
                deduped.append(index)
        return [items[index] for index in deduped]


def choose_mode(default: str = "strict") -> str:
    options = {
        "1": "preview",
        "2": "mirror",
        "3": "strict",
    }
    defaults = {"preview": "1", "mirror": "2", "strict": "3"}
    print()
    print("Sync modes:")
    print("  [1] preview  - inspect only, write nothing")
    print("  [2] mirror   - replace mapped bookmark roots, preserve unmapped target-only areas where possible")
    print("  [3] strict   - strongest local mirror; also clears target synced root when source has none")
    while True:
        answer = input(f"Choose mode [{defaults[default]}]: ").strip()
        if not answer:
            return default
        if answer in options:
            return options[answer]
        if answer in {"preview", "mirror", "strict"}:
            return answer
        print("Please choose 1, 2, or 3.")


def resolve_store(identifier: str, snapshots: list[BookmarkSnapshot]) -> BookmarkSnapshot:
    matches = [snapshot for snapshot in snapshots if snapshot.store.id == identifier or snapshot.store.label == identifier]
    if not matches:
        raise SystemExit(f"Unknown browser/profile: {identifier}")
    return matches[0]


def resolve_primary_source(snapshots: list[BookmarkSnapshot]) -> BookmarkSnapshot:
    primary_store_id = get_primary_store_id()
    if primary_store_id:
        for snapshot in snapshots:
            if snapshot.store.id == primary_store_id:
                return snapshot
    return max(snapshots, key=lambda snapshot: snapshot.modified_ts)


def default_targets_for_source(source: BookmarkSnapshot, snapshots: list[BookmarkSnapshot]) -> list[BookmarkSnapshot]:
    return [snapshot for snapshot in snapshots if snapshot.store.id != source.store.id]


def print_order_warning(source: BookmarkSnapshot, verification: SyncVerification) -> None:
    target = verification.reloaded
    source_names = portable_bar_names(source.portable)
    target_names = portable_bar_names(target.portable)
    if source_names == target_names and verification.same_portable:
        return
    if source_names != target_names:
        print("Warning: bookmark bar order still differs after sync.")
    elif not verification.same_portable:
        print("Warning: target still differs from source after sync.")
    if source.store.browser == "chrome" and target.store.browser == "edge":
        explain_edge_cloud_reinjection(source, verification)
        if verification.same_portable:
            return
        print("If this keeps happening, turn off Edge bookmark sync or re-run in strict mode after closing Edge.")


def print_verification_summary(verification: SyncVerification) -> None:
    if verification.same_portable:
        print("Verification: target matches source after the stabilization window.")
    else:
        print("Verification: target still differs from source after the stabilization window.")
    if verification.repair_attempts:
        print(f"Edge remediation: applied {verification.repair_attempts} repair pass(es) after the initial sync.")
    if verification.verification_passes > 1:
        print(f"Verification window: {verification.verification_passes} checks")
    if verification.observed_external_changes:
        print("Verification note: target changed again during post-write stabilization, which suggests browser or cloud sync activity.")
    if not verification.stabilized:
        print("Verification note: target did not become stable before the verification window ended.")


def run_post_open_check(
    source: BookmarkSnapshot,
    target: BrowserStore,
    mode: str,
    repair_on_drift: bool = True,
    backup_enabled: bool = True,
) -> tuple[bool, Path | None, SyncVerification]:
    profile = POST_OPEN_CHECK_PROFILES.get(target.browser)
    if profile is None:
        raise SystemExit(f"Post-open check is not configured for {target.browser}")

    open_browser(target.browser)
    time.sleep(profile["delay_seconds"])

    checks = 1
    drift_detected = False
    current = verify_snapshot_counts(target, source)
    if not current.same_portable:
        drift_detected = True

    while checks < profile["max_checks"]:
        time.sleep(profile["poll_interval"])
        current = verify_snapshot_counts(target, source)
        checks += 1
        if not current.same_portable:
            drift_detected = True

    current.verification_passes = checks
    if not drift_detected and current.same_portable:
        return False, None, current

    if not repair_on_drift:
        ensure_browsers_closed({target.browser}, auto_close=True)
        return True, None, current

    ensure_browsers_closed({target.browser}, auto_close=True)
    backup_path, repaired = sync_store(source, target, mode, backup_enabled=backup_enabled)
    return True, backup_path, repaired


def maybe_run_post_open_check(
    source: BookmarkSnapshot,
    target: BrowserStore,
    mode: str,
    enabled_browsers: set[str],
    backup_enabled: bool = True,
) -> None:
    if target.browser not in enabled_browsers:
        return

    print(f"Post-open check: launching {target.browser}, waiting for delayed sync activity, then evaluating drift.")
    drifted, backup_path, verification = run_post_open_check(source, target, mode, backup_enabled=backup_enabled)
    if drifted:
        print(f"Post-open check: {target.browser} drifted after opening; repaired with browser closed.")
        if backup_path is not None:
            print(f"Repair backup: {display_path(backup_path)}")
        print_verification_summary(verification)
        print_order_warning(source, verification)
    else:
        print(f"Post-open check: {target.browser} stayed aligned after opening.")


def should_auto_probe_after_direct(target: BrowserStore, strategy: str) -> bool:
    if strategy != "auto":
        return False
    if not supports_cloud_safe_strategy(target):
        return False
    if target.browser not in POST_OPEN_CHECK_PROFILES:
        return False
    enabled, _reason = chromium_sync_state(target)
    if not enabled:
        return False
    return get_cloud_issue_record(target) is None


def normalize_browser_filter(value: str | None) -> str:
    if value is None:
        return "all"
    normalized = value.strip().lower()
    if normalized == "all" or normalized in supported_browsers():
        return normalized
    raise SystemExit(f"Unknown browser filter: {value}")


def filtered_observations(browser: str) -> list[dict[str, Any]]:
    state = load_state()
    observations = state.get(SYNC_OBSERVATIONS_STATE_KEY, [])
    if not isinstance(observations, list):
        return []
    if browser == "all":
        return [item for item in observations if isinstance(item, dict)]
    return [
        item
        for item in observations
        if isinstance(item, dict) and item.get("target_browser") == browser
    ]


def print_doctor_report(browser: str) -> int:
    browsers = supported_browsers() if browser == "all" else [browser]
    stores_by_browser = {store.browser: store for store in detect_browser_stores()}
    any_output = False
    for browser_name in browsers:
        observations = filtered_observations(browser_name)
        stabilization_profile = get_stabilization_profile(browser_name)
        repair_profile = get_repair_profile(browser_name)
        print(f"[{browser_name}]")
        store = stores_by_browser.get(browser_name)
        if store is not None and supports_cloud_safe_strategy(store):
            enabled, reason = chromium_sync_state(store)
            print(f"Current sync state: {'enabled' if enabled else 'disabled'} ({reason})")
            issue = get_cloud_issue_record(store)
            if issue is None:
                print("Known cloud issue: none recorded")
            else:
                print(f"Known cloud issue: yes ({issue.get('reason')})")
        print(
            "Stabilization profile:"
            f" settle={stabilization_profile['settle_seconds']}s"
            f" poll={stabilization_profile['poll_interval']}s"
            f" stable_passes={stabilization_profile['stable_passes']}"
        )
        if repair_profile:
            print(
                "Repair profile:"
                f" max_attempts={repair_profile['max_attempts']}"
                f" retry_wait={repair_profile['retry_wait_seconds']}s"
            )
        else:
            print("Repair profile: none")
        if not observations:
            print("Observations: none yet")
            print()
            continue
        any_output = True
        total = len(observations)
        exact_matches = sum(1 for item in observations if item.get("same_portable"))
        external_changes = sum(1 for item in observations if item.get("observed_external_changes"))
        repaired = sum(1 for item in observations if (item.get("repair_attempts") or 0) > 0)
        unstable = sum(1 for item in observations if not item.get("stabilized"))
        max_passes = max(int(item.get("verification_passes", 1)) for item in observations)
        print(
            f"Observations: {total}, exact_match={exact_matches}, "
            f"external_changes={external_changes}, repaired={repaired}, unstable={unstable}"
        )
        print(f"Max verification passes: {max_passes}")
        last = observations[-1]
        print(
            "Last run:"
            f" target={last.get('target')}"
            f" mode={last.get('mode')}"
            f" matches={last.get('same_portable')}"
            f" repairs={last.get('repair_attempts')}"
            f" at={last.get('recorded_at')}"
        )
        print()
    if not any_output and browser == "all":
        print("No sync observations recorded yet.")
    return 0


def calibrate_browser_profiles(browser: str) -> int:
    browsers = supported_browsers() if browser == "all" else [browser]
    state = load_state()
    overrides = state.setdefault(BROWSER_PROFILE_OVERRIDES_STATE_KEY, {})
    if not isinstance(overrides, dict):
        overrides = {}
        state[BROWSER_PROFILE_OVERRIDES_STATE_KEY] = overrides
    stabilization_overrides = overrides.setdefault("stabilization", {})
    repair_overrides = overrides.setdefault("repair", {})

    for browser_name in browsers:
        observations = filtered_observations(browser_name)
        if not observations:
            print(f"[{browser_name}] no observations, profile unchanged")
            continue
        current_stabilization = get_stabilization_profile(browser_name)
        poll_interval = float(current_stabilization["poll_interval"])
        max_passes = max(int(item.get("verification_passes", 1)) for item in observations)
        unstable = any(not item.get("stabilized") for item in observations)
        external_changes = any(item.get("observed_external_changes") for item in observations)
        recommended_settle = max(float(current_stabilization["settle_seconds"]), max(1.0, max_passes - 1) * poll_interval)
        if external_changes:
            recommended_settle += poll_interval
        if unstable:
            recommended_settle += poll_interval * 2
        stabilization_overrides[browser_name] = {
            "settle_seconds": round(recommended_settle, 1),
            "poll_interval": poll_interval,
            "stable_passes": int(current_stabilization["stable_passes"]),
        }

        current_repair = get_repair_profile(browser_name)
        if current_repair:
            max_repairs_seen = max(int(item.get("repair_attempts", 0)) for item in observations)
            recommended_attempts = max(int(current_repair["max_attempts"]), min(3, max_repairs_seen + 1))
            repair_overrides[browser_name] = {
                "max_attempts": recommended_attempts,
                "retry_wait_seconds": float(current_repair["retry_wait_seconds"]),
            }
            print(
                f"[{browser_name}] calibrated settle={round(recommended_settle, 1)}s "
                f"poll={poll_interval}s stable_passes={current_stabilization['stable_passes']} "
                f"repair_attempts={recommended_attempts}"
            )
        else:
            print(
                f"[{browser_name}] calibrated settle={round(recommended_settle, 1)}s "
                f"poll={poll_interval}s stable_passes={current_stabilization['stable_passes']}"
            )

    save_state(state)
    return 0


def interactive_mode(snapshots: list[BookmarkSnapshot], auto_close: bool) -> int:
    default_source = resolve_primary_source(snapshots)
    summarize_snapshots(snapshots, preferred_source_id=default_source.store.id)
    default_targets = [
        index
        for index, snapshot in enumerate(snapshots)
        if snapshot.store.id != default_source.store.id
    ]

    print()
    source = choose_one("Choose source", snapshots, snapshots.index(default_source))
    targets = choose_many("Choose target(s)", snapshots, default_targets)
    targets = [target for target in targets if target.store.id != source.store.id]
    if not targets:
        raise SystemExit("No targets selected.")

    mode = choose_mode(default="strict")
    print()
    print(f"Source: {source.store.label}")
    print("Targets:", ", ".join(target.store.label for target in targets))
    print(f"Mode: {mode}")

    if mode == "preview":
        return preview_run(source, targets)

    confirm = input("Proceed? [y/N]: ").strip().lower()
    if confirm not in {"y", "yes"}:
        raise SystemExit("Cancelled.")

    required_browsers = {source.store.browser, *(target.store.browser for target in targets)}
    ensure_browsers_closed(required_browsers, auto_close=auto_close)
    source = load_snapshot_with_retry(source.store)

    for target in targets:
        backup_path, verification = sync_store(source, target.store, mode)
        reloaded = verification.reloaded
        print(f"Synced {source.store.label} -> {target.store.label}")
        print(f"Backup: {display_path(backup_path)}")
        print(f"Result: {reloaded.bookmark_count} bookmarks, modified {now_string(reloaded.modified_ts)}")
        print_verification_summary(verification)
        print_order_warning(source, verification)
        print()
    return 0


def preview_run(source: BookmarkSnapshot, targets: list[BookmarkSnapshot]) -> int:
    print()
    print(f"Preview source: {source.store.label}")
    print(f"Bookmarks: {source.bookmark_count}, folders: {source.folder_count}")
    print("Bookmark bar order:")
    for index, name in enumerate(portable_bar_names(source.portable), start=1):
        print(f"  {index:>2}. {name}")
    print()
    for target in targets:
        print(f"Target preview: {target.store.label}")
        print(f"  current bookmarks={target.bookmark_count}, folders={target.folder_count}, modified={now_string(target.modified_ts)}")
    return 0


def noninteractive_mode(args: argparse.Namespace, snapshots: list[BookmarkSnapshot]) -> int:
    preferred_source_id = get_primary_store_id()
    if args.list:
        summarize_snapshots(snapshots, preferred_source_id=preferred_source_id)
        return 0

    if args.doctor is not None:
        return print_doctor_report(normalize_browser_filter(args.doctor))

    if args.calibrate is not None:
        return calibrate_browser_profiles(normalize_browser_filter(args.calibrate))

    if args.set_primary:
        source = resolve_store(args.set_primary, snapshots)
        set_primary_store_id(source.store.id)
        print(f"Primary browser set to {source.store.label}")
        return 0

    if args.restore_backup or args.restore_target:
        if not args.restore_backup or not args.restore_target:
            raise SystemExit("--restore-backup and --restore-target must be used together")
        target = resolve_store(args.restore_target, snapshots).store
        ensure_browsers_closed({target.browser}, auto_close=args.auto_close)
        rollback_path, restored = restore_store_backup(Path(args.restore_backup).expanduser(), target)
        print(f"Restored {target.label} from {display_path(args.restore_backup)}")
        print(f"Rollback backup: {display_path(rollback_path)}")
        print(f"Result: {restored.bookmark_count} bookmarks")
        return 0

    if not args.source:
        return interactive_mode(snapshots, auto_close=args.auto_close)

    source = resolve_store(args.source, snapshots)
    if not args.targets:
        raise SystemExit("--targets is required when --source is used")
    targets = [resolve_store(identifier, snapshots) for identifier in args.targets.split(",") if identifier.strip()]
    targets = [target for target in targets if target.store.id != source.store.id]
    if not targets:
        raise SystemExit("No valid targets selected")
    post_open_browsers = {
        normalize_browser_filter(identifier)
        for identifier in (args.post_open_check or "").split(",")
        if identifier.strip()
    }

    if args.mode == "preview":
        return preview_run(source, targets)

    required_browsers = {source.store.browser, *(target.store.browser for target in targets)}

    if args.edge_empty_then_sync:
        require_cloud_purge_consent(args.allow_cloud_purge)
        if len(targets) != 1 or targets[0].store.browser != "edge":
            raise SystemExit("--edge-empty-then-sync requires exactly one Edge target")
        ensure_browsers_closed(required_browsers, auto_close=args.auto_close)
        source = load_snapshot_with_retry(source.store)
        clear_backup, sync_backup, verification = reset_edge_cloud_via_empty_file_then_sync(
            source,
            targets[0].store,
            args.mode,
            args.edge_empty_wait,
            backup_enabled=not args.no_backup,
        )
        reloaded = verification.reloaded
        print(f"Synced {source.store.label} -> {targets[0].store.label} after Edge empty phase")
        if clear_backup is not None:
            print(f"Empty phase backup: {display_path(clear_backup)}")
        if sync_backup is not None:
            print(f"Sync backup: {display_path(sync_backup)}")
        print(f"Result: {reloaded.bookmark_count} bookmarks")
        print_verification_summary(verification)
        print_order_warning(source, verification)
        return 0

    if args.edge_api_clear_then_sync:
        require_cloud_purge_consent(args.allow_cloud_purge)
        if len(targets) != 1 or not supports_cloud_safe_strategy(targets[0].store):
            raise SystemExit("--edge-api-clear-then-sync requires exactly one Chrome or Edge target")
        args.sync_strategy = "cloud-safe"

    ensure_browsers_closed(required_browsers, auto_close=args.auto_close)
    source = load_snapshot_with_retry(source.store)

    for target in targets:
        decision = choose_sync_strategy(target.store, args.sync_strategy)
        print(f"Strategy: {target.store.label} -> {decision.strategy} ({decision.reason})")
        if decision.strategy == "cloud-safe":
            require_cloud_purge_consent(args.allow_cloud_purge)
            clear_backup, sync_backup, verification = reset_chromium_cloud_via_api_then_sync(
                source,
                target.store,
                args.mode,
                args.edge_api_purge_window,
                args.edge_api_settle_wait,
                backup_enabled=not args.no_backup,
            )
            reloaded = verification.reloaded
            print(f"Synced {source.store.label} -> {target.store.label} after Chromium API purge phase")
            if clear_backup is not None:
                print(f"API purge phase backup: {display_path(clear_backup)}")
            if sync_backup is not None:
                print(f"Sync backup: {display_path(sync_backup)}")
        else:
            backup_path, verification = sync_store(source, target.store, args.mode, backup_enabled=not args.no_backup)
            reloaded = verification.reloaded
            print(f"Synced {source.store.label} -> {target.store.label}")
            if backup_path is not None:
                print(f"Backup: {display_path(backup_path)}")
        print(f"Result: {reloaded.bookmark_count} bookmarks")
        print_verification_summary(verification)
        print_order_warning(source, verification)
        if decision.strategy == "direct" and should_auto_probe_after_direct(target.store, args.sync_strategy):
            print(f"Auto probe: opening {target.store.browser} to check whether sync re-injects bookmarks after a normal direct write.")
            drifted, _backup_path, current = run_post_open_check(
                source,
                target.store,
                args.mode,
                repair_on_drift=False,
                backup_enabled=not args.no_backup,
            )
            if drifted:
                print(f"Auto probe: {target.store.browser} drifted after opening. Marking this target as a cloud reinjection case and retrying with cloud-safe sync.")
                mark_cloud_issue(target.store, f"post-open drift detected for {target.store.id}")
                require_cloud_purge_consent(args.allow_cloud_purge)
                clear_backup, sync_backup, verification = reset_chromium_cloud_via_api_then_sync(
                    source,
                    target.store,
                    args.mode,
                    args.edge_api_purge_window,
                    args.edge_api_settle_wait,
                    backup_enabled=not args.no_backup,
                )
                reloaded = verification.reloaded
                print(f"Cloud-safe resync complete for {target.store.label}")
                if clear_backup is not None:
                    print(f"API purge phase backup: {display_path(clear_backup)}")
                if sync_backup is not None:
                    print(f"Sync backup: {display_path(sync_backup)}")
                print(f"Result: {reloaded.bookmark_count} bookmarks")
                print_verification_summary(verification)
                print_order_warning(source, verification)
            else:
                print(f"Auto probe: {target.store.browser} stayed aligned after opening.")
        elif target.store.browser in post_open_browsers:
            maybe_run_post_open_check(source, target.store, args.mode, post_open_browsers, backup_enabled=not args.no_backup)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect supported local browser bookmarks, compare freshness/fullness, optionally remember a primary browser, and sync with confirmation."
    )
    parser.add_argument("--list", action="store_true", help="List detected bookmark stores and suggestions")
    parser.add_argument("--list-backups", action="store_true", help="List available backups, newest first")
    parser.add_argument("--set-primary", help="Remember this browser/profile as the default source for future interactive runs")
    parser.add_argument("--source", help="Source store id, for example chrome:Default, edge:Default, or safari")
    parser.add_argument("--targets", help="Comma-separated target store ids")
    parser.add_argument("--restore-backup", help="Restore a backup file after validating it")
    parser.add_argument("--restore-target", help="Target store id for --restore-backup")
    parser.add_argument(
        "--doctor",
        nargs="?",
        const="all",
        help="Show sync health and calibration observations for one browser or all: chrome, edge, safari, or all",
    )
    parser.add_argument(
        "--calibrate",
        nargs="?",
        const="all",
        help="Calibrate stabilization and repair profiles from recorded observations for one browser or all",
    )
    parser.add_argument(
        "--post-open-check",
        help="Comma-separated browser names to open after syncing, wait through a delayed drift window, then auto-close and repair once if drift appeared",
    )
    parser.add_argument(
        "--edge-empty-then-sync",
        action="store_true",
        help="Experimental Edge repair: while Edge is open, clear its bookmark file, wait for empty-state sync, then close Edge and sync source bookmarks back",
    )
    parser.add_argument(
        "--edge-empty-wait",
        type=float,
        default=EDGE_EMPTY_SYNC_WAIT_SECONDS,
        help="Seconds to wait after clearing Edge bookmarks before syncing source bookmarks back",
    )
    parser.add_argument(
        "--edge-api-clear-then-sync",
        action="store_true",
        help="Experimental Chromium repair: launch Chrome or Edge with a temporary extension that deletes all bookmarks through the browser API, wait for cloud settle, then sync source bookmarks back",
    )
    parser.add_argument(
        "--edge-api-purge-window",
        type=float,
        default=EDGE_API_PURGE_WINDOW_SECONDS,
        help="Maximum seconds to keep the temporary Edge purge extension active while waiting for local bookmarks to reach zero",
    )
    parser.add_argument(
        "--edge-api-settle-wait",
        type=float,
        default=EDGE_EMPTY_SYNC_WAIT_SECONDS,
        help="Seconds to keep Edge open after the API purge reaches zero so cloud sync can converge before restoring bookmarks",
    )
    parser.add_argument(
        "--sync-strategy",
        choices=["auto", "direct", "cloud-safe"],
        default="auto",
        help="Target write strategy: auto starts with direct writes and upgrades Chrome/Edge targets to cloud-safe after a detected or remembered cloud reinjection issue; direct always writes locally; cloud-safe forces the Chromium API purge flow",
    )
    parser.add_argument("--allow-cloud-purge", action="store_true", help="Explicitly allow cloud-safe to delete target bookmarks before restoring the source")
    parser.add_argument("--no-backup", action="store_true", help="Disable automatic target backups for direct sync only; incompatible with cloud purge")
    parser.add_argument(
        "--mode",
        choices=["preview", "mirror", "strict"],
        default="strict",
        help="preview only, normal mirror, or stricter local mirror",
    )
    parser.add_argument("--auto-close", action="store_true", help="Automatically quit running browsers before syncing")
    return parser


register_format_handler(
    FormatHandler(
        name="chromium",
        load_snapshot=load_chromium_snapshot,
        write_snapshot=write_chromium_snapshot,
    )
)
register_format_handler(
    FormatHandler(
        name="safari",
        load_snapshot=load_safari_snapshot,
        write_snapshot=write_safari_snapshot,
    )
)

register_browser_profile(
    BrowserProfile(
        browser="chrome",
        format_name="chromium",
        app_name="Google Chrome",
        executable_name="Google Chrome",
        detect_stores=make_chromium_detector("chrome", HOME / "Library" / "Application Support" / "Google" / "Chrome"),
        stabilization_profile={"settle_seconds": 1.0, "poll_interval": 0.5, "stable_passes": 2},
    )
)
register_browser_profile(
    BrowserProfile(
        browser="edge",
        format_name="chromium",
        app_name="Microsoft Edge",
        executable_name="Microsoft Edge",
        detect_stores=make_chromium_detector("edge", HOME / "Library" / "Application Support" / "Microsoft Edge"),
        stabilization_profile={"settle_seconds": 8.0, "poll_interval": 2.0, "stable_passes": 2},
        repair_profile={"max_attempts": 2, "retry_wait_seconds": 3.0},
    )
)
register_browser_profile(
    BrowserProfile(
        browser="brave",
        format_name="chromium",
        app_name="Brave Browser",
        executable_name="Brave Browser",
        detect_stores=make_chromium_detector("brave", HOME / "Library" / "Application Support" / "BraveSoftware" / "Brave-Browser"),
        stabilization_profile={"settle_seconds": 2.0, "poll_interval": 0.5, "stable_passes": 2},
    )
)
register_browser_profile(
    BrowserProfile(
        browser="vivaldi",
        format_name="chromium",
        app_name="Vivaldi",
        executable_name="Vivaldi",
        detect_stores=make_chromium_detector("vivaldi", HOME / "Library" / "Application Support" / "Vivaldi"),
        stabilization_profile={"settle_seconds": 2.0, "poll_interval": 0.5, "stable_passes": 2},
    )
)
register_browser_profile(
    BrowserProfile(
        browser="opera",
        format_name="chromium",
        app_name="Opera",
        executable_name="Opera",
        detect_stores=make_chromium_detector("opera", HOME / "Library" / "Application Support" / "com.operasoftware.Opera"),
        stabilization_profile={"settle_seconds": 2.0, "poll_interval": 0.5, "stable_passes": 2},
    )
)
register_browser_profile(
    BrowserProfile(
        browser="safari",
        format_name="safari",
        app_name="Safari",
        executable_name="Safari",
        detect_stores=detect_safari_stores,
        stabilization_profile={"settle_seconds": 8.0, "poll_interval": 2.0, "stable_passes": 2},
    )
)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.list_backups:
        return print_backup_list()

    stores = detect_browser_stores()
    if not stores:
        raise SystemExit("No supported browser bookmark stores found")

    snapshots = [load_snapshot(store) for store in stores]
    return noninteractive_mode(args, snapshots)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit("\nCancelled.")
