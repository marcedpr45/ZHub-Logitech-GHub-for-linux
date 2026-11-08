"""Cliente HID++ 2.0 sobre hidraw (DPI, tasa de respuesta, batería, modo de memoria)."""
from __future__ import annotations

import glob
import os
import select
import time

SHORT, LONG = 0x10, 0x11
DEVICE_INFO = 0x0003
ERR_BUSY = 8
ADJUSTABLE_DPI = 0x2201
REPORT_RATE = 0x8060
ONBOARD_PROFILES = 0x8100
BATTERY_STATUS = 0x1000
BATTERY_VOLTAGE = 0x1001
UNIFIED_BATTERY = 0x1004
MODE_ONBOARD, MODE_HOST = 1, 2


class HidppError(Exception):
    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


def _uevent(path: str) -> dict[str, str]:
    with open(path) as f:
        return dict(line.strip().split("=", 1) for line in f if "=" in line)


def find_endpoint(pid: int, receivers: tuple[int, ...]) -> tuple[str, int] | None:
    """Localiza (ruta hidraw, número de dispositivo) para un ratón en un receptor o por cable."""
    wanted_child, devnum = f":{pid:08X}", None
    for node in glob.glob("/sys/class/hidraw/hidraw*"):
        ev = _uevent(node + "/device/uevent")
        if ev.get("HID_ID", "").upper().endswith(wanted_child):
            phys = ev.get("HID_PHYS", "")
            if ":" in phys.rsplit("/", 1)[-1]:
                devnum = int(phys.rsplit(":", 1)[-1])
            else:
                return "/dev/" + os.path.basename(node), 0xFF
    if devnum is None:
        return None
    for node in glob.glob("/sys/class/hidraw/hidraw*"):
        ev = _uevent(node + "/device/uevent")
        hid_id = ev.get("HID_ID", "").upper()
        if any(hid_id.endswith(f":{r:08X}") for r in receivers) and ev.get("HID_PHYS", "").endswith("input2"):
            return "/dev/" + os.path.basename(node), devnum
    return None


def parse_dpi_list(data: bytes) -> list[int]:
    """Decodifica la respuesta de getSensorDpiList (valores sueltos y rangos con paso)."""
    out: list[int] = []
    i = 0
    while i + 1 < len(data):
        v = (data[i] << 8) | data[i + 1]
        i += 2
        if v == 0:
            break
        if v >= 0xE000:
            step = v & 0x1FFF
            end = (data[i] << 8) | data[i + 1]
            i += 2
            out.extend(range(out[-1] + step, end + 1, step))
        else:
            out.append(v)
    return out


