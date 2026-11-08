"""Catálogo de dispositivos soportados y utilidades de códigos de tecla."""
from __future__ import annotations

from dataclasses import dataclass, field

from evdev import ecodes as e

# Tabla estándar HID Keyboard Usage -> evdev (equivalente a hid_keyboard[] del kernel).
_HID = [
    0, 0, 0, 0, 30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38,
    50, 49, 24, 25, 16, 19, 31, 20, 22, 47, 17, 45, 21, 44, 2, 3,
    4, 5, 6, 7, 8, 9, 10, 11, 28, 1, 14, 15, 57, 12, 13, 26,
    27, 43, 43, 39, 40, 41, 51, 52, 53, 58, 59, 60, 61, 62, 63, 64,
    65, 66, 67, 68, 87, 88, 99, 70, 119, 110, 102, 104, 111, 107, 109, 106,
    105, 108, 103, 69, 98, 55, 74, 78, 96, 79, 80, 81, 75, 76, 77, 71,
    72, 73, 82, 83, 86, 127, 116, 117, 183, 184, 185, 186, 187, 188, 189, 190,
    191, 192, 193, 194,
]
_HID_MODS = {224: 29, 225: 42, 226: 56, 227: 125, 228: 97, 229: 54, 230: 100, 231: 126}


def hid_to_evdev(usage: int) -> int:
    """Convierte un HID Keyboard Usage en código evdev (0 si no existe)."""
    if usage in _HID_MODS:
        return _HID_MODS[usage]
    return _HID[usage] if 0 <= usage < len(_HID) else 0


def key_name(code: int) -> str:
    """Nombre canónico KEY_*/BTN_* de un código evdev."""
    name = e.keys.get(code, f"KEY_{code}")
    return name[0] if isinstance(name, (list, tuple)) else name


def key_code(name: str) -> int:
    return e.ecodes[name]


MOUSE_BUTTONS = {1: "BTN_LEFT", 2: "BTN_RIGHT", 3: "BTN_MIDDLE", 4: "BTN_SIDE", 5: "BTN_EXTRA"}


def mouse(n: int) -> dict:
    return {"type": "mouse", "button": n}


@dataclass(frozen=True)
class DeviceSpec:
    id: str
    name: str
    kind: str  # "mouse" | "wheel"
    pids: tuple[int, ...]
    receivers: tuple[int, ...] = ()
    buttons: dict[str, str] = field(default_factory=dict)  # id -> etiqueta
    inputs: dict[int, str] = field(default_factory=dict)  # código evdev -> id botón
    defaults: dict[str, dict] = field(default_factory=dict)
    hidpp: bool = False
    firmware_gshift: bool = False  # el firmware envía Mayús + tecla en la capa G-Shift
    experimental: bool = False  # sin probar aún con el hardware conectado
    dpi_min: int = 200
    dpi_max: int = 12000
    dpi_step: int = 50
    max_levels: int = 5
    rates: tuple[int, ...] = (125, 250, 500, 1000)
    has_lighting: bool = False


G305 = DeviceSpec(
    id="g305",
    name="G305 Lightspeed",
    kind="mouse",
    pids=(0x4074,),
    receivers=(0xC53F, 0xC539, 0xC547),
    buttons={
        "g1": "Clic izquierdo (G1)",
        "g2": "Clic derecho (G2)",
        "g3": "Rueda (G3)",
        "g4": "Lateral trasero (G4)",
        "g5": "Lateral delantero (G5)",
        "g6": "Botón DPI (G6)",
    },
    inputs={
        e.BTN_LEFT: "g1",
        e.BTN_RIGHT: "g2",
        e.BTN_MIDDLE: "g3",
        e.BTN_SIDE: "g4",
        e.BTN_EXTRA: "g5",
        e.BTN_FORWARD: "g6",
    },
    defaults={
        "g1": mouse(1),
        "g2": mouse(2),
        "g3": mouse(3),
        "g4": mouse(4),
        "g5": mouse(5),
        "g6": {"type": "dpi", "op": "cycle"},
    },
    hidpp=True,
)

_G600_SIDE = [
    e.KEY_1, e.KEY_2, e.KEY_3, e.KEY_4, e.KEY_5, e.KEY_6,
    e.KEY_7, e.KEY_8, e.KEY_9, e.KEY_0, e.KEY_MINUS, e.KEY_EQUAL,
]

G600 = DeviceSpec(
    id="g600",
    name="G600 MMO",
    kind="mouse",
    pids=(0xC24A,),
    buttons={
        "g1": "Clic izquierdo (G1)",
        "g2": "Clic derecho (G2)",
        "g3": "Rueda (G3)",
        "g4": "Inclinar rueda izq. (G4)",
        "g5": "Inclinar rueda der. (G5)",
        **{f"g{9 + i}": f"Lateral G{9 + i}" for i in range(12)},
    },
    inputs={
        e.BTN_LEFT: "g1",
        e.BTN_RIGHT: "g2",
        e.BTN_MIDDLE: "g3",
        e.BTN_SIDE: "g4",
        e.BTN_EXTRA: "g5",
        **{code: f"g{9 + i}" for i, code in enumerate(_G600_SIDE)},
    },
    defaults={
        "g1": mouse(1),
        "g2": mouse(2),
        "g3": mouse(3),
        "g4": mouse(4),
        "g5": mouse(5),
        **{f"g{9 + i}": {"type": "key", "keys": [key_name(c)]} for i, c in enumerate(_G600_SIDE)},
    },
    firmware_gshift=True,
    experimental=True,
    dpi_max=8200,
    max_levels=4,
    rates=(125, 142, 166, 200, 250, 333, 500, 1000),
    has_lighting=True,
)

G923 = DeviceSpec(
    id="g923",
    name="G923 Racing Wheel",
    kind="wheel",
    pids=(0xC26E, 0xC266, 0xC267),
    experimental=True,
)

CATALOG: dict[str, DeviceSpec] = {d.id: d for d in (G305, G600, G923)}
