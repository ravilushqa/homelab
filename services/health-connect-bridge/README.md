# Health Connect Webhook Bridge

Receives JSON webhooks from the [HC Webhook Android app](https://github.com/mcnaveen/health-connect-webhook), stores them in a local SQLite journal, and exposes a read-only CLI.

## Architecture

```
Android phone
  └─ HC Webhook app
       └─ POST https://health-connect.ravil.space/ingest/health-connect
            └─ Traefik (Komodo host) → 192.168.1.65:9121
                 └─ health-connect-bridge receiver (Python 3 stdlib)
                      └─ SQLite  ~/.hermes/profiles/health/workspace/
                                  health-connect-bridge/data/health.sqlite3
```

The receiver and CLI are completely separate processes. The CLI is read-only; the server has no read API.

## Security model

- **Token authentication**: `X-Health-Token` header; stored in `secrets/ingest-token` (mode 0600, never output to logs). Read the file directly on the host to retrieve the value for Android app configuration; do not output it in logs or chat.
- **Constant-time comparison**: `hmac.compare_digest` on HMAC-SHA256 digests prevents timing oracle.
- **No OIDC**: The Android HC Webhook app cannot perform OAuth flows, so the token is the sole auth mechanism. HTTPS + strong random token + idempotency mitigates replay risk.
- **No public read API**: `GET /healthz` only (returns `{"status":"ok"}`). All health data is read via local CLI only.
- **Rate limiting**: per-IP sliding window (10 req/60 s normal, 5 req/300 s on auth failure), global cap 200 req/60 s, concurrency 8.
- **Body bound**: 8 MiB max; Content-Length required; Transfer-Encoding: chunked rejected.
- **No health data in logs**: structured logging never includes record values, tokens, or headers.
- **DB permissions**: 0600 file, 0700 directory.

## Metadata and record identity

**Current JSON limitation**: The upstream app serializes only `data_origin`, `recording_method`, and `device{manufacturer,model,type}` in the `metadata` object. Record IDs (`id`, `client_record_id`, `client_record_version`, `last_modified_time`) appear in the Protobuf schema but are **not present in JSON payloads today**.

**Source key**: canonical JSON encoding of `{origin, method, device}` — all fields from the record metadata. Missing components are JSON `null`; absent metadata entirely produces `{"_no_metadata":true}`. Records with the same origin+method+device share a source key regardless of which physical device (no hardware serial in Health Connect JSON metadata). Explicit IDs are namespaced by `data_origin` so two apps with the same ID stay separate records.

**Deduplication**:
- Without explicit ID: temporal key (start+end or single timestamp) is the idempotency key per source. Same key + same value = skip. Same key + different value = UPDATE (value correction).
- With explicit ID (future JSON support): explicit ID namespaced by `data_origin` takes precedence; time-edit updates work correctly. For `heart_rate`, a shape prefix (`s:`/`b:`) is included in the sample discriminator so a raw sample and an aggregate bucket at the same id+origin+time remain two distinct records. Nanosecond-precision timestamps preserve sub-microsecond distinctness; integer arithmetic avoids float truncation.

**Known limitations**:
- Deletions: deleted on-device records persist in the journal forever (incremental sync sends no tombstones).
- Resegmentation: if one session is split into multiple on the device side, both old and new temporal keys coexist.
- Time edits: old temporal key persists alongside new one (two separate records).
- Unknown-source ambiguity: if two devices have identical metadata (same app, same device), their records share a source key and cannot be distinguished. When origin or device provenance is missing or absent, step `total` is reported as `null` (not authoritative); a `raw_unallocated_subtotal` is included for diagnostic purposes only and must not be treated as a true step count.

## CLI usage

```bash
# Show sync status and freshness
health-connect-read status

# Daily summary (today, Europe/Berlin)
health-connect-read day

# Daily summary for specific date
health-connect-read day 2026-01-15

# Sleep sessions ending on a date (Berlin time)
health-connect-read sleep 2026-01-15

# Exercise sessions starting on a date
health-connect-read workouts 2026-01-15
```

All CLI commands use `PRAGMA query_only=ON` and mode=ro SQLite URI. No mutations are possible.

## Android app configuration

1. Install [HC Webhook](https://github.com/mcnaveen/health-connect-webhook) on Android.
2. Create a new webhook:
   - URL: `https://health-connect.ravil.space/ingest/health-connect`
   - Format: JSON (not gRPC)
   - Custom header: `X-Health-Token: <value from secrets/ingest-token>`
3. Enable desired data types (steps, sleep, heart_rate, weight, exercise recommended).
4. Set sync interval: **60 minutes recommended**, 15 minutes minimum.
5. Sync window: the app uses a rolling 48-hour window with incremental watermarks.
   - If the phone is offline >48 hours, data gaps may occur.
   - To backfill: use "explicit range" sync in the app (device-dependent availability).
6. The app retries failed requests with backoff; a 2xx response confirms successful ingestion.

**Note**: There is no native signature mechanism. The pre-shared token over HTTPS is the only auth. Keep the token confidential.

## Deployment

### Install locally

```bash
./services/health-connect-bridge/install.sh
```

This:
- Copies code to `~/.hermes/profiles/health/workspace/health-connect-bridge/code/`
- Generates a random token (if not present) in `secrets/ingest-token`
- Installs `~/.config/systemd/user/health-connect-bridge.service`
- Creates `bin/health-connect-read` CLI wrapper

### Start the service

```bash
systemctl --user daemon-reload
systemctl --user enable --now health-connect-bridge
systemctl --user status health-connect-bridge

# Local health check (before routing is live)
curl http://192.168.1.65:9121/healthz
```

### HTTPS routing (GitOps)

The `komodo/stacks/health-connect-bridge/` Komodo stack deploys a router-only container that instructs Traefik to route:

```
POST https://health-connect.ravil.space/ingest/health-connect
  → http://192.168.1.65:9121
```

**This route is NOT live until the PR is merged and the Komodo stack is deployed.**

## Runbook: deploying the Traefik route

Deployment order (all steps require GitOps PR approval and merge first):

1. **Redeploy traefik stack first** (activates umami exclusion):
   - `komodo/stacks/traefik/compose.yaml` includes `ignoreURLs[0]=^/ingest/health-connect(\?.*)?$` for the umami-feeder plugin.
   - This exclusion prevents sync timing and path metadata for `/ingest/health-connect` from flowing to Umami analytics.
   - The exclusion is **NOT active** until the traefik stack is redeployed via Komodo after PR merge.
   - Request body and headers (token, health data) are never logged by umami regardless.

2. **Then deploy this router stack**:
   - The HTTPS route is **NOT live** until this step completes.

3. **After deploying**:
   - Check Traefik access logs: `docker logs traefik | grep health-connect`
   - Verify no token values or body content appear in logs.
   - Confirm the path only accepts POST (all other methods should 404/405 from the router).

3. **First sync test**:
   - Trigger manual sync from Android app.
   - Run `health-connect-read status` to confirm `state: ok` and non-zero record count.

## Privacy notes

- The receiver never logs health record values, tokens, or request headers.
- SQLite DB (mode 0600) is only readable by the owning user.
- Umami analytics captures request metadata (not body/headers) — see Runbook above.
- No cron jobs or background processes query or transmit health data automatically.
- The health profile skill reads data on-demand via the read-only CLI only.

## Testing

```bash
# Run from worktree root
python3 -m unittest discover -s services/health-connect-bridge/tests -p "test_*.py" -t services -v
```

All test fixtures are synthetic (fabricated). The test DB is isolated from production. Production DB is empty until first phone sync.
