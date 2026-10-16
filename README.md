# 3DS HomeAssistant Local Agent bridge

A small authenticated HTTP API that sits between a Nintendo 3DS
homebrew client and Home Assistant, so a game-like 3DS app can show live
house state, toggle real devices, control media and PCs, request music,
and chat with a local Muse agent — while every Home Assistant call stays
on the server side.

The bridge runs in production on my home server, serving the 3DS client
daily. It is a single Python file (`bridge.py`, standard library only)
speaking JSON over HTTP/1.1, with a tiny companion script
(`agent-reply.py`) that the local agent uses to answer.

## How it fits together

```
Nintendo 3DS client ──HTTP + shared key──▶ bridge.py ──▶ ha-api ──▶ Home Assistant REST API
   (homebrew, libcurl)                       │
                                             ├─▶ musegadget send-user-msg ──▶ Muse (chat)
                                             └─▶ replies.jsonl ◀── agent-reply.py ◀── Muse
```

The 3DS never holds a Home Assistant token, and the bridge never holds
one either: HA access goes through a small helper (`ha-api.example.sh`)
that reads a long-lived token from a mode-600 file and passes it to curl
via stdin, so the token never appears in process arguments.

Chat rides Meta's Muse Gadget pattern instead of a custom channel: the
bridge runs on a Linux box paired as a Muse gadget, `POST /v1/chat`
forwards the message with `musegadget send-user-msg`, and the assistant
appends replies to `replies.jsonl` (via `agent-reply.py`), which the
client polls with `GET /v1/reply?after=N`.

## Endpoints

All `/v1/*` endpoints require an `X-Bridge-Key` header matching the bridge
secret file.

| Endpoint | What it does |
|---|---|
| `GET /health` | `{"ok": true}` (unauthenticated) |
| `GET /v1/state` | Rooms and devices with live states |
| `GET /v1/worldstate` | Logical interactable states (lights, switches, TV, PCs, lock, vacuum) + thermostat temperature |
| `POST /v1/toggle` | `{"device": "<id>"}` — toggle a configured device's entities |
| `POST /v1/interact` | `{"entity": "<logical id>"}` — toggle a switch group, lock, vacuum, TV or light; returns a fresh worldstate |
| `POST /v1/music` | `{"text": "...", "target": "<optional>"}` — Music Assistant search, then play the first result |
| `POST /v1/media` | `{"device": "...", "cmd": "power\|play_pause\|volume_up\|volume_down"}` |
| `POST /v1/pc` | `{"pc": "<id>", "action": "on\|off"}` — start (Wake-on-LAN script) / graceful shutdown script |
| `POST /v1/chat` | `{"text": "..."}` — forward to the local agent |
| `GET /v1/reply?after=N` | Agent replies with id > N |
| `GET /dl/<file>` | Client binary downloads (`.3dsx` / `.cia` only) |

## Design notes from production

- **Logical ids, honest mapping.** The client talks in logical ids
  ("the kitchen switch"); the config maps them to grouped HA entities.
  A logical id with no real entity reports `mapped: false` — the bridge
  never invents an entity to fill a gap.
- **Group state aggregation.** A switch is *on* if any mapped member is
  on; an *unavailable* member casts no vote instead of dragging the
  whole group to *unknown* (powered-down bulbs taught this the hard
  way). HA state is cached for 3 seconds and invalidated after actions,
  with a short settle delay before re-reading.
- **PC state from telemetry, not plugs.** A smart plug can read "off"
  while the PC is on, so PC state comes from an uptime telemetry
  sensor: numeric uptime plus a telemetry timestamp fresh within
  180 s means *on*; numeric-but-stale means *off*; anything missing is
  *unknown*. `/v1/pc` re-derives state at fire time and refuses
  redundant or blind actions (`already_on`, `already_off`,
  `state_unknown`).
- **Music fallback.** If Music Assistant search fails, the request is
  forwarded to the local agent as a chat message rather than failing
  silently.
- **Downloads are an allowlist.** Only client binaries are served from
  `/dl/`; a client config file is never served (it can embed the bridge
  key).

## Configuration

Everything house-specific lives in `config.json` (see
`config.example.json`): devices, logical light groups, switch groups,
media players, locks, vacuums, PCs, the Music Assistant default target,
and an optional presence passthrough. Environment overrides:
`BRIDGE_PORT`, `BRIDGE_CONFIG`, `HA_API_BIN`, `MUSEGADGET_BIN`.

Two files beside `bridge.py` are runtime state and are never committed:
`secret.txt` (the shared key) and `session_id.txt` (the Muse gadget
session id for chat).

## Running it

```bash
cp config.example.json config.json   # replace the entity ids with yours
printf 'choose-a-long-random-key' > secret.txt && chmod 600 secret.txt
printf 'your-gadget-session-id' > session_id.txt   # only needed for chat
HA_API_BIN=/path/to/ha-api python3 bridge.py
curl localhost:8377/health
```

A systemd unit example is in `bridge.service.example`.

## The client

The 3DS client (a homebrew app with a walk-around house map) is not in
this repository; this is the server half it talks to. The protocol
above is the whole contract.

## What changed for publication

This is the production bridge with the house taken out of the code: the
device/switch/lock/PC mappings were literals in the running copy and are
configuration here, the few one-house quirks were generalized, and the
download allowlist no longer includes any config file. Endpoint
behavior and the state logic are unchanged.

## License

MIT — see `LICENSE`.
