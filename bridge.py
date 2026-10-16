#!/usr/bin/env python3
"""3DS Seb Home bridge.

Thin, authenticated HTTP API between a Nintendo 3DS homebrew client and
Home Assistant, with a chat path to a Muse gadget assistant:

  GET  /health            -> {"ok": true}
  GET  /v1/state          -> rooms + devices with live states (via ha-api)
  GET  /v1/worldstate     -> logical interactable states + thermostat temp
  POST /v1/interact       -> {"entity": "<logical id>"} toggles its HA
                             entities, returns a fresh worldstate payload
  POST /v1/toggle         -> {"device": "<id>"} toggles that device
  POST /v1/chat           -> {"text": "..."} forwards to the assistant via
                             `musegadget send-user-msg --session-id <uuid>`
  GET  /v1/reply?after=N  -> assistant replies appended by seb-reply.py
  POST /v1/music          -> {"text": "...", "target": "<optional entity>"}
                             Music Assistant search + play_media
  POST /v1/media          -> {"device": "...", "cmd": "power|..."}
  POST /v1/pc             -> {"pc": "<id>", "action": "on|off"} start /
                             shutdown scripts; state from uptime telemetry

All /v1/* endpoints require header X-Seb-Key matching the secret file.
No Home Assistant token lives here; HA access goes through a small
ha-api helper (example included) that holds the token.

Every house-specific mapping (devices, logical lights, switches, locks,
PCs, media players) lives in config.json — see config.example.json.
This file is the publishable edition of a bridge running in production;
the production copy carries the author's real config, which is not
published.
"""
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE = Path(__file__).resolve().parent
SECRET = (BASE / "secret.txt").read_text().strip()
CONFIG_PATH = Path(os.environ.get("BRIDGE_CONFIG", BASE / "config.json"))
CONFIG = {}
if CONFIG_PATH.exists():
    CONFIG = json.loads(CONFIG_PATH.read_text())
SESSION_ID = (BASE / "session_id.txt").read_text().strip()
CHAT_LOG = BASE / "chat_log.jsonl"
REPLIES = BASE / "replies.jsonl"
PORT = int(os.environ.get("BRIDGE_PORT", "8377"))

DEVICES = [(d["id"], d["room"], d["name"], list(d["entities"])
            ) for d in CONFIG.get("devices", [])]
DEVICE_BY_ID = {d[0]: d for d in DEVICES}
ALL_ENTITIES = sorted({e for d in DEVICES for e in d[3]})

# --- /v1/worldstate + /v1/interact mapping ---------------------------
# Logical ids (what the game/app shows) -> the real HA entities behind
# them. A logical id with no real HA entity belongs in light_unmapped and
# honestly reports mapped:false — never invent an entity.
LIGHT_MAP = {k: list(v) for k, v in CONFIG.get("light_map", {}).items()}
LIGHT_UNMAPPED = list(CONFIG.get("light_unmapped", []))
# Wall switches: switch id -> grouped logical light/lamp ids.
# Group semantics: state is "on" if any mapped member is on; a toggle
# turns every mapped member off when any is on, else turns them all on.
SWITCHES = {k: list(v) for k, v in CONFIG.get("switches", {}).items()}
# Direct (non-switch) interactables (media players).
DIRECT_MAP = {k: list(v) for k, v in CONFIG.get("direct_map", {}).items()}
DIRECT_UNMAPPED = list(CONFIG.get("direct_unmapped", []))
# Optional second entity for the main TV that accepts volume commands
# when the primary streamer does not (may be null).
TV_VOLUME_ENTITY = CONFIG.get("tv_volume_entity")
# PCs: status truth is the uptime telemetry sensor (numeric seconds =
# on; unavailable/unknown = unknown), never a smart-plug switch, which
# can read "off" while the PC is on. On = Wake-on-LAN start script,
# off = graceful shutdown script.
PC_MAP = CONFIG.get("pcs", {})
# PC case/RGB lights with no HA entity stay honestly mapped:false.
CASE_UNMAPPED = list(CONFIG.get("case_unmapped", []))
LOCK_MAP = dict(CONFIG.get("locks", {}))
VACUUM_MAP = dict(CONFIG.get("vacuums", {}))
# Music Assistant: default player for /v1/music.
MA_DEFAULT_TARGET = CONFIG.get("music_default_target", "")
_MA_ENTITY_RE = re.compile(r"^media_player\.[a-z0-9_]+$")
_ma_entry_cache = {"id": None}
_ON_STATES = {"on", "playing", "paused", "idle"}  # idle = powered media_player

