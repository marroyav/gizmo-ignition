#!/usr/bin/env python3
"""Generate a read-only Ignition 8.3 draft for the GIZMo OPC-UA model.

The generated tree can be merged into a stopped, commissioned Ignition
installation.  It creates a read-only external OPC-UA connection, deterministic
tag resources for every variable in a captured GIZMo schema, and a small
Perspective project.  It never configures a database connection.  Tag history
is disabled by default; after a verified backfill, --enable-history enables
only the 50 operational series represented in that backfill.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from prepare_history_backfill import (
    FAST_IMPORT_PATHS,
    PLATFORM_CAPTURE_PATHS,
    SERVICE_NODE_TO_UNIT,
    ignition_tag_path,
)


NAMESPACE_URI = "urn:fnal:gizmo"
CONNECTION_NAME = "GIZMo Bridge"
PROJECT_NAME = "GIZMo"
TAG_ROOT = "GIZMo"
HISTORY_PROVIDER = "GIZMo History"
FAST_HISTORY_PATHS = frozenset(ignition_tag_path(path) for path in FAST_IMPORT_PATHS)
PLATFORM_HISTORY_PATHS = frozenset(
    ignition_tag_path(path) for path in PLATFORM_CAPTURE_PATHS
)
HISTORY_PATHS = FAST_HISTORY_PATHS | PLATFORM_HISTORY_PATHS

TYPE_MAP = {
    "Boolean": "Boolean",
    "DateTime": "DateTime",
    "Double": "Float8",
    "Int32": "Int4",
    "String": "String",
    # Ignition does not expose unsigned atomic tag types.  Widen UInt32 and
    # use the signed 64-bit carrier for counters whose observed values are in
    # range.  The original OPC type remains available in the schema manifest.
    "UInt32": "Int8",
    "UInt64": "Int8",
}

FAST_AREAS = {"Measurement", "Alarm", "Thermal", "Time", "Configuration"}
PLATFORM_AREAS = {"OperatingSystem", "Network", "Services", "Health"}
INVENTORY_AREAS = {"Identity", "Storage", "Firmware", "Calibration"}

COLORS = {
    "background": "#1c1f22",
    "surface": "#3f464d",
    "muted": "#747d86",
    "normal": "#b8c41f",
    "foreground": "#f3f4ea",
    "alarm": "#ff6a2a",
}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def resource(files: list[str], *, scope: str = "G", description: str = "") -> dict[str, Any]:
    value: dict[str, Any] = {
        "scope": scope,
        "version": 1,
        "restricted": False,
        "overridable": True,
        "files": files,
    }
    if scope == "G":
        value["attributes"] = {"config": {}}
    else:
        value["attributes"] = {}
    if description:
        value["description"] = description
    return value


def project_resource(files: list[str]) -> dict[str, Any]:
    value = resource(files, scope="A")
    value["attributes"] = {
        "lastModification": {
            "actor": "gizmo-ignition-generator",
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    }
    return value


def _contract_variables(raw: dict[str, Any]) -> list[dict[str, Any]]:
    objects = {
        str(item["path"]): item
        for item in raw.get("objects", [])
        if isinstance(item, dict) and "path" in item
    }

    def object_browse_path(path: str) -> tuple[str, ...]:
        parts: list[str] = []
        observed: set[str] = set()
        while path != "Device":
            if path in observed or path not in objects:
                raise ValueError(f"invalid contract object hierarchy at {path}")
            observed.add(path)
            item = objects[path]
            parts.append(str(item["browse_name"]))
            parent = item.get("parent")
            if not isinstance(parent, str):
                raise ValueError(f"contract object has no parent: {path}")
            path = parent
        return tuple(reversed(parts))

    variables: list[dict[str, Any]] = []
    for item in raw["variables"]:
        parent = str(item["parent"])
        path = "/".join((*object_browse_path(parent), str(item["browse_name"])))
        unit = item.get("engineering_unit")
        writable = item.get("access") == "ReadWrite"
        variables.append(
            {
                "path": path,
                "node_id": item["node_id"],
                "data_type": item["data_type"],
                "writable": writable,
                "description": item.get("description", ""),
                "engineering_unit": unit.get("symbol", "")
                if isinstance(unit, dict)
                else "",
                "engineering_range": item.get("engineering_range"),
            }
        )
    return variables


def load_schema(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("variables"), list):
        raise ValueError("schema must be a gizmo-opcua-client JSON schema export")
    variables = (
        _contract_variables(raw)
        if raw.get("schema_version") is not None
        else raw["variables"]
    )
    required = {"path", "node_id", "data_type", "writable"}
    for index, variable in enumerate(variables):
        if not isinstance(variable, dict) or not required.issubset(variable):
            raise ValueError(f"schema variable {index} is incomplete")
    paths = [str(variable["path"]) for variable in variables]
    if len(paths) != len(set(paths)):
        raise ValueError("schema contains duplicate variable paths")
    return raw, variables


def ensure_history_variables(variables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add deterministic tags for service counters absent from an older export."""

    result = copy.deepcopy(variables)
    present = {str(variable["path"]) for variable in result}
    source_by_tag = {
        ignition_tag_path(source): source
        for source in PLATFORM_CAPTURE_PATHS
        if source.startswith("Services.Units.")
    }
    missing = sorted(HISTORY_PATHS - present)
    unsupported = [path for path in missing if path not in source_by_tag]
    if unsupported:
        raise ValueError(
            "schema is missing historical variables that cannot be synthesized: "
            + ", ".join(unsupported)
        )
    for path in missing:
        source = source_by_tag[path]
        unit_key = source.split(".")[2]
        unit = SERVICE_NODE_TO_UNIT[unit_key]
        result.append(
            {
                "data_type": "UInt32",
                "description": f"Automatic restart count for {unit}.",
                "node_id": f"ns=0;s=GIZMo.{source}",
                "path": path,
                "writable": False,
            }
        )
    return result


