#!/usr/bin/env python3
"""Safely create an Ignition PostgreSQL connection and SQL Historian provider.

The default action is a redacted plan.  ``--apply`` requires both passwords in
environment variables and never replaces an existing Gateway resource.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any
from urllib.parse import quote, urlencode

import requests

from configure_history import GatewaySession, compact_health


DATABASE_RESOURCE_TYPE = "ignition/database-connection"
HISTORIAN_RESOURCE_TYPE = "com.inductiveautomation.historian/historian-provider"
SSL_MODES = ("require", "verify-ca", "verify-full")
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]+$")


def validate_host(host: str) -> str:
    value = host.strip()
    if (
        not value
        or "://" in value
        or any(character in value for character in "/?#@")
        or any(character.isspace() for character in value)
    ):
        raise ValueError("PostgreSQL host must be a DNS name or IP address")
    if value.startswith("[") and value.endswith("]"):
        return value
    if ":" in value:
        return f"[{value}]"
    return value


def validate_identifier(value: str, label: str) -> str:
    candidate = value.strip()
    if not SAFE_IDENTIFIER.fullmatch(candidate):
        raise ValueError(
            f"{label} may contain only letters, digits, underscore, dot, and dash"
        )
    return candidate


def jdbc_url(
    host: str,
    port: int,
    database: str,
    sslmode: str,
    ssl_root_cert: str = "",
) -> str:
    if not 1 <= port <= 65535:
        raise ValueError("PostgreSQL port must be between 1 and 65535")
    if sslmode not in SSL_MODES:
        raise ValueError(f"unsupported PostgreSQL sslmode: {sslmode}")
    hostname = validate_host(host)
    database_name = validate_identifier(database, "database name")
    parameters = [
        ("sslmode", sslmode),
        ("ApplicationName", "GIZMo-Ignition"),
    ]
    if ssl_root_cert:
        if not ssl_root_cert.startswith("/"):
            raise ValueError("TLS root certificate path must be absolute")
        parameters.append(("sslrootcert", ssl_root_cert))
    return (
        f"jdbc:postgresql://{hostname}:{port}/{quote(database_name, safe='')}?"
        f"{urlencode(parameters)}"
    )


def database_connection_payload(
    *,
    name: str,
    connect_url: str,
    username: str,
    password: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": (
            "Managed PostgreSQL connection dedicated to the GIZMo Ignition "
            "SQL Historian."
        ),
        "enabled": True,
        "config": {
            "driver": "PostgreSQL",
            "translator": "POSTGRES",
            "includeSchemaInTableName": False,
            "connectURL": connect_url,
            "username": username,
            "password": password,
            "connectionProps": "",
            "connectionResetParams": "",
            "defaultTransactionLevel": "DEFAULT",
            "poolInitSize": 0,
            "poolMaxActive": 8,
            "poolMaxIdle": 8,
            "poolMinIdle": 0,
            "poolMaxWait": 5000,
            "validationQuery": "SELECT 1",
            "testOnBorrow": True,
            "testOnReturn": False,
            "testWhileIdle": False,
            "evictionRate": -1,
            "evictionTests": 3,
            "evictionTime": 1_800_000,
            "failoverProfile": "",
            "failoverMode": "STANDARD",
            "slowQueryLogThreshold": 60_000,
            "validationSleepTime": 10_000,
        },
    }


def sql_historian_payload(name: str, database_connection: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": (
            "Permanent GIZMo SQL Historian backed by managed PostgreSQL; "
            "monthly partitions and no automatic pruning."
        ),
        "enabled": True,
        "config": {
            "profile": {"type": "SqlHistorian"},
            "settings": {
                "database": database_connection,
                "partition": {
                    "enabled": True,
                    "size": 1,
                    "sizeUnits": "MONTH",
                    "partitionSeedQueryLimit": 2,
                    "optimized": False,
                    "optimizedWindowSeconds": 60,
                },
                "pruning": {
                    "enabled": False,
                    "age": 6,
                    "ageUnits": "YEAR",
                },
                "trackSce": True,
                "staleMultiplier": 2,
            },
        },
    }


def historian_splitter_payload(
    name: str, primary_historian: str, secondary_historian: str
) -> dict[str, Any]:
    if len({name, primary_historian, secondary_historian}) != 3:
        raise ValueError("splitter and child historian names must be distinct")
    return {
        "name": name,
        "description": (
            "GIZMo dual-writer historian. Queries use "
            f"{primary_historian}; every new sample is also sent to "
            f"{secondary_historian}."
        ),
        "enabled": True,
        "config": {
            "profile": {"type": "HistorySplitter"},
            "settings": {
                "primaryHistorian": primary_historian,
                "secondaryHistorian": secondary_historian,
                "queryLimit": {
                    "enabled": False,
                    "size": 1,
                    "units": "MONTH",
                },
            },
        },
    }


def find_path(resource_type: str, name: str) -> str:
    return f"/data/api/v1/resources/find/{resource_type}/{quote(name, safe='')}"


def list_resources(
    gateway: GatewaySession, resource_type: str
) -> dict[str, dict[str, Any]]:
    listing = gateway.get_json(f"/data/api/v1/resources/list/{resource_type}")
    return {
        str(item.get("name")): item
        for item in listing.get("items", [])
        if isinstance(item, dict) and item.get("name")
    }


def create_resource(
    gateway: GatewaySession, resource_type: str, payload: dict[str, Any]
) -> None:
    result = gateway.post_json(
        f"/data/api/v1/resources/{resource_type}", [payload]
    )
    if result.get("problem") or result.get("success") is False:
        raise RuntimeError(
            f"Ignition rejected creation of {payload['name']!r}: {result}"
        )


def wait_healthy(
    gateway: GatewaySession,
    resource_type: str,
    name: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    resource: dict[str, Any] = {}
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        resource = gateway.get_json(find_path(resource_type, name))
        status = (
            resource.get("healthchecks", {}).get("status", {}).get("result", {})
        )
        if status.get("healthy") is True:
            return resource
        time.sleep(1)
    raise RuntimeError(
        f"resource {name!r} did not become healthy: {compact_health(resource)}"
    )


def verify_database_connection(
    resource: dict[str, Any], expected_url: str, expected_username: str
) -> None:
    config = resource.get("config", {})
    checks = {
        "driver": "PostgreSQL",
        "translator": "POSTGRES",
        "connectURL": expected_url,
        "username": expected_username,
    }
    differences = {
        key: {"expected": expected, "actual": config.get(key)}
        for key, expected in checks.items()
        if config.get(key) != expected
    }
    if differences:
        raise RuntimeError(
            "existing database connection differs from the requested non-secret "
            f"configuration; refusing to overwrite it: {differences}"
        )


def verify_sql_historian(
    resource: dict[str, Any], database_connection: str
) -> None:
    config = resource.get("config", {})
    settings = config.get("settings", {}) if isinstance(config, dict) else {}
    if (
        config.get("profile", {}).get("type") != "SqlHistorian"
        or settings.get("database") != database_connection
        or settings.get("pruning", {}).get("enabled") is not False
    ):
        raise RuntimeError(
            "existing historian is not the requested unpruned PostgreSQL SQL "
            "Historian; refusing to overwrite it"
        )


def verify_history_splitter(
    resource: dict[str, Any], primary_historian: str, secondary_historian: str
) -> None:
    config = resource.get("config", {})
    settings = config.get("settings", {}) if isinstance(config, dict) else {}
    if (
        config.get("profile", {}).get("type") != "HistorySplitter"
        or settings.get("primaryHistorian") != primary_historian
        or settings.get("secondaryHistorian") != secondary_historian
        or settings.get("queryLimit", {}).get("enabled") is not False
    ):
        raise RuntimeError(
            "existing splitter does not have the requested ordered historian "
            "pair; refusing to overwrite it"
        )


def redacted_plan(
    database_payload: dict[str, Any],
    historian_payload: dict[str, Any],
    splitter_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    safe_database = json.loads(json.dumps(database_payload))
    safe_database["config"]["password"] = "<GIZMO_POSTGRES_PASSWORD>"
    changes = [
        {
            "resource_type": DATABASE_RESOURCE_TYPE,
            "resource": safe_database,
        },
        {
            "resource_type": HISTORIAN_RESOURCE_TYPE,
            "resource": historian_payload,
        },
    ]
    if splitter_payload is not None:
        changes.append(
            {
                "resource_type": HISTORIAN_RESOURCE_TYPE,
                "resource": splitter_payload,
            }
        )
    return {
        "action": "plan",
        "changes": changes,
        "not_changed": [
            "GIZMo History Core Historian",
            "live tag history providers",
            "validated Core backfill data",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--database", default="gizmo_history")
    parser.add_argument("--db-user", default="gizmo_ignition")
    parser.add_argument(
        "--database-connection", default="GIZMo PostgreSQL"
    )
    parser.add_argument(
        "--historian-provider", default="GIZMo PostgreSQL History"
    )
    parser.add_argument("--core-provider", default="GIZMo History")
    parser.add_argument("--splitter-provider", default="GIZMo Dual History")
    parser.add_argument(
        "--splitter-primary",
        choices=("core", "postgresql"),
        default="core",
        help="query-side provider of the optional dual writer",
    )
    parser.add_argument(
        "--without-splitter",
        action="store_true",
        help="omit the migration dual-writer provider",
    )
    parser.add_argument("--sslmode", choices=SSL_MODES, default="verify-full")
    parser.add_argument("--ssl-root-cert", default="")
    parser.add_argument(
        "--gateway-url",
        default=os.environ.get("IGNITION_URL", "http://127.0.0.1:18088"),
    )
    parser.add_argument(
        "--ignition-user",
        default=os.environ.get("IGNITION_USERNAME", "admin"),
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--health-timeout", type=int, default=60)
    args = parser.parse_args()

    if not 1 <= args.health_timeout <= 600:
        parser.error("--health-timeout must be between 1 and 600 seconds")
    try:
        username = validate_identifier(args.db_user, "database user")
        url = jdbc_url(
            args.host,
            args.port,
            args.database,
            args.sslmode,
            args.ssl_root_cert,
        )
    except ValueError as error:
        parser.error(str(error))

    database_password = os.environ.get("GIZMO_POSTGRES_PASSWORD", "")
    database_payload = database_connection_payload(
        name=args.database_connection,
        connect_url=url,
        username=username,
        password=database_password,
    )
    historian_payload = sql_historian_payload(
        args.historian_provider, args.database_connection
    )
    try:
        splitter_primary = (
            args.core_provider
            if args.splitter_primary == "core"
            else args.historian_provider
        )
        splitter_secondary = (
            args.historian_provider
            if args.splitter_primary == "core"
            else args.core_provider
        )
        splitter_payload = (
            None
            if args.without_splitter
            else historian_splitter_payload(
                args.splitter_provider,
                splitter_primary,
                splitter_secondary,
            )
        )
    except ValueError as error:
        parser.error(str(error))

    if not args.apply:
        print(
            json.dumps(
                redacted_plan(
                    database_payload, historian_payload, splitter_payload
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    ignition_password = os.environ.get("IGNITION_PASSWORD", "")
    if not ignition_password:
        parser.error("set IGNITION_PASSWORD in the environment for --apply")
    if not database_password:
        parser.error("set GIZMO_POSTGRES_PASSWORD in the environment for --apply")

    gateway = GatewaySession(
        args.gateway_url, args.ignition_user, ignition_password
    )
    database_resources = list_resources(gateway, DATABASE_RESOURCE_TYPE)
    if args.database_connection in database_resources:
        database_resource = gateway.get_json(
            find_path(DATABASE_RESOURCE_TYPE, args.database_connection)
        )
        verify_database_connection(database_resource, url, username)
        database_action = "already-present"
    else:
        create_resource(gateway, DATABASE_RESOURCE_TYPE, database_payload)
        database_action = "created"

    # Do not create a historian over a faulted database connection.  Ignition
    # retains a newly-created connection for diagnosis, but this tool never
    # rewrites or removes resources automatically.
    database_resource = wait_healthy(
        gateway,
        DATABASE_RESOURCE_TYPE,
        args.database_connection,
        args.health_timeout,
    )

    historian_resources = list_resources(gateway, HISTORIAN_RESOURCE_TYPE)
    if args.historian_provider in historian_resources:
        historian_resource = gateway.get_json(
            find_path(HISTORIAN_RESOURCE_TYPE, args.historian_provider)
        )
        verify_sql_historian(historian_resource, args.database_connection)
        historian_action = "already-present"
    else:
        create_resource(
            gateway, HISTORIAN_RESOURCE_TYPE, historian_payload
        )
        historian_action = "created"
    historian_resource = wait_healthy(
        gateway,
        HISTORIAN_RESOURCE_TYPE,
        args.historian_provider,
        args.health_timeout,
    )

    splitter_action: str | None = None
    splitter_resource: dict[str, Any] | None = None
    if splitter_payload is not None:
        historian_resources = list_resources(gateway, HISTORIAN_RESOURCE_TYPE)
        if args.core_provider not in historian_resources:
            raise RuntimeError(
                f"Core historian {args.core_provider!r} is missing; refusing "
                "to create the migration splitter"
            )
        wait_healthy(
            gateway,
            HISTORIAN_RESOURCE_TYPE,
            args.core_provider,
            args.health_timeout,
        )
        if args.splitter_provider in historian_resources:
            splitter_resource = gateway.get_json(
                find_path(HISTORIAN_RESOURCE_TYPE, args.splitter_provider)
            )
            verify_history_splitter(
                splitter_resource,
                splitter_primary,
                splitter_secondary,
            )
            splitter_action = "already-present"
        else:
            create_resource(
                gateway, HISTORIAN_RESOURCE_TYPE, splitter_payload
            )
            splitter_action = "created"
        splitter_resource = wait_healthy(
            gateway,
            HISTORIAN_RESOURCE_TYPE,
            args.splitter_provider,
            args.health_timeout,
        )

    outcome: dict[str, Any] = {
        "database_connection": {
            "action": database_action,
            "name": args.database_connection,
            "health": compact_health(database_resource),
        },
        "historian_provider": {
            "action": historian_action,
            "name": args.historian_provider,
            "health": compact_health(historian_resource),
            "pruning_enabled": False,
        },
        "core_historian_changed": False,
        "tag_history_providers_changed": False,
    }
    if splitter_resource is not None:
        outcome["migration_splitter"] = {
            "action": splitter_action,
            "name": args.splitter_provider,
            "primary": splitter_primary,
            "secondary": splitter_secondary,
            "health": compact_health(splitter_resource),
            "tag_assignments_changed": False,
        }

    print(
        json.dumps(
            outcome,
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