_cache = {"ts": 0.0, "states": {}}
_cache_lock = threading.Lock()


def ha_api(method, path, data=None):
    cmd = [os.environ.get("HA_API_BIN", str(Path.home() / "bin" / "ha-api")), method, path]
    if data is not None:
        cmd.append(json.dumps(data))
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(f"ha-api failed: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout) if out.stdout.strip() else {}


def live_states():
    with _cache_lock:
        if time.time() - _cache["ts"] < 3 and _cache["states"]:
            return _cache["states"]
        states = {e["entity_id"]: e.get("state", "unknown") for e in ha_api("GET", "/api/states")}
        _cache.update(ts=time.time(), states=states)
        return states


def _full_states():
    """Full state entries (state + attributes), short-cached like live_states."""
    with _cache_lock:
        if time.time() - _cache.get("fts", 0) < 3 and _cache.get("full"):
            return _cache["full"]
        full = {e["entity_id"]: e for e in ha_api("GET", "/api/states")}
        _cache.update(fts=time.time(), full=full)
        return full


def _aggregate(vals):
    # An "unavailable" member (HA cannot reach the device right now)
    # casts no vote. On beats off; any mapped member reporting off with
    # none on reads "off"; only when no member reports a real state do
    # we say "unknown". (Learned in production: powered-down bulbs used
    # to make a whole switch read UNKNOWN even though its lamps were off.)
    if any(v in _ON_STATES for v in vals):
        return "on"
    if any(v == "off" for v in vals):
        return "off"
    return "unknown"


def _logical_state(full, logical):
    vals = [full.get(e, {}).get("state", "unavailable")
            for e in LIGHT_MAP[logical]]
    return _aggregate(vals)


def _invalidate_cache():
    _cache["ts"] = 0.0
    _cache["fts"] = 0.0


def _pc_state(full, pc):
    """PC on/off truth from uptime telemetry, never switch.*_s_pc.

    The uptime sensor is push telemetry: when a PC shuts down, HA keeps
    the last number forever, so a numeric uptime alone reads "on"
    indefinitely. "on" requires the companion telemetry_updated
    timestamp to be fresh (<180 s; it advances ~every 30 s while the
    PC is up). Numeric-but-stale -> "off"; anything missing or
    unparseable -> "unknown".
    """
    entry = full.get(PC_MAP[pc]["uptime"])
    if not entry:
        return "unknown"
    raw = entry.get("state", "unknown")
    try:
        float(raw)
    except (TypeError, ValueError):
        return "unknown"
    tele = full.get(PC_MAP[pc].get("telemetry", ""))
    if not tele:
        return "unknown"
    try:
        ts = datetime.fromisoformat(str(tele.get("state", "")).replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return "on" if age < 180 else "off"


def _volume_of(full, entity_id):
    vol = full.get(entity_id, {}).get("attributes", {}).get("volume_level")
    if isinstance(vol, bool) or not isinstance(vol, (int, float)):
        return None
    return float(vol)


def worldstate_payload():
    full = _full_states()
    states = {}
    # Flattened logical light/lamp entries (lamp art + switch members read
    # these directly in the app).
    for logical in LIGHT_MAP:
        states[logical] = {"state": _logical_state(full, logical), "mapped": True}
    for logical in LIGHT_UNMAPPED:
        states[logical] = {"state": "unknown", "mapped": False}
    # One entry per wall switch: group state + member detail.
    for switch, members in SWITCHES.items():
        member_states = {}
        mapped_any = False
        any_on = False
        any_off = False
        for logical in members:
            if logical in LIGHT_MAP:
                st = _logical_state(full, logical)
                member_states[logical] = {"state": st, "mapped": True}
                mapped_any = True
                # v1.2.21 (HW item I): an unknown/unreachable member no
                # longer vetoes the off reading of members that do
                # report (see _aggregate).
                if st == "on":
                    any_on = True
                elif st == "off":
                    any_off = True
            else:
                member_states[logical] = {"state": "unknown", "mapped": False}
        state = "on" if any_on else ("off" if (mapped_any and any_off) else "unknown")
        states[switch] = {"state": state, "mapped": mapped_any,
                          "members": member_states}
    for entity in DIRECT_MAP:
        vals = [full.get(e, {}).get("state", "unavailable")
                for e in DIRECT_MAP[entity]]
        states[entity] = {"state": _aggregate(vals), "mapped": True}
    # tv_family (v1.2.9): also expose the streamer's real media state and a
    # volume level (primary entity first, MA shadow player as fallback) so
    # the app's TV menu does not need extra round-trips. Additive only.
    _tv_id = CONFIG.get("main_tv", "tv_family")
    tv_entry = full.get(DIRECT_MAP[_tv_id][0], {}) if _tv_id in DIRECT_MAP else {}
    tv_volume = _volume_of(full, DIRECT_MAP[_tv_id][0]) if _tv_id in DIRECT_MAP else None
    if tv_volume is None and TV_VOLUME_ENTITY:
        tv_volume = _volume_of(full, TV_VOLUME_ENTITY)
    if _tv_id in states:
        states[_tv_id].update({
            "media_state": tv_entry.get("state", "unknown"),
            "volume_level": tv_volume,
        })
    for entity in DIRECT_UNMAPPED:
        states[entity] = {"state": "unknown", "mapped": False}
    # PCs + case lights (v1.2.9).
    for pc in PC_MAP:
        states[f"pc_{pc}"] = {"state": _pc_state(full, pc), "mapped": True}
    for logical in CASE_UNMAPPED:
        states[logical] = {"state": "unknown", "mapped": False}
    # Lock: keep the honest locked/unlocked vocabulary (the app special-
    # cases this one); anything else collapses to unknown.
    for logical, entity_id in LOCK_MAP.items():
        raw = full.get(entity_id, {}).get("state", "unknown")
        states[logical] = {
            "state": raw if raw in ("locked", "unlocked") else "unknown",
            "mapped": True}
    # Vacuum: state passthrough (docked / cleaning / returning / ...).
    for logical, entity_id in VACUUM_MAP.items():
        raw = full.get(entity_id, {}).get("state", "unknown")
        states[logical] = {
            "state": "unknown" if raw in ("unavailable", "") else raw,
            "mapped": True}
    # Optional presence passthrough (e.g. a partner's office occupancy
    # sensor, so the app can show an NPC at their desk). Not a
    # controllable device: mapped stays False.
    _presence = CONFIG.get("presence")
    if _presence:
        raw = full.get(_presence["entity"], {}).get("state", "unknown")
        states[_presence["key"]] = {
            "state": raw if raw in ("on", "off") else "unknown",
            "mapped": False}
    temp = None
    for eid, entry in full.items():
        if eid.startswith("climate."):
            attrs = entry.get("attributes", {})
            t = attrs.get("current_temperature", attrs.get("temperature"))
            if isinstance(t, (int, float)):
                temp = t
            break
    return {"states": states, "thermostat": {"temp": temp}}


def _set_group(members_on):
    """Turn every HA entity behind the given logical ids on or off."""
    targets = []
    for logical, on in members_on:
        targets.extend(LIGHT_MAP[logical])
    service = "turn_on" if all(on for _l, on in members_on) else "turn_off"
    ha_api("POST", f"/api/services/homeassistant/{service}",
           {"entity_id": targets})


def interact_payload(entity):
    """POST /v1/interact semantics; returns (http_code, payload)."""
    if entity in SWITCHES:
        mapped_members = [m for m in SWITCHES[entity] if m in LIGHT_MAP]
        if not mapped_members:
            return 200, {"ok": False, "mapped": False}
        full = _full_states()
        any_on = any(_logical_state(full, m) == "on" for m in mapped_members)
        _set_group([(m, not any_on) for m in mapped_members])
        _cache["ts"] = 0.0
        _cache["fts"] = 0.0
        time.sleep(0.6)
        payload = worldstate_payload()
        payload.update({"ok": True, "mapped": True,
                        "state": payload["states"][entity]["state"]})
        return 200, payload
    if entity in LOCK_MAP:
        # Toggle based on current state; the app does the A/B confirm, the
        # bridge just executes the matching lock service.
        full = _full_states()
        current = full.get(LOCK_MAP[entity], {}).get("state", "unknown")
        if current == "locked":
            service = "unlock"
        elif current == "unlocked":
            service = "lock"
        else:
            return 200, {"ok": False, "mapped": True,
                         "error": "lock unavailable", "state": current}
        ha_api("POST", f"/api/services/lock/{service}",
               {"entity_id": LOCK_MAP[entity]})
        _invalidate_cache()
        time.sleep(0.6)
        payload = worldstate_payload()
        payload.update({"ok": True, "mapped": True,
                        "state": payload["states"][entity]["state"]})
        return 200, payload
    if entity in VACUUM_MAP:
        full = _full_states()
        current = full.get(VACUUM_MAP[entity], {}).get("state", "unknown")
        if current not in ("docked", "idle"):
            return 200, {"ok": False, "mapped": True,
                         "error": f"vacuum is {current}", "state": current}
        ha_api("POST", "/api/services/vacuum/start",
               {"entity_id": VACUUM_MAP[entity]})
        _invalidate_cache()
        time.sleep(0.6)
        payload = worldstate_payload()
        payload.update({"ok": True, "mapped": True,
                        "state": payload["states"][entity]["state"]})
        return 200, payload
    if entity in DIRECT_MAP:
        ha_api("POST", "/api/services/homeassistant/toggle",
               {"entity_id": DIRECT_MAP[entity]})
        _cache["ts"] = 0.0
        _cache["fts"] = 0.0
        time.sleep(0.6)
        return 200, worldstate_payload()
    if entity in LIGHT_MAP:
        ha_api("POST", "/api/services/homeassistant/toggle",
               {"entity_id": LIGHT_MAP[entity]})
        _cache["ts"] = 0.0
        _cache["fts"] = 0.0
        time.sleep(0.6)
        return 200, worldstate_payload()
    return 200, {"ok": False, "mapped": False}


def _send_seb(message):
    return subprocess.run(
        [os.environ.get("MUSEGADGET_BIN", "/usr/local/bin/musegadget"), "send-user-msg",
         "--session-id", SESSION_ID, message],
        capture_output=True, text=True, timeout=30)


def _forward_music_to_seb(text):
    """Music fallback: hand the request to Seb in Muse ([3DS-MUSIC])."""
    mid = next_id(CHAT_LOG)
    append_jsonl(CHAT_LOG, {"id": mid, "ts": time.time(),
                            "text": f"[3DS-MUSIC] {text}"})
    try:
        return _send_seb(f"[3DS-MUSIC] {text}").returncode == 0
    except Exception:
        return False


def _ma_entry_id():
    if _ma_entry_cache["id"]:
        return _ma_entry_cache["id"]
    for entry in ha_api("GET", "/api/config/config_entries/entry"):
        if entry.get("domain") == "music_assistant" \
                and entry.get("state") == "loaded":
            _ma_entry_cache["id"] = entry["entry_id"]
            return entry["entry_id"]
    raise RuntimeError("music assistant not available")


def _music_error(text, exc):
    forwarded = _forward_music_to_seb(text)
    return 200, {"ok": False, "forwarded_to_seb": forwarded,
                 "error": str(exc)[:200] or "music assistant unavailable"}


def music_payload(body):
    """POST /v1/music: MA search, then play_media on the target player.

    NOTE (2026-10-04): this MA version rejects any search_options block
    with HTTP 400, so none is sent; search depth is the server default.
    Response items carry their playable id in "uri".
    """
    text = (body.get("text") or "").strip() \
        if isinstance(body.get("text"), str) else ""
    if not text:
        return 400, {"ok": False, "error": "empty text"}
    target = body.get("target")
    if target is None or target == "":
        target = MA_DEFAULT_TARGET
    if not isinstance(target, str) or not _MA_ENTITY_RE.match(target):
        return 400, {"ok": False, "error": "invalid target"}
    try:
        result = ha_api(
            "POST", "/api/services/music_assistant/search?return_response=true",
            {"config_entry_id": _ma_entry_id(), "name": text,
             "media_type": ["track", "album", "playlist", "artist",
                            "radio", "audiobook", "podcast"]})
    except Exception as exc:
        return _music_error(text, exc)
    response = result.get("service_response", {}) if isinstance(result, dict) else {}
    item = None
    for key in ("tracks", "albums", "playlists", "artists", "radio",
                "audiobooks", "podcasts"):
        items = response.get(key) or []
        if items:
            item = items[0]
            break
    if not item or not item.get("uri"):
        return 200, {"ok": False, "error": "not found"}
    play_data = {"entity_id": target, "media_id": item["uri"],
                 "enqueue": "play"}
    if item.get("media_type"):
        play_data["media_type"] = item["media_type"]
    try:
        ha_api("POST", "/api/services/music_assistant/play_media", play_data)
    except Exception as exc:
        return _music_error(text, exc)
    _invalidate_cache()
    return 200, {"ok": True, "playing": item.get("name") or item["uri"],
                 "target": target}


_MEDIA_CMDS = {"power", "play_pause", "volume_up", "volume_down"}


def media_payload(body):
    """POST /v1/media: power / play-pause / volume for tv_family."""
    device = body.get("device", "")
    cmd = body.get("cmd", "")
    if device == "tv_master":
        return 200, {"ok": False, "error": "Nothing smart here yet."}
    if device not in DIRECT_MAP:
        return 400, {"ok": False, "error": "unknown device"}
    if cmd not in _MEDIA_CMDS:
        return 400, {"ok": False, "error": "unknown cmd"}
    primary = DIRECT_MAP[device][0]
    full = _full_states()
    entry = full.get(primary)
    if not entry or entry.get("state") in ("unavailable", "unknown"):
        return 200, {"ok": False, "error": "TV unavailable"}
    current = entry.get("state", "unknown")
    try:
        if cmd == "power":
            service = "turn_on" if current == "off" else "turn_off"
            ha_api("POST", f"/api/services/media_player/{service}",
                   {"entity_id": primary})
        elif cmd == "play_pause":
            ha_api("POST", "/api/services/media_player/media_play_pause",
                   {"entity_id": primary})
        else:  # volume: route to the entity that accepts VOLUME_STEP
            vol_entity = TV_VOLUME_ENTITY or primary
            vol_entry = full.get(vol_entity)
            if not vol_entry or vol_entry.get("state") in ("unavailable",
                                                           "unknown"):
                vol_entity = primary
            ha_api("POST", f"/api/services/media_player/{cmd}",
                   {"entity_id": vol_entity})
    except Exception as exc:
        return 200, {"ok": False, "error": str(exc)[:200]}
    _invalidate_cache()
    time.sleep(0.6)
    full = _full_states()
    new_entry = full.get(primary, {})
    volume = _volume_of(full, primary)
    if volume is None and TV_VOLUME_ENTITY:
        volume = _volume_of(full, TV_VOLUME_ENTITY)
    return 200, {"ok": True, "state": new_entry.get("state", current),
                 "volume": volume}


def pc_payload(body):
    """POST /v1/pc: start (WoL script) / shutdown (graceful script)."""
    pc = body.get("pc", "")
    action = body.get("action", "")
    if pc not in PC_MAP:
        return 400, {"ok": False, "error": "unknown pc"}
    if action not in ("on", "off"):
        return 400, {"ok": False, "error": "unknown action"}
    state_before = _pc_state(_full_states(), pc)
    # Guard (2026-10-05): never fire a redundant or blind action. The
    # state is re-derived here, at fire time, not trusted from the app.
    if action == "off" and state_before == "off":
        return 200, {"ok": False, "error": "already_off", "pc": pc,
                     "state": state_before}
    if action == "off" and state_before == "unknown":
        return 200, {"ok": False, "error": "state_unknown", "pc": pc,
                     "state": state_before}
    if action == "on" and state_before == "on":
        return 200, {"ok": False, "error": "already_on", "pc": pc,
                     "state": state_before}
    script = PC_MAP[pc]["start" if action == "on" else "shutdown"]
    try:
        ha_api("POST", "/api/services/script/turn_on",
               {"entity_id": script})
    except Exception as exc:
        return 200, {"ok": False, "error": str(exc)[:200]}
    _invalidate_cache()
    time.sleep(0.6)
    return 200, {"ok": True, "pc": pc, "action": action,
                 "state_before": state_before,
                 "state_after": _pc_state(_full_states(), pc)}


def state_payload():
    states = live_states()
    rooms = {}
    for dev_id, room, name, entities in DEVICES:
        vals = [states.get(e, "unavailable") for e in entities]
        state = "on" if any(v == "on" for v in vals) else ("off" if all(v == "off" for v in vals) else vals[0])
        rooms.setdefault(room, []).append(
            {"id": dev_id, "name": name, "state": state, "entities": entities})
    return {"rooms": [{"name": r, "devices": d} for r, d in rooms.items()]}


def append_jsonl(path, record):
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def read_jsonl_after(path, after):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("id", 0) > after:
            rows.append(row)
    return rows


def next_id(path):
    if not path.exists():
        return 1
    ids = [json.loads(l).get("id", 0) for l in path.read_text().splitlines() if l.strip()]
    return (max(ids) + 1) if ids else 1


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        return self.headers.get("X-Seb-Key", "") == SECRET

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else {}

    def do_HEAD(self):
        url = urlparse(self.path)
        if url.path.startswith("/dl/"):
            name = url.path[len("/dl/"):]
            fp = BASE / "download" / name
            if name in ("sebhome.3dsx", "sebhome.cia", "sebhome.unistore") and fp.exists():
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(fp.stat().st_size))
                self.end_headers()
                return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path.startswith("/dl/"):
            name = url.path[len("/dl/"):]
            if name in ("sebhome.3dsx", "sebhome.cia", "sebhome.unistore"):
                fp = BASE / "download" / name
                if fp.exists():
                    data = fp.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
            return self._send(404, {"error": "not found"})
        if url.path == "/health":
            return self._send(200, {"ok": True, "service": "3ds-seb-bridge"})
        if not self._authed():
            return self._send(401, {"error": "bad key"})
        try:
            if url.path == "/v1/state":
                return self._send(200, state_payload())
            if url.path == "/v1/worldstate":
                return self._send(200, worldstate_payload())
            if url.path == "/v1/reply":
                after = int(parse_qs(url.query).get("after", ["0"])[0])
                rows = read_jsonl_after(REPLIES, after)
                return self._send(200, {"replies": rows})
            return self._send(404, {"error": "not found"})
        except Exception as exc:
            return self._send(502, {"error": str(exc)[:200]})

    def do_POST(self):
        url = urlparse(self.path)
        if not self._authed():
            return self._send(401, {"error": "bad key"})
        try:
            body = self._body()
            if url.path == "/v1/toggle":
                device = DEVICE_BY_ID.get(body.get("device", ""))
                if not device:
                    return self._send(400, {"error": "unknown device"})
                ha_api("POST", "/api/services/homeassistant/toggle",
                       {"entity_id": device[3]})
                _cache["ts"] = 0.0
                time.sleep(0.6)
                return self._send(200, state_payload())
            if url.path == "/v1/interact":
                code, payload = interact_payload(body.get("entity", ""))
                return self._send(code, payload)
            if url.path == "/v1/music":
                code, payload = music_payload(body)
                return self._send(code, payload)
            if url.path == "/v1/media":
                code, payload = media_payload(body)
                return self._send(code, payload)
            if url.path == "/v1/pc":
                code, payload = pc_payload(body)
                return self._send(code, payload)
            if url.path == "/v1/chat":
                text = (body.get("text") or "").strip()
                if not text:
                    return self._send(400, {"error": "empty text"})
                mid = next_id(CHAT_LOG)
                append_jsonl(CHAT_LOG, {"id": mid, "ts": time.time(), "text": text})
                proc = subprocess.run(
                    [os.environ.get("MUSEGADGET_BIN", "/usr/local/bin/musegadget"), "send-user-msg",
                     "--session-id", SESSION_ID, f"[3DS] {text}"],
                    capture_output=True, text=True, timeout=30)
                if proc.returncode != 0:
                    return self._send(503, {"ok": False, "id": mid,
                                            "error": (proc.stderr or proc.stdout).strip()[:200] or "musegadget failed"})
                return self._send(200, {"ok": True, "id": mid})
            return self._send(404, {"error": "not found"})
        except Exception as exc:
            return self._send(502, {"error": str(exc)[:200]})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