def expanded_node_id(variable: dict[str, Any]) -> str:
    node_id = str(variable["node_id"])
    marker = ";s="
    if marker not in node_id:
        raise ValueError(f"unsupported non-string NodeId: {node_id}")
    identifier = node_id.split(marker, 1)[1]
    return f"nsu={NAMESPACE_URI};s={identifier}"


def tag_group(path: str) -> str:
    area = path.split("/", 1)[0]
    if path == "SDR/LatestFrame":
        return "GIZMo Waveform"
    if area == "SDR" or area in FAST_AREAS:
        return "GIZMo Fast"
    if area in PLATFORM_AREAS:
        return "GIZMo Platform"
    if area in INVENTORY_AREAS:
        return "GIZMo Inventory"
    return "GIZMo Platform"


def ignition_data_type(variable: dict[str, Any]) -> str:
    if variable["path"] == "SDR/LatestFrame":
        return "Int4Array"
    try:
        return TYPE_MAP[str(variable["data_type"])]
    except KeyError as error:
        raise ValueError(f"unsupported OPC data type: {error.args[0]}") from error


def atomic_tag(
    variable: dict[str, Any],
    *,
    enable_history: bool = False,
    history_provider: str = HISTORY_PROVIDER,
) -> dict[str, Any]:
    path = str(variable["path"])
    tag: dict[str, Any] = {
        "name": path.rsplit("/", 1)[-1],
        "tagType": "AtomicTag",
        "valueSource": "opc",
        "dataType": ignition_data_type(variable),
        "enabled": True,
        "readOnly": True,
        "historyEnabled": enable_history and path in HISTORY_PATHS,
        "tagGroup": tag_group(path),
        "opcServer": CONNECTION_NAME,
        "opcItemPath": expanded_node_id(variable),
    }
    if enable_history and path in HISTORY_PATHS:
        sample_rate = 1 if path in FAST_HISTORY_PATHS else 10
        tag.update(
            {
                "historyProvider": history_provider,
                "sampleMode": "Periodic",
                "historySampleRate": sample_rate,
                "historySampleRateUnits": "SEC",
                "deadbandMode": "Off",
                "historicalDeadbandMode": "Off",
                "historyMaxAge": sample_rate,
                "historyMaxAgeUnits": "SEC",
            }
        )
    description = str(variable.get("description", "")).strip()
    if description:
        tag["documentation"] = description
    unit = str(variable.get("engineering_unit", "")).strip()
    if unit:
        tag["engUnit"] = unit
    engineering_range = variable.get("engineering_range")
    if isinstance(engineering_range, dict):
        if "low" in engineering_range:
            tag["engLow"] = engineering_range["low"]
        if "high" in engineering_range:
            tag["engHigh"] = engineering_range["high"]
    if path == "Alarm/Active":
        tag["alarms"] = [
            {
                "name": "Ground Fault",
                "mode": "WhenTrue",
                "priority": "Critical",
                "ackMode": "Manual",
                "displayPath": "GIZMo Ground Fault",
                "notes": (
                    "Authoritative composite alarm emitted by the GIZMo ZMon "
                    "engine; Ignition does not recompute resistance or phase rules."
                ),
            }
        ]
    return tag


