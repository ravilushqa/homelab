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
meal entries in the write-back pipeline.  `draft` entries are unapproved and
are not queued for delivery; only `confirmed` entries are pending phone
delivery.  `acked` and `cancelled` are terminal states.  Never merge or
compare these two data sources numerically.

---

## Running the tool

After the standalone file is deployed (see Deployment below), use the
installed path:

```bash
python3 /home/claw/.hermes/profiles/health/scripts/consultation_read.py \
    --date 2026-10-11        # optional; defaults to today in Europe/Berlin
```

From the repository (pre-deployment / development):

```bash
python3 services/health-connect-bridge/consultation_read.py --date 2026-10-11
```

Defaults:
- `--health-db`: `/home/claw/.hermes/profiles/health/workspace/health-connect-bridge/data/health.sqlite3`
- `--nutrition-db`: `/home/claw/.hermes/profiles/health/workspace/health-connect-bridge/data/nutrition.sqlite3`
- `--date`: today in Europe/Berlin (CET/CEST)

The tool exits with status 0 on success.  Individual database errors are
captured in the `errors` key without aborting the whole run.

### Deployment

`consultation_read.py` is a standalone stdlib-only file (no dependency on the
`health_connect_bridge` package or any third-party library).  It is deployed
as a single file copy separate from `install.sh`, which installs only the
bridge receiver package.

