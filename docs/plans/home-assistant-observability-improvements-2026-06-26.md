# Home Assistant Improvements and Observability Plan

> **For Hermes:** Use `homelab-ops` for live HA/Grafana verification and `subagent-driven-development` if this plan is implemented as repository changes.

**Goal:** Clean up the current Home Assistant installation and add Grafana/Prometheus visibility with actionable alerts for low batteries, unavailable devices, lock safety, and Home Assistant health.

**Architecture:** Keep Home Assistant as the source of smart-home state. Enable the Home Assistant Prometheus endpoint and have the existing `grafana-lgtm` Komodo stack scrape it. Add Grafana dashboards and alert rules backed by Prometheus metrics; use HA automations only where stateful home actions are better than monitoring alerts.

**Tech Stack:** Home Assistant 2026.6.4, Home Assistant Prometheus integration, Grafana LGTM stack (`komodo/stacks/grafana-lgtm`), Prometheus scrape config, Grafana dashboards/alerts, Telegram/Grafana contact points.

**Source analysis date:** 2026-06-26 09:13 Europe/Berlin.

---

## Current Findings

### Home Assistant inventory

- Runtime entities: 315
- Entity registry entries: 898
- Disabled entities: 588
- Devices: 65
- Areas: 5 (`Bathroom`, `Bedroom`, `Entrance`, `Kitchen`, `Living Room`)
- HA version: 2026.6.4

### Active problems to fix

1. **Broken tablet socket automations**
   - `automation.turn_on_tablet_socket`
   - `automation.turn_off_tablet_socket`
   - HA repair error: `Unknown device '4d5b12d4f002b8ec10d6e20deab5e4cf'`
   - Both are `unavailable` and never triggered.

2. **Presence/mobile app unreliable**
   - `person.ravil` is `unknown`.
   - Multiple duplicate/old trackers exist: `device_tracker.pixel_8`, `device_tracker.pixel_8_2`, `device_tracker.pixel_10_pro`, `device_tracker.pixel_watch_4`.
   - Presence-based automations are not trustworthy until this is cleaned up.

3. **Fully Kiosk / Fire Tablet offline**
   - Many `fire_tablet` entities are `unavailable`, including battery, screen, camera, notify, kiosk mode, and media player.
   - Decide whether the tablet dashboard is still active. If not, remove the integration and related automations.

4. **Dreo Tower Fan offline**
   - `fan.tower_fan`, `switch.tower_fan_power`, and related temperature/display/sound entities are `unavailable`.

5. **Berlin Transport / Calendar unavailable**
   - `sensor.friedrich_wolf_str_berlin`
   - `sensor.s_kopenick_berlin`
   - `sensor.s_grunau_berlin`
   - `calendar.familie`

6. **Battery maintenance**
   - `Door motion`: 23% — replace soon.
   - `Bathroom motion`: 32%.
   - `Kitchen motion`: 35%.
   - `Vanna motion senser`: 40%.

7. **Available OTA update**
   - `Bedroom switch hue`: installed `33565696`, latest `33576193`.

8. **Automation cleanup**
   - Old disabled automations have not triggered in 200-400+ days.
   - `automation.test` is named `Bathroom switch`.
   - `automation.living_room_sofa_switch` is named `Kitchen switch`.

9. **Climate conflict smell**
   - Midea AC is in `cool` mode while thermostats are still in `heat` mode.
   - Current temperatures are above target, so this may not currently waste heat, but the model should be explicit via seasonal mode or conflict guard.

10. **Door lock safety**
    - `lock.home_door` was `unlocked` during analysis.
    - Add alerting/automation for unlocked too long and unlocked while away.

---

## Proposed Observability Design

### Metrics path

1. Enable Home Assistant Prometheus integration.
2. Expose HA metrics at:

```text
https://ha.ravil.space/api/prometheus
```

3. Store a dedicated HA long-lived token as a Komodo secret/variable for `grafana-lgtm`.
4. Mount the token into the Grafana LGTM container as a file.
5. Add a Prometheus scrape job in:

```text
komodo/stacks/grafana-lgtm/prometheus.yaml
```

6. Add dashboards under:

```text
komodo/stacks/grafana-lgtm/dashboards/
```

7. Add Grafana provisioning under:

```text
komodo/stacks/grafana-lgtm/provisioning/
```

### Why scrape HA instead of pushing metrics

- HA already owns entity state and device metadata.
- Prometheus scrape is simple, pull-based, and fits the existing `grafana-lgtm` stack.
- Battery/unavailable/update metrics become queryable and alertable without writing custom exporters.

---

## Implementation Tasks

### Task 1: Enable Prometheus in Home Assistant

**Objective:** Make HA expose Prometheus metrics.

**Files:**
- Modify in HA config, likely `/config/configuration.yaml` on HAOS.
- No repository file unless HA config is later moved into GitOps.

**Config:**

```yaml
prometheus:
  namespace: homeassistant
  requires_auth: true
```

**Steps:**

1. Add the config in HA.
2. Restart Home Assistant.
3. Create a dedicated long-lived access token, e.g. `grafana-prometheus-scrape`.
4. Verify from a trusted machine:

