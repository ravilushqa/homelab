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
| `inventory` | `health.sqlite3` | Per-(record_type, source) record counts and freshness |
| `date_nutrition_ingest` | `health.sqlite3` | Raw nutrition records received from Health Connect for the query date |
| `date_exercise` | `health.sqlite3` | Raw exercise records received for the query date |
| `queue_by_state` | `nutrition.sqlite3` | AI-estimated meal entries staged for write-back, grouped by state; each entry includes a `correlation` field |

**These two data sources are never summed.**  A record appearing in
`date_nutrition_ingest` was already read from Health Connect.  A record in
`queue_by_state` is waiting to be written back.  They represent opposite
directions of data flow and must not be merged or compared numerically.

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
captured in the `errors` key without aborting the whole run, so a partial
result is returned when one database is unavailable.

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

**Known sources** observed in the inventory:

| Origin / device | Notes |
|---|---|
| `com.google.android.apps.fitness` (Google Fit) | Steps, calories, activity; also used by Pixel Watch — see provenance note below |
| `com.samsung.health` (Samsung Health) | Sleep, HR, weight |
| `com.whoop.android` (WHOOP) | HRV, resting HR, sleep stages — retained separately; see below |
| `com.google.android.wearable.healthservices` (Pixel Watch wearable services) | See provenance note below |
| Hevy | Workout sessions; **does not include per-set reps or weights** — Hevy exports only session-level summaries to Health Connect |
| `{"_no_metadata":true}` | Origin unknown; cannot be attributed to a device |

**WHOOP data** is exported from the WHOOP app into Health Connect and ingested
by the bridge as standard health records.  WHOOP records HRV, resting heart
rate, and detailed sleep stages.  These are retained in `health.sqlite3`
alongside all other sources and are not stored in a separate WHOOP-specific
database.

**Hevy** exports exercise sessions to Health Connect without set-level detail.
The bridge receives exercise records with `type`, `start_time`, `end_time`,
and `distance_meters` where applicable, but individual sets, reps, and weights
are absent.  Refer to the Hevy app directly for set-level data.

**Pixel Watch provenance**: The tool flags records from two Pixel Watch
source patterns:
1. Origin `com.google.android.wearable.healthservices` (exclusive to Pixel Watch).
2. Origin `com.google.android.apps.fitness` (shared with other apps) **combined
   with** a device model containing "Pixel Watch".

Records from these sources were first received by this bridge on 2026-10-08.
Generic English exercise or record titles are not sufficient evidence to
determine whether records represent real activity or data loaded during device
setup.  External verification — cross-referencing timestamps with known
activity, or checking the Pixel Watch app's own history — is required before
relying on these records.  The tool notes this ambiguity but does not declare
records real or fake.

### Nutrition queue (`nutrition.sqlite3`)

AI-estimated meal entries created via `nutrition_cli.py create`.  Each entry
progresses through a state machine:

```
draft → confirmed → acked
  ↓        ↓
cancelled  cancelled
```

| State | Meaning |
|---|---|
| `draft` | AI estimate created; not yet reviewed |
| `confirmed` | Confirmed by the user with the affirm phrase; queued for phone delivery |
| `acked` | Phone client reported delivery to Health Connect after its insert attempt |
| `cancelled` | Entry withdrawn; will not be delivered |

**Critical**: `acked` state is set when the phone client calls
`POST /nutrition/ack` after it has attempted to write the record to Health
Connect.  This is **client-reported** and is **not independently verified**.
The client could report success without actually completing the write.
Independent phone verification is always required (see below).

Each queue entry includes a `correlation` field showing whether a matching
record was found in `health_records` using exact `client_record_id` and
`client_record_version`.  In the current JSON payload format, this field
is absent from ingest records, so `correlation.status` will be `not_verified`
for all current entries.  If a future payload format includes explicit IDs,
`correlation.status` will be `identity_match` with a `content_match`
sub-field comparing time and macros.

