"""G600: ZHub usa solo su tercer modo de memoria; los otros dos quedan para G HUB."""
from __future__ import annotations

import fcntl
import glob
import os

REPORT_SIZE = 154
ACTIVE_SIZE = 4
REPORT_ACTIVE = 0xF0
REPORT_SLOT = 0xF5
SLOT_MODE = 2
RATES = (1000, 500, 333, 250, 200, 166, 142, 125)
EFFECTS = {"fixed": 0, "breathing": 1, "cycle": 2}
_RGB, _EFFECT, _DURATION, _RATE, _SHIFT, _DEFAULT, _DPI, _BUTTONS, _GSHIFT_RGB, _GSHIFT_BUTTONS = 1, 4, 5, 11, 12, 13, 14, 31, 91, 94
# G6, G7 y G8 pasan a enviar F13, F14 y F15 para que ZHub los gestione.
SOFT_BUTTONS = {5: 0x68, 6: 0x69, 7: 0x6A}


class G600Error(Exception):
    pass


def _ioc(number: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord("H") << 8) | number


def _rgb(text: str) -> bytes:
    return bytes.fromhex(text.lstrip("#"))


def dpi_table(settings: dict) -> list[int]:
    """Las 4 posiciones del ratón: los niveles y, si cabe, el DPI Shift."""
    levels = [min(max(int(v), 200), 8200) for v in settings["levels"]][:4]
    shift = min(max(int(settings["shift"]), 200), 8200)
    return (levels + ([] if shift in levels else [shift]) + [0] * 4)[:4]


def level_index(settings: dict, value: int) -> int:
    """Posición con ese DPI o, si no está en la tabla, la más cercana."""
    table = dpi_table(settings)
    if value in table:
        return table.index(value)
    return min((i for i, v in enumerate(table) if v), key=lambda i: abs(table[i] - value))


def validate(report: bytes) -> None:
    """Rechaza cualquier informe que no tenga la estructura esperada antes de modificarlo."""
    if len(report) != REPORT_SIZE or report[0] != REPORT_SLOT:
        raise G600Error("el G600 devolvió un formato inesperado")
    if report[_RATE] >= len(RATES) or report[_EFFECT] > 2 or not 1 <= report[_DEFAULT] <= 5:
        raise G600Error("el G600 devolvió valores inesperados; no se modifica nada")


def build(report: bytes, settings: dict, lighting: dict) -> bytes:
    """Devuelve una copia del informe con los ajustes aplicados."""
    validate(report)
    out = bytearray(report)
    out[_DPI:_DPI + 5] = bytes(v // 50 for v in dpi_table(settings)) + b"\0"
    out[_SHIFT] = min(max(int(settings["shift"]), 200), 8200) // 50
    out[_DEFAULT] = level_index(settings, settings["default"]) + 1
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
    for table_start in (_BUTTONS, _GSHIFT_BUTTONS):
        for index, usage in SOFT_BUTTONS.items():
            out[table_start + 3 * index:table_start + 3 * index + 3] = bytes([0, 0, usage])
    return bytes(out)


class G600:
    def __init__(self, fd: int):
        self.fd = fd
        self.previous: tuple[int, int] | None = None

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
                device = cls(fd)
                validate(device.read())
                return device
            except (OSError, G600Error) as exc:
                last = exc
                os.close(fd)
        raise G600Error(str(last) if last else "G600 no encontrado")

    def read(self) -> bytes:
        buf = bytearray([REPORT_SLOT]) + bytearray(REPORT_SIZE - 1)
        fcntl.ioctl(self.fd, _ioc(0x07, REPORT_SIZE), buf)
        return bytes(buf)

    def _active(self) -> tuple[int, int]:
        buf = bytearray([REPORT_ACTIVE]) + bytearray(ACTIVE_SIZE - 1)
        fcntl.ioctl(self.fd, _ioc(0x07, ACTIVE_SIZE), buf)
        return (buf[1] >> 4) & 0x3, (buf[1] >> 1) & 0x3

    def _set_active(self, value: int) -> None:
        fcntl.ioctl(self.fd, _ioc(0x06, ACTIVE_SIZE), bytearray([REPORT_ACTIVE, value, 0, 0]))

    def select_mode(self, mode: int) -> None:
        self._set_active(0x80 | (mode << 4))

    def set_level(self, level: int) -> None:
        self._set_active(0x40 | (level << 1))

    def activate(self) -> None:
        """Pasa al modo de ZHub recordando el modo y el nivel que había."""
        if self.previous is None:
            self.previous = self._active()
        self.select_mode(SLOT_MODE)

    def restore(self) -> None:
        if self.previous is not None:
            mode, level = self.previous
            self.select_mode(mode)
            self.set_level(level)

    def apply(self, settings: dict, lighting: dict) -> bool:
        """Escribe solo si hay diferencias, para no desgastar la memoria del ratón."""
        current = self.read()
        target = build(current, settings, lighting)
        if target == current:
            return False
        fcntl.ioctl(self.fd, _ioc(0x06, REPORT_SIZE), bytearray(target))
        return True

    def close(self) -> None:
        try:
            self.restore()
        finally:
            os.close(self.fd)
