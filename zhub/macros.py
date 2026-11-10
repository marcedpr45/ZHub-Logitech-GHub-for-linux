"""Macros por perfil con los mismos cuatro tipos que G HUB."""
from __future__ import annotations

import shlex
import subprocess
import threading
import time
import uuid
from typing import Callable

from evdev import ecodes as e

from .devices import MOUSE_BUTTONS, key_code

MODES = {
    "no_repeat": "Sin repetición",
    "repeat_hold": "Repetir mientras se mantiene pulsado",
    "toggle": "Alternar",
    "sequence": "Secuencia",
}
MODE_HELP = {
    "no_repeat": "Se reproduce una vez cada vez que pulsas el botón.",
    "repeat_hold": "Se repite mientras mantienes pulsado el botón y se detiene al soltarlo.",
    "toggle": "Una pulsación la inicia y se repite hasta que vuelves a pulsar el botón.",
    "sequence": "Tres partes: al pulsar, en bucle mientras mantienes pulsado y al soltar.",
}
PHASES = {"press": "Al pulsar", "held": "Mientras se mantiene", "release": "Al soltar"}
DEFAULT_DELAY = 50

# Distribución US: carácter -> (tecla, necesita Mayús)
_US_PLAIN = "`1234567890-=qwertyuiop[]\\asdfghjkl;'zxcvbnm,./"
_US_SHIFT = "~!@#$%^&*()_+QWERTYUIOP{}|ASDFGHJKL:\"ZXCVBNM<>?"
_US_KEYS = ["KEY_GRAVE", *[f"KEY_{c}" for c in "1234567890"], "KEY_MINUS", "KEY_EQUAL",
            *[f"KEY_{c}" for c in "QWERTYUIOP"], "KEY_LEFTBRACE", "KEY_RIGHTBRACE", "KEY_BACKSLASH",
            *[f"KEY_{c}" for c in "ASDFGHJKL"], "KEY_SEMICOLON", "KEY_APOSTROPHE",
            *[f"KEY_{c}" for c in "ZXCVBNM"], "KEY_COMMA", "KEY_DOT", "KEY_SLASH"]
CHARS: dict[str, tuple[str, bool]] = {" ": ("KEY_SPACE", False), "\n": ("KEY_ENTER", False), "\t": ("KEY_TAB", False)}
CHARS.update({c: (k, False) for c, k in zip(_US_PLAIN, _US_KEYS)})
CHARS.update({c: (k, True) for c, k in zip(_US_SHIFT, _US_KEYS)})


def new_macro(name: str, mode: str = "no_repeat") -> dict:
    return {"id": str(uuid.uuid4()), "name": name, "mode": mode, "standard_delay": False, "delay": DEFAULT_DELAY,
            "steps": [], "press": [], "held": [], "release": []}


def from_legacy(a: dict) -> dict:
    """Convierte las macros incrustadas en una asignación (formato anterior) al formato de biblioteca."""
    m = new_macro(a.get("name") or "Macro")
    if a.get("toggle"):
        m.update(mode="toggle", steps=a["toggle"])
    elif a.get("held") and not a.get("press") and not a.get("release"):
        m.update(mode="repeat_hold", steps=a["held"])
    elif a.get("held") or a.get("release"):
        m.update(mode="sequence", press=a.get("press", []), held=a.get("held", []), release=a.get("release", []))
    else:
        m["steps"] = a.get("press", [])
    return m


def library(profile: dict) -> list[dict]:
    return profile.setdefault("macros", [])


def find(profile: dict, macro_id: str | None) -> dict | None:
    return next((m for m in profile.get("macros", []) if m["id"] == macro_id), None)


def resolve(profile: dict, action: dict) -> dict | None:
    """Macro que ejecuta una asignación, sea una referencia a la biblioteca o una macro antigua."""
    if action.get("type") == "macro":
        return find(profile, action.get("id"))
    if action.get("type") == "sequence":
        return from_legacy(action)
    return None


def text_supported(text: str) -> bool:
    return all(c in CHARS for c in text)


_KEY_LABELS = {"LEFTCTRL": "Ctrl izq.", "RIGHTCTRL": "Ctrl der.", "LEFTSHIFT": "Mayús izq.", "RIGHTSHIFT": "Mayús der.",
               "LEFTALT": "Alt", "RIGHTALT": "AltGr", "LEFTMETA": "Super", "RIGHTMETA": "Super der.", "SPACE": "Espacio",
               "ENTER": "Intro", "ESC": "Esc", "BACKSPACE": "Retroceso", "TAB": "Tab"}


def key_label(name: str) -> str:
    bare = name.removeprefix("KEY_")
    return _KEY_LABELS.get(bare, bare.title())


def describe_step(s: dict) -> str:
    if "delay" in s:
        return f"Retardo {s['delay']} ms"
    if "key" in s:
        name = key_label(s["key"])
        if s.get("tap"):
            return f"Pulsar {name}"
        return f"{'↓' if s.get('down', True) else '↑'} {name}"
    if "mouse" in s:
        label = {1: "izquierdo", 2: "derecho", 3: "central", 4: "atrás", 5: "adelante"}.get(s["mouse"], s["mouse"])
        return f"{'↓' if s.get('down', True) else '↑'} Clic {label}"
    if "text" in s:
        return f"Texto «{s['text']}»"
    if "run" in s:
        return f"Abrir {s['run']}"
    return "?"


class Player:
    """Reproduce pasos sobre los dispositivos virtuales de ZHub."""

    def __init__(self, out, launch: Callable[[str], None] | None = None):
        self.out = out
        self.launch = launch or (lambda cmd: subprocess.Popen(shlex.split(cmd), start_new_session=True))

    def run(self, steps: list[dict], stop: threading.Event | None = None, standard: int | None = None) -> None:
        """Con `standard` se ignoran los retardos grabados y se espera ese tiempo entre acciones."""
        held: set[int] = set()

        def wait(ms: float) -> None:
            if stop is not None:
                stop.wait(ms / 1000)
            else:
                time.sleep(ms / 1000)

        def press(code: int, down: bool) -> None:
            self.out.key(code, down)
            (held.add if down else held.discard)(code)

        for s in steps:
            if stop is not None and stop.is_set():
                break
            if "delay" in s:
                if standard is None:
                    wait(s["delay"])
                continue
            if "key" in s:
                code = key_code(s["key"])
                if s.get("tap"):
                    press(code, True)
                    press(code, False)
                else:
                    press(code, s.get("down", True))
            elif "mouse" in s:
                press(e.ecodes[MOUSE_BUTTONS[s["mouse"]]], s.get("down", True))
            elif "text" in s:
                for char in s["text"]:
                    if char not in CHARS:
                        continue
                    key, shift = CHARS[char]
                    if shift:
                        self.out.key(e.KEY_LEFTSHIFT, True)
                    self.out.key(key_code(key), True)
                    self.out.key(key_code(key), False)
                    if shift:
                        self.out.key(e.KEY_LEFTSHIFT, False)
            elif "run" in s:
                try:
                    self.launch(s["run"])
                except (OSError, ValueError):
                    pass
            if standard is not None:
                wait(standard)
        if stop is not None and stop.is_set():  # al cortarla, no deja teclas pulsadas
            for code in held:
                self.out.key(code, False)
