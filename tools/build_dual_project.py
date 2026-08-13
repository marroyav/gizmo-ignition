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
WRITE_POLICY = {
    "Kria": frozenset(
        {
            "Configuration/AveragesPerCalculation",
            "Configuration/ThresholdOhm",
        }
    ),
    "Legacy": frozenset({"Configuration/ThresholdOhm"}),
}
STATUS_LABEL = {
    "Kria": "REMOTE CONFIG · THRESHOLD + AVERAGES",
    "Legacy": "REMOTE CONFIG · THRESHOLD ONLY",
}


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
        if value == "READ ONLY · IGNITION BRIDGE":
            return STATUS_LABEL[device]
        return value

    return transform


def configure_tag_access(tag: dict[str, Any], path: str, device: str) -> bool:
    writable = path in WRITE_POLICY[device]
    tag["readOnly"] = not writable
    if device == "Legacy" and path == "Configuration/ThresholdOhm":
        tag["documentation"] = (
            "Authenticated remote legacy threshold. The native ZedBoard "
            "server accepts UInt32 values from 0 through 1023 ohm and "
            "performs persistent, live-word, and display-transaction readback."
        )
        tag["engLow"] = 0.0
        tag["engHigh"] = 1023.0
    return writable


def configure_resource_write_access(device_root: Path, device: str) -> None:
    configured: set[str] = set()
    for tags_path in sorted(device_root.rglob("tags.json")):
        tags = read_json(tags_path)
        parent = tags_path.parent.relative_to(device_root)
        for tag in tags:
            name = tag.get("name")
            if tag.get("tagType") != "AtomicTag" or not isinstance(name, str):
                continue
            path = "/".join((*parent.parts, name))
            if configure_tag_access(tag, path, device):
                configured.add(path)
        write_json(tags_path, tags)
    if configured != WRITE_POLICY[device]:
        missing = sorted(WRITE_POLICY[device] - configured)
        unexpected = sorted(configured - WRITE_POLICY[device])
        raise ValueError(
            f"{device} write-policy mismatch: missing={missing}, "
            f"unexpected={unexpected}"
        )


def configure_export_write_access(
    tags: list[dict[str, Any]], device: str, prefix: tuple[str, ...] = ()
) -> set[str]:
    configured: set[str] = set()
    for tag in tags:
        name = tag.get("name")
        if not isinstance(name, str):
            continue
        path_parts = (*prefix, name)
        children = tag.get("tags")
        if tag.get("tagType") == "Folder" and isinstance(children, list):
            configured.update(
                configure_export_write_access(children, device, path_parts)
            )
            continue
        path = "/".join(path_parts)
        if configure_tag_access(tag, path, device):
            configured.add(path)
    return configured


def direct_tag_binding(tag_path: str, *, bidirectional: bool = False) -> dict[str, Any]:
    config: dict[str, Any] = {
        "mode": "direct",
        "tagPath": tag_path,
        "fallbackDelay": 2.5,
        "publishInitial": False,
    }
    if bidirectional:
        config["bidirectional"] = True
    return {"binding": {"type": "tag", "config": config}}


def configuration_input(
    device: str,
    *,
    name: str,
    title: str,
    tag: str,
    minimum: int,
    maximum: int,
    help_text: str,
) -> dict[str, Any]:
    return {
        "type": "ia.container.flex",
        "meta": {"name": name},
        "position": {"basis": "280px", "grow": 1, "shrink": 1},
        "props": {
            "direction": "column",
            "style": {
                "backgroundColor": "#3f464d",
                "borderRadius": "8px",
                "margin": "6px",
                "minHeight": "150px",
                "padding": "18px",
            },
        },
        "children": [
            {
                "type": "ia.display.label",
                "meta": {"name": f"{name}Title"},
                "position": {"basis": "28px", "grow": 0, "shrink": 1},
                "props": {
                    "text": title,
                    "textStyle": {
                        "color": "#c3c9cf",
                        "fontFamily": "JetBrains Mono, IBM Plex Mono, monospace",
                        "fontSize": "11px",
                        "fontWeight": 700,
                    },
                },
            },
            {
                "type": "ia.input.numeric-entry-field",
                "version": 0,
                "meta": {"name": f"{name}Input"},
                "position": {"basis": "58px", "grow": 0, "shrink": 0},
                "props": {
                    "value": 100,
                    "format": "0",
                    "mode": "protected",
                    "align": "right",
                    "enabled": True,
                    "inputBounds": {
                        "minimum": minimum,
                        "maximum": maximum,
                        "invalidStyle": {"color": "#ff6a2a"},
                    },
                    "spinner": {"enabled": True, "increment": 1},
                    "tooltipText": help_text,
                    "style": {
                        "backgroundColor": "#1c1f22",
                        "borderColor": "#b8c41f",
                        "borderRadius": "5px",
                        "color": "#f3f4ea",
                        "fontFamily": "JetBrains Mono, IBM Plex Mono, monospace",
                        "fontSize": "24px",
                        "fontWeight": 700,
                        "paddingLeft": "12px",
                        "paddingRight": "12px",
                    },
                },
                "propConfig": {
                    "props.value": direct_tag_binding(
                        f"[default]GIZMo/{device}/Configuration/{tag}",
                        bidirectional=True,
                    )
                },
            },
            {
                "type": "ia.display.label",
                "meta": {"name": f"{name}Help"},
                "position": {"basis": "auto", "grow": 1, "shrink": 1},
                "props": {
                    "text": help_text,
                    "style": {"whiteSpace": "normal"},
                    "textStyle": {
                        "color": "#c3c9cf",
                        "fontFamily": "JetBrains Mono, IBM Plex Mono, monospace",
                        "fontSize": "10px",
                    },
                },
            },
        ],
    }


