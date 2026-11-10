"""Importa perfiles, asignaciones y macros de una instalación de G HUB (solo lectura)."""
from __future__ import annotations

import glob
import json
import re
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path

from . import macros
from .devices import CATALOG, hid_to_evdev, key_name
from .profiles import new_profile

_BUILTIN = "0f82f693-5b78-4cf5-867e-"
_SLOT = re.compile(r"^(g\d+)(?:-\w+)?_(g\d+)_m\d(_shifted)?$")
_EXES = {"League of Legends": ["league of legends.exe"]}
_NAMES = {"APPLICATION_NAME_DESKTOP": "Escritorio"}
_MEDIA = {
    "PLAY_PAUSE": "KEY_PLAYPAUSE", "STOP": "KEY_STOPCD", "PREVIOUS": "KEY_PREVIOUSSONG",
    "NEXT": "KEY_NEXTSONG", "VOLUME_UP": "KEY_VOLUMEUP", "VOLUME_DOWN": "KEY_VOLUMEDOWN", "MUTE": "KEY_MUTE",
}
# Identificadores internos deducidos de las asignaciones de fábrica de cada ratón.
_MODIFIERS = ("KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_LEFTALT", "KEY_LEFTMETA",
              "KEY_RIGHTCTRL", "KEY_RIGHTSHIFT", "KEY_RIGHTALT", "KEY_RIGHTMETA")
_SPECIAL = {
    "0404": {"type": "dpi", "op": "cycle"},
    "0905": {"type": "gshift"},
    "0901": {"type": "profile_cycle"},
}


def find_settings(mounts: list[str] | None = None) -> list[Path]:
    """Busca settings.db de G HUB en las particiones de Windows montadas."""
    found: list[Path] = []
    for root in mounts or glob.glob("/run/media/*/*") + glob.glob("/mnt/*") + glob.glob("/media/*"):
        found += [Path(p) for p in glob.glob(f"{root}/Users/*/AppData/Local/LGHUB/settings.db")]
    return found


def read_settings(db: Path) -> dict:
    """Lee la base en una copia temporal: NTFS suele estar en solo lectura y hay WAL."""
    with tempfile.TemporaryDirectory() as tmp:
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db) + suffix)
            if src.exists():
                shutil.copy(src, Path(tmp) / src.name)
        con = sqlite3.connect(Path(tmp) / db.name)
        blob = con.execute("select file from data order by _id desc limit 1").fetchone()[0]
        con.close()
    return json.loads(blob)


def _keys_from_keystroke(ks: dict) -> list[str]:
    usages = list(ks.get("modifiers", [])) + [ks.get("code", 0)]
    codes = [hid_to_evdev(int(u)) for u in usages]
    return [key_name(c) for c in codes if c]


def _steps(components: list[dict]) -> list[dict]:
    out: list[dict] = []
    for c in components:
        if "delay" in c:
            out.append({"delay": c["delay"].get("durationMs", 0)})
        elif "keyboard" in c:
            code = hid_to_evdev(int(c["keyboard"]["hidUsage"]))
            if code:
                out.append({"key": key_name(code), "down": bool(c["keyboard"].get("isDown"))})
        elif "mouse" in c:
            out.append({"mouse": int(c["mouse"].get("hidUsage", 1)), "down": bool(c["mouse"].get("isDown"))})
    return out


def _flat(seq) -> list[dict]:
    if isinstance(seq, dict):
        seq = seq.get("components", [])
    return _steps(seq or [])


def _components(seq) -> list[dict]:
    """Pasos de una secuencia de G HUB, incluidos texto y programas."""
    comps = seq.get("components", []) if isinstance(seq, dict) else (seq or [])
    out: list[dict] = []
    for c in comps:
        if "textBlock" in c:
            out.append({"text": c["textBlock"].get("text", "")})
        elif "app" in c:
            out.append({"run": c["app"].get("executablePath", "")})
        else:
            out.extend(_steps([c]))
    return out


