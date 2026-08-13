#!/usr/bin/env python3
"""Idempotently configure the GIZMo Core Historian on an Ignition 8.3 Gateway."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

import requests


RESOURCE_TYPE = "com.inductiveautomation.historian/historian-provider"


class GatewaySession:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self._authenticate(username, password)

    def _authenticate(self, username: str, password: str) -> None:
        login = self.session.get(
            f"{self.base_url}/data/app/login",
            timeout=20,
        )
        login.raise_for_status()
        parsed = urlparse(login.url)
        query = {
            key: values[-1]
            for key, values in parse_qs(parsed.query).items()
        }
        token = query["token"]

        challenge = self.session.post(
            f"{self.base_url}/idp/default/authn/next-challenge",
            json={"token": token},
            timeout=20,
        )
        challenge.raise_for_status()
        token = challenge.json()["token"]

        basic = self.session.post(
            f"{self.base_url}/idp/default/authn/submit-challenge/basic",
            json={
                "token": token,
                "rememberMe": False,
                "challenge": {"username": username, "password": password},
            },
            timeout=20,
        )
        basic.raise_for_status()
        basic_payload = basic.json()
        if not basic_payload.get("success"):
            raise RuntimeError("Ignition rejected the administrator credentials")

        complete = self.session.post(
            f"{self.base_url}/idp/default/authn/next-challenge",
            json={"token": basic_payload["token"]},
            timeout=20,
        )
        complete.raise_for_status()
        complete_payload = complete.json()
        if not complete_payload.get("complete"):
            raise RuntimeError("Ignition requested an unsupported extra challenge")

        query["token"] = complete_payload["token"]
        query["cancel"] = "false"
        authorization = self.session.get(
            f"{self.base_url}/idp/default/oidc/auth?{urlencode(query)}",
            allow_redirects=True,
            timeout=30,
        )
        authorization.raise_for_status()
        if urlparse(authorization.url).path != "/app":
            raise RuntimeError("Ignition administrator authentication did not complete")

        session_state = self.session.get(
            f"{self.base_url}/data/app/session",
            headers={"Accept": "application/json"},
            timeout=20,
        )
        session_state.raise_for_status()
        csrf_token = session_state.json().get("csrfToken")
        if not isinstance(csrf_token, str) or not csrf_token:
            raise RuntimeError("Ignition did not provide a CSRF token")
        self.session.headers.update({"X-CSRF-Token": csrf_token})

    def get_json(self, path: str) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}{path}", timeout=30)
        response.raise_for_status()
        return response.json()

    def post_json(self, path: str, payload: Any) -> dict[str, Any]:
        response = self.session.post(
            f"{self.base_url}{path}",
            json=payload,
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(
                f"POST {path} failed with HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
        return response.json()


def provider_payload(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": (
            "Deduplicating Core Historian for GIZMo live data and the verified "
            "legacy SQLite backfill."
        ),
        "enabled": True,
        "config": {
            "profile": {"type": "CoreHistorian"},
            "settings": {
                "partitionInterval": "MONTH",
                "dataDeduplication": True,
                "maintenanceSettings": {
                    "strategy": "NONE",
                    "directory": "",
                    "maintenanceAgeUnits": "MONTH",
                    "maintenanceAge": 6,
                },
            },
        },
    }


def compact_health(resource: dict[str, Any]) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    for name, check in resource.get("healthchecks", {}).items():
        result = check.get("result", {}) if isinstance(check, dict) else {}
        checks[name] = {
            "healthy": result.get("healthy"),
            "message": result.get("message", ""),
        }
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway-url",
        default=os.environ.get("IGNITION_URL", "http://127.0.0.1:18088"),
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("IGNITION_USERNAME", "admin"),
    )
    parser.add_argument(
        "--provider",
        default="GIZMo History",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    password = os.environ.get("IGNITION_PASSWORD")
    if not password:
        parser.error("set IGNITION_PASSWORD in the environment")

    gateway = GatewaySession(args.gateway_url, args.username, password)
    encoded_name = quote(args.provider, safe="")
    find_path = f"/data/api/v1/resources/find/{RESOURCE_TYPE}/{encoded_name}"

    listing = gateway.get_json(
        f"/data/api/v1/resources/list/{RESOURCE_TYPE}"
    )
    existing = {
        item.get("name"): item
        for item in listing.get("items", [])
        if isinstance(item, dict)
    }
    desired = provider_payload(args.provider)

    if args.provider not in existing:
        if args.dry_run:
            print(json.dumps({"action": "create", "resource": desired}, indent=2))
            return 0
        result = gateway.post_json(
            f"/data/api/v1/resources/{RESOURCE_TYPE}",
            [desired],
        )
        if result.get("problem") or result.get("success") is False:
            raise RuntimeError(f"failed to create historian: {result}")
        action = "created"
    else:
        resource = gateway.get_json(find_path)
        config = resource.get("config", {})
        settings = config.get("settings", {}) if isinstance(config, dict) else {}
        if (
            config.get("profile", {}).get("type") != "CoreHistorian"
            or settings.get("dataDeduplication") is not True
        ):
            raise RuntimeError(
                f"existing provider {args.provider!r} is not the expected "
                "deduplicating Core Historian; refusing to overwrite it"
            )
        action = "already-present"

    resource: dict[str, Any] = {}
    for _attempt in range(30):
        resource = gateway.get_json(find_path)
        status = (
            resource.get("healthchecks", {})
            .get("status", {})
            .get("result", {})
        )
        if status.get("healthy") is True:
            break
        time.sleep(1)
    else:
        raise RuntimeError(
            f"historian did not become healthy: {compact_health(resource)}"
        )

    print(
        json.dumps(
            {
                "action": action,
                "name": resource.get("name"),
                "type": resource.get("config", {}).get("profile", {}).get("type"),
                "deduplication": resource.get("config", {})
                .get("settings", {})
                .get("dataDeduplication"),
                "health": compact_health(resource),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (requests.RequestException, RuntimeError, KeyError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
