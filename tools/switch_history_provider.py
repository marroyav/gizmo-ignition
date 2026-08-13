#!/usr/bin/env python3
"""Plan or atomically switch historized tags in an Ignition resource tree."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def atomic_tags(
    tags: list[dict[str, Any]], prefix: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], dict[str, Any]]]:
    for tag in tags:
        if not isinstance(tag, dict) or not isinstance(tag.get("name"), str):
            raise ValueError("tag resource contains an invalid tag entry")
        path = (*prefix, tag["name"])
        children = tag.get("tags")
        if children is not None:
            if not isinstance(children, list):
                raise ValueError("tag folder contains a non-list tags property")
            yield from atomic_tags(children, path)
        if tag.get("tagType") == "AtomicTag":
            yield path, tag


def load_resources(
    resource_root: Path,
) -> list[tuple[Path, list[dict[str, Any]]]]:
    if not resource_root.is_dir():
        raise ValueError(f"tag resource root is not a directory: {resource_root}")
    resources = []
    for path in sorted(resource_root.rglob("tags.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError(f"tag resource is not a JSON list: {path}")
        resources.append((path, value))
    if not resources:
        raise ValueError(f"no tags.json resources found under {resource_root}")
    return resources


def migration_plan(
    resource_root: Path,
    from_provider: str,
    to_provider: str,
    expected_count: int,
) -> tuple[list[tuple[Path, list[dict[str, Any]]]], dict[str, Any]]:
    if not from_provider or not to_provider or from_provider == to_provider:
        raise ValueError("source and destination providers must be non-empty and distinct")
    resources = load_resources(resource_root)
    selected: list[dict[str, str]] = []
    mismatches: list[dict[str, str]] = []
    changed_files: set[str] = set()
    for file_path, tags in resources:
        relative_parent = file_path.parent.relative_to(resource_root)
        prefix = () if str(relative_parent) == "." else relative_parent.parts
        for tag_path, tag in atomic_tags(tags, prefix):
            if tag.get("historyEnabled") is not True:
                continue
            provider = str(tag.get("historyProvider", ""))
            item = {
                "path": "/".join(tag_path),
                "provider": provider,
                "resource": str(file_path.relative_to(resource_root)),
            }
            if provider == from_provider:
                selected.append(item)
                changed_files.add(item["resource"])
            else:
                mismatches.append(item)
    if mismatches:
        raise ValueError(
            "history-enabled tags do not all use the expected source provider: "
            + json.dumps(mismatches[:10], sort_keys=True)
        )
    if len(selected) != expected_count:
        raise ValueError(
            f"expected {expected_count} history-enabled tags, found {len(selected)}"
        )
    return resources, {
        "action": "plan",
        "resource_root": str(resource_root),
        "from_provider": from_provider,
        "to_provider": to_provider,
        "tag_count": len(selected),
        "resource_count": len(changed_files),
        "resources": sorted(changed_files),
        "tags": selected,
    }


def write_json_atomic(path: Path, value: object) -> None:
    original_mode = stat.S_IMODE(path.stat().st_mode)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, original_mode)
    os.replace(temporary, path)


def apply_plan(
    resource_root: Path,
    resources: list[tuple[Path, list[dict[str, Any]]]],
    plan: dict[str, Any],
    backup_root: Path,
) -> dict[str, Any]:
    if backup_root.exists():
        raise ValueError(f"backup destination already exists: {backup_root}")
    changed = set(plan["resources"])
    backup_root.mkdir(parents=True, mode=0o700)
    for file_path, tags in resources:
        relative = file_path.relative_to(resource_root)
        if str(relative) not in changed:
            continue
        backup_path = backup_root / relative
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, backup_path)
        for _tag_path, tag in atomic_tags(tags):
            if (
                tag.get("historyEnabled") is True
                and tag.get("historyProvider") == plan["from_provider"]
            ):
                tag["historyProvider"] = plan["to_provider"]
        write_json_atomic(file_path, tags)

    # Read the tree again and prove that exactly the expected tags now point at
    # the destination. This call also catches partial or concurrent edits.
    _resources, reverse = migration_plan(
        resource_root,
        plan["to_provider"],
        plan["from_provider"],
        int(plan["tag_count"]),
    )
    return {
        **plan,
        "action": "applied",
        "backup": str(backup_root),
        "verified_tag_count": reverse["tag_count"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource-root", type=Path, required=True)
    parser.add_argument("--from-provider", default="GIZMo History")
    parser.add_argument("--to-provider", required=True)
    parser.add_argument("--expected-count", type=int, default=50)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--confirm-gateway-stopped",
        action="store_true",
        help="required for apply; prevents live resource-tree edits",
    )
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()

    if args.expected_count < 1:
        parser.error("--expected-count must be positive")
    root = args.resource_root.resolve()
    try:
        resources, plan = migration_plan(
            root,
            args.from_provider,
            args.to_provider,
            args.expected_count,
        )
        if args.apply:
            if not args.confirm_gateway_stopped:
                parser.error("--apply requires --confirm-gateway-stopped")
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = (
                args.backup_dir.resolve()
                if args.backup_dir
                else root.parent / f"history-provider-backup-{timestamp}"
            )
            outcome = apply_plan(root, resources, plan, backup)
        else:
            outcome = plan
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(json.dumps(outcome, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