def nested_tag_export(
    variables: list[dict[str, Any]],
    *,
    enable_history: bool = False,
    history_provider: str = HISTORY_PROVIDER,
) -> list[dict[str, Any]]:
    root: dict[str, Any] = {"name": TAG_ROOT, "tagType": "Folder", "tags": []}
    folders: dict[tuple[str, ...], dict[str, Any]] = {(): root}
    for variable in sorted(variables, key=lambda item: str(item["path"])):
        parts = str(variable["path"]).split("/")
        parent_key: tuple[str, ...] = ()
        for folder_name in parts[:-1]:
            key = (*parent_key, folder_name)
            if key not in folders:
                folder = {"name": folder_name, "tagType": "Folder", "tags": []}
                folders[parent_key]["tags"].append(folder)
                folders[key] = folder
            parent_key = key
        folders[parent_key]["tags"].append(
            atomic_tag(
                variable,
                enable_history=enable_history,
                history_provider=history_provider,
            )
        )
    return [root]


def install_tag_resources(
    output: Path,
    variables: list[dict[str, Any]],
    *,
    enable_history: bool = False,
    history_provider: str = HISTORY_PROVIDER,
) -> None:
    base = output / "data/config/resources/core/ignition/tag-definition/default" / TAG_ROOT
    by_parent: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    folders: set[tuple[str, ...]] = {()}
    for variable in variables:
        parts = str(variable["path"]).split("/")
        parent = tuple(parts[:-1])
        by_parent[parent].append(
            atomic_tag(
                variable,
                enable_history=enable_history,
                history_provider=history_provider,
            )
        )
        for length in range(1, len(parent) + 1):
            folders.add(parent[:length])

    for folder in sorted(folders):
        destination = base.joinpath(*folder)
        tags = sorted(by_parent.get(folder, []), key=lambda item: item["name"])
        files: list[str] = []
        if tags:
            write_json(destination / "tags.json", tags)
            files.append("tags.json")
        write_json(destination / "unary-resource.json", resource(files))


def install_tag_groups(output: Path) -> None:
    base = output / "data/config/resources/core/ignition/tag-group/default"
    groups = {
        "GIZMo Fast": 1000,
        "GIZMo Platform": 10000,
        "GIZMo Inventory": 30000,
        "GIZMo Waveform": 5000,
    }
    for name, rate in groups.items():
        destination = base / name
        write_json(
            destination / "config.json",
            {
                "config": {
                    "mode": "Direct",
                    "oneShot": False,
                    "opcDataMode": "Subscribed",
                    "optWriteTimeout": 60000,
                    "optWrites": False,
                    "rate": rate,
                    "readAfterWrite": False,
                }
            },
        )
        write_json(destination / "resource.json", resource(["config.json"], scope="A"))


def load_security_template(path: Path) -> dict[str, Any]:
    """Copy the gateway-bound OPC client certificate configuration.

    Ignition's OPC-UA client always needs its application key pair, including
    for SecurityPolicy=None.  The encrypted key-store password is bound to the
    commissioned Gateway, so it must be taken from an existing working OPC-UA
    connection on that same Gateway and must never be committed.
    """
    value = json.loads(path.read_text(encoding="utf-8"))
    try:
        security = value["settings"]["security"]
        password = security["keyStoreAliasPassword"]
    except (KeyError, TypeError) as error:
        raise ValueError(
            "security template must be an Ignition OPC connection config.json "
            "containing settings.security.keyStoreAliasPassword"
        ) from error
    if not isinstance(security, dict) or not isinstance(password, dict):
        raise ValueError("security template contains an invalid key-store secret")
    if not isinstance(security.get("keyStoreAlias"), str):
        raise ValueError("security template is missing settings.security.keyStoreAlias")
    return copy.deepcopy(security)


