# Health Profile Consultation Workflow

Operational guide for reviewing health and nutrition records before confirming
write-back to Health Connect.  Covers sources, freshness, consent, phone
verification, and privacy flags.

---

## Overview

The consultation tool (`consultation_read.py`) reads two local SQLite databases
in read-only mode and emits a JSON summary:

| Output section | Source database | Contents |
|---|---|---|
| `inventory` | `health.sqlite3` | Per-(record_type, source) record counts and age fields |
| `date_nutrition_ingest` | `health.sqlite3` | Raw nutrition records received from Health Connect for the query date |
| `date_exercise` | `health.sqlite3` | Raw exercise records received for the query date |
| `queue_by_state` | `nutrition.sqlite3` | AI-estimated meal entries grouped by state; each entry includes a `correlation` field |

**Ingest and queue are never summed.**  Records in `date_nutrition_ingest`
were already read from Health Connect.  Records in `queue_by_state` represent
meal entries at various stages of the write-back pipeline — `draft` and
`confirmed` entries are pending delivery; `acked` and `cancelled` entries are
terminal states.  Never merge or compare these two data sources numerically.

---

## Running the tool

```bash
python -m health-connect-bridge.consultation_read \
    --date 2026-10-11        # optional; defaults to today in Europe/Berlin
```

Or as a script:

```bash
python services/health-connect-bridge/consultation_read.py --date 2026-10-11
```

Defaults:
- `--health-db`: `/home/claw/.hermes/profiles/health/workspace/health-connect-bridge/data/health.sqlite3`
- `--nutrition-db`: `/home/claw/.hermes/profiles/health/workspace/health-connect-bridge/data/nutrition.sqlite3`
- `--date`: today in Europe/Berlin (CET/CEST)

The tool exits with status 0 on success.  Individual database errors are
captured in the `errors` key without aborting the whole run.

---

## Data sources and freshness

### Health Connect ingest (`health.sqlite3`)

Records arrive via the bridge receiver when the Android app calls
`POST /ingest/health-connect`.  The `received_at` column records when the
bridge received the payload; `record_start_s` / `record_end_s` record when
the health event occurred.

Each inventory entry includes two age fields:

- **`latest_record_age_hours`**: hours since `MAX(record_end_s)` — how old the
  most recent health data actually is.
- **`transport_age_hours`**: hours since `MAX(received_at)` — how recently the
  bridge last received data for this source.

These can diverge: a run recorded weeks ago but synced today will show a large
`latest_record_age_hours` and a small `transport_age_hours`.  Neither field alone
is sufficient to judge data quality; interpret both together.

**Illustrative source types** (not an exhaustive or audited list; actual
source keys in your database may differ):

| Origin pattern | Typical data |
|---|---|
| `com.google.android.apps.fitness` (Google Fit / Pixel Watch via Google Fit) | Steps, calories, activity |
| `com.google.android.wearable.healthservices` (Pixel Watch wearable services) | On-device sensor data |
| `com.whoop.android` (WHOOP app HC export) | HRV, resting HR, sleep stages exported to HC |
| Exercise tracking apps (e.g. Hevy) | Workout sessions |
| `{"_no_metadata":true}` | Origin unknown; cannot be attributed to a device |

The table above is illustrative.  Run the tool and inspect `inventory` to see
the actual source keys present in your database.  No production source keys or
biometric values are recorded here.

**WHOOP data in Health Connect**: WHOOP exports data to Health Connect via its
Android app.  The bridge ingests these exports alongside all other HC sources.
WHOOP also provides independent API access and may be integrated separately
(e.g. via WHOOP API / Biohub pipelines).  Health Connect exports from WHOOP
may lag live cloud data.  These independent integrations are preserved and
operate separately from this bridge; do not assume this bridge's `health.sqlite3`
contains all WHOOP data.

**Hevy**: exports exercise sessions to Health Connect without set-level detail.
The bridge receives exercise records with session type, start/end times, and
distance where applicable.  Individual sets, reps, and weights are absent from
HC exports.  Refer to the Hevy app directly for set-level data.

**Pixel Watch provenance**: The tool flags records from two Pixel Watch
source patterns:
1. Origin `com.google.android.wearable.healthservices` (exclusive to Pixel Watch).
2. Origin `com.google.android.apps.fitness` **combined with** a device model
   containing "Pixel Watch".

Generic English exercise or record titles are not sufficient evidence to
determine whether records represent real activity or data loaded during device
setup.  External verification — cross-referencing timestamps with known
activity, or checking the Pixel Watch app's own history — is required.
The tool notes this ambiguity without declaring records real or fake.

