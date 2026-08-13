#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
BUILDER = REPOSITORY / "tools/build_dual_project.py"
PROJECT = REPOSITORY / "ignition-project"


class DualProjectTests(unittest.TestCase):
    expected_writes = {
        "Kria": {
            "Configuration/AveragesPerCalculation",
            "Configuration/ThresholdOhm",
        },
        "Legacy": {"Configuration/ThresholdOhm"},
    }

    @staticmethod
    def walk_components(component: dict) -> list[dict]:
        result = [component]
        for child in component.get("children", []):
            result.extend(DualProjectTests.walk_components(child))
        return result

    def test_committed_project_is_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            generated = Path(temporary) / "project"
            subprocess.run(
                [sys.executable, str(BUILDER), "--output", str(generated)],
                check=True,
            )
            committed = {
                path.relative_to(PROJECT): path.read_bytes()
                for path in PROJECT.rglob("*")
                if path.is_file()
            }
            rebuilt = {
                path.relative_to(generated): path.read_bytes()
                for path in generated.rglob("*")
                if path.is_file()
            }
            self.assertEqual(committed, rebuilt)

    def test_two_device_trees_have_distinct_connections(self) -> None:
        root = (
            PROJECT
            / "data/config/resources/core/ignition/tag-definition/default/GIZMo"
        )
        expected = {"Kria": "GIZMo Kria", "Legacy": "GIZMo Legacy"}
        for device, connection in expected.items():
            tag_files = sorted((root / device).rglob("tags.json"))
            self.assertTrue(tag_files)
            serialized = "\n".join(path.read_text() for path in tag_files)
            self.assertIn(f'"opcServer": "{connection}"', serialized)
            other = expected["Legacy" if device == "Kria" else "Kria"]
            self.assertNotIn(f'"opcServer": "{other}"', serialized)

    def test_legacy_alarm_is_not_enabled_before_commissioning(self) -> None:
        alarm_path = (
            PROJECT
            / "data/config/resources/core/ignition/tag-definition/default/GIZMo/Legacy/Alarm/tags.json"
        )
        tags = json.loads(alarm_path.read_text())
        active = next(tag for tag in tags if tag["name"] == "Active")
        self.assertNotIn("alarms", active)

    def test_only_validated_configuration_tags_are_writable(self) -> None:
        root = (
            PROJECT
            / "data/config/resources/core/ignition/tag-definition/default/GIZMo"
        )
        for device, expected in self.expected_writes.items():
            writable = set()
            for tags_path in sorted((root / device).rglob("tags.json")):
                tags = json.loads(tags_path.read_text())
                parent = tags_path.parent.relative_to(root / device)
                for tag in tags:
                    if tag.get("tagType") != "AtomicTag":
                        continue
                    path = "/".join((*parent.parts, tag["name"]))
                    if not tag.get("readOnly", False):
                        writable.add(path)
            self.assertEqual(writable, expected, device)

    def test_both_thresholds_use_the_common_contract_range(self) -> None:
        base = (
            PROJECT
            / "data/config/resources/core/ignition/tag-definition/default/GIZMo"
        )
        for device in ("Kria", "Legacy"):
            tags = json.loads(
                (base / device / "Configuration/tags.json").read_text()
            )
            threshold = next(
                tag for tag in tags if tag["name"] == "ThresholdOhm"
            )
            self.assertFalse(threshold["readOnly"])
            self.assertEqual(threshold["engLow"], 0.0)
            self.assertEqual(threshold["engHigh"], 1023.0)
        legacy = json.loads(
            (base / "Legacy/Configuration/tags.json").read_text()
        )
        threshold = next(tag for tag in legacy if tag["name"] == "ThresholdOhm")
        self.assertIn(
            "authenticated remote legacy threshold",
            threshold["documentation"].lower(),
        )

    def test_manifest_records_capability_scoped_write_policy(self) -> None:
        manifest = json.loads((PROJECT / "manifest.json").read_text())
        self.assertEqual(manifest["format"], "gizmo-ignition-dual/v2")
        self.assertEqual(manifest["opcua_model_version"], "1.3.1")
        self.assertEqual(
            manifest["opcua_contract_sha256"],
            "1f10f15a8991a758d7b0c59ef220002336a9615e3378401a6456668e86b51b97",
        )
        self.assertEqual(manifest["variable_count_per_device"], 457)
        self.assertEqual(manifest["total_tag_count"], 914)
        self.assertFalse(manifest["bridge_read_only"])
        self.assertEqual(
            {key: set(value) for key, value in manifest["write_policy"].items()},
            self.expected_writes,
        )

    def test_complete_service_inventory_is_present_for_both_devices(self) -> None:
        base = (
            PROJECT
            / "data/config/resources/core/ignition/tag-definition/default/GIZMo"
        )
        for device in ("Kria", "Legacy"):
            for unit in ("gizmo-dashboard.service", "gizmo-historian.service"):
                tags = json.loads(
                    (base / device / "Services/Units" / unit / "tags.json").read_text()
                )
                self.assertEqual(len(tags), 13)

    def test_overviews_expose_only_the_approved_remote_inputs(self) -> None:
        expected = {
            "Kria": {
                "Configuration/ThresholdOhm": (0, 1023),
                "Configuration/AveragesPerCalculation": (1, 1_000_000),
            },
            "Legacy": {"Configuration/ThresholdOhm": (0, 1023)},
        }
        base = (
            PROJECT
            / "data/projects/GIZMo/com.inductiveautomation.perspective/views/Pages"
        )
        for device, expected_inputs in expected.items():
            view = json.loads((base / f"{device}Overview/view.json").read_text())
            components = self.walk_components(view["root"])
            numeric = [
                item
                for item in components
                if item.get("type") == "ia.input.numeric-entry-field"
            ]
            actual = {}
            for item in numeric:
                config = item["propConfig"]["props.value"]["binding"]["config"]
                prefix = f"[default]GIZMo/{device}/"
                self.assertTrue(config["tagPath"].startswith(prefix))
                path = config["tagPath"][len(prefix) :]
                self.assertTrue(config["bidirectional"])
                self.assertEqual(item["props"]["mode"], "protected")
                bounds = item["props"]["inputBounds"]
                actual[path] = (bounds["minimum"], bounds["maximum"])
            self.assertEqual(actual, expected_inputs, device)

            result = next(
                item
                for item in components
                if item.get("meta", {}).get("name") == "CommandResultValue"
            )
            config = result["propConfig"]["props.text"]["binding"]["config"]
            self.assertNotIn("bidirectional", config)
            self.assertEqual(
                config["tagPath"],
                f"[default]GIZMo/{device}/Configuration/LastCommandResult",
            )

    def test_no_site_connection_or_database_resource_is_committed(self) -> None:
        forbidden_parts = {"opc-connection", "database-connection"}
        for path in PROJECT.rglob("*"):
            self.assertTrue(forbidden_parts.isdisjoint(path.parts), path)
        manifest = json.loads((PROJECT / "manifest.json").read_text())
        self.assertFalse(manifest["connection_resources_committed"])
        self.assertFalse(manifest["database_connections_committed"])

    def test_all_json_is_valid(self) -> None:
        for path in REPOSITORY.rglob("*.json"):
            with self.subTest(path=path):
                json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