---

## Consent workflow

### Affirm phrase for confirmation

To confirm a draft entry for Health Connect delivery, the user must supply the
exact affirm phrase:

> **I confirm this meal for Health Connect**

This phrase must be passed unchanged to `nutrition_cli.py confirm`.

### Russian consent statement

Before confirming a record, the full consent statement must be read aloud or
displayed in full, with `{name}`, `{id}`, and `{version}` substituted from
the actual record:

> Я, Равиль, подтверждаю запись Health Connect для {name}, ID {id}, версия {version}, с показанным временем и макросами.

This text must be passed unchanged.  Do not paraphrase, abbreviate, or
translate it for this purpose.

### Workflow: draft → confirmed → acked → phone verification

1. **Read the draft** — run `consultation_read.py --date <date>` and inspect
   `queue_by_state.draft`.  Verify name, start/end times, and macro values
   (energy_kcal, protein_g, carbohydrate_g, fat_g) match the intended meal.

2. **Confirm** — run `nutrition_cli.py confirm` with:
   - The `client_record_id` from the draft
   - The `client_record_version` from the draft
   - The exact affirm phrase above

3. **Read back draft status before confirming** — always run
   `consultation_read.py` again after step 1 and before step 2 to verify the
   record is still in `draft` state.  Another process may have edited or
   cancelled the record since you last read it.

4. **Phone delivery** — after confirmation, the phone client will fetch the
   record on its next `GET /nutrition/queue` call.  The record transitions to
   `acked` after the phone calls `POST /nutrition/ack`.

5. **Independent phone verification** — after `acked`, verify the record
   appears in Health Connect on the phone:
   - Open Health Connect → Nutrition → check the entry is present with the
     correct time and macros.
   - This step is independent of the bridge and cannot be automated here.

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

**No new production test meals**: do not create new meals in the queue solely
to test the delivery pipeline.  Use the existing draft or confirmed entries.
Test entries in Health Connect cannot easily be distinguished from real data
and will persist in the Health Connect history.

---

## Production privacy flags

The following flags apply to all production runs.  Each must be explicitly
acknowledged before running the tool against production data.

| Flag | Requirement |
|---|---|
| **Read-only** | The tool opens databases with `mode=ro` and `PRAGMA query_only=ON`.  No writes occur. |
| **No PHI in output files** | Do not redirect output to files stored in shared or version-controlled locations.  Output contains health record contents. |
| **No production identifiers in repository** | Do not commit real `client_record_id` values, meal names, HRV readings, or any production record content to the git repository. |
| **No test meals in production** | Never create entries in the production nutrition queue for testing purposes. |
| **Symlink rejection** | The tool rejects symlinked DB paths and ancestor directories.  Do not create symlinks to the production databases. |
| **DB path verification** | Before running, confirm `--health-db` and `--nutrition-db` point to the intended databases.  The tool reports the resolved paths in its JSON output. |
| **Draft readback before confirm** | Always re-read the draft state immediately before confirming.  Version mismatches raise a `VersionConflict` error. |

---

## Operational checklist

```
[ ] Run consultation_read.py --date <date> and review inventory freshness
[ ] Check provenance_notes for any source ambiguity warnings
[ ] Review date_exercise for exercise sessions from expected sources
[ ] Review date_nutrition_ingest for HC-received nutrition records (if any)
[ ] Review queue_by_state.draft for pending AI estimates
[ ] For each draft to confirm:
    [ ] Read Russian consent statement with actual name/ID/version substituted
    [ ] Re-read draft status immediately before confirming
    [ ] Run nutrition_cli.py confirm with exact affirm phrase
    [ ] Wait for phone delivery (queue state → acked)
    [ ] Verify independently on phone in Health Connect UI
[ ] Confirm no cross-source sums or numeric totals have been computed
[ ] Confirm no production identifiers have been captured in notes or files
```