def install_connection(
    output: Path,
    endpoint: str,
    security: dict[str, Any],
) -> None:
    destination = (
        output
        / "data/config/resources/core/ignition/opc-connection"
        / CONNECTION_NAME
    )
    write_json(
        destination / "config.json",
        {
            "profile": {
                "readOnly": True,
                "type": "com.inductiveautomation.OpcUaServerType",
            },
            "settings": {
                "advanced": {
                    "acknowledgeTimeout": 5000,
                    "browseOrigin": "OBJECTS_FOLDER",
                    "connectTimeout": 5000,
                    "deprecatedDataTypeDictionarySupport": False,
                    "maxArrayLength": 2147483647,
                    "maxMessageSize": 33554432,
                    "maxNotificationsPerPublish": 65535,
                    "maxPendingPublishRequests": 2,
                    "maxPerOperation": 8192,
                    "maxReferencesPerNode": 8192,
                    "maxStringLength": 2147483647,
                    "requestTimeout": 60000,
                    "sessionTimeout": 120000,
                    "timestampSource": "OPC_PREFER_SOURCE",
                },
                "authentication": {"authenticationType": "ANONYMOUS"},
                "configVersion": 2,
                "endpoint": {
                    "discoveryUrl": endpoint,
                    "endpointUrl": endpoint,
                    "hostOverride": "",
                    "securityMode": "None",
                    "securityPolicy": "None",
                },
                "failover": {
                    "discoveryUrl": "",
                    "enabled": False,
                    "endpointUrl": "",
                    "hostOverride": "",
                    "threshold": 3,
                },
                "keepAlive": {
                    "failuresAllowed": 1,
                    "interval": 15000,
                    "timeout": 10000,
                },
                "security": security,
            },
        },
    )
    connection_resource = resource(
        ["config.json"],
        scope="A",
        description="Read-only anonymous OPC-UA bridge to the GIZMo canonical model.",
    )
    connection_resource["attributes"] = {
        "uuid": str(uuid5(NAMESPACE_URL, "urn:fnal:gizmo:ignition:opc-bridge")),
        "enabled": True,
    }
    write_json(destination / "resource.json", connection_resource)


def tag_binding(path: str, transform: str | None = None) -> dict[str, Any]:
    binding: dict[str, Any] = {
        "type": "tag",
        "config": {
            "mode": "direct",
            "tagPath": f"[default]{TAG_ROOT}/{path}",
            "fallbackDelay": 2.5,
            "publishInitial": False,
        },
    }
    if transform:
        binding["transforms"] = [{"type": "expression", "expression": transform}]
    return binding