Proposed deployment command (execute after parent verifies PR #767):

```bash
cp /path/to/repo/services/health-connect-bridge/consultation_read.py \
   /home/claw/.hermes/profiles/health/scripts/consultation_read.py
chmod 0700 /home/claw/.hermes/profiles/health/scripts/consultation_read.py
```

Verify after copy:

```bash
python3 /home/claw/.hermes/profiles/health/scripts/consultation_read.py \
    --date "$(python3 -c "from datetime import datetime; from zoneinfo import ZoneInfo; print(datetime.now(ZoneInfo('Europe/Berlin')).strftime('%Y-%m-%d'))")" \
    2>/dev/null | python3 -m json.tool | head -10
```

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

**Pixel Watch provenance**: The tool flags records from two heuristic source
patterns associated with Pixel Watch.  These are **NOT verified product
guarantees** — other devices or apps may share the same origins:
1. Origin `com.google.android.wearable.healthservices` (heuristic: often
   seen with Pixel Watch, but not exclusively guaranteed to be Pixel Watch).
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
| `draft` | AI estimate created; unapproved — NOT pending automatic delivery |
| `confirmed` | User consent recorded; entry queued for phone delivery |
| `acked` | Phone client reported a successful Health Connect insert |
| `cancelled` | Entry withdrawn; will not be delivered to phone |

**`acked` is client-reported**: the phone calls `POST /nutrition/ack` after
reporting a successful Health Connect insert.  This is **client-reported**
and is **not independently verified** by the bridge or this tool.
Independent phone UI readback is always required (see below).

**Cancelled-after-fetch caveat**: if an entry was already fetched by the phone
before being cancelled, the bridge cannot guarantee it was not written to
Health Connect.  A cancelled entry that had `delivery_possible=true` should
be checked in Health Connect UI.

**`acked` and `cancelled` are terminal**: do not describe them as "waiting
for delivery".  Only `confirmed` entries are pending delivery; `draft`
entries are unapproved.

Each queue entry includes a `correlation` field.  At runtime the tool
searches `health_records` for:
1. A primary match: `expl_ns='client'`, `expl_id=client_record_id`,
   `data_origin='com.ravil.healthconnect.nutrition'`.
2. A secondary bounded scan (up to 200 rows with `expl_ns='server'` and
   expected origin) of `record_json` metadata for the client ID.

`correlation.status` reflects what is found in the database at query time:
`not_verified` when no matching explicit ID is present; `identity_match`
when one is found.  Do not assume the absence of IDs is permanent — this
depends on what the ingest payload contained, not on a fixed format
constraint.  Identity match (ID + version) and content match (macros + times)
are reported separately and must not be conflated.

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
python /home/claw/.hermes/profiles/health/skills/health/nutrition-writeback/nutrition_writeback.py \
    --privacy-verifier /home/claw/.hermes/hermes-agent/homelab/services/health-connect-bridge/privacy_gate_check.py \
    --privacy-source /home/claw/.hermes/hermes-agent/homelab/komodo/stacks/traefik/compose.yaml \
    --live-host 192.168.1.166 \
    --ssh-user claw \
    --receiver-url https://health-connect.ravil.space \
    confirm \
    --id <client_record_id> \
    --version <client_record_version> \
    --confirmer Равиль \
    --meal-name "<meal name from draft>" \
    --consent-file /PRIVATE/actual-user-consent.txt
```

`--consent-file` must point to a file containing the user's raw consent text
from their actual message (never agent-generated).  The file is read once and
its contents are never logged.

All flags are mandatory for production.  The `--privacy-verifier` and
`--privacy-source` flags are explicit production gates; omitting them causes
the wrapper to refuse to proceed.  Never call `nutrition_cli.py confirm`
directly — it bypasses the wrapper and consent validation.

### Short final-preview consent (`preview` / `short-confirm`)

The long named consent grammar above stays valid and unchanged. For a lower
friction path the workflow may instead register the **final preview it actually
sent** and accept a short reply to that exact message.

1. After sending the final preview, the workflow runs
   `nutrition_writeback.py preview --id ID --confirmer NAME --user-id U --chat-id C
   [--thread-id T] --preview-message-id M --preview-sent-at ISO8601
   --expected-version V --expected-snapshot-hash H [--ttl-seconds N]`.
   `V` and `H` are the `client_record_version` and `snapshot_hash` that
   `draft`/`status` returned for the draft the preview text was rendered from
   (the exact displayed content). Registration rereads the draft and refuses
   unless it is still `draft`, at version `V`, with live content hash `H`; this is
   checked before and again under the store lock, so a mutation between display and
   registration (even at the same version) is rejected. It then stores a private (0700 dir, 0600
   file, atomic, flock-serialised) pending preview bound to: queue ID, version,
   name, start, end, the four macros and description (SHA-256 snapshot hash), the
   intended user/chat/thread, the preview message ID and send time, and
   created/expiry times (default 15 min, max 60 min). A newer preview for the same
   record supersedes older ones; reusing a preview message ID is refused.
2. When the user replies, the workflow writes the raw transport reply (0600 JSON:
   `sender_id, chat_id, thread_id, message_id, timestamp` with offset,
   `reply_to_message_id, text`) and runs
   `nutrition_writeback.py short-confirm --transport-file PATH`. There is **no
   `--id`**: the preview is found only by (chat, thread, reply target); zero or
   several matches are rejected.
3. Accepted text is only a bare `да`, `запиши`, `да, запиши`, `yes`, `record it`
   (case-insensitive, trailing `.`/`!` ignored). Negation, conditionals and
   anything longer are rejected.
4. Rejected: wrong sender/chat/thread/reply target, reply not strictly after the
   preview, expiry, non-draft state, version change, **any change to the four
   macros, name, times or description at the same version**, tampered preview
   file, replay, and insecure/symlinked store or reply file. The privacy,
   token and receiver readiness gates run first, exactly as for long consent.
5. Consumption: the draft is reread, then the preview is claimed
   `pending -> consuming` under flock *before* the CLI confirm, then post-confirm
   readback runs and the preview becomes `consumed`. A CLI refusal releases the claim
   back to `pending` **only** if the draft is still `draft`, same version and
   identical content hash. If anything changed (including same-version content)
   the preview is `revoked` and can never authorize again; a crash or any other
   outcome stays `consuming` and fails closed (new preview + reply required).
   The content binding is enforced **inside the queue's own `BEGIN IMMEDIATE`
   transaction**: `short-confirm` always passes
   `confirm ... --expected-snapshot-hash H` (`NutritionStore.confirm_entry(...,
   expected_snapshot_hash=H)`). The store recomputes the same canonical hash
   (name, start, end, four macros as floats, description; SHA-256 of compact
   sorted-key JSON) after the version check and raises *before any state
   mutation* on mismatch, so a late same-version mutation can never reach the
   confirmed queue (version CAS alone is insufficient). The option is optional
   and validated as 64 lowercase hex; the long-consent grammar passes none.
   The claim step is also rechecked under the lock: the store must still have no
   unreadable entries, exactly one file must match the reply target, that file must
   be field-for-field the record that was validated and still `pending`, and the
   raw reply `message_id` must not be in the preview's durable `used_reply_ids`.
   A released (untouched-draft) preview records the failed reply's id in
   `used_reply_ids`; the same raw reply is rejected afterwards and a NEW actual
   reply is required. Content/version/state drift at any point (selection, claim,
   pre-confirm, or after a failed confirm) revokes the preview; restoring the
   content never revives it.
6. Store integrity: any unreadable, corrupt, wrong-mode, symlinked, foreign or
   unsupported-schema entry in `pending-previews/` makes both `preview` and
   `short-confirm` fail closed (ambiguity is never ignored).

**Final preview content (workflow text).** One compact message: meal name,
assumptions/portion, the four macros, tool-checked Berlin local time, and the
proposed technical one-minute interval (start to start+1 min, stated as a
technical queue interval, not the real duration). A correction invalidates the
previous preview (send and register a new one). Only after `short-confirm` and
exact readback may the workflow say
"Отправлено в очередь Health Connect. Телефон запишет при синхронизации."; a
client-confirmed write may be claimed only after an ACK.

**Trust boundary.** Both the preview file and the transport-reply file are
written by the trusted agent workflow as the same OS user. They are cross-bound
and tamper-checked, but a local JSON file is *workflow-attested*, not
*server-authenticated*: anything able to write as this user can forge a reply.
Results carry `provenance_level: "workflow_attested"` and any self-declared
"server authenticated" claim is ignored. Independent authentication would need
the messaging gateway to sign reply records (e.g. HMAC with a key the agent
cannot read); that is **not implemented**. The wrapper still passes the CLI's
own internal affirm string when confirming; that string is a CLI argument, not
user consent, and is reachable only after the checks above.

#### Source / deployment mapping

| Source (repo) | Deployed to |
| --- | --- |
| `services/health-connect-bridge/agent-workflow/nutrition_writeback.py` | installed bridge `bin/` via `install.sh` (not done) |
| `services/health-connect-bridge/nutrition_queue.py` (adds `snapshot_hash_of_row`, `confirm_entry(expected_snapshot_hash=)`) | installed bridge code dir, **required** together with the wrapper |
| `services/health-connect-bridge/nutrition_cli.py` (adds `confirm --expected-snapshot-hash`) | installed bridge code dir, **required**; the CLI launcher script from `install.sh` is unchanged |
| `services/health-connect-bridge/tests/test_short_preview_consent.py`, `tests/test_short_consent_sqlite.py` | repo only |
| proposed workflow text above | Hermes health skill (not done) |

#### Proposed deployment / workflow list (not yet performed)

1. Parent independently reviews the diff and runs the focused tests
   (`tests/test_short_preview_consent.py`, `tests/test_short_consent_sqlite.py` (real SQLite, wrapper to isolated CLI, multiprocess, synthetic photo/text integration; synthetic fixtures are not user consent nor vision validation), `tests/test_skill_isolation_root.py`).
2. Merge, then install the updated `nutrition_writeback.py`, `nutrition_queue.py`
   and `nutrition_cli.py` via the normal `install.sh` flow. The wrapper's
   `short-confirm` requires the new CLI option; an old CLI rejects it, so short
   confirm fails closed (never confirms unbound).
3. Update the Hermes health skill (outside this repo; NOT edited in this run) so
   the workflow: creates the draft, takes `client_record_version` and
   `snapshot_hash` from its output, sends the final preview, calls `preview` with
   those values and the real transport message ID/time, writes
   the raw reply record, and calls `short-confirm`.
4. Decide whether to build a gateway-signed reply record for server-authenticated
   provenance before treating short consent as more than workflow-attested.

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

5. **Status readback** — run `consultation_read.py --date <date>` (installed at `/home/claw/.hermes/profiles/health/scripts/consultation_read.py`) and read
   back the exact draft record: name, `client_record_id`, `client_record_version`,
   start/end times.  Also note the КБЖУ preview (К=energy_kcal,
   Б=protein_g, Ж=fat_g, У=carbohydrate_g) from the draft macros.
   Confirm all values match intent before proceeding.

6. **Preview consent** — display the consent statement with `{name}`, `{id}`,
   and `{version}` substituted from the actual draft values:
   > Я, Равиль, подтверждаю запись Health Connect для {name}, ID {id}, версия {version}, с показанным временем и макросами.

7. **User provides consent** — the user reads the full consent statement and
   their exact message text is saved to a private consent file (e.g.
   `/PRIVATE/actual-user-consent.txt`).  This raw text is never
   agent-generated or paraphrased.

8. **Re-read draft status immediately after consent, before confirm** — run
   `consultation_read.py` again to verify the record is still in `draft`
   state with the same version.  Another process may have edited or cancelled
   it between consent and confirmation.

9. **Confirm via wrapper** — invoke the installed `nutrition_writeback.py`
   confirm command (see above) with `--consent-file` pointing to the user's
   raw consent file.  The wrapper validates consent, enforces privacy gates,
   and calls the technical affirm internally.

10. **Status ACK readback** — run `consultation_read.py` (installed path) and verify the entry
    has transitioned to `confirmed`, then `acked` after phone delivery.

11. **HC export correlation** — once `acked`, check `correlation.status` in
    the tool output.  The tool searches `health_records` at runtime for an
    explicit ID match; status is `not_verified` when no matching record is
    found, or `identity_match` with a separate `content_match` field when
    one is.  If `identity_match`, verify `content_match` is `true` and that
    `content_issues` is absent before treating the ingest record as
    independently confirmed.

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

**Evidence scope**: server-side and unit tests establish behaviour of the
bridge server, the queue state machine, and the read-only tool.  They do
**not** establish that background writes succeed, that offline replay is
duplicate-free, or that the real Android app correctly reads and writes on a
physical phone.  Server/unit evidence and real-phone acceptance evidence must
be stated separately.

**Display evidence (confirmed 2026-10-11)**: an existing breakfast entry was
confirmed visible in both Health Connect and Google Health
(`com.fitbit.FitbitMobile` version `5.09.health-mobile-1104120883-985409492`)
on the real phone.  This closes the HC/display check for that specific entry.
Still open for each new entry: exact ID/version readback confirmation and
real-phone background-write / offline-replay acceptance.

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
[ ] Run /home/claw/.hermes/profiles/health/scripts/consultation_read.py --date <date> and review inventory age fields
[ ] Check provenance_notes for any source ambiguity warnings
[ ] Review date_exercise for exercise sessions from expected sources
[ ] Review date_nutrition_ingest for HC-received nutrition records (if any)
[ ] Review queue_by_state.draft for pending AI estimates
[ ] For each draft to confirm:
    [ ] Read exact draft: name, ID, version, times, КБЖУ macros
    [ ] Display full consent statement with actual name/ID/version substituted
    [ ] User provides consent — save raw message text to /PRIVATE/consent-file
    [ ] Re-read draft status AFTER consent, BEFORE confirm (same version?)
    [ ] Run INSTALLED nutrition_writeback.py wrapper with all production flags:
        --confirmer Равиль --meal-name NAME --consent-file /PRIVATE/...
    [ ] Run installed consultation_read.py and confirm state → confirmed → acked
    [ ] Verify independently on phone in Health Connect UI
    [ ] Note: server/unit tests do not establish real-phone background/offline acceptance
[ ] Confirm no cross-source sums or numeric totals have been computed
[ ] Confirm no production identifiers have been captured in notes or files
```
