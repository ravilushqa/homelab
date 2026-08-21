# Guest Door Access

Small public web app for HomeExchange guests at:

```text
https://guest-access.ravil.space/#<GUEST_DOOR_ACCESS_TOKEN>
```

The guest access token stays in the browser URL fragment, so it is not sent in the HTTP request path or query to this server or Traefik. The page never receives the Home Assistant token. It only posts the guest access token and required PIN back to this server, which then calls Home Assistant.

## Komodo Variables

Required:

- `GUEST_DOOR_HA_TOKEN`: Home Assistant long-lived access token with permission to press the configured entities.
- `GUEST_DOOR_ACCESS_TOKEN`: unguessable fragment token shared with the guest.
- `GUEST_DOOR_PIN`: PIN required by the web page and API.

Optional:

- `GUEST_DOOR_ACCESS_START`: ISO datetime when access starts. Empty means no lower bound.
- `GUEST_DOOR_ACCESS_END`: ISO datetime when access ends. Empty means no upper bound.

## Home Assistant Entities

Defaults baked into the app:

- Building entrance / Comelit: `button.comelit_default_open_door` via `button.press`
- Apartment / Nuki: `button.home_door_unlatch` via `button.press`

`lock.home_door` exists, but the apartment action intentionally uses the Nuki unlatch button by default.

The app also supports these environment overrides: `BUILDING_ENTITY`, `BUILDING_DOMAIN`, `BUILDING_SERVICE`, `APARTMENT_ENTITY`, `APARTMENT_DOMAIN`, and `APARTMENT_SERVICE`.

## Verification

Health check:

```sh
curl -i https://guest-access.ravil.space/health
```

Fetch guest page:

```sh
curl -i "https://guest-access.ravil.space/"
```

Trigger building door:

```sh
curl -i -X POST https://guest-access.ravil.space/api/open/building \
  -H 'Content-Type: application/json' \
  -d '{"token":"'"$ACCESS_TOKEN"'","pin":"'"$PIN"'"}'
```

Trigger apartment door:

```sh
curl -i -X POST https://guest-access.ravil.space/api/open/apartment \
  -H 'Content-Type: application/json' \
  -d '{"token":"'"$ACCESS_TOKEN"'","pin":"'"$PIN"'"}'
```
