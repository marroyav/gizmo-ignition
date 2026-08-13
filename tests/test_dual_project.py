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
