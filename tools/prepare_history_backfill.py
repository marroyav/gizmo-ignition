#!/usr/bin/env python3
"""Convert the preserved GIZMo SQLite history into resumable Ignition batches.

The converter is deliberately read-only with respect to the source database.
It emits one gzip-compressed NDJSON file per UTC day and source cadence.  Each
line contains a receive timestamp, values, and Ignition quality codes whose
ordering is fixed by the accompanying manifest.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import sqlite3
import sys
import zlib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import quote


FORMAT_VERSION = 1
TAG_PATH_POLICY = "ignition-safe-service-keys/v1"

# The binary ordering is part of the on-device historian schema.  Keep the
# complete arrays here, then select only useful, chartable operational data for
# Ignition.  Repeated identity/diagnostic strings remain in the preserved
# SQLite source without consuming a historical point every second.
FAST_CAPTURE_PATHS = (
    "Identity.ModelVersion",
    "Identity.RuntimeVersion",
    "Identity.BootId",
    "Measurement.Sequence",
    "Measurement.SampleTime",
    "Measurement.ResistanceOhm",
    "Measurement.CapacitanceNanofarad",
    "Measurement.ThresholdOhm",
    "Measurement.StimulusFrequencyHertz",
    "Measurement.MagnitudeCount",
    "Measurement.PhaseAtanDegrees",
    "Measurement.PhaseAtan2Degrees",
    "Measurement.PhaseInterpolatedDegrees",
    "Measurement.InPhaseCount",
    "Measurement.QuadratureCount",
    "Thermal.ChassisTemperatureCelsius",
    "Thermal.CPU1TemperatureCelsius",
    "Thermal.CPU2TemperatureCelsius",
    "Thermal.CPU3TemperatureCelsius",
    "Time.UptimeSeconds",
    "SDR.FrameSequence",
    "Measurement.ResistanceRange",
    "Measurement.Quality",
    "Measurement.Diagnostic",
    "Alarm.Active",
    "Alarm.Latched",
    "Alarm.Reason",
    "Alarm.LatchTime",
)

FAST_IMPORT_PATHS = (
    "Measurement.ResistanceOhm",
    "Measurement.CapacitanceNanofarad",
    "Measurement.ThresholdOhm",
    "Measurement.StimulusFrequencyHertz",
    "Measurement.MagnitudeCount",
    "Measurement.PhaseAtanDegrees",
    "Measurement.PhaseAtan2Degrees",
    "Measurement.PhaseInterpolatedDegrees",
    "Measurement.InPhaseCount",
    "Measurement.QuadratureCount",
    "Thermal.ChassisTemperatureCelsius",
    "Thermal.CPU1TemperatureCelsius",
    "Thermal.CPU2TemperatureCelsius",
    "Thermal.CPU3TemperatureCelsius",
    "Time.UptimeSeconds",
    "SDR.FrameSequence",
    "Alarm.Active",
    # This string is authoritative for HIGH Z.  ResistanceOhm stays null with
    # Bad_OutofRange quality; no artificial 500 ohm value is introduced.
    "Measurement.ResistanceRange",
)

PLATFORM_CAPTURE_PATHS = (
    "OperatingSystem.CpuUtilizationPercent",
    "OperatingSystem.Load1Minute",
    "OperatingSystem.Load5Minute",
    "OperatingSystem.Load15Minute",
    "OperatingSystem.MemoryUsedBytes",
    "OperatingSystem.MemoryAvailableBytes",
    "OperatingSystem.ProcessCount",
    "OperatingSystem.OpenFileHandles",
    "Storage.Filesystems.Root.UsedPercent",
    "Storage.Filesystems.State.UsedPercent",
    "Storage.Filesystems.Run.UsedPercent",
    "Network.Interfaces.eth0.RxBytes",
    "Network.Interfaces.eth0.TxBytes",
    "Network.Interfaces.eth0.RxErrors",
    "Network.Interfaces.eth0.TxErrors",
    "Network.Interfaces.eth1.RxBytes",
    "Network.Interfaces.eth1.TxBytes",
    "Network.Interfaces.eth1.RxErrors",
    "Network.Interfaces.eth1.TxErrors",
    "Services.Units.gizmo_target.RestartCount",
    "Services.Units.gizmo_network_service.RestartCount",
    "Services.Units.gizmo_hardware_service.RestartCount",
    "Services.Units.gizmo_control_socket.RestartCount",
    "Services.Units.gizmo_control_service.RestartCount",
    "Services.Units.gizmo_zmon_service.RestartCount",
    "Services.Units.gizmo_display_service.RestartCount",
    "Services.Units.gizmo_temperature_service.RestartCount",
    "Services.Units.gizmo_sdr_service.RestartCount",
    "Services.Units.gizmo_zmq_service.RestartCount",
    "Services.Units.gizmo_opcua_service.RestartCount",
    "Services.Units.gizmo_historian_service.RestartCount",
    "Services.Units.gizmo_dashboard_service.RestartCount",
)

GROUPS = (
    ("fast", "fast_sample", FAST_CAPTURE_PATHS, FAST_IMPORT_PATHS),
    (
        "platform",
        "platform_sample",
        PLATFORM_CAPTURE_PATHS,
        PLATFORM_CAPTURE_PATHS,
    ),
)

SERVICE_NODE_TO_UNIT = {
    "gizmo_target": "gizmo.target",
    "gizmo_network_service": "gizmo-network.service",
    "gizmo_hardware_service": "gizmo-hardware.service",
    "gizmo_control_socket": "gizmo-control.socket",
    "gizmo_control_service": "gizmo-control.service",
    "gizmo_zmon_service": "gizmo-zmon.service",
    "gizmo_display_service": "gizmo-display.service",
    "gizmo_temperature_service": "gizmo-temperature.service",
    "gizmo_sdr_service": "gizmo-sdr.service",
    "gizmo_zmq_service": "gizmo-zmq.service",
    "gizmo_opcua_service": "gizmo-opcua.service",
    "gizmo_historian_service": "gizmo-historian.service",
    "gizmo_dashboard_service": "gizmo-dashboard.service",
}

# Ignition 8.3 quality subcodes.  Good_Backfill identifies intentional
# out-of-order historical ingestion while retaining Good semantics.
GOOD_BACKFILL = 203
UNCERTAIN = 256
UNCERTAIN_INITIAL = 258
BAD = 512
BAD_UNAUTHORIZED = 513
BAD_ACCESS_DENIED = 514
BAD_NOT_FOUND = 519
BAD_NOT_CONNECTED = 522
BAD_OUT_OF_RANGE = 524


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def quality_code(status: Any) -> int:
    """Map OPC UA status names to the closest Ignition quality subcode."""

    name = str(status or "BadWaitingForInitialData")
    lowered = "".join(character for character in name.lower() if character.isalnum())
    if lowered.startswith("good"):
        return GOOD_BACKFILL
    if lowered.startswith("uncertain"):
        return UNCERTAIN
    if "waitingforinitialdata" in lowered or "initialvalue" in lowered:
        return UNCERTAIN_INITIAL
    if "outofrange" in lowered:
        return BAD_OUT_OF_RANGE
    if any(
        marker in lowered
        for marker in (
            "notconnected",
            "connectionclosed",
            "communicationerror",
            "servernotconnected",
            "nocommunication",
        )
    ):
        return BAD_NOT_CONNECTED
    if "useraccessdenied" in lowered or "accessdenied" in lowered:
        return BAD_ACCESS_DENIED
    if "unauthorized" in lowered or "identitytoken" in lowered:
        return BAD_UNAUTHORIZED
    if any(marker in lowered for marker in ("notfound", "nodeidunknown")):
        return BAD_NOT_FOUND
    if lowered.startswith("bad") or lowered.startswith("error"):
        return BAD
    return BAD


def decode_entries(payload: bytes, expected_count: int) -> list[list[Any]]:
    entries = json.loads(zlib.decompress(payload))
    if not isinstance(entries, list) or len(entries) != expected_count:
        raise ValueError(
            "historian payload entry count does not match its capture schema"
        )
    for entry in entries:
        if not isinstance(entry, list) or len(entry) != 3:
            raise ValueError("historian payload contains an invalid scalar entry")
    return entries


def ignition_tag_path(variable: str) -> str:
    """Translate the canonical NodeId path to its Ignition browse/tag path."""

    parts = variable.split(".")
    if len(parts) == 4 and parts[:2] == ["Services", "Units"]:
        # Keep the canonical OPC-safe unit key in the browse path. Ignition's
        # TagPathParser treats a dot as the separator before a tag property,
        # so a readable systemd name such as ``gizmo-historian.service``
        # cannot be used as an intermediate folder in a historical tag path.
        # The human-readable unit remains available in the Unit tag and the
        # tag documentation.
        unit_key = parts[2]
        if unit_key not in SERVICE_NODE_TO_UNIT:
            raise ValueError(f"unknown normalized systemd unit key: {parts[2]}")
        return f"Services/Units/{unit_key}/{parts[3]}"
    if len(parts) == 2 and parts[0] == "Thermal" and parts[1].startswith("CPU"):
        return "Thermal/Cpu" + parts[1][3:]
    return variable.replace(".", "/")


def readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def historical_path(
    variable: str,
    *,
    provider: str,
    gateway: str,
    tag_provider: str,
    tag_root: str,
) -> str:
    tag_path = ignition_tag_path(variable)
    return (
        f"histprov:{provider}:/sys:{gateway}:/prov:{tag_provider}:"
        f"/tag:{tag_root}/{tag_path}"
    )


class DailyWriter:
    def __init__(self, output: Path, group: str, day: str) -> None:
        self.output = output
        self.group = group
        self.day = day
        self.relative_path = f"batches/{day}-{group}.ndjson.gz"
        self.final_path = output / self.relative_path
        self.temporary_path = self.final_path.with_suffix(
            self.final_path.suffix + ".tmp"
        )
        self.temporary_path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = gzip.GzipFile(
            filename=str(self.temporary_path), mode="wb", compresslevel=6, mtime=0
        )
        self.rows = 0
        self.first_timestamp_ms: Optional[int] = None
        self.last_timestamp_ms: Optional[int] = None

    def write(self, timestamp_ms: int, values: list[Any], qualities: list[int]) -> None:
        if self.first_timestamp_ms is None:
            self.first_timestamp_ms = timestamp_ms
        self.last_timestamp_ms = timestamp_ms
        encoded = json.dumps(
            [timestamp_ms, values, qualities],
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self.stream.write(encoded + b"\n")
        self.rows += 1

    def close(self, path_count: int) -> dict[str, Any]:
        self.stream.close()
        os.replace(self.temporary_path, self.final_path)
        return {
            "day": self.day,
            "group": self.group,
            "path": self.relative_path,
            "rows": self.rows,
            "points": self.rows * path_count,
            "first_timestamp_ms": self.first_timestamp_ms,
            "last_timestamp_ms": self.last_timestamp_ms,
            "bytes": self.final_path.stat().st_size,
            "sha256": sha256_file(self.final_path),
        }


def source_rows(
    connection: sqlite3.Connection, table: str
) -> Iterator[sqlite3.Row]:
    yield from connection.execute(
        f"SELECT receive_time_us, payload FROM {table} ORDER BY receive_time_us"
    )


def prepare_group(
    connection: sqlite3.Connection,
    output: Path,
    group: str,
    table: str,
    capture_paths: tuple[str, ...],
    import_paths: tuple[str, ...],
) -> tuple[list[dict[str, Any]], Counter[str], int]:
    indices = [capture_paths.index(path) for path in import_paths]
    status_counts: Counter[str] = Counter()
    files: list[dict[str, Any]] = []
    writer: Optional[DailyWriter] = None
    total_rows = 0

    for row in source_rows(connection, table):
        receive_time_us = int(row["receive_time_us"])
        timestamp_ms = receive_time_us // 1000
        day = datetime.fromtimestamp(
            timestamp_ms / 1000, timezone.utc
        ).date().isoformat()
        if writer is None or writer.day != day:
            if writer is not None:
                files.append(writer.close(len(import_paths)))
            writer = DailyWriter(output, group, day)

        entries = decode_entries(row["payload"], len(capture_paths))
        selected = [entries[index] for index in indices]
        values = [normalize_value(entry[0]) for entry in selected]
        statuses = [str(entry[1] or "BadWaitingForInitialData") for entry in selected]
        qualities = [quality_code(status) for status in statuses]
        status_counts.update(statuses)
        writer.write(timestamp_ms, values, qualities)
        total_rows += 1

    if writer is not None:
        files.append(writer.close(len(import_paths)))
    return files, status_counts, total_rows


def existing_output_is_valid(output: Path, source_sha256: str) -> bool:
    manifest_path = output / "manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if manifest.get("source", {}).get("sha256") != source_sha256:
        return False
    if manifest.get("tag_path_policy") != TAG_PATH_POLICY:
        return False
    for item in manifest.get("files", []):
        path = output / str(item.get("path", ""))
        if not path.is_file() or sha256_file(path) != item.get("sha256"):
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--historian-provider", default="GIZMo History")
    parser.add_argument("--gateway-name", required=True)
    parser.add_argument("--tag-provider", default="default")
    parser.add_argument("--tag-root", default="GIZMo")
    args = parser.parse_args()

    database = args.database.resolve()
    if not database.is_file():
        parser.error(f"source database does not exist: {database}")
    source_sha256 = sha256_file(database)
    if args.expected_sha256 and source_sha256 != args.expected_sha256.lower():
        parser.error(
            f"source SHA-256 mismatch: expected {args.expected_sha256}, "
            f"found {source_sha256}"
        )

    output = args.output_dir.resolve()
    if output.exists() and existing_output_is_valid(output, source_sha256):
        print(
            json.dumps(
                {
                    "action": "already-prepared",
                    "manifest": str(output / "manifest.json"),
                    "source_sha256": source_sha256,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if output.exists() and any(output.iterdir()):
        parser.error(
            f"output directory is non-empty and has no valid matching manifest: {output}"
        )
    output.mkdir(parents=True, exist_ok=True)

    with readonly_connection(database) as connection:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise RuntimeError(f"SQLite quick_check failed: {quick_check}")
        schema_row = connection.execute(
            "SELECT value FROM historian_meta WHERE key='schema_version'"
        ).fetchone()
        if schema_row is None or int(schema_row["value"]) != 2:
            raise RuntimeError("expected GIZMo historian schema version 2")

        group_details: dict[str, dict[str, Any]] = {}
        files: list[dict[str, Any]] = []
        for group, table, capture_paths, import_paths in GROUPS:
            group_files, status_counts, total_rows = prepare_group(
                connection,
                output,
                group,
                table,
                capture_paths,
                import_paths,
            )
            files.extend(group_files)
            group_details[group] = {
                "source_table": table,
                "source_rows": total_rows,
                "source_paths": list(import_paths),
                "historical_paths": [
                    historical_path(
                        path,
                        provider=args.historian_provider,
                        gateway=args.gateway_name,
                        tag_provider=args.tag_provider,
                        tag_root=args.tag_root,
                    )
                    for path in import_paths
                ],
                "status_counts": dict(sorted(status_counts.items())),
            }

    files.sort(key=lambda item: (item["day"], item["group"]))
    manifest = {
        "format_version": FORMAT_VERSION,
        "tag_path_policy": TAG_PATH_POLICY,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "timestamp_policy": (
            "SQLite receive_time_us truncated to epoch milliseconds; per-scalar "
            "source timestamps are intentionally not used because the captured "
            "board clock experienced discontinuities."
        ),
        "high_z_policy": (
            "ResistanceOhm remains null/Bad_OutofRange and "
            "ResistanceRange retains the authoritative OutOfRange string."
        ),
        "quality_policy": (
            "Source Good values map to Ignition Good_Backfill (203); recognized "
            "uncertain and bad conditions retain the closest Ignition subcode."
        ),
        "source": {
            "path": str(database),
            "sha256": source_sha256,
            "bytes": database.stat().st_size,
            "mtime_ns": database.stat().st_mtime_ns,
            "schema_version": 2,
            "quick_check": "ok",
        },
        "destination": {
            "historian_provider": args.historian_provider,
            "gateway_name": args.gateway_name,
            "tag_provider": args.tag_provider,
            "tag_root": args.tag_root,
        },
        "groups": group_details,
        "files": files,
        "totals": {
            "files": len(files),
            "rows": sum(item["rows"] for item in files),
            "points": sum(item["points"] for item in files),
            "compressed_bytes": sum(item["bytes"] for item in files),
        },
    }
    manifest_path = output / "manifest.json"
    temporary_manifest = manifest_path.with_suffix(".json.tmp")
    temporary_manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary_manifest, manifest_path)

    print(
        json.dumps(
            {
                "action": "prepared",
                "manifest": str(manifest_path),
                "source_sha256": source_sha256,
                **manifest["totals"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, sqlite3.Error, RuntimeError, ValueError, zlib.error) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