### Nutrition queue states (`nutrition.sqlite3`)

AI-estimated meal entries created by the skill.  Each entry progresses through
a state machine:

```
draft → confirmed → acked
  ↓        ↓
cancelled  cancelled
```

| State | Meaning |
|---|---|
| `draft` | AI estimate created; not yet reviewed by user |
| `confirmed` | User consent recorded; entry queued and pending phone delivery |
| `acked` | Phone client reported successful HC delivery after insert attempt |
| `cancelled` | Entry withdrawn; will not be delivered |

**`acked` is client-reported**: the phone calls `POST /nutrition/ack` after
it has attempted to write the record to Health Connect.  This is
**client-reported** and is **not independently verified** — the client could
report success without the write completing.  Independent phone verification
is always required (see below).

**`acked` and `cancelled` are terminal**: do not describe them as "waiting
for delivery".  Only `draft` and `confirmed` entries are pending.

Each queue entry includes a `correlation` field.  Correlation requires an
exact `client_record_id` AND `client_record_version` match in `health_records`
(stored in `expl_id` with `expl_ns='client'`, or in `record_json` metadata
when a server-assigned HC ID takes precedence).  In the current JSON payload
format neither ID is present, so `correlation.status` will be `not_verified`
for all current entries.  Identity match and content match are reported
separately and must not be conflated.

---

## Consent and confirmation workflow

### User consent — what the user provides

The user provides ONLY the exact Russian consent statement (or the
supported exact English equivalent), with `{name}`, `{id}`, and `{version}`
substituted from the actual record being confirmed:

> Я, Равиль, подтверждаю запись Health Connect для {name}, ID {id}, версия {version}, с показанным временем и макросами.

This text is passed **unchanged** to the installed skill
(`nutrition_writeback.py`).  Do not paraphrase, abbreviate, or translate it.

**The technical affirm phrase ("I confirm this meal for Health Connect") is
internal to the skill wrapper and is NEVER supplied by the user directly.**
Never call `nutrition_cli.py confirm` directly to bypass the wrapper.
The wrapper validates consent, invokes the technical affirm internally, and
enforces production privacy flags.

### Installed wrapper command

The installed skill (`nutrition_writeback.py`) requires explicit production
privacy gate flags.  The full command template for a confirm subcommand is:

```bash
python /home/claw/.hermes/hermes-agent/homelab/services/health-connect-bridge/agent-workflow/nutrition_writeback.py \
    --privacy-verifier /home/claw/.hermes/hermes-agent/homelab/services/health-connect-bridge/privacy_gate_check.py \
    --privacy-source /home/claw/.hermes/hermes-agent/homelab/komodo/stacks/traefik/compose.yaml \
    --live-host 192.168.1.166 \
    --ssh-user claw \
    --receiver-url https://health-connect.ravil.space \
    confirm \
    --id <client_record_id> \
    --version <client_record_version> \
    --consent "<exact Russian or English consent text>"
```

All flags are mandatory for production.  The `--privacy-verifier` and
`--privacy-source` flags are explicit production gates; omitting them causes
the wrapper to refuse to proceed.

### Complete workflow: photo to Health Connect

The full order for a new meal entry is:

1. **Food photo** — take a photo of the meal at the time of eating.

2. **Composition and portion estimation** — describe ingredients, portion
   sizes, and preparation method.  Note any uncertainty (unknown oil type,
   sauces, beverages with unconfirmed volumes, etc.).  Clarify before
   creating a draft entry.

3. **Time and timezone** — confirm the exact local time and timezone
   (Europe/Berlin).  Agree on the start/end interval before drafting.

4. **Create draft** — invoke the skill to create a draft entry with the
   agreed name, time, and AI-estimated macros.

5. **Status readback** — run `consultation_read.py --date <date>` and read
   back the exact draft record: name, `client_record_id`, `client_record_version`,
   start/end times, and macros.  Confirm these match intent.

6. **Preview consent** — display the consent statement with `{name}`, `{id}`,
   and `{version}` substituted from the actual draft values:
   > Я, Равиль, подтверждаю запись Health Connect для {name}, ID {id}, версия {version}, с показанным временем и макросами.

7. **Re-read draft status** — run `consultation_read.py` again immediately
   before confirming to verify the record is still in `draft` state with the
   same version.  Another process may have edited or cancelled it.

8. **User provides consent** — the user reads the full consent statement aloud
   and passes it unchanged to the wrapper.

