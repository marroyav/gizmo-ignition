#!/usr/bin/env python3
"""Install the resumable GIZMo backfill hook into an Ignition project."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path


STORAGE_PATH_MODES = ("live-tag", "qualified-historian")


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def ignition_event_source(source: str) -> str:
    """Use the tab indentation emitted by Ignition's event-script serializer."""

    converted = []
    for number, line in enumerate(source.splitlines(keepends=True), start=1):
        spaces = len(line) - len(line.lstrip(" "))
        if spaces % 4:
            raise ValueError(
                f"startup script line {number} is not indented by four-space levels"
            )
        converted.append("\t" * (spaces // 4) + line[spaces:])
    return "".join(converted)


def safe_filename(value: str, option: str) -> str:
    path = Path(value)
    if not value or path.name != value or value in {".", ".."}:
        raise ValueError(f"{option} must be a plain file name")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument(
        "--script",
        type=Path,
        default=Path(__file__).with_name("history_backfill_on_startup.py"),
    )
    parser.add_argument(
        "--import-root",
        type=Path,
        default=Path("/var/lib/gizmo-ignition/history-import"),
    )
    parser.add_argument("--batch-points", type=int, default=5000)
    parser.add_argument("--startup-delay-seconds", type=int, default=15)
    parser.add_argument("--max-rows-per-file", type=int, default=100)
    parser.add_argument("--include-day", action="append", default=[])
    parser.add_argument("--probe-source-path", default="")
    parser.add_argument("--probe-history-path", default="")
    parser.add_argument(
        "--storage-path-mode",
        choices=STORAGE_PATH_MODES,
        default="live-tag",
        help=(
            "store via live tags (Core workaround) or an explicitly qualified "
            "destination historian"
        ),
    )
    parser.add_argument("--destination-historian", default="")
    parser.add_argument("--state-file", default="state.json")
    parser.add_argument("--validation-file", default="validation-result.json")
    parser.add_argument("--probe-result-file", default="probe-result.json")
    parser.add_argument("--lock-file", default="backfill.lock")
    parser.add_argument("--query-attempts", type=int, default=3)
    parser.add_argument("--query-delay-ms", type=int, default=250)
    parser.add_argument("--migrate-service-tag-paths", action="store_true")
    parser.add_argument(
        "--service-tag-resource-root",
        default=(
            "/opt/ignition/data/config/resources/"
            "core/ignition/tag-definition/default/GIZMo/Services/Units"
        ),
    )
    parser.add_argument("--disabled", action="store_true")
    args = parser.parse_args()

    project = args.project_dir.resolve()
    if not (project / "project.json").is_file():
        parser.error(f"not an Ignition project directory: {project}")
    source = args.script.resolve()
    if not source.is_file():
        parser.error(f"startup script is missing: {source}")
    if not 100 <= args.batch_points <= 25000:
        parser.error("--batch-points must be between 100 and 25000")
    if args.max_rows_per_file < 0:
        parser.error("--max-rows-per-file cannot be negative")
    if not 0 <= args.startup_delay_seconds <= 120:
        parser.error("--startup-delay-seconds must be between 0 and 120")
    if not 1 <= args.query_attempts <= 600:
        parser.error("--query-attempts must be between 1 and 600")
    if not 0 <= args.query_delay_ms <= 10000:
        parser.error("--query-delay-ms must be between 0 and 10000")
    if (
        args.storage_path_mode == "qualified-historian"
        and not args.destination_historian.strip()
    ):
        parser.error(
            "--destination-historian is required with qualified-historian mode"
        )
    try:
        state_file = safe_filename(args.state_file, "--state-file")
        validation_file = safe_filename(
            args.validation_file, "--validation-file"
        )
        probe_result_file = safe_filename(
            args.probe_result_file, "--probe-result-file"
        )
        lock_file = safe_filename(args.lock_file, "--lock-file")
    except ValueError as error:
        parser.error(str(error))

    import_root = args.import_root.resolve()
    import_root.mkdir(parents=True, exist_ok=True)
    startup_source = source.read_text(encoding="utf-8")
    default_root_line = 'root = "/var/lib/gizmo-ignition/history-import"'
    replacement_root_line = f"root = {json.dumps(str(import_root))}"
    if default_root_line not in startup_source:
        parser.error("startup script does not contain the expected import root")
    startup_source = startup_source.replace(
        default_root_line, replacement_root_line, 1
    )

    # Startup is a singleton project resource in Ignition, so its files live
    # directly at ignition/startup (unlike named timer/message resources).
    destination = project / "ignition" / "startup"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "onStartup.py").write_text(
        ignition_event_source(startup_source),
        encoding="utf-8",
    )
    write_json_atomic(
        destination / "resource.json",
        {
            "scope": "A",
            "version": 1,
            "restricted": False,
            "overridable": True,
            "files": ["onStartup.py"],
            "attributes": {
                "enabled": True,
                "lastModification": {
                    "actor": "gizmo-history-backfill-installer",
                    "timestamp": datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                },
            },
        },
    )

    write_json_atomic(
        import_root / "control.json",
        {
            "enabled": not args.disabled,
            "batch_points": args.batch_points,
            "startup_delay_seconds": args.startup_delay_seconds,
            "max_rows_per_file": args.max_rows_per_file,
            "include_days": args.include_day,
            "probe_source_path": args.probe_source_path,
            "probe_history_path": args.probe_history_path,
            "storage_path_mode": args.storage_path_mode,
            "destination_historian": args.destination_historian.strip(),
            "state_file": state_file,
            "validation_file": validation_file,
            "probe_result_file": probe_result_file,
            "lock_file": lock_file,
            "query_attempts": args.query_attempts,
            "query_delay_ms": args.query_delay_ms,
            "migrate_service_tag_paths": args.migrate_service_tag_paths,
            "legacy_service_tag_resource_root": args.service_tag_resource_root,
        },
    )
    print(
        json.dumps(
            {
                "startup_resource": str(destination),
                "control": str(import_root / "control.json"),
                "enabled": not args.disabled,
                "max_rows_per_file": args.max_rows_per_file,
                "include_days": args.include_day,
                "storage_path_mode": args.storage_path_mode,
                "destination_historian": args.destination_historian.strip(),
                "state": str(import_root / state_file),
                "validation": str(import_root / validation_file),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