```bash
curl -fsS \
  -H "Authorization: Bearer $HA_PROMETHEUS_TOKEN" \
  https://ha.ravil.space/api/prometheus | head
```

**Expected:** Prometheus text format, containing metrics with names beginning with `homeassistant_`.

**Notes:**
- Do not reuse the general Hermes HA token.
- Keep this token scoped operationally by naming and storage, even if HA long-lived tokens are broad.

---

### Task 2: Add HA scrape target to Grafana LGTM Prometheus

**Objective:** Let the existing Grafana LGTM stack scrape HA metrics.

**Files:**
- Modify: `komodo/stacks/grafana-lgtm/compose.yaml`
- Modify: `komodo/stacks/grafana-lgtm/prometheus.yaml`

**Compose additions:**

Mount a token file into the `lgtm` service:

```yaml
services:
  lgtm:
    volumes:
      - ./secrets/ha-prometheus-token:/run/secrets/ha-prometheus-token:ro
```

If Komodo secrets cannot materialize as files for this stack, use a local `.env` variable or a generated file in the stack run directory, but prefer file-based `authorization.credentials_file` to avoid exposing the token in process args or config exports.

**Prometheus scrape config:**

```yaml
scrape_configs:
  - job_name: "cadvisor"
    static_configs:
      - targets: ["cadvisor:8080"]

  - job_name: "home-assistant"
    metrics_path: /api/prometheus
    scheme: https
    authorization:
      type: Bearer
      credentials_file: /run/secrets/ha-prometheus-token
    static_configs:
      - targets: ["ha.ravil.space"]
        labels:
          service: home-assistant
          site: ravil-home
```

**Verification:**

```bash
ssh 192.168.1.166 'cd /etc/komodo/stacks/grafana-lgtm/komodo/stacks/grafana-lgtm && docker compose up -d'
ssh 192.168.1.166 'docker logs grafana-lgtm --tail 100'
```

Then check in Grafana Explore / Prometheus:

```promql
up{job="home-assistant"}
```

Expected: `1`.

**Important:** After deploy, verify Traefik access logs for `grafana.ravil.space` if accessing the UI through the public route.

---

### Task 3: Identify exact HA metric names

**Objective:** Avoid writing alerts against guessed metric names.

**Files:**
- No files initially.
- Create notes in this plan or in a dashboard README after discovery.

**Commands:**

```bash
curl -fsS \
  -H "Authorization: Bearer $HA_PROMETHEUS_TOKEN" \
  https://ha.ravil.space/api/prometheus > /tmp/ha.prom

grep -E 'battery|unavailable|home_door|update|automation|climate' /tmp/ha.prom | head -100
```

**PromQL discovery examples:**

```promql
{job="home-assistant", entity=~"sensor.*battery.*"}
{job="home-assistant", entity=~"lock.home_door"}
{job="home-assistant", entity=~"update.*"}
```

**Expected:** Document the actual metric names/labels before finalizing alert expressions. Home Assistant metric names vary by entity domain, device class, unit, and integration.

---

### Task 4: Add HA health dashboard

**Objective:** One dashboard for smart-home operational health.

**Files:**
- Create: `komodo/stacks/grafana-lgtm/dashboards/home-assistant-health.json`
- Existing provisioning should auto-load dashboards from `/dashboards`.

**Panels:**

1. HA scrape status: `up{job="home-assistant"}`
2. Low batteries: sensors with battery value under thresholds.
3. Unavailable/unknown entities count.
4. Door lock state.
5. Available updates.
6. Automation last-triggered or disabled/unavailable automations if available in metrics.
7. Climate state overview: Midea AC + thermostats.

**Dashboard variables:**

- `entity`
- `area` if HA exports area labels or if relabeled later.
- `domain`

**Verification:**

- Open `https://grafana.ravil.space`.
- Confirm dashboard loads via provisioning.
- Confirm panels show real HA data.
- Check Traefik access logs, not only browser/curl.

---

### Task 5: Add Grafana alerts for batteries

**Objective:** Alert before Zigbee/Matter devices die.

**Files:**
- Prefer Grafana provisioned alert rules under `komodo/stacks/grafana-lgtm/provisioning/alerting/`.
- Exact file depends on existing Grafana provisioning conventions.

**Initial policy:**

- Warning: battery <= 25% for 30 minutes.
- Critical: battery <= 10% for 15 minutes.
- Ignore phone/watch batteries unless explicitly included.
- Include Zigbee/Nuki/Matter sensors.

**PromQL template:**

```promql
# Replace metric name after Task 3 discovery.
homeassistant_sensor_battery_percent{job="home-assistant", entity!~"sensor.pixel_.*|sensor.*watch.*"} <= 25
```

**Alert annotations:**

```yaml
summary: "HA battery low: {{ $labels.friendly_name | default $labels.entity }}"
description: "Battery is {{ $values.A.Value }}%. Replace battery for {{ $labels.entity }}."
```

**Current known battery priorities:**