def macro_definition(s: dict, macro_id: str, name: str) -> dict:
    """Macro de biblioteca con el mismo tipo que en G HUB."""
    m = macros.new_macro(name or "Macro")
    m["id"] = macro_id
    m["delay"] = int(s.get("defaultDelay", macros.DEFAULT_DELAY))
    press, held, release = (_components(s.get(k)) for k in ("pressSequence", "heldSequence", "releaseSequence"))
    if s.get("useSimpleActions", True):
        m["steps"] = _components(s.get("simpleSequence"))
    elif _components(s.get("toggleSequence")):
        m.update(mode="toggle", steps=_components(s["toggleSequence"]))
    elif held and not press and not release:
        m.update(mode="repeat_hold", steps=held)
    else:
        m.update(mode="sequence", press=press, held=held, release=release)
    return m


def convert_macro(m: dict, name: str = "", card_id: str = "") -> dict:
    t = m.get("type")
    if t == "KEYSTROKE":
        return {"type": "key", "keys": _keys_from_keystroke(m["keystroke"])}
    if t == "SEQUENCE":
        macro_id = card_id or str(uuid.uuid4())
        return {"type": "macro", "id": macro_id, "definition": macro_definition(m["sequence"], macro_id, name)}
    if t == "MOUSE":
        mo = m["mouse"]
        a = mo.get("action")
        if a == "BUTTON":
            return {"type": "mouse", "button": mo["hidUsage"]}
        if a == "BUTTON_DOUBLE_CLICK":
            return {"type": "double_click"}
        if a and a.startswith("WHEEL_") and a != "WHEEL_MODE_TOGGLE":
            return {"type": "wheel", "dir": a[6:].lower()}
        ops = {"DPI_UP": "up", "DPI_DOWN": "down", "DPI_SHIFT": "shift", "DPI_DEFAULT": "default", "DPI_CYCLE": "cycle"}
        if a in ops:
            return {"type": "dpi", "op": ops[a]}
    if t == "MEDIA" and m["media"].get("usage") in _MEDIA:
        return {"type": "key", "keys": [_MEDIA[m["media"]["usage"]]]}
    if t == "DEVICE":
        a = m["device"].get("action")
        if a == "G_SHIFT":
            return {"type": "gshift"}
        if a == "PROFILE_CYCLE":
            return {"type": "profile_cycle"}
        if a == "REPORT_RATE_CYCLE":
            return {"type": "report_rate_cycle"}
    if t == "GHUB":
        return {"type": "profile_cycle"}
    return {"type": "unsupported", "label": name or str(t)}


def convert_card(card_id: str, cards: dict[str, dict]) -> dict | None:
    """Traduce una tarjeta; None significa «sin asignar» (se usa la capa base)."""
    if card_id in cards and cards[card_id].get("macro"):
        c = cards[card_id]
        return convert_macro(c["macro"], c.get("name", ""), card_id)
    if card_id.replace("-", "").strip("f") == "":
        return {"type": "none"}
    if not card_id.startswith(_BUILTIN):
        return None
    tail = card_id[len(_BUILTIN):]
    kind, value, mods = tail[:2], int(tail[2:4], 16), int(tail[4:6], 16)
    if kind == "02":
        return {"type": "mouse", "button": value}
    if kind == "01":
        code = hid_to_evdev(value)
        keys = [key_name(code)] if code else []
        held = [name for bit, name in enumerate(_MODIFIERS) if mods & (1 << bit)]
        return {"type": "key", "keys": held + keys}
    return _SPECIAL.get(tail[:4])


