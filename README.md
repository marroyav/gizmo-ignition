# GIZMo Ignition project

This repository contains the publication-safe Ignition 8.3 resources and
migration tooling for two GIZMo implementations:

- `[default]GIZMo/Kria`, using the `GIZMo Kria` OPC UA connection; and
- `[default]GIZMo/Legacy`, using the `GIZMo Legacy` native OPC UA server
  connection.

Both devices use the canonical `urn:fnal:gizmo` namespace. The committed
project contains 431 tags per device, a Perspective overview for each device,
a tag inventory, tag groups, history migration tools, and deterministic
resource-generation inputs.

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
| `GIZMo Legacy` | site-assigned legacy server, TCP 4842; test Gateway uses `opc.tcp://127.0.0.1:48454` | authenticated, capability-bounded |

The Kria tree enables writes only for `Configuration/ThresholdOhm` and
`Configuration/AveragesPerCalculation`. The legacy tree enables writes only
for `Configuration/ThresholdOhm`, with the recovered 0--1023-ohm hardware
range. All measurement/readback tags remain read-only. The legacy connection
must use the target-generated control credential stored in the Gateway's
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

The native legacy OPC UA server, target-local recovery buffer, and automatic
time service are deployed on the ZedBoard. Its authenticated 100-ohm
idempotent threshold write passed persistent-file, controller-word, recovery-
journal, and display-transaction readback. Production acceptance still
requires the 100-cycle physical-display comparison, authoritative alarm-return
readback, and approval of the isolated-network credential policy described by
the ICD. Other legacy writes and all legacy methods remain unsupported.

## Rebuild and test

Rebuild the committed dual-device tree from the sanitized single-device source:

```sh
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

## Publication boundary

No production endpoint, credential, private key, Gateway security object,
database snapshot, or operational history is committed. This repository does
not grant a blanket license for GIZMo-specific material; ownership and release
terms must be established by the relevant project maintainers before reuse or
redistribution beyond viewing this public repository.
