# GIZMo Ignition project

This repository contains the publication-safe Ignition 8.3 resources and
migration tooling for two GIZMo implementations:

- `[default]GIZMo/Kria`, using the `GIZMo Kria` OPC UA connection; and
- `[default]GIZMo/Legacy`, using the `GIZMo Legacy` native OPC UA server
  connection.

Both devices use the canonical `urn:fnal:gizmo` namespace. The Kria
implementation is the sole authority for this GIZMo--Slow Controls contract;
the ZedBoard is a separate conforming producer at a distinct endpoint. The
committed project contains 472 tags per device, a Perspective overview for
each device, a tag inventory, tag groups, history migration tools, and
deterministic resource-generation inputs.

Model 1.4.0 is pinned by the contract hash recorded in the project manifest.
It adds command-audit and gate state, calibration/restoration state, narrow
recovery methods, and the reserved stimulus-current monitor.
The normative machine-readable artifact is
[`schema/gizmo-opcua-contract.json`](https://github.com/marroyav/GIZMo/blob/main/schema/gizmo-opcua-contract.json)
in the Kria producer repository. An exact digest-pinned snapshot is committed
locally at [`schema/gizmo-opcua-contract.json`](schema/gizmo-opcua-contract.json),
and the single-device resources are generated directly from that artifact.

The public-review interface and acceptance gates are maintained in
[`marroyav/gizmo-icd`](https://github.com/marroyav/gizmo-icd); the Kria
producer is maintained in [`marroyav/GIZMo`](https://github.com/marroyav/GIZMo).

## Import

The installable resource tree is under `ignition-project/`. Merge it into an
Ignition Gateway only through the normal Gateway project/configuration import
workflow and only after taking a Gateway backup.

Create the following OPC UA connections on the target Gateway:

| Connection name | Endpoint | Mode |
|---|---|---|
| `GIZMo Kria` | site-assigned | writable configuration connection |
| `GIZMo Legacy` | separate site-assigned ZedBoard endpoint | authenticated, capability-bounded |

The Kria tree enables writes only for `Configuration/ThresholdOhm` and
`Configuration/AveragesPerCalculation`. The legacy tree enables writes only
for `Configuration/ThresholdOhm`. Both platforms use the canonical
0--1,000,000-ohm threshold metadata. The ZedBoard server accepts the narrower
0--1023-ohm hardware subset and returns `BadOutOfRange` above it; that does not
change the contract. All measurement/readback tags remain read-only.
The legacy connection must use the target-generated control credential stored in the Gateway's
protected credential store; its anonymous session remains useful for reads but
cannot write. Limit write permission to the approved Ignition operator role.

Connection resources are intentionally absent. Even a connection using
`SecurityPolicy=None` can contain a Gateway-bound encrypted key-store secret.
Endpoints, certificates, credentials, database connections, user sources, and
Gateway backups must remain in the site configuration—not in this repository.

The Perspective routes are `/kria`, `/legacy`, and `/tags`. The root route
opens the Kria view. Each device overview includes protected numeric inputs
for exactly the writable tags listed above plus read-only
`Configuration/LastCommandResult` feedback. A value is committed when the
operator presses Enter or leaves the field. The legacy `Alarm.Active` tag is
imported without an Ignition alarm definition until commissioning validates
the server's authoritative alarm readback.

The two OPC UA connections are independent. The Ignition project does not use
one device as a fallback, proxy, or control path for the other, and neither
connection starts, stops, or configures the other server. Both trees retain
the same canonical NodeIds; device identity and connection name select the
producer.

`HIGH Z` is a valid good-quality range state. Both tag trees display
`ResistanceRange=OutOfRange` as `HIGH Z`, keep `ResistanceOhm` non-numeric,
and retain `Good` status; neither tree fabricates a 500- or 999-ohm sample.
Historical model-1.3.0 backfills preserve the status code actually captured in
those older records rather than silently rewriting history.

The native legacy OPC UA server, target-local recovery buffer, and automatic
time service are under development for the ZedBoard. Earlier bench work
exercised an authenticated 100-ohm idempotent threshold transaction through
persistent-file, controller-word, recovery-journal, and display-transaction
readback. Production acceptance still
requires the 100-cycle physical-display comparison, authoritative alarm-return
readback, and approval of the isolated-network credential policy described by
the ICD. Other legacy writes and all legacy methods remain unsupported.

## Rebuild and test

Rebuild the committed dual-device tree from the sanitized single-device source:

```sh
python3 tools/generate_draft.py \
  --schema schema/gizmo-opcua-contract.json \
  --output source/single-device \
  --omit-connection --force
python3 tools/build_dual_project.py --force
python3 -m unittest discover -s tests -v
```

`tools/generate_draft.py` is retained for regenerating a single-device source
tree from a canonical OPC UA schema. It can emit an OPC connection resource;
that output is excluded by `.gitignore` and must be handled as site-private.

The SQLite-to-Ignition scripts verify immutable inputs, checksums, SQLite
integrity, resumable batches, historian boundary reads, and redacted database
configuration plans. Real SQLite archives and generated batches are not
included.

PostgreSQL is supported as the off-board long-term historian. The configuration
tool encrypts the database credential with the destination Gateway's key; the
backfill uses SQL Historian `drv` paths and can require exact readback before
each batch is checkpointed. Database endpoints, certificates, credentials,
backups, and production ownership remain site configuration.

## Publication boundary

No production endpoint, credential, private key, Gateway security object,
database snapshot, or operational history is committed. This repository does
not grant a blanket license for GIZMo-specific material; ownership and release
terms must be established by the relevant project maintainers before reuse or
redistribution beyond viewing this public repository.