def convert(settings: dict) -> dict:
    cards = {c["id"]: c for c in settings["cards"]["cards"]}
    apps = {a["applicationId"]: a for a in settings["applications"]["applications"]}
    profiles = []
    for gp in settings["profiles"]["profiles"]:
        app_name = apps.get(gp["applicationId"], {}).get("name", "?")
        is_default = app_name == "APPLICATION_NAME_DESKTOP" and gp.get("activeForApplication", False)
        name = _NAMES.get(app_name, app_name)
        if gp.get("name") != "PROFILE_NAME_DEFAULT":
            name = f"{name} – {gp['name']}"
        match = {"exe": list(_EXES.get(app_name, [])) if gp.get("activeForApplication") else [],
                 "wm_class": [], "steam_appid": [], "title": ""}
        profile = new_profile(name, match, is_default)
        for c in cards.values():  # en G HUB las macros pertenecen a la aplicación, estén asignadas o no
            if c.get("applicationId") == gp["applicationId"] and (c.get("macro") or {}).get("type") == "SEQUENCE":
                macros.library(profile).append(macro_definition(c["macro"]["sequence"], c["id"], c.get("name", "")))
        for a in gp["assignments"]:
            slot, card = a["slotId"], cards.get(a["cardId"], {})
            dev_id = slot.split("_")[0].split("-")[0]
            if dev_id not in CATALOG:
                continue
            dev = profile["devices"].setdefault(dev_id, {})
            if card.get("attribute") == "MOUSE_SETTINGS":
                ms = card["mouseSettings"]
                dpi = ms["dpiTable"]
                dev["mouse"] = {"levels": dpi["levels"], "default": dpi["defaultDpi"],
                                "shift": dpi.get("shiftDpi", dpi["defaultDpi"]),
                                "report_rate": ms.get("reportRate", {}).get("value", 1000)}
            elif card.get("attribute") == "WHEEL_SETTINGS":
                ws = card["wheelSettings"]
                dev.setdefault("wheel", {}).update(
                    range=ws["wheelAperture"]["value"],
                    autocenter=ws["centerSpringStrength"]["value"] if ws.get("useCenterSpringStrength") else 0,
                    damping=ws["wheelDamping"]["value"], sensitivity=ws["wheelSensitivity"]["value"])
            elif card.get("attribute") == "TRUEFORCE_SETTINGS":
                dev.setdefault("wheel", {})["gain"] = card["trueforceSettings"]["kfGain"]["value"]
            elif card.get("attribute") == "PEDAL_SETTINGS":
                ps = card["pedalSettings"]
                dev.setdefault("wheel", {}).update(
                    throttle=ps["acceleratorSensitivity"]["value"], brake=ps["brakeSensitivity"]["value"],
                    clutch=ps["clutchSensitivity"]["value"], combined=bool(ps.get("combinedPedals", False)))
            elif card.get("attribute") == "FIRMWARE_LIGHTING_SETTINGS":
                fx = card["firmwareLightingSettings"]["effects"][0]
                params = next((v for k, v in fx.items() if k.endswith("Params")), {})
                dev["lighting"] = {"effect": {"FIXED": "fixed", "BREATHING": "breathing", "CYCLE": "cycle"}.get(fx.get("id"), "cycle"),
                                   "period": params.get("periodInMs", 5000), "color": params.get("color", "#00b8fc")}
            elif (m := _SLOT.match(slot)) and m.group(2) in CATALOG[dev_id].buttons:
                action = convert_card(a["cardId"], cards)
                if action and "definition" in action:
                    definition = action.pop("definition")
                    if not macros.find(profile, definition["id"]):
                        macros.library(profile).append(definition)
                if action:
                    dev.setdefault("shifted" if m.group(3) else "buttons", {})[m.group(2)] = action
        profiles.append(profile)
    if profiles and not any(p["default"] for p in profiles):
        profiles[0]["default"] = True
    profiles.sort(key=lambda p: not p["default"])
    return {"auto_switch": settings.get("profiles/auto_switching/enabled", True),
            "persistent_profile": None, "profiles": profiles}


def import_ghub(db: Path) -> dict:
    return convert(read_settings(db))
