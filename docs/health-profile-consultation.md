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
| `queue_day_report` | `nutrition.sqlite3` | Pending / done / excluded queue records of the day with reasons and canonical `user_status`; no totals |

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
assumptions/portion, the four macros, the Berlin local time (from a real tool call in production; the test uses a controlled-clock fixture, not tool output), and the
proposed technical one-minute interval (start to start+1 min, stated as a
technical queue interval, not the real duration), ending exactly «Записать или поправишь?». The record id, version and
snapshot hash are never shown; they are bound internally at `preview` registration. Synthetic photo/text
fixtures are not vision validation and the fixture "Да" is not the user's consent. A correction invalidates the
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

### Personal-chat short consent (`personal-preview` / `personal-confirm`)

For a **private one-to-one chat only** the workflow may use a lighter flow with no
Reply, no Telegram message IDs, no receipts and no long phrase. The receipt-bound
`preview`/`short-confirm` and the long named grammar stay valid and unchanged; they
are just not the default personal flow. Group/platform channels must not use this mode.

1. **Single active preview.** The agent first stores the preview, then shows the
   user exactly the returned `display_text`:
   `nutrition_writeback.py personal-preview --scope LABEL --attest-personal-chat
   --id ID --confirmer NAME --expected-version V --expected-snapshot-hash H
   [--notes-file PATH] [--ttl-seconds N]`. `LABEL` is a stable opaque label
   (e.g. `personal-main`; not a transport/chat ID, must start alphanumeric) and, like
   the hash and confirmer, is validated exactly as supplied (nothing is stripped,
   lowercased or repaired). `--attest-personal-chat` is the agent's explicit
   declaration that the scope is a one-to-one chat. The wrapper itself builds a
   compact text from the **saved** record, once, with no IDs, hashes or ISO strings:

   ```
   Тосты с яйцами и беконом
   ≈500 ккал · Б 30 · Ж 25 · У 40 г
   Порции приблизительные; масло — допущение.
   14 октября, 09:30–09:45, Берлин
   Записать?
   ```

   Macros are the exact saved values (safe numeric rendering). The time line converts
   the saved instants (UTC or explicit offset) to Europe/Berlin with Russian month
   names; a range crossing midnight shows both dates, crossing a year shows the year,
   and a wall time that occurs twice on the autumn DST change gets a `(UTC+N)` hint.
   The display does not claim whether the time was user-supplied or proposed: the
   agent's optional note (`--notes-file`, ONE line, max 240 chars; longer, multi-line
   or control-character notes are rejected, never truncated) states an assumption
   only when the time was not given by the user. The preview saves `display_format: 2`;
   confirm recomposes and validates with the saved format. Previews without that
   field (older personal previews) keep the previous text and validation unchanged, so
   they stay confirmable without re-consent; unknown or tampered formats fail closed.
   The saved snapshot (incl. description), its hash, version, notes, display text and
   display hash, scope and a stored `expires_at` (default 15 min, max 60 min; kept for
   audit and structure validation, see *Late consent* below) live in
   `personal-previews/` (0700 dir, 0600 files, flock, atomic
   writes, no symlinks). A friendlier rounded rendering may be added by the agent
   outside `display_text`, but consent only covers the returned exact values.
2. **One slot per scope.** A new preview in the same scope marks the older pending
   one `superseded` (its draft is not touched) and needs fresh consent — whether or
   not the older one is past its stored `expires_at`. The slot is shared with recipe
   previews and is closed by a `personal-log-known` fast log (see below): at most one
   yes/no question is open per scope. Previews an older wrapper already marked
   `expired` stay unconfirmable. Several active previews in one scope (store
   corruption) fail closed for both commands. Other scopes are never affected.
