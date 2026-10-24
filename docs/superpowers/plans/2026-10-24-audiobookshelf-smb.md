# Audiobookshelf SMB Stack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Komodo stack running Audiobookshelf 2.37.2 over the existing read-only `/mnt/audiobooks` CIFS bind.

**Architecture:** One compose service with local named volumes for state and a read-only long-syntax bind for media. Traefik labels with an env toggle. One `[[stack]]` entry pinned to the feature branch.

**Tech Stack:** Docker Compose, Traefik labels, Komodo resource TOML, Python stdlib `unittest` + `tomllib`, `docker compose config`.

**Spec:** `docs/superpowers/specs/2026-10-24-audiobookshelf-smb-design.md`

## Global Constraints

- Image `ghcr.io/advplyr/audiobookshelf:2.37.2`; `TZ=Europe/Berlin`; `restart: unless-stopped`; no `container_name`.
- `traefik.enable=${AUDIOBOOKSHELF_TRAEFIK_ENABLED:-true}`; no `oidc-auth` middleware.
- `/mnt/audiobooks` → `/audiobooks`: `type: bind`, `read_only: true`, `bind.create_host_path: false`.
- `/config` and `/metadata` are named volumes, never bind paths.
- TOML: `branch = "feat/audiobookshelf-smb"`, `auto_update = false`, `deploy = true`, no secrets.
  `stack.config.branch` only selects the branch Komodo clones for this stack's compose files; the global
  resource sync reads `stacks.toml` from its own branch (`main`) independently. `auto_update = false` only
  disables image-poll redeploys; webhook, sync (`deploy = true`) and manual deploys still run.
- `stack.config.pre_deploy` (`shell_mode = true`): read-only guard that fails unless
  `findmnt -rn -M /mnt/audiobooks -t cifs -o SOURCE` equals `//192.168.1.2/proxmox[/audiobooks]` and
  `ls -A /mnt/audiobooks/` succeeds. Never mounts or changes host state.
- Do not modify `komodo/sync.toml`, other stacks, or `logs/`. No commit, push, or deploy.

## Review Focus

- Compose short syntax `/mnt/audiobooks:/audiobooks:ro` would silently create the host path if it were missing → the test asserts long syntax with `create_host_path: false`.
- `create_host_path: false` only rejects a missing path; an existing but unmounted `/mnt/audiobooks` would bind an empty dir → the `pre_deploy` guard is run under `sh -c` against PATH-stubbed `findmnt`/`ls`: correct source passes, wrong source fails, nothing mounted fails even though the directory exists, unreadable fails.
- An unset `AUDIOBOOKSHELF_TRAEFIK_ENABLED` must default to exposed (`true`) and `false` must disable it → the test renders both cases.
- A copy-paste of another stack's labels could bring in `oidc-auth` → the test asserts no middleware label.
- A second `audiobookshelf` TOML entry, or `branch = "main"` in it, would make the stack deploy compose files from the wrong branch → the test asserts exactly one entry with `stack.config.branch` on the feature branch, and no other stack changed branch or gained `pre_deploy`.
- The healthcheck must not depend on curl or wget → the test asserts `node` and `/healthcheck`.

---

### Task 1: Stack, Komodo resource, tests, docs

**Files:**
- Create: `komodo/stacks/audiobookshelf/compose.yaml`
- Create: `komodo/stacks/audiobookshelf/README.md`
- Create: `komodo/stacks/audiobookshelf/tests/test_stack.py`
- Modify: `komodo/stacks.toml` (append to the media section after `your_spotify`)

- [x] **Step 1: Write failing tests** in `tests/test_stack.py` (stdlib `unittest`):
  `docker compose config --format json` for the rendered model (skipped if the docker CLI is
  missing), and `tomllib` for `stacks.toml`. Tests:
  `test_image_pinned`, `test_media_bind_long_syntax_readonly_no_create`, `test_state_on_named_volumes`,
  `test_traefik_labels`, `test_no_oidc_middleware`, `test_traefik_toggle_false`, `test_no_container_name_restart_tz`,
  `test_healthcheck_node_route`, `test_toml_single_entry_feature_branch`, `test_no_writable_extra_mounts`,
  and `PreDeployGuardTests` (pass / wrong source / unmounted existing dir / unreadable / no host writes).
- [x] **Step 2: Run** `python3 -m unittest discover -s komodo/stacks/audiobookshelf/tests -v` → expect FAIL (no compose file, no TOML entry).
- [x] **Step 3: Write** `compose.yaml`, the TOML entry (with `pre_deploy` mount guard), and `README.md` (bootstrap, limitations, mount guard, migration).
- [x] **Step 4: Run** the tests → all PASS. Run `docker compose -f komodo/stacks/audiobookshelf/compose.yaml config -q` → exit 0. Run a `tomllib` parse of `stacks.toml` → OK.
- [x] **Step 5: Leave uncommitted** (Hermes commits).