- `sensor.0x583bc2fffe010b08_battery` / Door motion: 23%
- `sensor.0x8c65a3fffe6ee0d3_battery` / Bathroom motion: 32%
- `sensor.0xd44867fffe5aab6a_battery` / Kitchen motion: 35%
- `sensor.0x8c65a3fffe3e244a_battery` / Vanna motion senser: 40%

---

### Task 6: Add alerts for unavailable critical entities

**Objective:** Catch broken integrations before noticing manually.

**Critical entities:**

```text
lock.home_door
climate.midea_ac_153931629509942
climate.0xa4c138f46d5f9432
climate.0xa4c1385e612c7b9a
climate.0xa4c13876a0af4564
fan.tower_fan
person.ravil
```

**Alert examples:**

- `lock.home_door` unavailable for > 5 minutes.
- `person.ravil` unknown for > 30 minutes.
- Any climate entity unavailable for > 15 minutes.
- `fan.tower_fan` unavailable for > 1 hour, if the fan is still used.

**PromQL template:**

```promql
# Replace metric/state encoding after Task 3 discovery.
homeassistant_entity_available{job="home-assistant", entity=~"lock.home_door|climate.*|person.ravil"} == 0
```

If HA Prometheus does not expose availability cleanly, use HA-side automations for unavailable states and expose them as helper sensors, or create a small exporter that polls `/api/states` and emits normalized metrics.

---

### Task 7: Add door lock safety alerts/automations

**Objective:** Avoid leaving the apartment unlocked accidentally.

**Recommended split:**

- HA automation: immediate smart-home action/notification.
- Grafana alert: observability/history and escalation.

**HA automations:**

1. Door unlocked for > 10 minutes → Telegram/HA mobile notification.
2. Door unlocked while `person.ravil != home` → high-priority notification.
3. Optional: Nuki battery < 25% → notification.

**Grafana alerts:**

- Door unlocked for > 15 minutes.
- Door lock entity unavailable for > 5 minutes.

**Current observed state:** `lock.home_door` was `unlocked` during analysis.

---

### Task 8: Add climate conflict guard

**Objective:** Prevent cooling and heating modes from fighting each other.

**HA helper:**

```yaml
input_select:
  home_season_mode:
    name: Home season mode
    options:
      - summer
      - winter
      - shoulder
```

**Rules:**

- If `home_season_mode == summer` and Midea AC is `cool`, set thermostats to `off` or eco.
- If any thermostat is actively heating while AC is cooling, notify or turn off heat.
- If windows/doors are later added as sensors, stop HVAC when open for > N minutes.

**Grafana panel:**

- AC mode, target/current temp.
- Thermostat modes, target/current temps.
- Conflict boolean helper if added.

---

### Task 9: Clean up HA configuration

**Objective:** Reduce noise and make dashboards/alerts meaningful.

**Actions:**

1. Remove or fix tablet socket automations with unknown device IDs.
2. Fix `person.ravil` tracker mapping.
3. Remove duplicate/old mobile app devices.
4. Decide whether Fire Tablet/Fully Kiosk is still used; fix or remove.
5. Decide whether Dreo Tower Fan is still used; fix or remove.
6. Re-auth Google Calendar if needed.
7. Fix/disable Berlin Transport sensors if not used.
8. Rename confusing automations:
   - `automation.test` → meaningful entity/name.
   - `automation.living_room_sofa_switch` currently named `Kitchen switch`.
9. Assign devices to areas. Current area assignment is only 11/65 devices.

---

## Alert Contact Points

Preferred delivery options:

1. Grafana contact point → Telegram bot/home chat, if already configured.
2. Grafana contact point → webhook into Hermes gateway, if Telegram contact point is not available.
3. HA native notifications for local smart-home events requiring immediate action.

**Routing policy:**

- P0/security: door unlocked away, lock unavailable, HA scrape down.
- P1/maintenance: battery critical, HVAC conflict, critical integration unavailable.
- P2/noise: transport/calendar/tablet/fan unavailable unless actively used.

---

## Verification Checklist

Before calling this done:

- [ ] `curl https://ha.ravil.space/api/prometheus` with token returns metrics.
- [ ] `up{job="home-assistant"}` is `1` in Grafana/Prometheus.
- [ ] Battery metrics are visible and actual metric names are documented.
- [ ] Dashboard `Home Assistant Health` is provisioned and renders real data.
- [ ] Battery warning alert fires in test mode for the current Door motion 23% sensor.
- [ ] Door lock alert is tested with a safe synthetic/evaluation query, not by leaving the door unlocked.
- [ ] HA repairs list no longer contains the tablet socket unknown-device errors, or they are intentionally removed.
- [ ] Traefik access logs for `grafana.ravil.space` checked after deploy.

---

## Suggested Implementation Order

1. HA cleanup P0: tablet socket repair errors + `person.ravil` tracker.
2. Enable HA Prometheus endpoint.
3. Add Grafana scrape config and verify `up`.
4. Discover exact metric names.
5. Add battery dashboard + battery alerts.
6. Add door lock dashboard + alerts.
7. Add unavailable critical entities alerts.
8. Add climate conflict guard.
9. Clean up non-critical integrations and stale automations.