3. **Bare yes.** When the user replies, the agent decides from the conversation
   whether the message is an unambiguous yes to *that* shown preview. If ambiguous
   (e.g. "да, но поправь", "ок наверное") it asks again and does not call confirm.
   If unambiguous, it writes the user's actual raw text to a fresh 0600 file and runs
   `personal-confirm --scope LABEL --attest-personal-chat --consent-file PATH`.
   Accepted text is only `да`, `запиши`, `да, запиши`, `yes`, `record it`
   (case-insensitive, trailing `.`/`!` and surrounding whitespace ignored). Negation,
   conditionals, corrections, multiple lines and anything longer are rejected.
   Each consent file is single-use for the **whole store**: a file already claimed for
   any preview (consumed, revoked, superseded, any scope) is rejected, and a file whose
   real mtime predates the active preview's creation is rejected as stale. That is a
   local workflow-freshness check, not a user timestamp; a NEW file with the same text
   is accepted under agent attestation (the wrapper cannot tell who wrote it).
   `personal-confirm` re-validates the saved snapshot, display text and hashes, notes,
   timestamps/TTL and structure; any mismatch or malformed/foreign record fails closed.
4. **Corrections.** A correction edits the *same* record (new version) and requires a
   new `personal-preview`; the old snapshot can never be confirmed (drift revokes it,
   and restoring the content does not revive it). No new meal is created.
5. **Snapshot CAS / dedup.** `personal-confirm` runs the same privacy/token/receiver
   readiness gates as long consent, claims the preview `pending -> consuming` under
   the lock, then calls the queue confirm with the version and
   `--expected-snapshot-hash`, verified inside the queue's own transaction (same as
   `short-confirm`). Replays, duplicate "Да", concurrent confirms and a reused consent
   file are rejected read-only (no queue mutation, no second delivery); an interrupted
   run stays `consuming` and fails closed. If the helper reports success but its output
   is not a valid confirmed readback, the result is an *uncertain outcome* error (the
   preview stays `consumed`, nothing is replayed; read the record with `status`) - a
   success is never synthesized. After a CLI refusal on an untouched draft
   the preview is released, but the consumed consent file cannot be reused.
6. **Audit.** The raw consent text is stored unchanged (`consent_raw_text`, plus the
   file SHA-256) on the preview record and echoed under `personal_consent`. The
   wrapper invents no user timestamp, author or message ID (`claimed_at` is the
   wrapper's own clock; the claimed consent file is tracked as an internal
   `consent_file_key`, not a Telegram id).
7. **Late consent (no time-only expiry).** A personal preview is no longer rejected
   *only* because its stored `expires_at` has passed. `personal-confirm` accepts a late
   bare yes only if **all** of these still hold: the preview is the scope's single
   `pending` question (never superseded, revoked, consuming, consumed); same scope and
   `--attest-personal-chat`; same record id, version, full snapshot hash (name, times,
   four macros, description), display text and display hash (re-validated); the
   consent file is a fresh 0600 file whose real mtime is after the preview was created
   and that was never used for anything in the store; privacy/token/receiver gates
   pass; the queue confirm passes version + `--expected-snapshot-hash` CAS and the
   post-confirm readback. The preview is never renewed or recreated: `expires_at` and
   `created_at` stay untouched, and the record gains `late_consent` (bool) and
   `original_expires_at`; both are echoed under `personal_consent`. Any change to the
   meal still needs a fresh preview and a new actual yes. The receipt-bound
   `preview`/`short-confirm` expiry is **unchanged** (still enforced).

#### User-facing statuses (canonical)

Every `status` readback (single id and list), `personal-confirm` /
`personal-log-known` result and both day reports carry `user_status`
`{code, text, known}`. Use exactly these texts:

| Queue state | `text` |
| --- | --- |
| `draft` | Черновик — ждёт подтверждения |
| `confirmed` | Принято — ожидает синхронизации |
| `acked` | Телефон подтвердил запись в Health Connect |
| `cancelled` (never fetched) | Не записано: отменено |
| `cancelled` after a possible phone fetch | Отменено, но телефон уже мог получить запись — проверьте Health Connect на телефоне |
| failed command | Не записано: `<reason>` |
| any other / malformed state | Статус неизвестен — не считать записанным (`known: false`) |

