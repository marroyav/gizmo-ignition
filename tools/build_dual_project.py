#!/usr/bin/env python3
"""Build a secret-free dual-device Ignition resource tree.

The committed single-device source tree contains only tag, tag-group, and
Perspective resources.  OPC connection resources are deliberately supplied by
the target Gateway because they contain site endpoints and Gateway-bound
security material.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
from pathlib import Path
from typing import Any


REPOSITORY = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPOSITORY / "source" / "single-device"
DEFAULT_OUTPUT = REPOSITORY / "ignition-project"
DEVICES = (
    ("Kria", "GIZMo Kria"),
    ("Legacy", "GIZMo Legacy"),
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def map_strings(value: Any, transform: Any) -> Any:
    if isinstance(value, dict):
        return {key: map_strings(item, transform) for key, item in value.items()}
    if isinstance(value, list):
        return [map_strings(item, transform) for item in value]
    if isinstance(value, str):
        return transform(value)
    return value


def replace_strings(path: Path, transform: Any) -> None:
    for json_path in sorted(path.rglob("*.json")):
        write_json(json_path, map_strings(read_json(json_path), transform))


def connection_transform(connection: str, device: str) -> Any:
    def transform(value: str) -> str:
        if value == "GIZMo Bridge":
            return connection
        if value == "GIZMo Ground Fault":
            return f"GIZMo {device} Ground Fault"
        return value

    return transform


def view_transform(device: str) -> Any:
    prefix = f"[default]GIZMo/{device}/"

    def transform(value: str) -> str:
        value = value.replace("[default]GIZMo/", prefix)
        if value == "GIZMo":
            return f"GIZMo {device}"
        return value

    return transform


def remove_legacy_alarm_configuration(device_root: Path) -> None:
    alarm_path = device_root / "Alarm" / "tags.json"
    alarms = read_json(alarm_path)
    for tag in alarms:
        if tag.get("name") == "Active":
            tag.pop("alarms", None)
            tag["documentation"] = (
                "Legacy authoritative alarm state. No Ignition alarm is enabled "
                "until commissioning validates the adapter readback."
            )
    write_json(alarm_path, alarms)


def build(source: Path, output: Path, *, force: bool) -> None:
    if (source / "data/config/resources/core/ignition/opc-connection").exists():
        raise ValueError("source must not contain an OPC connection resource")
    if output.exists():
        if not force:
            raise FileExistsError(f"output exists; pass --force: {output}")
        shutil.rmtree(output)

    source_ignition = source / "data/config/resources/core/ignition"
    output_ignition = output / "data/config/resources/core/ignition"
    shutil.copytree(source_ignition / "tag-group", output_ignition / "tag-group")

    source_root = source_ignition / "tag-definition/default/GIZMo"
    output_root = output_ignition / "tag-definition/default/GIZMo"
    output_root.mkdir(parents=True)
    shutil.copy2(source_root / "unary-resource.json", output_root)

    for device, connection in DEVICES:
        device_root = output_root / device
        shutil.copytree(source_root, device_root)
        replace_strings(device_root, connection_transform(connection, device))
        if device == "Legacy":
            remove_legacy_alarm_configuration(device_root)

    source_projects = source / "data/projects"
    output_projects = output / "data/projects"
    shutil.copytree(source_projects, output_projects)
    project = output_projects / "GIZMo"
    project_json = read_json(project / "project.json")
    project_json["title"] = "GIZMo Dual Monitor"
    project_json["description"] = (
        "Read-only Perspective project for Kria and legacy GIZMo OPC-UA adapters."
    )
    write_json(project / "project.json", project_json)

    views = project / "com.inductiveautomation.perspective/views/Pages"
    original_overview = views / "Overview"
    for device, _connection in DEVICES:
        destination = views / f"{device}Overview"
        shutil.copytree(original_overview, destination)
        replace_strings(destination, view_transform(device))
    shutil.rmtree(original_overview)

    inventory_path = views / "TagInventory" / "view.json"
    inventory = map_strings(
        read_json(inventory_path),
        lambda value: {
            "GIZMo tag inventory": "GIZMo dual-device tag inventory",
            (
                "431 canonical OPC-UA variables · all imported read-only · "
                "history disabled"
            ): (
                "431 canonical variables per device · two read-only device "
                "trees · history disabled"
            ),
        }.get(value, value),
    )
    write_json(inventory_path, inventory)

    page_config_path = (
        project
        / "com.inductiveautomation.perspective/page-config/config.json"
    )
    page_config = read_json(page_config_path)
    page_config["pages"] = {
        "/": {
            "viewPath": "Pages/KriaOverview",
            "title": "GIZMo Kria",
        },
        "/kria": {
            "viewPath": "Pages/KriaOverview",
            "title": "GIZMo Kria",
        },
        "/legacy": {
            "viewPath": "Pages/LegacyOverview",
            "title": "GIZMo Legacy",
        },
        "/tags": {
            "viewPath": "Pages/TagInventory",
            "title": "GIZMo Tags",
        },
    }
    write_json(page_config_path, page_config)

    exported = read_json(source / "exports/gizmo-tags.json")
    if not isinstance(exported, list) or len(exported) != 1:
        raise ValueError("single-device tag export must contain one root folder")
    canonical_tags = exported[0].get("tags")
    if not isinstance(canonical_tags, list):
        raise ValueError("single-device tag export has no tag list")
    dual_root = copy.deepcopy(exported[0])
    dual_root["tags"] = []
    for device, connection in DEVICES:
        tags = map_strings(
            copy.deepcopy(canonical_tags),
            connection_transform(connection, device),
        )
        if device == "Legacy":
            for folder in tags:
                if folder.get("name") != "Alarm":
                    continue
                for tag in folder.get("tags", []):
                    if tag.get("name") == "Active":
                        tag.pop("alarms", None)
        dual_root["tags"].append(
            {"name": device, "tagType": "Folder", "tags": tags}
        )
    write_json(output / "exports/gizmo-dual-tags.json", [dual_root])

    source_manifest = read_json(source / "manifest.json")
    variable_count = int(source_manifest["variable_count"])
    write_json(
        output / "manifest.json",
        {
            "format": "gizmo-ignition-dual/v1",
            "ignition_target": source_manifest["ignition_target"],
            "project": "GIZMo",
            "namespace_uri": source_manifest["namespace_uri"],
            "schema_sha256": source_manifest["schema_sha256"],
            "devices": [
                {
                    "name": device,
                    "connection": connection,
                    "tag_root": f"[default]GIZMo/{device}",
                    "endpoint": "site-configured",
                }
                for device, connection in DEVICES
            ],
            "variable_count_per_device": variable_count,
            "total_tag_count": variable_count * len(DEVICES),
            "bridge_read_only": True,
            "tag_history_enabled": False,
            "connection_resources_committed": False,
            "database_connections_committed": False,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    build(args.source, args.output, force=args.force)


if __name__ == "__main__":
    main()
