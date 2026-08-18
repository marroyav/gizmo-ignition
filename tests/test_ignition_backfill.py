#!/usr/bin/env python3
"""Contract tests for the SQLite-to-Ignition history converter."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import Mock


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "tools/prepare_history_backfill.py"
INSTALL_SCRIPT = REPO / "tools/install_history_backfill.py"
BRIDGE_INSTALL_SCRIPT = REPO / "tools/install_quality_history_bridge.py"
POSTGRES_SCRIPT = REPO / "tools/configure_postgresql_history.py"
SWITCH_SCRIPT = REPO / "tools/switch_history_provider.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("prepare_history_backfill", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
backfill = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backfill)
import generate_draft  # noqa: E402  (depends on the deployment helper path above)

POSTGRES_SPEC = importlib.util.spec_from_file_location(
    "configure_postgresql_history", POSTGRES_SCRIPT
)
assert POSTGRES_SPEC is not None and POSTGRES_SPEC.loader is not None
postgres_history = importlib.util.module_from_spec(POSTGRES_SPEC)
POSTGRES_SPEC.loader.exec_module(postgres_history)


def payload(paths: tuple[str, ...], overrides: dict[str, tuple[object, str]]) -> bytes:
    entries = []
    for index, path in enumerate(paths):
        value, status = overrides.get(path, (float(index), "Good"))
        entries.append([value, status, None])
    return zlib.compress(
        json.dumps(entries, separators=(",", ":")).encode("utf-8"), level=1
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class IgnitionBackfillTests(unittest.TestCase):
    def test_history_provider_switch_requires_stopped_gateway_and_keeps_backup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = root / "GIZMo"
            measurement = resources / "Measurement"
            measurement.mkdir(parents=True)
            original = [
                {
                    "name": "ResistanceOhm",
                    "tagType": "AtomicTag",
                    "historyEnabled": True,
                    "historyProvider": "GIZMo History",
                },
                {
                    "name": "ReadOnlyIdentity",
                    "tagType": "AtomicTag",
                    "historyEnabled": False,
                },
            ]
            tag_file = measurement / "tags.json"
            tag_file.write_text(json.dumps(original) + "\n", encoding="utf-8")
            command = [
                sys.executable,
                str(SWITCH_SCRIPT),
                "--resource-root",
                str(resources),
                "--to-provider",
                "GIZMo Dual History",
                "--expected-count",
                "1",
            ]
            dry_run = subprocess.run(
                command, check=True, text=True, capture_output=True
            )
            self.assertEqual(json.loads(dry_run.stdout)["tag_count"], 1)
            self.assertEqual(json.loads(tag_file.read_text()), original)

            refused = subprocess.run(
                [*command, "--apply"], text=True, capture_output=True
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("confirm-gateway-stopped", refused.stderr)

            backup = root / "backup"
            applied = subprocess.run(
                [
                    *command,
                    "--apply",
                    "--confirm-gateway-stopped",
                    "--backup-dir",
                    str(backup),
                ],
                check=True,
                text=True,
                capture_output=True,
            )
            result = json.loads(applied.stdout)
            self.assertEqual(result["verified_tag_count"], 1)
            changed = json.loads(tag_file.read_text())
            self.assertEqual(
                changed[0]["historyProvider"], "GIZMo Dual History"
            )
            self.assertEqual(
                json.loads((backup / "Measurement/tags.json").read_text()),
                original,
            )

    def test_postgresql_resources_are_tls_unpruned_and_secret_safe(self) -> None:
        url = postgres_history.jdbc_url(
            "postgres.example.invalid",
            5432,
            "gizmo_history",
            "verify-full",
            "/etc/pki/ca-trust/source/anchors/fermilab-postgres.pem",
        )
        self.assertEqual(
            url,
            "jdbc:postgresql://postgres.example.invalid:5432/gizmo_history?"
            "sslmode=verify-full&ApplicationName=GIZMo-Ignition&"
            "sslrootcert=%2Fetc%2Fpki%2Fca-trust%2Fsource%2Fanchors%2F"
            "fermilab-postgres.pem",
        )
        synthetic_credential = "test-only-database-value"
        database = postgres_history.database_connection_payload(
            name="GIZMo PostgreSQL",
            connect_url=url,
            username="gizmo_ignition",
            password=synthetic_credential,
        )
        historian = postgres_history.sql_historian_payload(
            "GIZMo PostgreSQL History", "GIZMo PostgreSQL"
        )
        splitter = postgres_history.historian_splitter_payload(
            "GIZMo Dual History", "GIZMo History", "GIZMo PostgreSQL History"
        )
        plan = postgres_history.redacted_plan(database, historian, splitter)
        serialized = json.dumps(plan)
        self.assertNotIn(synthetic_credential, serialized)
        self.assertEqual(
            plan["changes"][0]["resource"]["config"]["password"],
            "<GIZMO_POSTGRES_PASSWORD>",
        )
        settings = historian["config"]["settings"]
        self.assertEqual(settings["database"], "GIZMo PostgreSQL")
        self.assertTrue(settings["partition"]["enabled"])
        self.assertEqual(settings["partition"]["sizeUnits"], "MONTH")
        self.assertFalse(settings["pruning"]["enabled"])
        self.assertEqual(splitter["config"]["profile"]["type"], "HistorySplitter")
        self.assertEqual(
            splitter["config"]["settings"]["primaryHistorian"],
            "GIZMo History",
        )
        self.assertEqual(
            splitter["config"]["settings"]["secondaryHistorian"],
            "GIZMo PostgreSQL History",
        )
        self.assertFalse(
            splitter["config"]["settings"]["queryLimit"]["enabled"]
        )

    def test_postgresql_password_is_gateway_encrypted(self) -> None:
        ciphertext = {
            "protected": "protected-value",
            "encrypted_key": "encrypted-key-value",
            "iv": "iv-value",
            "ciphertext": "ciphertext-value",
            "tag": "tag-value",
        }
        response = Mock(ok=True)
        response.json.return_value = ciphertext
        gateway = Mock()
        gateway.base_url = "http://127.0.0.1:8088"
        gateway.session.post.return_value = response

        secret = postgres_history.embedded_secret_config(
            gateway, "never-print-this"
        )

        self.assertEqual(secret, {"type": "Embedded", "data": ciphertext})
        gateway.session.post.assert_called_once_with(
            "http://127.0.0.1:8088/data/api/v1/encryption/encrypt",
            data=b"never-print-this",
            headers={"Content-Type": "text/plain; charset=utf-8"},
            timeout=30,
        )

    def test_installer_isolates_postgresql_replay_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            (project / "project.json").write_text("{}\n", encoding="utf-8")
            import_root = root / "import"
            command = [
                sys.executable,
                str(INSTALL_SCRIPT),
                "--project-dir",
                str(project),
                "--import-root",
                str(import_root),
                "--storage-path-mode",
                "qualified-historian",
                "--destination-historian",
                "GIZMo PostgreSQL History",
                "--state-file",
                "state-postgresql.json",
                "--validation-file",
                "validation-postgresql.json",
                "--probe-result-file",
                "probe-postgresql.json",
                "--lock-file",
                "backfill-postgresql.lock",
                "--query-attempts",
                "120",
                "--query-delay-ms",
                "1000",
                "--verify-each-batch",
                "--disabled",
            ]
            result = subprocess.run(
                command, check=True, text=True, capture_output=True
            )
            summary = json.loads(result.stdout)
            control = json.loads((import_root / "control.json").read_text())
            self.assertEqual(control["storage_path_mode"], "qualified-historian")
            self.assertEqual(
                control["destination_historian"], "GIZMo PostgreSQL History"
            )
            self.assertEqual(control["state_file"], "state-postgresql.json")
            self.assertEqual(control["query_attempts"], 120)
            self.assertTrue(control["verify_each_batch"])
            self.assertFalse(control["enabled"])
            self.assertEqual(
                summary["validation"],
                str(import_root / "validation-postgresql.json"),
            )
            startup = (
                project / "ignition/startup/onStartup.py"
            ).read_text(encoding="utf-8")
            self.assertIn(f'root = "{import_root}"', startup)
            self.assertIn("qualifiedHistorianPath", startup)
            self.assertIn("system.historian.types.dataPoint", startup)
            self.assertIn("Long(str(value))", startup)

            invalid = subprocess.run(
                [
                    *command,
                    "--state-file",
                    "../shared-state.json",
                ],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("plain file name", invalid.stderr)

    def test_non_good_history_bridge_is_dedicated_and_quality_preserving(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            (project / "project.json").write_text("{}\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(BRIDGE_INSTALL_SCRIPT),
                    "--project-dir",
                    str(project),
                    "--historian-provider",
                    "GIZMo PostgreSQL History",
                    "--gateway-name",
                    "test-gateway",
                ],
                check=True,
                text=True,
                capture_output=True,
            )
            summary = json.loads(result.stdout)
            self.assertTrue(summary["enabled"])
            timer = project / "ignition/timer/GIZMo Non-Good History"
            resource = json.loads((timer / "resource.json").read_text())
            self.assertEqual(resource["attributes"]["delay"], 1000)
            self.assertTrue(resource["attributes"]["fixedDelay"])
            self.assertFalse(resource["attributes"]["sharedThread"])
            source = (timer / "handleTimerEvent.py").read_text()
            self.assertIn("def handleTimerEvent():", source)
            self.assertIn('historianProvider = "GIZMo PostgreSQL History"', source)
            self.assertIn('gatewayName = "test-gateway"', source)
            self.assertIn("quality.getCode()) & 1023", source)
            self.assertIn("if value is None or quality.isGood():", source)
            self.assertNotIn("forceQuality", source)

    def test_quality_mapping(self) -> None:
        self.assertEqual(backfill.quality_code("Good"), 203)
        self.assertEqual(backfill.quality_code("BadOutOfRange"), 524)
        self.assertEqual(backfill.quality_code("BadNotConnected"), 522)
        self.assertEqual(backfill.quality_code("BadWaitingForInitialData"), 258)
        self.assertEqual(backfill.quality_code("BadNodeIdUnknown"), 519)
        self.assertEqual(
            backfill.ignition_tag_path(
                "Services.Units.gizmo_historian_service.RestartCount"
            ),
            "Services/Units/gizmo_historian_service/RestartCount",
        )
        self.assertEqual(
            backfill.historical_path(
                "Measurement.ThresholdOhm",
                provider="GIZMo PostgreSQL History",
                gateway="test-gateway",
                tag_provider="default",
                tag_root="GIZMo",
                path_syntax="sql",
            ),
            "histprov:GIZMo PostgreSQL History:/drv:test-gateway:default:"
            "/tag:GIZMo/Measurement/ThresholdOhm",
        )

    def test_curated_live_history_matches_backfill_model(self) -> None:
        source_paths = (*backfill.FAST_IMPORT_PATHS, *backfill.PLATFORM_CAPTURE_PATHS)
        variables = []
        for source in source_paths:
            path = backfill.ignition_tag_path(source)
            if path in {
                "Services/Units/gizmo_historian_service/RestartCount",
                "Services/Units/gizmo_dashboard_service/RestartCount",
            }:
                continue
            variables.append(
                {
                    "path": path,
                    "node_id": f"ns=3;s=GIZMo.{source}",
                    "data_type": (
                        "Boolean"
                        if source == "Alarm.Active"
                        else "String"
                        if source == "Measurement.ResistanceRange"
                        else "Double"
                    ),
                    "writable": False,
                }
            )
        variables = generate_draft.ensure_history_variables(variables)
        tags = [
            generate_draft.atomic_tag(variable, enable_history=True)
            for variable in variables
        ]
        self.assertEqual(len(tags), 50)
        self.assertTrue(all(tag["historyEnabled"] for tag in tags))
        self.assertTrue(
            all(tag["historyProvider"] == "GIZMo History" for tag in tags)
        )
        self.assertEqual(sum(tag["historySampleRate"] == 1 for tag in tags), 18)
        self.assertEqual(sum(tag["historySampleRate"] == 10 for tag in tags), 32)
        self.assertTrue(all(tag["deadbandMode"] == "Off" for tag in tags))
        self.assertTrue(
            all(
                tag["historyMaxAge"] == tag["historySampleRate"]
                and tag["historyMaxAgeUnits"] == "SEC"
                for tag in tags
            )
        )
        migration_tag = generate_draft.atomic_tag(
            variables[0],
            enable_history=True,
            history_provider="GIZMo Dual History",
        )
        if variables[0]["path"] in generate_draft.HISTORY_PATHS:
            self.assertEqual(
                migration_tag["historyProvider"], "GIZMo Dual History"
            )

    def test_prepare_is_read_only_deterministic_and_high_z_aware(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "history.sqlite3"
            output = root / "staged"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE historian_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO historian_meta VALUES('schema_version', '2');
                    CREATE TABLE fast_sample(receive_time_us INTEGER PRIMARY KEY, payload BLOB NOT NULL);
                    CREATE TABLE platform_sample(receive_time_us INTEGER PRIMARY KEY, payload BLOB NOT NULL);
                    """
                )
                high_z = {
                    "Measurement.ResistanceOhm": (None, "BadOutOfRange"),
                    "Measurement.ResistanceRange": ("OutOfRange", "Good"),
                    "Alarm.Active": (False, "Good"),
                }
                connection.executemany(
                    "INSERT INTO fast_sample VALUES(?, ?)",
                    [
                        (
                            1_774_742_400_123_000,
                            payload(backfill.FAST_CAPTURE_PATHS, high_z),
                        ),
                        (
                            1_774_828_800_456_000,
                            payload(backfill.FAST_CAPTURE_PATHS, {}),
                        ),
                    ],
                )
                connection.execute(
                    "INSERT INTO platform_sample VALUES(?, ?)",
                    (
                        1_774_742_400_123_000,
                        payload(backfill.PLATFORM_CAPTURE_PATHS, {}),
                    ),
                )

            before = sha256(database)
            command = [
                sys.executable,
                str(SCRIPT),
                "--database",
                str(database),
                "--output-dir",
                str(output),
                "--expected-sha256",
                before,
                "--gateway-name",
                "test-gateway",
            ]
            first = subprocess.run(command, check=True, text=True, capture_output=True)
            self.assertEqual(sha256(database), before)
            summary = json.loads(first.stdout)
            self.assertEqual(summary["action"], "prepared")
            self.assertEqual(summary["rows"], 3)
            self.assertEqual(summary["points"], 68)

            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["source"]["quick_check"], "ok")
            self.assertEqual(
                manifest["tag_path_policy"],
                "ignition-safe-service-keys/v1",
            )
            self.assertEqual(manifest["totals"]["files"], 3)
            fast_path = manifest["groups"]["fast"]["source_paths"]
            resistance_index = fast_path.index("Measurement.ResistanceOhm")
            range_index = fast_path.index("Measurement.ResistanceRange")
            first_fast = next(item for item in manifest["files"] if item["group"] == "fast")
            with gzip.open(output / first_fast["path"], "rt", encoding="utf-8") as stream:
                record = json.loads(stream.readline())
            self.assertIsNone(record[1][resistance_index])
            self.assertEqual(record[2][resistance_index], 524)
            self.assertEqual(record[1][range_index], "OutOfRange")
            self.assertEqual(record[2][range_index], 203)
            self.assertIn("/sys:test-gateway:/", manifest["groups"]["fast"]["historical_paths"][0])

            sql_output = root / "staged-sql"
            sql_command = [
                *command,
                "--output-dir",
                str(sql_output),
                "--historian-provider",
                "GIZMo PostgreSQL History",
                "--historian-path-syntax",
                "sql",
            ]
            sql_result = subprocess.run(
                sql_command, check=True, text=True, capture_output=True
            )
            self.assertEqual(json.loads(sql_result.stdout)["action"], "prepared")
            sql_manifest = json.loads((sql_output / "manifest.json").read_text())
            self.assertEqual(
                sql_manifest["destination"]["historian_path_syntax"], "sql"
            )
            self.assertIn(
                "/drv:test-gateway:default:/",
                sql_manifest["groups"]["fast"]["historical_paths"][0],
            )

            second = subprocess.run(command, check=True, text=True, capture_output=True)
            self.assertEqual(json.loads(second.stdout)["action"], "already-prepared")
            self.assertEqual(sha256(database), before)

            bounded_output = root / "staged-bounded"
            cutoff_ms = 1_774_828_800_000
            bounded = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--database",
                    str(database),
                    "--output-dir",
                    str(bounded_output),
                    "--expected-sha256",
                    before,
                    "--gateway-name",
                    "test-gateway",
                    "--before-timestamp-ms",
                    str(cutoff_ms),
                ],
                check=True,
                text=True,
                capture_output=True,
            )
            bounded_summary = json.loads(bounded.stdout)
            self.assertEqual(bounded_summary["rows"], 2)
            self.assertEqual(bounded_summary["points"], 50)
            bounded_manifest = json.loads(
                (bounded_output / "manifest.json").read_text()
            )
            self.assertEqual(
                bounded_manifest["source_window"],
                {
                    "start_timestamp_ms_inclusive": None,
                    "before_timestamp_ms_exclusive": cutoff_ms,
                },
            )
            self.assertLess(
                max(
                    item["last_timestamp_ms"]
                    for item in bounded_manifest["files"]
                ),
                cutoff_ms,
            )

            selective_output = root / "staged-selective"
            selective = subprocess.run(
                [
                    *command,
                    "--output-dir",
                    str(selective_output),
                    "--include-source-path",
                    "Measurement.ThresholdOhm",
                    "--include-source-path",
                    "Alarm.Active",
                ],
                check=True,
                text=True,
                capture_output=True,
            )
            selective_summary = json.loads(selective.stdout)
            self.assertEqual(selective_summary["rows"], 2)
            self.assertEqual(selective_summary["points"], 4)
            selective_manifest = json.loads(
                (selective_output / "manifest.json").read_text()
            )
            self.assertEqual(
                selective_manifest["source_path_filter"],
                ["Measurement.ThresholdOhm", "Alarm.Active"],
            )
            self.assertEqual(
                selective_manifest["groups"]["fast"]["source_paths"],
                ["Measurement.ThresholdOhm", "Alarm.Active"],
            )
            self.assertNotIn("platform", selective_manifest["groups"])

            duplicate_path = subprocess.run(
                [
                    *command,
                    "--output-dir",
                    str(root / "staged-duplicate-path"),
                    "--include-source-path",
                    "Alarm.Active",
                    "--include-source-path",
                    "Alarm.Active",
                ],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(duplicate_path.returncode, 0)
            self.assertIn("must be unique", duplicate_path.stderr)


if __name__ == "__main__":
    unittest.main()