def command_result(device: str) -> dict[str, Any]:
    return {
        "type": "ia.container.flex",
        "meta": {"name": "CommandResult"},
        "position": {"basis": "280px", "grow": 1, "shrink": 1},
        "props": {
            "direction": "column",
            "style": {
                "backgroundColor": "#3f464d",
                "borderRadius": "8px",
                "margin": "6px",
                "minHeight": "150px",
                "padding": "18px",
            },
        },
        "children": [
            {
                "type": "ia.display.label",
                "meta": {"name": "CommandResultTitle"},
                "position": {"basis": "28px", "grow": 0, "shrink": 1},
                "props": {
                    "text": "LAST CONFIGURATION RESULT",
                    "textStyle": {
                        "color": "#c3c9cf",
                        "fontFamily": "JetBrains Mono, IBM Plex Mono, monospace",
                        "fontSize": "11px",
                        "fontWeight": 700,
                    },
                },
            },
            {
                "type": "ia.display.label",
                "meta": {"name": "CommandResultValue"},
                "position": {"basis": "auto", "grow": 1, "shrink": 1},
                "props": {
                    "text": "No configuration command reported",
                    "style": {"whiteSpace": "normal"},
                    "textStyle": {
                        "color": "#f3f4ea",
                        "fontFamily": "JetBrains Mono, IBM Plex Mono, monospace",
                        "fontSize": "13px",
                    },
                },
                "propConfig": {
                    "props.text": direct_tag_binding(
                        f"[default]GIZMo/{device}/Configuration/LastCommandResult"
                    )
                },
            },
        ],
    }


def add_configuration_panel(view_path: Path, device: str) -> None:
    view = read_json(view_path)
    inputs = [
        configuration_input(
            device,
            name="ThresholdSetpoint",
            title="REMOTE THRESHOLD SETPOINT · Ω",
            tag="ThresholdOhm",
            minimum=1,
            maximum=500,
            help_text=(
                "Protected entry; press Enter or leave the field to commit. "
                "Initial operator band: 1–500 Ω."
            ),
        )
    ]
    if device == "Kria":
        inputs.append(
            configuration_input(
                device,
                name="AveragesSetpoint",
                title="REMOTE AVERAGES PER CALCULATION",
                tag="AveragesPerCalculation",
                minimum=1,
                maximum=1_000_000,
                help_text=(
                    "Protected entry; press Enter or leave the field to commit. "
                    "Requested normal value: 100."
                ),
            )
        )
    inputs.append(command_result(device))
    view["root"]["children"].append(
        {
            "type": "ia.container.flex",
            "meta": {"name": "RemoteConfiguration"},
            "position": {"basis": "220px", "grow": 0, "shrink": 0},
            "props": {"direction": "column"},
            "children": [
                {
                    "type": "ia.display.label",
                    "meta": {"name": "RemoteConfigurationTitle"},
                    "position": {"basis": "34px", "grow": 0, "shrink": 0},
                    "props": {
                        "text": "REMOTE CONFIGURATION",
                        "textStyle": {
                            "color": "#b8c41f",
                            "fontFamily": "JetBrains Mono, IBM Plex Mono, monospace",
                            "fontSize": "16px",
                            "fontWeight": 800,
                        },
                    },
                },
                {
                    "type": "ia.container.flex",
                    "meta": {"name": "RemoteConfigurationFields"},
                    "position": {"basis": "auto", "grow": 1, "shrink": 1},
                    "props": {"direction": "row", "wrap": "wrap"},
                    "children": inputs,
                },
            ],
        }
    )
    write_json(view_path, view)


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
        configure_resource_write_access(device_root, device)
        if device == "Legacy":
            remove_legacy_alarm_configuration(device_root)

    source_projects = source / "data/projects"
    output_projects = output / "data/projects"
    shutil.copytree(source_projects, output_projects)
    project = output_projects / "GIZMo"
    project_json = read_json(project / "project.json")
    project_json["title"] = "GIZMo Dual Interface"
    project_json["description"] = (
        "Dual-platform GIZMo monitoring and capability-scoped remote configuration."
    )
    write_json(project / "project.json", project_json)

    views = project / "com.inductiveautomation.perspective/views/Pages"
    original_overview = views / "Overview"
    for device, _connection in DEVICES:
        destination = views / f"{device}Overview"
        shutil.copytree(original_overview, destination)
        replace_strings(destination, view_transform(device))
        add_configuration_panel(destination / "view.json", device)
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
                "431 canonical variables per device · approved configuration "
                "writes enabled · history disabled"
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
        configured = configure_export_write_access(tags, device)
        if configured != WRITE_POLICY[device]:
            raise ValueError(
                f"{device} exported write-policy mismatch: "
                f"configured={sorted(configured)}"
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
            "format": "gizmo-ignition-dual/v2",
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
            "bridge_read_only": False,
            "write_policy": {
                device: sorted(WRITE_POLICY[device]) for device, _ in DEVICES
            },
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