`status <id>` exits 1 for an unknown state. Failures of `personal-confirm` /
`personal-log-known` get `user_status` on their JSON error line: `failed`
("Не записано: …") when nothing was written, `replay` ("Повтор — ничего нового не
записано…") for reused files/already-confirmed previews, and `unknown` whenever the
queue *may* already hold the record (invalid helper output, failure after the confirm
step, timeouts). A draft that was edited after a possible delivery adds the note that
the previous version may already be on the phone and phone-side replacement is **not
guaranteed** (the protocol's version handling on the phone is not verified here).

#### Day report (read-only)

`nutrition-writeback day-report [--date YYYY-MM-DD]` (default: today in
Europe/Berlin) lists the queue rows whose `start_time` falls on that Berlin calendar
day (DST-aware, half-open midnight-to-midnight): `pending` (draft = unapproved,
confirmed = waiting for phone sync/ACK), `done` (acked) and `excluded` (cancelled —
with a "phone may have fetched it" note when applicable — unknown state, unusable or
timezone-less `start_time` with `day_known: false`), each with a `reason` and
`user_status`. It has **no totals** (`"totals": null`): drafts are never intake, and
queue rows are never summed with ingest. It queries only the requested day with a
parameterised SQL statement on a `mode=ro` + `PRAGMA query_only` connection (the DB
is never written or created; no migration), `LIMIT 200+1`, so an old day is found
however long the history is; `possibly_incomplete` is true only when the day itself
has more than 200 rows (`limit`, `returned` are reported). Timezone-aware
`start_time` values are placed by instant (offsets and fractions honoured); a value
that is not timezone-aware is listed under `excluded` (`day_known: false`) only if its
literal date prefix is the requested date; malformed rows of other days are not
counted. `consultation_read.py` adds the same classification and SQL as
`queue_day_report`; `queue_by_state` and `date_nutrition_ingest` are unchanged and
still separate.

#### Known-recipe registry (`recipe-preview` / `recipe-confirm` / `recipe-show`)

A known recipe is a user-confirmed, immutable template in the personal scope, stored
in the same private `personal-previews/` store as `kind: "recipe"` records:

1. The agent writes a 0600 JSON file and runs `recipe-preview --scope LABEL
   --attest-personal-chat --recipe-file PATH`:
   `{"key": "oat-std", "name": "...", "aliases": ["..."], "ingredients": [{"item":
   "...", "quantity": "50 г"}], "per_portion": {"energy_kcal": N, "protein_g": N,
   "carbohydrate_g": N, "fat_g": N}, "macro_basis": "label"}`. Exactly these fields;
   every quantity contains a number; macros within the queue bounds and plausible
   (energy within max(30 kcal, 25 %) of 4/4/9 kcal per g); `macro_basis` must be
   `label` (no estimates); duplicate JSON keys/NaN rejected. Aliases are the words the
   user uses for the meal (inflected forms must be listed explicitly, e.g. «овсянку»);
   2-60 letters/digits, not filler/date/command words. The wrapper returns one
   `display_text` (name, each ingredient with quantity, the four macros "по
   этикеткам", "Сохранить рецепт?"); show exactly that.
2. After the user's bare yes: `recipe-confirm --scope LABEL --attest-personal-chat
   --consent-file PATH` (same bare-affirmative grammar, freshness and store-wide
   single-use rules as `personal-confirm`). The recipe becomes `active` with an integer
   `version` (monotonic per key) and `recipe_hash` (SHA-256 of the canonical recipe); a
   previous active version of the key becomes `retired`. Changing a recipe = a new
   version through the same two steps.
3. **Saving a recipe never logs a meal**: no queue call is made (so the privacy gates
   are not needed for these commands). `recipe-show --scope LABEL
   --attest-personal-chat --recipe-key KEY` is read-only and returns the active
   recipe plus the exact `draft_template` (name, four macros, description
   `Известный рецепт vN: <ingredients with quantities>`).

##### `recipe-register`: an ALREADY user-confirmed recipe (no second yes)

If the user has already explicitly asked to save/remember a standard recipe — a
clear «запомни …» message now, or an earlier explicit confirmation found in the real
conversation history — do **not** ask again and do not use `recipe-preview` (that would
also take over the scope's open yes/no question). Use:

```
nutrition-writeback recipe-register --scope LABEL --attest-personal-chat \
    --attest-user-confirmed-recipe --recipe-file /PRIVATE/recipe.json \
    --evidence-file /PRIVATE/evidence.json
```

`recipe.json` is the same schema as `recipe-preview` (key, name, aliases, ingredients
with quantities, `per_portion` four macros, `macro_basis: "label"`). `evidence.json`
(0600, ≤ 4 KiB, exactly these keys, duplicate keys rejected):

```
{"source": "direct_request" | "history_import",
 "session_id": "<real conversation/session id>",
 "message_id": "<real id of the user's message>",
 "user_text": "<the user's message, verbatim, one line, ≤ 600 chars>",
 "received_at": "<ISO-8601 with UTC offset: when that message was received>",
 "basis": "<short note: where the label values came from, e.g. private saved label photos>"}
```

For `source: history_import` ONLY, an explicit confirmed-standard reply is accepted
instead of a save verb: a verbatim single-line, non-quoted, non-negated, non-hypothetical
text that starts with «Да», calls the recipe the user's standard/default (стандартн…,
обычн…, по умолчанию) and names THIS recipe (its name or a listed alias), e.g.
«Да, стандартная <name>, <quantities>». A bare «Да», an unrelated standard, a changed or
hedged assertion («теперь», «но», «примерно», «без», …), a question, a quote or
example/assignment wording is refused. `direct_request` always needs explicit save intent;
there is no blanket yes authorization, and `--attest-user-confirmed-recipe` plus the
real ids/time/basis stay mandatory.

Checks: the recipe passes the same canonical validation as `recipe-preview` (estimated or
unlabeled recipes are refused → log such meals through `personal-preview`); `user_text`
must contain an explicit save verb (запомни / сохрани / зафиксируй …) and be free of
negation, questions, hypotheticals («можешь», «если», «бы», …), quotes/brackets and
example/assignment wording («задание», «пользователь», «например», …); `received_at`
is not in the future, and for `direct_request` the message and the evidence file are
fresh (≤ 30 min) — an older message must be `history_import`; each (session_id,
message_id) pair is single-use store-wide. On success a new immutable recipe version
(integer `version`, `recipe_hash` over the full canonical recipe) becomes `active` and the
previous version of the key is `retired`; the record keeps the evidence (source, ids,
verbatim text, received_at, basis, SHA-256 of the evidence file) and no consent text — no
yes is fabricated. It does not touch the queue, the privacy gates or any open preview
question. The evidence is agent-attested (`agent_conversation_attested`): the wrapper
cannot verify that the ids or text are real. The import route is for recipes the user
really confirmed; never invent evidence or recipe values.

Migration of an existing confirmed standard = one `recipe-register` per recipe key with
`source: history_import`, the real prior message's ids/text/time and the private label
evidence in `basis`; then `recipe-show --recipe-key KEY` to read `recipe_version` /
`recipe_hash` for the fast path.

#### Known-recipe text log fast path (`personal-log-known`)

For a **personal chat only**, when the user's own direct text message asks to log a
*known* recipe at a time they state explicitly, no preview and no extra yes are
needed; the request itself is the consent. Every other case (photo, new or estimated
meal, unknown recipe, assumed/approximate time, portions, corrections, mixed meals)
uses `personal-preview` + a fresh yes. There is **no** "correction + записывай"
shortcut.

1. The agent writes the user's raw message to a fresh 0600 request file **when it
   arrives**, then creates the draft with `draft` from `recipe-show`'s
   `draft_template` plus `start_time` (the stated Berlin time as UTC/offset ISO) and
   `end_time = start_time + 60 s`, then runs:

   ```
   nutrition-writeback <production privacy flags> personal-log-known \
       --scope LABEL --attest-personal-chat --attest-direct-user-request \
       --attest-user-time --request-file /PRIVATE/req.txt \
       --request-received-at 2026-10-14T11:58:03+02:00 \
       --time-evidence-file /PRIVATE/time.json \
       --recipe-key KEY --recipe-version N --recipe-hash H \
       --id ID --expected-version 1 --expected-snapshot-hash S --confirmer NAME
   ```

   `--request-received-at` is the time the user's message was actually received, taken
   from the real conversation metadata (never invented, never the file write time). It
   decides which day «сегодня»/«вчера»/the default day mean, so a message received at
   23:58 and written to the file at 00:01 is still «сегодня». It is required, must be
   timezone-aware, not in the future, not older than 30 min, and consistent with the
   request file (file written no earlier than 60 s before and no later than 10 min after
   the receipt); the file's mtime is used only as a freshness check. The value is stored
   in the audit record (`request_received_at`).

   `time.json` (0600) = `{"time_phrase": "<the user's words for the time, verbatim>",
   "local_date": "YYYY-MM-DD", "local_time": "HH:MM"}` (Europe/Berlin). The flags are
   internal wrapper arguments, never something the user types.
2. The recognizer is a small whitelist, not NLP. Accepted: one line ≤ 300 chars that
   **starts** with «запиши(те)», «залогируй(те)» or «добавь / внеси / занеси в
   дневник (питания)» (optional leading «пожалуйста»), names the recipe (name or alias)
   exactly once, contains exactly one explicit clock time («в 8:30», «8:30 вечера», «в 8
   утра», «в 2 дня», «в 13») and at most one date («сегодня», «вчера», «позавчера»,
   «12.10», «12 октября [2026]»; default = the Berlin day of `--request-received-at`),
   and otherwise only filler words (мой/мою/обычную, на завтрак/обед/ужин/перекус, по
   рецепту, в дневник, пожалуйста). Rejected (→ preview path): negations,
   questions/«?», hypotheticals/conditionals («если», «можешь», «бы», «наверное»),
   quotes/brackets/«>», example/assignment/test/«пользователь» wording, photos,
   estimates/quantities/units, approximate or vague times («около», «утром», «часов
   в», «в 8» without утра/вечера, «8.30»), more than one time or date, future dates,
   dates older than 7 days, a meal time more than 1 h after the request, DST-ambiguous
   or non-existent local times, corrections/portions/mixed meals («и», «с», «без»,
   «половину», «вместо», «поправь», …), multi-line text, a message that only asks to
   save a recipe. No mandatory user phrasing beyond a direct log instruction.
3. The wrapper then requires: request file 0600, owned, no symlink/hardlink, ≤ 2 KiB,
   real mtime within the last 30 min, never used before in the store (any kind/scope);
   the recipe `active` in this scope with exactly `--recipe-version`/`--recipe-hash`;
   the agent's time evidence equal to the wrapper's own resolution and its phrase a
   verbatim part of the request; the live draft at `--expected-version 1` (a fresh,
   never-edited draft), exact `--expected-snapshot-hash`, name/four macros/description
   equal to the recipe, start = the stated time, end = start + 60 s, never fetched,
   created no earlier than 2 min before the request file; no other confirmed/acked queue
   row and no known-log record with the same recipe and start time (duplicate guard);
   the draft not already bound to another request. Then the same privacy → token →
   receiver gates, a claim under the store lock (re-checking replay, binding, duplicate
   and the recipe record; closes any open preview question of the scope), and the queue
   confirm with version + `--expected-snapshot-hash` CAS and readback.
4. Result: compact `ack_text` (`<name> — <Berlin time>` + the canonical status, e.g.
   «Принято — ожидает синхронизации»), `user_status`, and a `known_log` audit (raw
   request text, file SHA-256, recipe key/version/hash, time phrase/date/time;
   `provenance_level: agent_conversation_attested`). A known-log record is `consuming`
   → `consumed`; a refused queue confirm on a verifiably untouched draft → `failed`
   (the request file stays used; a new request or the preview path is required); any
   uncertain outcome stays `consuming` and is reported as unknown status.
5. **Corrections** go through the existing queue `edit` of the **same** record (new
   version, back to `draft`, same row), then `personal-preview` + a fresh actual yes.
   The fast path refuses every version other than 1, so neither the old request nor a
   new direct request can confirm a corrected version. If the record may already have
   reached the phone, do not promise that the phone replaces it.

**Trust boundary (fast path).** Like personal consent this is
`agent_conversation_attested`: the wrapper cannot prove who wrote the request file,
that it is the user's direct message, or that no photo was attached — the agent
attests that with `--attest-direct-user-request` / `--attest-user-time`. What the
wrapper does enforce is the whitelist grammar above, that the stated time is in the
user's words and matches the draft exactly, that the draft is exactly the confirmed
recipe, single use of each request file, one request → one record/version, and the
same privacy gates and queue CAS as every other confirm.

**Weaker provenance.** `provenance_level` is `agent_conversation_attested`: weaker
than the receipt-bound `workflow_attested` path. Nothing links the consent file to a
Telegram server message or authenticates the sender; the agent's judgement of the
conversation is the only evidence, and anything able to write as this OS user can
forge the files or touch their mtime. No author identity is claimed. Do not describe it
as server-authenticated.

**Queue vs ACK vs UI.** `personal-confirm` success only means *confirmed in the
server queue*. The phone writes to Health Connect on its next sync; only an ACK from
the phone means it was written. After confirm use the canonical `user_status.text`
(«Принято — ожидает синхронизации»); claim a client-confirmed write («Телефон
подтвердил запись в Health Connect») only after an ACK. Any UI/chat display is
not evidence of either.

Example (fake values): `personal-preview --scope personal-main --attest-personal-chat
--id nutr_example --confirmer Alex --expected-version 1 --expected-snapshot-hash
<64-hex> --notes-file /secure/dir/notes.txt`, later `personal-confirm --scope
personal-main --attest-personal-chat --consent-file /secure/dir/reply.txt`.

Tests: `tests/test_personal_chat_consent.py` (fake queue and real SQLite + isolated
CLI + multiprocess; synthetic fixtures, not a real user's consent) and
`tests/test_nutrition_consent_ux.py` (statuses, day reports, late consent, recipe
registry, fast path: real SQLite + isolated CLI, multiprocess races, real
`/usr/bin/python3 -I` wrapper processes for read-only commands; synthetic fixtures).

Run (from `services/health-connect-bridge`, so `tests` is the import root; temp dirs
are private 0700 dirs under `~/.hermes/profiles/health/cache/scratch`, no network,
no production files):

```bash
cd services/health-connect-bridge
/usr/bin/python3 -m unittest tests.test_nutrition_consent_ux tests.test_personal_chat_consent \
    tests.test_short_preview_consent tests.test_short_consent_sqlite \
    tests.test_skill_isolation_root tests.test_consultation_read
```

#### Installation file set (consent UX; not performed here)

Only two files change; `nutrition_queue.py` / `nutrition_cli.py` are unchanged and the
installed CLI already supports `confirm --expected-snapshot-hash`.

| Source (repo) | Installed to |
| --- | --- |
| `services/health-connect-bridge/agent-workflow/nutrition_writeback.py` | `~/.hermes/profiles/health/workspace/health-connect-bridge/bin/nutrition-writeback` (0700) |
| `services/health-connect-bridge/consultation_read.py` | `~/.hermes/profiles/health/scripts/consultation_read.py` (0700) |

`services/health-connect-bridge/agent-workflow/consent-ux.sha256` holds the SHA-256 of
both sources (`sha256sum -c` from the repo root). Verify the copies afterwards with
`sha256sum` against the same values. The new stores reuse the existing private
`personal-previews/` directory; existing pending/consumed records stay valid (meal
previews have no `kind` field). The health skill text is **not** updated here; until it
is, the agent will not use `recipe-*` / `personal-log-known` / `day-report`.

#### Risks and limitations (consent UX)

- Late consent removes the time limit for an unchanged, still-open personal preview:
  a yes given long after the preview still confirms it if the agent attests it answers
  that preview. Mitigation: one open question per scope, and any new preview, recipe
  preview or fast log closes it.
- The fast-path recognizer is intentionally narrow (Russian only, explicit
  verb-first, whitelist leftovers) and rejects many harmless phrasings; those fall
  back to the preview path. Aliases must be listed in every inflected form the user
  uses. It is not language understanding and does not detect sarcasm or context.
- Request/consent freshness and single use rely on local file metadata (mtime,
  inode); anything able to write as this OS user can forge them
  (`agent_conversation_attested`, no server authentication, no author identity).
- The day report reads only the most recent rows the queue CLI lists (200) and flags
  `possibly_incomplete`; it never computes totals.
- Nothing here proves a phone write: `confirmed` is server-side only, `acked` is
  phone-reported; phone-side replacement of corrected versions is not verified.

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