def label(
    name: str,
    text: Any,
    *,
    size: str = "14px",
    color: str | None = None,
    weight: int = 500,
    basis: str = "auto",
    grow: int = 0,
    binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    component: dict[str, Any] = {
        "type": "ia.display.label",
        "meta": {"name": name},
        "position": {"basis": basis, "grow": grow, "shrink": 1},
        "props": {
            "text": text,
            "textStyle": {
                "color": color or COLORS["foreground"],
                "fontFamily": "JetBrains Mono, IBM Plex Mono, monospace",
                "fontSize": size,
                "fontWeight": weight,
            },
        },
    }
    if binding:
        component["propConfig"] = {"props.text": {"binding": binding}}
    return component


def metric(
    name: str,
    title: str,
    path: str,
    *,
    transform: str | None = None,
    big: bool = False,
) -> dict[str, Any]:
    return {
        "type": "ia.container.flex",
        "meta": {"name": name},
        "position": {"basis": "250px", "grow": 1, "shrink": 1},
        "props": {
            "direction": "column",
            "style": {
                "backgroundColor": COLORS["surface"],
                "borderRadius": "8px",
                "margin": "6px",
                "minHeight": "126px" if not big else "185px",
                "padding": "18px",
            },
        },
        "children": [
            label(
                f"{name}Title",
                title.upper(),
                size="11px",
                color="#c3c9cf",
                weight=700,
                basis="26px",
            ),
            label(
                f"{name}Value",
                "—",
                size="46px" if big else "23px",
                color=COLORS["foreground"],
                weight=700,
                basis="auto",
                grow=1,
                binding=tag_binding(path, transform),
            ),
        ],
    }


def overview_view(variable_count: int) -> dict[str, Any]:
    resistance_expression = (
        "if({[default]GIZMo/Measurement/ResistanceRange} = 'OutOfRange', "
        "'HIGH Z', concat(numberFormat({[default]GIZMo/Measurement/ResistanceOhm}, "
        "'0.0'), ' Ω'))"
    )
    root: dict[str, Any] = {
        "type": "ia.container.flex",
        "meta": {"name": "root"},
        "props": {
            "direction": "column",
            "style": {
                "backgroundColor": COLORS["background"],
                "boxSizing": "border-box",
                "color": COLORS["foreground"],
                "fontFamily": "JetBrains Mono, IBM Plex Mono, monospace",
                "minHeight": "100%",
                "padding": "22px",
            },
        },
        "children": [
            {
                "type": "ia.container.flex",
                "meta": {"name": "AlarmRail"},
                "position": {"basis": "8px", "grow": 0, "shrink": 0},
                "props": {
                    "style": {
                        "backgroundColor": COLORS["normal"],
                        "borderRadius": "5px",
                        "marginBottom": "18px",
                    }
                },
                "propConfig": {
                    "props.style.backgroundColor": {
                        "binding": tag_binding(
                            "Alarm/Active",
                            f"if({{value}}, '{COLORS['alarm']}', '{COLORS['normal']}')",
                        )
                    }
                },
            },
            {
                "type": "ia.container.flex",
                "meta": {"name": "Heading"},
                "position": {"basis": "84px", "grow": 0, "shrink": 0},
                "props": {"direction": "row", "alignItems": "center"},
                "children": [
                    label(
                        "Title",
                        "GIZMo",
                        size="32px",
                        color=COLORS["normal"],
                        weight=800,
                        grow=1,
                    ),
                    label(
                        "Environment",
                        "READ ONLY · IGNITION BRIDGE",
                        size="12px",
                        color="#c3c9cf",
                        weight=700,
                        basis="280px",
                    ),
                ],
            },
            {
                "type": "ia.container.flex",
                "meta": {"name": "Primary"},
                "position": {"basis": "205px", "grow": 0, "shrink": 0},
                "props": {"direction": "row", "wrap": "wrap"},
                "children": [
                    metric(
                        "Resistance",
                        "Equivalent impedance",
                        "Measurement/ResistanceOhm",
                        big=True,
                    ),
                    metric(
                        "Alarm",
                        "Authoritative alarm",
                        "Alarm/Active",
                        transform="if({value}, 'ALARM', 'NORMAL')",
                        big=True,
                    ),
                    metric(
                        "Reason",
                        "Alarm reason",
                        "Alarm/Reason",
                        big=True,
                    ),
                ],
            },
            {
                "type": "ia.container.flex",
                "meta": {"name": "Measurements"},
                "position": {"basis": "150px", "grow": 0, "shrink": 0},
                "props": {"direction": "row", "wrap": "wrap"},
                "children": [
                    metric(
                        "ResistanceDisplay",
                        "Resistance",
                        "Measurement/ResistanceOhm",
                        transform="concat(numberFormat({value}, '0.0'), ' Ω')",
                    ),
                    metric(
                        "Threshold",
                        "Threshold",
                        "Measurement/ThresholdOhm",
                        transform="concat(numberFormat({value}, '0'), ' Ω')",
                    ),
                    metric(
                        "Capacitance",
                        "Capacitance",
                        "Measurement/CapacitanceNanofarad",
                        transform="concat(numberFormat({value}, '0.00'), ' nF')",
                    ),
                    metric(
                        "Phase",
                        "Phase",
                        "Measurement/PhaseInterpolatedDegrees",
                        transform="concat(numberFormat({value}, '0.00'), '°')",
                    ),
                ],
            },
            {
                "type": "ia.container.flex",
                "meta": {"name": "System"},
                "position": {"basis": "150px", "grow": 0, "shrink": 0},
                "props": {"direction": "row", "wrap": "wrap"},
                "children": [
                    metric("Health", "Overall health", "Health/Overall"),
                    metric("LocalTime", "GIZMo local time", "Time/CurrentLocal"),
                    metric("LatchTime", "Alarm latch time", "Alarm/LatchTime"),
                    metric(
                        "Temperature",
                        "Chassis temperature",
                        "Thermal/ChassisTemperatureCelsius",
                        transform="concat(numberFormat({value}, '0.0'), ' °C')",
                    ),
                ],
            },
            label(
                "Note",
                (
                    "The GIZMo is offline, so Bad/Disconnected quality overlays are expected. "
                    f"All {variable_count} canonical variables are configured under "
                    "[default]GIZMo."
                ),
                size="12px",
                color="#c3c9cf",
                weight=500,
                basis="54px",
            ),
        ],
    }

    # Resistance needs a multi-tag expression rather than a transform of one
    # tag so HIGH Z retains the authoritative range semantics.
    resistance = root["children"][2]["children"][0]["children"][1]
    resistance["propConfig"] = {
        "props.text": {
            "binding": {
                "type": "expr",
                "config": {"expression": resistance_expression},
            }
        }
    }
    return {"root": root, "props": {"defaultSize": {"width": 1440, "height": 900}}}


def inventory_view(
    variables: list[dict[str, Any]], *, enable_history: bool = False
) -> dict[str, Any]:
    rows = [
        {
            "path": variable["path"],
            "opcType": variable["data_type"],
            "ignitionType": ignition_data_type(variable),
            "unit": variable.get("engineering_unit", ""),
            "group": tag_group(str(variable["path"])),
            "sourceWritable": bool(variable.get("writable", False)),
            "bridgeReadOnly": True,
            "description": variable.get("description", ""),
        }
        for variable in sorted(variables, key=lambda item: str(item["path"]))
    ]
    root = {
        "type": "ia.container.flex",
        "meta": {"name": "root"},
        "props": {
            "direction": "column",
            "style": {
                "backgroundColor": COLORS["background"],
                "boxSizing": "border-box",
                "color": COLORS["foreground"],
                "fontFamily": "JetBrains Mono, IBM Plex Mono, monospace",
                "height": "100%",
                "padding": "22px",
            },
        },
        "children": [
            label(
                "Title",
                "GIZMo tag inventory",
                size="28px",
                color=COLORS["normal"],
                weight=800,
                basis="52px",
            ),
            label(
                "Summary",
                (
                    f"{len(rows)} canonical OPC-UA variables · all imported read-only · "
                    + (
                        f"{len(HISTORY_PATHS)} operational tags historized"
                        if enable_history
                        else "history disabled"
                    )
                ),
                size="12px",
                color="#c3c9cf",
                weight=600,
                basis="42px",
            ),
            {
                "type": "ia.display.table",
                "meta": {"name": "TagInventory"},
                "position": {"basis": "auto", "grow": 1, "shrink": 1},
                "props": {
                    "data": rows,
                    "filter": {"enabled": True, "visible": True},
                    "pager": {"enabled": True, "pageSize": 25},
                    "selection": {"enabled": True, "mode": "single"},
                    "style": {
                        "backgroundColor": COLORS["surface"],
                        "border": "none",
                        "color": COLORS["foreground"],
                        "fontFamily": "JetBrains Mono, IBM Plex Mono, monospace",
                    },
                },
            },
        ],
    }
    return {"root": root, "props": {"defaultSize": {"width": 1440, "height": 900}}}


def install_project(
    output: Path,
    variables: list[dict[str, Any]],
    *,
    enable_history: bool = False,
) -> None:
    project = output / "data/projects" / PROJECT_NAME
    write_json(
        project / "project.json",
        {
            "title": "GIZMo Monitor",
            "description": (
                "Read-only draft Perspective project for the canonical GIZMo OPC-UA model."
            ),
            "enabled": True,
            "inheritable": False,
            "parent": "",
        },
    )
    page_config = project / "com.inductiveautomation.perspective/page-config"
    write_json(
        page_config / "config.json",
        {
            "pages": {
                "/": {"viewPath": "Pages/Overview", "title": "GIZMo Monitor"},
                "/tags": {"viewPath": "Pages/TagInventory", "title": "GIZMo Tags"},
            },
            "sharedDocks": {},
        },
    )
    write_json(page_config / "resource.json", project_resource(["config.json"]))
    views = project / "com.inductiveautomation.perspective/views/Pages"
    for name, view in (
        ("Overview", overview_view(len(variables))),
        (
            "TagInventory",
            inventory_view(variables, enable_history=enable_history),
        ),
    ):
        destination = views / name
        write_json(destination / "view.json", view)
        write_json(destination / "resource.json", project_resource(["view.json"]))


def generate(
    schema_path: Path,
    output: Path,
    endpoint: str,
    security_template_path: Path,
    *,
    force: bool,
    omit_connection: bool = False,
    enable_history: bool = False,
    history_provider: str = HISTORY_PROVIDER,
) -> None:
    schema, variables = load_schema(schema_path)
    if enable_history:
        variables = ensure_history_variables(variables)
    security = (
        None if omit_connection else load_security_template(security_template_path)
    )
    if output.exists():
        if not force:
            raise FileExistsError(f"output exists; use --force: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True)

    if security is not None:
        install_connection(output, endpoint, security)
    install_tag_groups(output)
    install_tag_resources(
        output,
        variables,
        enable_history=enable_history,
        history_provider=history_provider,
    )
    install_project(output, variables, enable_history=enable_history)
    write_json(
        output / "exports/gizmo-tags.json",
        nested_tag_export(
            variables,
            enable_history=enable_history,
            history_provider=history_provider,
        ),
    )

    schema_bytes = schema_path.read_bytes()
    type_counts = Counter(str(variable["data_type"]) for variable in variables)
    area_counts = Counter(str(variable["path"]).split("/", 1)[0] for variable in variables)
    write_json(
        output / "manifest.json",
        {
            "format": "gizmo-ignition-draft/v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "ignition_target": "8.3.8",
            "project": PROJECT_NAME,
            "connection": CONNECTION_NAME,
            "endpoint": endpoint,
            "namespace_uri": schema.get("namespace_uri", NAMESPACE_URI),
            "schema_sha256": hashlib.sha256(schema_bytes).hexdigest(),
            "opcua_model_version": schema.get("model_version"),
            "opcua_contract_authority": schema.get("authority"),
            "opcua_contract_sha256": schema.get("contract_sha256"),
            "variable_count": len(variables),
            "opc_type_counts": dict(sorted(type_counts.items())),
            "area_counts": dict(sorted(area_counts.items())),
            "bridge_read_only": True,
            "connection_resources_committed": not omit_connection,
            "gateway_bound_keystore_security": not omit_connection,
            "tag_history_enabled": enable_history,
            "historized_tag_count": len(HISTORY_PATHS) if enable_history else 0,
            "history_provider": history_provider if enable_history else None,
            "database_connections_created": 0,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--endpoint",
        default="opc.tcp://127.0.0.1:48453",
        help="GIZMo OPC-UA endpoint as seen by the Ignition host",
    )
    parser.add_argument(
        "--security-template",
        type=Path,
        required=False,
        help=(
            "config.json from an existing working OPC-UA connection on the "
            "target commissioned Gateway; its encrypted key-store secret is "
            "copied into the generated connection resource"
        ),
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--omit-connection",
        action="store_true",
        help="generate publication-safe resources without an OPC connection",
    )
    parser.add_argument(
        "--enable-history",
        action="store_true",
        help=(
            "enable periodic Core Historian storage for the 50 curated "
            "operational variables; use only after the SQLite backfill is verified"
        ),
    )
    parser.add_argument(
        "--history-provider",
        default=HISTORY_PROVIDER,
        help=(
            "historian assigned to the curated tags when --enable-history is set"
        ),
    )
    args = parser.parse_args()
    if not args.omit_connection and args.security_template is None:
        parser.error("--security-template is required unless --omit-connection is used")
    generate(
        args.schema,
        args.output,
        args.endpoint,
        args.security_template or Path("."),
        force=args.force,
        omit_connection=args.omit_connection,
        enable_history=args.enable_history,
        history_provider=args.history_provider,
    )


if __name__ == "__main__":
    main()
