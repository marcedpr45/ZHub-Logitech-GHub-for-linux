"""Ajustes del G600 guardados en sus tres modos: DPI, tasa de respuesta e iluminación."""
from __future__ import annotations

import fcntl
import glob
import os

REPORT_SIZE = 154
REPORT_IDS = (0xF3, 0xF4, 0xF5)
RATES = (1000, 500, 333, 250, 200, 166, 142, 125)
EFFECTS = {"fixed": 0, "breathing": 1, "cycle": 2}
_RGB, _EFFECT, _DURATION, _RATE, _SHIFT, _DEFAULT, _DPI, _GSHIFT_RGB = 1, 4, 5, 11, 12, 13, 14, 91


class G600Error(Exception):
    pass


def _ioc(number: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord("H") << 8) | number


def _rgb(text: str) -> bytes:
    return bytes.fromhex(text.lstrip("#"))


def validate(report: bytes) -> None:
    """Rechaza cualquier informe que no tenga la estructura esperada antes de modificarlo."""
    if len(report) != REPORT_SIZE or report[0] not in REPORT_IDS:
        raise G600Error("el G600 devolvió un formato inesperado")
    if report[_RATE] >= len(RATES) or report[_EFFECT] > 2 or not 1 <= report[_DEFAULT] <= 4:
        raise G600Error("el G600 devolvió valores inesperados; no se modifica nada")


def build(report: bytes, settings: dict, lighting: dict) -> bytes:
    """Devuelve una copia del informe con los ajustes aplicados."""
    validate(report)
    out = bytearray(report)
    levels = [min(max(int(v), 200), 8200) for v in settings["levels"]][:4]
    padded = levels + [0] * (4 - len(levels))
    out[_DPI:_DPI + 4] = bytes(v // 50 for v in padded)
    out[_SHIFT] = min(max(int(settings["shift"]), 200), 8200) // 50
    out[_DEFAULT] = levels.index(settings["default"]) + 1 if settings["default"] in levels else 1
    rate = settings.get("report_rate", 1000)
    out[_RATE] = RATES.index(rate) if rate in RATES else 0
    effect = lighting.get("effect", "cycle")
    if effect == "off":
        out[_RGB:_RGB + 3] = b"\0\0\0"
        out[_EFFECT] = 0
    else:
        out[_RGB:_RGB + 3] = _rgb(lighting.get("color", "#00b8fc"))
        out[_EFFECT] = EFFECTS.get(effect, 2)
    out[_DURATION] = min(max(round(lighting.get("period", 5000) / 1000), 1), 15)
    if lighting.get("shift_color"):
        out[_GSHIFT_RGB:_GSHIFT_RGB + 3] = _rgb(lighting["shift_color"])
    return bytes(out)


class G600:
    def __init__(self, fd: int):
        self.fd = fd

    @classmethod
    def open(cls) -> "G600":
        last: Exception | None = None
        for node in glob.glob("/sys/class/hidraw/hidraw*"):
            with open(node + "/device/uevent") as f:
                if ":0000C24A" not in f.read().upper():
                    continue
            try:
                fd = os.open("/dev/" + os.path.basename(node), os.O_RDWR)
            except OSError as exc:
                last = exc
                continue
            try:
                validate(cls(fd).read(REPORT_IDS[0]))
                return cls(fd)
            except (OSError, G600Error) as exc:
                last = exc
                os.close(fd)
        raise G600Error(str(last) if last else "G600 no encontrado")

    def read(self, report_id: int) -> bytes:
        buf = bytearray([report_id]) + bytearray(REPORT_SIZE - 1)
        size = fcntl.ioctl(self.fd, _ioc(0x07, REPORT_SIZE), buf)
        return bytes(buf[:size])

    def apply(self, settings: dict, lighting: dict) -> bool:
        """Escribe solo si hay diferencias, para no desgastar la memoria del ratón."""
        changed = False
        for rid in REPORT_IDS:
            current = self.read(rid)
            target = build(current, settings, lighting)
            if target != current:
                fcntl.ioctl(self.fd, _ioc(0x06, REPORT_SIZE), bytearray(target))
                changed = True
        return changed

    def close(self) -> None:
        os.close(self.fd)
