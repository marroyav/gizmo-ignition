#!/usr/bin/env python3
"""Install the queue-safe GIZMo non-Good history timer."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def ignition_event_source(source: str) -> str:
    converted = []
    for number, line in enumerate(source.splitlines(keepends=True), start=1):
        spaces = len(line) - len(line.lstrip(" "))
        if spaces % 4:
            raise ValueError(
                f"timer script line {number} is not indented by four-space levels"
            )
        converted.append("\t" * (spaces // 4) + line[spaces:])
    return "".join(converted)


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise ValueError(f"timer template must contain exactly one {old!r}")
    return source.replace(old, new, 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument(
        "--script",
        type=Path,
        default=Path(__file__).with_name("quality_history_bridge_timer.py"),
    )
    parser.add_argument("--historian-provider", required=True)
    parser.add_argument(
        "--gateway-name",
        help="Accepted for compatibility; tag-history routing does not use it.",
    )
    parser.add_argument("--tag-provider", default="default")
    parser.add_argument("--delay-ms", type=int, default=1000)
    parser.add_argument("--disabled", action="store_true")
    args = parser.parse_args()

    project = args.project_dir.resolve()
    if not (project / "project.json").is_file():
        parser.error(f"not an Ignition project directory: {project}")
    script = args.script.resolve()
    if not script.is_file():
        parser.error(f"timer script is missing: {script}")
    if not 1000 <= args.delay_ms <= 60000:
        parser.error("--delay-ms must be between 1000 and 60000")
    for label, value in (
        ("--historian-provider", args.historian_provider),
        ("--tag-provider", args.tag_provider),
    ):
        if not value.strip() or any(character in value for character in "\r\n"):
            parser.error(f"{label} must be a non-empty single-line value")
    if args.gateway_name is not None and (
        not args.gateway_name.strip()
        or any(character in args.gateway_name for character in "\r\n")
    ):
        parser.error("--gateway-name must be a non-empty single-line value")

    source = script.read_text(encoding="utf-8")
    source = replace_once(
        source,
        '"__GIZMO_HISTORIAN_PROVIDER__"',
        json.dumps(args.historian_provider.strip()),
    )
    source = replace_once(
        source,
        '"__GIZMO_TAG_PROVIDER__"',
        json.dumps(args.tag_provider.strip()),
    )

    destination = project / "ignition" / "timer" / "GIZMo Non-Good History"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "handleTimerEvent.py").write_text(
        ignition_event_source(source), encoding="utf-8"
    )
    write_json_atomic(
        destination / "resource.json",
        {
            "scope": "A",
            "version": 1,
            "restricted": False,
            "overridable": True,
            "files": ["handleTimerEvent.py"],
            "attributes": {
                "delay": args.delay_ms,
                "fixedDelay": True,
                "sharedThread": False,
                "enabled": not args.disabled,
                "lastModification": {
                    "actor": "gizmo-history-bridge-installer",
                    "timestamp": datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                },
            },
        },
    )
    print(
        json.dumps(
            {
                "timer_resource": str(destination),
                "historian_provider": args.historian_provider.strip(),
                "gateway_name": (
                    args.gateway_name.strip() if args.gateway_name is not None else None
                ),
                "tag_provider": args.tag_provider.strip(),
                "delay_ms": args.delay_ms,
                "enabled": not args.disabled,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
