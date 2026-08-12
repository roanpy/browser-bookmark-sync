#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from bookmark_sync_version import __version__


SCRIPT_DIR = Path(__file__).resolve().parent
ENGINE = SCRIPT_DIR / "bookmark_sync.py"
STORE_ALIASES = {
    "chrome": "chrome:Default",
    "edge": "edge:Default",
    "brave": "brave:Default",
    "vivaldi": "vivaldi:Default",
    "opera": "opera:Default",
    "safari": "safari",
}
DEFAULT_STORE_IDS = ["chrome:Default", "edge:Default", "safari"]
JSON_SCHEMA_VERSION = 1
BACKUP_PREFIXES = (
    "Backup:",
    "Sync backup:",
    "API purge phase backup:",
    "Empty phase backup:",
    "Repair backup:",
    "Rollback backup:",
)


def normalize_store_alias(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in STORE_ALIASES:
        return STORE_ALIASES[normalized]
    for store_id in STORE_ALIASES.values():
        if store_id.lower() == normalized:
            return store_id
    raise SystemExit(f"Unsupported browser alias or id: {value}")


def normalize_to_values(values: list[str] | None) -> list[str]:
    if not values:
        return []
    return [item.strip() for item in ",".join(values).split(",") if item.strip()]


def resolve_source(args: argparse.Namespace) -> str:
    if args.source and args.from_:
        raise SystemExit("Use either positional SOURCE or --from, not both.")
    source = args.from_ or args.source
    if not source:
        raise SystemExit("source is required unless an inspect, recovery, doctor, or calibrate command is used")
    return normalize_store_alias(source)


def resolve_targets(args: argparse.Namespace, source_id: str) -> list[str]:
    if args.targets and args.to:
        raise SystemExit("Use either positional targets or --to, not both.")
    target_values = args.targets if args.targets else normalize_to_values(args.to)
    if not target_values:
        return default_target_ids(source_id)
    target_ids = [normalize_store_alias(value) for value in target_values]
    target_ids = [target_id for target_id in target_ids if target_id != source_id]
    if not target_ids:
        raise SystemExit("No valid targets selected")
    return target_ids


def default_target_ids(source_id: str) -> list[str]:
    return [store_id for store_id in DEFAULT_STORE_IDS if store_id != source_id]


def build_sync_command(args: argparse.Namespace) -> list[str]:
    source_id = resolve_source(args)
    target_ids = resolve_targets(args, source_id)

    command = [
        sys.executable,
        str(ENGINE),
        "--source",
        source_id,
        "--targets",
        ",".join(target_ids),
        "--mode",
        args.mode,
        "--sync-strategy",
        args.sync_strategy,
    ]
    if args.auto_close:
        command.append("--auto-close")
    if args.allow_cloud_purge:
        command.append("--allow-cloud-purge")
    if args.no_backup:
        command.append("--no-backup")
    if args.post_open_check:
        command.extend(["--post-open-check", args.post_open_check])
    return command


def build_restore_command(args: argparse.Namespace) -> list[str]:
    if not args.restore_backup or not args.restore_target:
        raise SystemExit("--restore-backup and --restore-target must be used together")
    command = [
        sys.executable,
        str(ENGINE),
        "--restore-backup",
        args.restore_backup,
        "--restore-target",
        normalize_store_alias(args.restore_target),
    ]
    if args.auto_close:
        command.append("--auto-close")
    return command


def build_passthrough_command(args: argparse.Namespace) -> list[str]:
    command = [sys.executable, str(ENGINE)]
    if args.list:
        command.append("--list")
        return command
    if args.list_backups:
        command.append("--list-backups")
        return command
    if args.recover:
        command.append("--recover")
        if args.auto_close:
            command.append("--auto-close")
        return command
    if args.discard_recovery:
        command.append("--discard-recovery")
        return command
    if args.doctor is not None:
        command.extend(["--doctor", args.doctor])
        return command
    if args.calibrate is not None:
        command.extend(["--calibrate", args.calibrate])
        return command
    raise SystemExit("Nothing to run")


def operation_name(args: argparse.Namespace) -> str:
    if args.list:
        return "list"
    if args.list_backups:
        return "list_backups"
    if args.recover:
        return "recover"
    if args.discard_recovery:
        return "discard_recovery"
    if args.doctor is not None:
        return "doctor"
    if args.calibrate is not None:
        return "calibrate"
    if args.restore_backup or args.restore_target:
        return "restore"
    return "sync"


def json_metadata(args: argparse.Namespace) -> dict[str, object]:
    operation = operation_name(args)
    metadata: dict[str, object] = {"operation": operation, "version": __version__}
    if operation == "sync":
        source_id = resolve_source(args)
        metadata["source"] = source_id
        metadata["targets"] = resolve_targets(args, source_id)
        metadata["mode"] = args.mode
        metadata["sync_strategy"] = args.sync_strategy
    elif operation == "restore":
        if args.restore_target:
            metadata["target"] = normalize_store_alias(args.restore_target)
        if args.restore_backup:
            metadata["backup"] = str(Path(args.restore_backup).expanduser())
    elif operation in {"doctor", "calibrate"}:
        metadata["browser"] = args.doctor if operation == "doctor" else args.calibrate
    return metadata


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items and value.lower() != "none":
        items.append(value)


def _parse_json_summary(output: str, metadata: dict[str, object]) -> dict[str, object]:
    lines = output.splitlines()
    payload: dict[str, object] = dict(metadata)
    backups: list[str] = []
    strategies: list[dict[str, str]] = []
    results: list[dict[str, object]] = []
    stores: list[dict[str, object]] = []
    backup_files: list[dict[str, object]] = []
    doctor: list[dict[str, object]] = []
    current_result: dict[str, object] | None = None
    current_doctor: dict[str, object] | None = None

    for line in lines:
        stripped = line.strip()
        doctor_header = re.match(r"^\[([a-z]+)\]$", stripped)
        if doctor_header:
            current_doctor = {"browser": doctor_header.group(1)}
            doctor.append(current_doctor)

        backup_file_match = re.match(
            r"^\[\d+\] target=(\S+) created=(\S+) age=(\S+) size=(\d+) path=(.+)$",
            stripped,
        )
        if backup_file_match:
            backup_files.append(
                {
                    "target": backup_file_match.group(1),
                    "created": backup_file_match.group(2),
                    "age": backup_file_match.group(3),
                    "size": int(backup_file_match.group(4)),
                    "path": backup_file_match.group(5),
                }
            )

        if current_doctor is not None:
            sync_match = re.match(r"^Current sync state: (enabled|disabled) \((.+)\)$", stripped)
            if sync_match:
                current_doctor["sync_enabled"] = sync_match.group(1) == "enabled"
                current_doctor["sync_reason"] = sync_match.group(2)
            issue_match = re.match(r"^Known cloud issue: (none recorded|yes \((.*)\))$", stripped)
            if issue_match:
                current_doctor["cloud_issue"] = issue_match.group(1) != "none recorded"
                if issue_match.group(2) is not None:
                    current_doctor["cloud_issue_reason"] = issue_match.group(2)
            stabilization_match = re.match(
                r"^Stabilization profile: settle=([\d.]+)s poll=([\d.]+)s stable_passes=(\d+)$",
                stripped,
            )
            if stabilization_match:
                current_doctor["stabilization"] = {
                    "settle_seconds": float(stabilization_match.group(1)),
                    "poll_interval": float(stabilization_match.group(2)),
                    "stable_passes": int(stabilization_match.group(3)),
                }
            repair_match = re.match(r"^Repair profile: max_attempts=(\d+) retry_wait=([\d.]+)s$", stripped)
            if repair_match:
                current_doctor["repair"] = {
                    "max_attempts": int(repair_match.group(1)),
                    "retry_wait_seconds": float(repair_match.group(2)),
                }
            elif stripped == "Repair profile: none":
                current_doctor["repair"] = None
            observations_match = re.match(
                r"^Observations: (\d+), exact_match=(\d+), external_changes=(\d+), repaired=(\d+), unstable=(\d+)$",
                stripped,
            )
            if observations_match:
                current_doctor["observations"] = {
                    "total": int(observations_match.group(1)),
                    "exact_match": int(observations_match.group(2)),
                    "external_changes": int(observations_match.group(3)),
                    "repaired": int(observations_match.group(4)),
                    "unstable": int(observations_match.group(5)),
                }
            elif stripped == "Observations: none yet":
                current_doctor["observations"] = {"total": 0}
            passes_match = re.match(r"^Max verification passes: (\d+)$", stripped)
            if passes_match:
                current_doctor["max_verification_passes"] = int(passes_match.group(1))
            last_run_match = re.match(
                r"^Last run: target=(\S+) mode=(\S+) matches=(True|False) repairs=(\d+) at=(\S+)$",
                stripped,
            )
            if last_run_match:
                current_doctor["last_run"] = {
                    "target": last_run_match.group(1),
                    "mode": last_run_match.group(2),
                    "matches": last_run_match.group(3) == "True",
                    "repairs": int(last_run_match.group(4)),
                    "at": last_run_match.group(5),
                }
        for prefix in BACKUP_PREFIXES:
            if stripped.startswith(prefix):
                _append_unique(backups, stripped[len(prefix) :].strip())
                break

        strategy_match = re.match(r"^Strategy: (.+?) -> (direct|cloud-safe) \((.+)\)$", stripped)
        if strategy_match:
            strategies.append(
                {
                    "target": strategy_match.group(1),
                    "strategy": strategy_match.group(2),
                    "reason": strategy_match.group(3),
                }
            )

        sync_match = re.match(r"^Synced .+ -> (.+?)(?: after .*)?$", stripped)
        if sync_match:
            current_result = {"target": sync_match.group(1)}
            results.append(current_result)
        else:
            recovery_match = re.match(r"^Recovered (.+?) from .+$", stripped)
            if recovery_match:
                current_result = {"target": recovery_match.group(1)}
                results.append(current_result)
            cloud_result_match = re.match(r"^Cloud-safe resync complete for (.+)$", stripped)
            if cloud_result_match:
                current_result = {"target": cloud_result_match.group(1)}
                results.append(current_result)

        result_match = re.search(r"Result: (\d+) bookmarks", stripped)
        if result_match:
            if current_result is None:
                current_result = {}
                results.append(current_result)
            current_result["bookmarks"] = int(result_match.group(1))

        if stripped.startswith("Verification:"):
            if current_result is None:
                current_result = {}
                results.append(current_result)
            current_result["verification"] = stripped.removeprefix("Verification:").strip()

        store_match = re.match(
            r"^\[\d+\]\s+(.+?)\s+bookmarks=(\d+)\s+folders=(\d+)\s+modified=(.+?)(?:\s+\[primary\])?$",
            stripped,
        )
        if store_match:
            stores.append(
                {
                    "id": store_match.group(1),
                    "bookmarks": int(store_match.group(2)),
                    "folders": int(store_match.group(3)),
                    "modified": store_match.group(4),
                    "primary": "[primary]" in stripped,
                }
            )

    if backups:
        payload["backups"] = backups
    if strategies:
        payload["strategies"] = strategies
    if results:
        payload["results"] = results
    if stores:
        payload["stores"] = stores
    if metadata.get("operation") == "list_backups":
        payload["backup_files"] = backup_files
    if doctor:
        payload["doctor"] = doctor
    return payload


def build_json_result(
    args: argparse.Namespace,
    result: subprocess.CompletedProcess[str],
    metadata: dict[str, object],
) -> dict[str, object]:
    payload = _parse_json_summary(result.stdout, metadata)
    payload["schema"] = JSON_SCHEMA_VERSION
    payload["ok"] = result.returncode == 0
    payload["exit_code"] = result.returncode
    if result.returncode != 0:
        error = result.stderr.strip() or f"Command failed with exit code {result.returncode}"
        payload["error"] = error[:500]
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Short wrapper for bookmark_sync.py with sensible automation defaults."
    )
    parser.add_argument("source", nargs="?", help="Source browser alias or store id")
    parser.add_argument("--from", dest="from_", help="Explicit source alias or store id")
    parser.add_argument(
        "--to",
        nargs="+",
        default=None,
        help="Explicit target aliases or store ids. Supports comma-separated values, for example: --to edge,safari",
    )
    parser.add_argument("targets", nargs="*", help="Optional target browser aliases/ids; defaults to all other supported browsers")
    parser.add_argument("--list", action="store_true", help="List detected bookmark stores")
    parser.add_argument("--list-backups", action="store_true", help="List available backups, newest first")
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument("--recover", action="store_true", help="Restore the target from an unfinished operation")
    recovery.add_argument("--discard-recovery", action="store_true", help="Keep the current target and clear an unfinished operation")
    parser.add_argument("--doctor", nargs="?", const="all", help="Run the health report for one browser or all")
    parser.add_argument("--calibrate", nargs="?", const="all", help="Calibrate stabilization profiles for one browser or all")
    parser.add_argument("--restore-backup", help="Restore a backup file")
    parser.add_argument("--restore-target", help="Browser alias or store id to restore")
    parser.add_argument("--mode", choices=["preview", "mirror", "strict"], default="strict", help="Sync mode")
    parser.add_argument(
        "--sync-strategy",
        choices=["auto", "direct", "cloud-safe"],
        default="auto",
        help="Target strategy selection",
    )
    parser.add_argument("--post-open-check", help="Optional comma-separated browsers for an extra delayed open check")
    parser.add_argument("--allow-cloud-purge", action="store_true", help="Allow cloud-safe to clear target cloud bookmarks before restoring the source")
    parser.add_argument("--no-backup", action="store_true", help="Disable backups for direct sync only; incompatible with cloud purge")
    parser.add_argument(
        "--auto-close",
        action="store_true",
        dest="auto_close",
        help="Automatically close running browsers after explicit command confirmation",
    )
    parser.add_argument(
        "--no-auto-close",
        action="store_false",
        dest="auto_close",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a stable machine-readable result on stdout; detailed logs go to stderr",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.set_defaults(auto_close=False)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    metadata: dict[str, object] = (
        {"operation": operation_name(args), "version": __version__} if args.json else {}
    )

    try:
        if args.json:
            metadata = json_metadata(args)
        if args.restore_backup or args.restore_target:
            command = build_restore_command(args)
        elif args.list or args.list_backups or args.recover or args.discard_recovery or args.doctor is not None or args.calibrate is not None:
            command = build_passthrough_command(args)
        else:
            command = build_sync_command(args)
    except SystemExit as exc:
        if not args.json or exc.code in (0, None):
            raise
        payload = {
            "schema": JSON_SCHEMA_VERSION,
            **metadata,
            "ok": False,
            "exit_code": 2,
            "error": str(exc),
        }
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        return 2

    if not args.json:
        result = subprocess.run(command, check=False)
        return result.returncode

    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.stdout:
        sys.stderr.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    print(json.dumps(build_json_result(args, result, metadata), ensure_ascii=True, sort_keys=True))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
