---
name: health-connect
description: Read Android Health Connect data (steps, sleep, heart rate, weight, exercise) from the local SQLite journal. Use for health summaries, daily activity, sleep analysis, workout history, or sync status.
tags:
  - health
  - android
  - health-connect
  - steps
  - sleep
  - heart-rate
  - exercise
  - weight
---

# Health Connect Bridge

## When to use

Use this skill whenever the user asks for data from their Android phone's Health Connect: steps, sleep, heart rate, weight, workouts/exercise, or the sync status/freshness of the bridge.

**Do not** use this skill for WHOOP data (see whoop skill), Google Health / Fitbit data (see google-health-api skill), or invented values.

## Data flow

```
Android HC Webhook app → POST https://health-connect.ravil.space/ingest/health-connect
  → Hermes host (192.168.1.65:9121) → SQLite journal
    → read-only CLI (this skill)
```

The bridge is **receive-only**. The CLI reads from `health.sqlite3` with `PRAGMA query_only=ON`; no mutations are possible.

## CLI commands

Always check status first. If `state` is not `ok`, say so explicitly — do not invent values.

```bash
# Always run this first
/home/claw/.hermes/profiles/health/workspace/health-connect-bridge/bin/health-connect-read status

# Daily summary (today, Europe/Berlin)
/home/claw/.hermes/profiles/health/workspace/health-connect-bridge/bin/health-connect-read day

# Specific date
/home/claw/.hermes/profiles/health/workspace/health-connect-bridge/bin/health-connect-read day YYYY-MM-DD

# Sleep sessions
/home/claw/.hermes/profiles/health/workspace/health-connect-bridge/bin/health-connect-read sleep [YYYY-MM-DD]

# Workout/exercise sessions
/home/claw/.hermes/profiles/health/workspace/health-connect-bridge/bin/health-connect-read workouts [YYYY-MM-DD]
```

All commands output JSON to stdout. Parse with your tool.

## Interpreting the output

### status

| `state` | Meaning |
|---------|---------|
| `not_synced` | Bridge never received data (DB missing or empty ingest log) |
| `empty` | Ingest log exists but 0 health records stored |
| `ok` | Latest health record < 48 h old |
| `stale` | Latest health record >= 48 h old — data may be incomplete |

`state` reflects **data freshness** (age of the most recent health record), not just transport.

Actual JSON fields in the `ok`/`stale` response:
- `transport.last_received_at` — ISO-8601 UTC timestamp of the last HTTP POST received
- `transport.transport_age_hours` — hours since last POST
- `data.total_records` — total stored health records
- `data.latest_record_age_hours` — hours since the newest health record's end time
- `data.per_source` — map of source_key → `{latest_record_utc, record_age_hours, record_count}`

There is no `latest_payload_ts` field in the status output.

### day / per-source data

**Critical**: All metrics are returned **per `source_key`**. Never sum across sources — they may overlap in time (two apps reading the same phone sensor, or two devices).

A source_key is canonical JSON. Examples:
- Normal: `{"device":{"manufacturer":"Google","model":"Pixel 8","type":3},"method":2,"origin":"com.google.android.apps.healthdata"}`
- No metadata: `{"_no_metadata":true}`
- Missing device: `{"device":null,"method":2,"origin":"com.example.app"}`

Do not describe source_key as "containing `unknown`" — missing fields are JSON `null` or `_no_metadata:true`, not a literal string "unknown".

### Heart rate

Two possible shapes per source (may both appear in a mixed payload):
- **sample_summary** (full resolution): `samples` count, `avg_bpm`/`min_bpm`/`max_bpm` computed from raw `bpm` values.
- **bucket_summary** (N-minute resolution): `buckets` count, `avg_of_bucket_avgs` is the unweighted average of bucket means (approximation, not a true session average).

### Sleep

Sessions are identified by `session_end_time`. Duration is in `duration_hours`. Stages list sleep phase breakdown.

### Workouts

Each workout has `type` (Health Connect string), `duration_minutes`, optional `distance_km` and `steps`.

### Steps

`total` is the authoritative step sum for clean disjoint within-day intervals.
`total` is `null` when overlapping or cross-boundary intervals are detected — summing would double-count.
`ambiguity_warning` explains why. `contained_disjoint_subtotal` (if present) is the sub-total of contained intervals only — **not a full-day total**.

## Important constraints

1. **Always check `status` first.** If `state` is `not_synced`, `empty`, or `stale`, say explicitly — do not guess or fill in values.
2. **Never cross-source totals.** The `sources` dict is intentional. State clearly which source(s) you're reading from. Do not sum or average across sources.
3. **Sync window**: The Android app uses a rolling 48-hour window. Data older than 48 hours may not appear unless backfilled.
4. **Offline gaps**: If the phone was offline >48 hours, data for those days is missing.
5. **No automated queries**: Do not set up cron jobs or background processes. Query on-demand only.
6. **Sync interval**: 60 minutes recommended; 15 minutes is the minimum the app permits.
7. **Backfill / history**: Android and device-dependent. Some devices/versions allow backfilling up to ~30 days on first sync; others do not. There is no guarantee of historical data. The app does not run a local HTTP server on the phone.
8. **Production DB is empty until first phone sync.**
9. **HTTPS not live until GitOps PR merged, Komodo traefik stack redeployed (with umami exclusion), and router stack deployed.**
10. **WHOOP**: This skill has no access to WHOOP data and makes no changes to WHOOP configuration.

## Idempotency and deduplication notes

Records are deduplicated by `(record_type, source_key, temporal_key)` for temporal-fallback records, or `(record_type, ns, data_origin, id)` for explicit-ID records (plus shape discriminator for heart_rate samples vs buckets). The same record sent twice is stored once. A corrected value updates in place. Records with explicit IDs from different `data_origin` apps are stored separately even if the IDs are the same.

## Token management

The ingest token is stored at:
```
~/.hermes/profiles/health/workspace/health-connect-bridge/secrets/ingest-token
```

**Never output the token value in a chat or log.** The file has mode 0600. The health agent has no need to read this token — only the Android app configuration requires it. To configure the Android app, the operator reads the file directly on the host.

## Service management

```bash
# Check if receiver is running
systemctl --user status health-connect-bridge

# Local health check (no auth required)
curl http://192.168.1.65:9121/healthz
```

## HTTPS endpoint

`https://health-connect.ravil.space/ingest/health-connect`

This endpoint only accepts POST with a valid `X-Health-Token` header. It is NOT live until the GitOps PR is merged, the Komodo traefik stack is redeployed (activating the umami exclusion), and the router stack is deployed.