def parse_rate_flags(flags: int) -> list[int]:
    """Convierte el mapa de bits de intervalos (ms) en tasas en Hz, de menor a mayor."""
    return sorted(1000 // (b + 1) for b in range(8) if flags & (1 << b))


class HidppDevice:
    SWID = 0x0B

    def __init__(self, path: str, devnum: int):
        self.path, self.devnum = path, devnum
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self._features: dict[int, int] = {}
        self.link: bool | None = None  # último aviso de enlace del receptor sin consumir

    def reset(self) -> None:
        """Olvida lo aprendido del dispositivo (tras apagarse o reconectarse)."""
        self._features.clear()

    def close(self) -> None:
        os.close(self.fd)

    def _note(self, r: bytes) -> None:
        """Registra los avisos 0x41 del receptor: encendido o pérdida de enlace."""
        if len(r) >= 5 and r[0] == SHORT and r[1] == self.devnum and r[2] == 0x41:
            self.link = not r[4] & 0x40

    def poll(self) -> None:
        """Lee los avisos pendientes sin bloquear."""
        while select.select([self.fd], [], [], 0)[0]:
            try:
                self._note(os.read(self.fd, 64))
            except BlockingIOError:
                return

    def request(self, index: int, fn: int, *params: int, timeout: float = 1.0) -> bytes:
        for attempt in range(3):
            try:
                return self._request(index, fn, params, timeout)
            except HidppError as exc:
                if exc.code != ERR_BUSY or attempt == 2:
                    raise
                time.sleep(0.05)
        raise HidppError("dispositivo ocupado")

    def ping(self) -> bool:
        try:
            self.request(0, 1, 0, 0, 0xAA, timeout=0.6)
            return True
        except (OSError, HidppError):
            return False

    def _request(self, index: int, fn: int, params: tuple[int, ...], timeout: float) -> bytes:
        func = (fn << 4) | self.SWID
        short = len(params) <= 3
        size, rid = (7, SHORT) if short else (20, LONG)
        msg = bytes([rid, self.devnum, index, func, *params]).ljust(size, b"\0")
        self.poll()
        os.write(self.fd, msg)
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            if not select.select([self.fd], [], [], left)[0]:
                break
            r = os.read(self.fd, 64)
            self._note(r)
            if len(r) < 5 or r[0] not in (SHORT, LONG) or r[1] != self.devnum:
                continue
            if r[2] == 0xFF and r[3] == index and r[4] == func:
                raise HidppError(f"error HID++ {r[5]:#x} en índice {index:#x}", r[5])
            if r[2] == 0x8F and r[3] == index:
                raise HidppError("dispositivo no disponible (¿dormido o apagado?)")
            if r[2] == index and r[3] == func:
                return bytes(r[4:])
        raise HidppError("sin respuesta del dispositivo")

    def feature(self, fid: int) -> int:
        if fid not in self._features:
            self._features[fid] = self.request(0, 0, fid >> 8, fid & 0xFF)[0]
        return self._features[fid]

    def _idx(self, fid: int) -> int:
        idx = self.feature(fid)
        if not idx:
            raise HidppError(f"función {fid:#06x} no soportada")
        return idx

    def list_features(self) -> list[int]:
        """Enumera las funciones HID++ del dispositivo (diagnóstico)."""
        fs = self._idx(0x0001)
        count = self.request(fs, 0)[0]
        out = []
        for i in range(1, count + 1):
            r = self.request(fs, 1, i)
            out.append((r[0] << 8) | r[1])
        return out

    def firmware(self) -> str:
        """Versión del firmware principal, o cadena vacía si no se puede leer."""
        idx = self.feature(DEVICE_INFO)
        if not idx:
            return ""
        for entity in range(self.request(idx, 0)[0]):
            r = self.request(idx, 1, entity)
            if r[0] & 0x0F == 0:
                return f"{r[1:4].decode('ascii', 'replace').strip(chr(0) + ' ')} {r[4]:02X}.{r[5]:02X}.B{(r[6] << 8) | r[7]:04X}"
        return ""

    # --- DPI ---
    def dpi_list(self) -> list[int]:
        return parse_dpi_list(self.request(self._idx(ADJUSTABLE_DPI), 1, 0)[1:])

    def get_dpi(self) -> int:
        r = self.request(self._idx(ADJUSTABLE_DPI), 2, 0)
        return (r[1] << 8) | r[2]

    def set_dpi(self, dpi: int) -> None:
        self.request(self._idx(ADJUSTABLE_DPI), 3, 0, dpi >> 8, dpi & 0xFF)

    # --- Tasa de respuesta ---
    def report_rates(self) -> list[int]:
        return parse_rate_flags(self.request(self._idx(REPORT_RATE), 0)[0])

    def get_report_rate(self) -> int:
        return 1000 // self.request(self._idx(REPORT_RATE), 1)[0]

    def set_report_rate(self, hz: int) -> None:
        self.request(self._idx(REPORT_RATE), 2, 1000 // hz)

    # --- Memoria integrada ---
    def get_mode(self) -> int | None:
        idx = self.feature(ONBOARD_PROFILES)
        return self.request(idx, 2)[0] if idx else None

    def set_mode(self, mode: int) -> None:
        idx = self.feature(ONBOARD_PROFILES)
        if idx:
            self.request(idx, 1, mode)

    # --- Batería ---
    def battery(self) -> str:
        """Texto de estado de batería con la mejor función disponible."""
        if idx := self.feature(UNIFIED_BATTERY):
            r = self.request(idx, 1)
            return f"{r[0]} %"
        if idx := self.feature(BATTERY_STATUS):
            r = self.request(idx, 0)
            return f"{r[0]} %"
        if idx := self.feature(BATTERY_VOLTAGE):
            r = self.request(idx, 0)
            return f"{(r[0] << 8) | r[1]} mV"
        return "desconocida"