9. **Confirm via wrapper** — invoke the installed `nutrition_writeback.py`
   confirm command (see above).  The wrapper validates consent and calls the
   technical affirm internally.

10. **Status ACK readback** — run `consultation_read.py` and verify the entry
    has transitioned to `confirmed`, then `acked` after phone delivery.

11. **HC export correlation** — once `acked`, check `correlation.status` in the
    tool output.  In the current format this will be `not_verified`.  If a
    future format supports explicit IDs, verify `identity_match` and
    `content_match`.

12. **Google Health display** — independently verify the entry appears in the
    Google Health or Health Connect UI on the phone with the correct time and
    macros.  This step is separate from the bridge and cannot be automated.

---

## Phone verification protocol

Phone verification confirms that a queued record was actually written to
Health Connect, independent of the bridge `acked` state.

**General approach**:
- Open Health Connect (or a health app that reads from it) on the phone.
- Navigate to Nutrition for the relevant date.
- Confirm the entry name, time window, and macros match what was confirmed.

**Background / offline considerations**:
- The Health Connect app may take several minutes to display newly written
  records.  If the entry does not appear immediately, wait and retry.
- If the phone was offline when the bridge served the queue, the Android app
  may have cached the record locally.  The write to Health Connect occurs when
  the app processes it; verify after the phone comes back online.
- **Retry behaviour**: the bridge continues serving confirmed (not-yet-acked)
  records on each `GET /nutrition/queue` call.  If the app did not write
  the record (e.g. it was backgrounded before the Health Connect insert),
  the record will reappear on the next fetch and the app will retry.
- **ACK is client-reported**: the `acked` state does not prove the write
  succeeded.  Always verify in Health Connect UI regardless of queue state.

**Evidence scope**: server-side and unit tests (including the consultation
tool's own test suite) establish behaviour of the bridge server, the queue
state machine, and the read-only tool.  They do **not** establish that
background writes succeed, that offline replay is duplicate-free, or that
the real Android app correctly reads and writes on a physical phone.
Server/unit evidence and real-phone acceptance evidence must be stated
separately.

**No new production test meals**: do not create new meals in the queue solely
to test the delivery pipeline.  Test entries in Health Connect cannot easily
be distinguished from real data and will persist in the Health Connect history.

---

## Production privacy flags

The following flags apply to all production runs.

| Flag | Requirement |
|---|---|
| **Read-only** | The tool opens databases with `mode=ro` and `PRAGMA query_only=ON`.  No writes occur. |
| **Wrapper required** | Always use the installed `nutrition_writeback.py` wrapper with explicit `--privacy-verifier` and `--privacy-source` flags.  Never bypass it by calling `nutrition_cli.py` directly. |
| **No PHI in output files** | Do not redirect output to files stored in shared or version-controlled locations.  Output contains health record contents. |
| **No production identifiers in repository** | Do not commit real `client_record_id` values, meal names, HRV readings, or any production record content to the git repository. |
| **No test meals in production** | Never create entries in the production nutrition queue for testing purposes. |
| **Symlink rejection** | The tool rejects symlinked DB paths and ancestor directories.  Do not create symlinks to the production databases. |
| **DB path verification** | Before running, confirm `--health-db` and `--nutrition-db` point to the intended databases.  The tool reports the resolved paths in its JSON output. |
| **Draft readback before confirm** | Always re-read the draft state immediately before confirming.  Version mismatches raise a `VersionConflict` error. |

---

## Operational checklist

```
[ ] Run consultation_read.py --date <date> and review inventory age fields
[ ] Check provenance_notes for any source ambiguity warnings
[ ] Review date_exercise for exercise sessions from expected sources
[ ] Review date_nutrition_ingest for HC-received nutrition records (if any)
[ ] Review queue_by_state.draft for pending AI estimates
[ ] For each draft to confirm:
    [ ] Read exact draft values: name, ID, version, times, macros
    [ ] Display full consent statement with actual name/ID/version substituted
    [ ] Re-read draft status immediately before confirming (same version?)
    [ ] User provides exact consent text unchanged
    [ ] Run nutrition_writeback.py wrapper with all production privacy flags
    [ ] Run consultation_read.py and confirm state transitions to confirmed/acked
    [ ] Verify independently on phone in Health Connect UI
    [ ] Note: server/unit tests do not establish real-phone acceptance
[ ] Confirm no cross-source sums or numeric totals have been computed
[ ] Confirm no production identifiers have been captured in notes or files
```
