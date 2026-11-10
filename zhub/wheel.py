"""G923: ajustes del controlador y dispositivo virtual para curvas y pedales combinados."""
from __future__ import annotations

import glob
import logging
import selectors
import threading

import evdev
from evdev import UInput, ecodes as e, ff

from . import hidpp

log = logging.getLogger("zhub")
AXES = {e.ABS_X: "sensitivity", e.ABS_Y: "throttle", e.ABS_Z: "brake", e.ABS_RZ: "clutch"}
VIRTUAL_PHYS = "zhub-wheel"


def apply_curve(value: int, low: int, high: int, sensitivity: int, centered: bool = False) -> int:
    """Curva de respuesta: 50 es lineal, menos suaviza el inicio y más lo acelera."""
    if sensitivity == 50 or high <= low:
        return value
    gamma = 2 ** ((50 - sensitivity) / 25)
    if centered:
        mid = (low + high) / 2
        span = (high - low) / 2
        x = (value - mid) / span
        return round(mid + span * abs(x) ** gamma * (1 if x >= 0 else -1))
    x = (value - low) / (high - low)
    return round(low + (high - low) * x ** gamma)


def combine(throttle: int, brake: int, low: int, high: int) -> int:
    """Acelerador y freno en un solo eje, con el reposo en el centro."""
    span = high - low
    return round(low + span / 2 + ((throttle - low) - (brake - low)) * span / (2 * span))


def needs_transform(w: dict) -> bool:
    return bool(w.get("combined")) or any(w.get(k, 50) != 50 for k in ("sensitivity", "throttle", "brake", "clutch"))


def write_range(degrees: int) -> str:
    """Devuelve el error en texto, o cadena vacía si se aplicó."""
    for attr in glob.glob("/sys/bus/hid/devices/*046D:C26E*/range"):
        try:
            with open(attr, "w") as f:
                f.write(str(degrees))
            return ""
        except OSError as exc:
            return f"no se pudo escribir el giro: {exc}"
    return ""


def full_version(usb: str, reference: str) -> str:
    """El USB solo guarda dos cifras del número principal: 139.02 se ve como 39.02."""
    parts = reference.split(".")
    if len(parts) >= 2 and usb == f"{int(parts[0]) % 100:02d}.{parts[1]}":
        return f"{parts[0]}.{parts[1]}"
    return usb


def read_firmware(reference: str) -> str:
    return full_version(hidpp.usb_version(0xC26E), reference)


class LiveAxes:
    """Posición física actual; no consume eventos y funciona aunque el volante esté capturado."""

    PEDALS = {"throttle": e.ABS_Y, "brake": e.ABS_Z, "clutch": e.ABS_RZ}

    def __init__(self, path: str):
        self.dev = evdev.InputDevice(path)

    def read(self) -> dict[str, float]:
        """Volante de -1 a 1 (derecha positivo) y pedales de 0 a 1."""
        x = self.dev.absinfo(e.ABS_X)
        out = {"wheel": (2 * x.value - x.max - x.min) / (x.max - x.min)}
        for name, code in self.PEDALS.items():
            a = self.dev.absinfo(code)
            out[name] = (a.value - a.min) / (a.max - a.min)
        return out


class WheelTransform(threading.Thread):
    """Sustituye al volante real por una copia con ejes ajustados y force feedback reenviado."""

    def __init__(self, path: str, settings: dict):
        super().__init__(daemon=True)
        self.dev = evdev.InputDevice(path)
        self.settings = settings
        self.halt = threading.Event()
        self.ids: dict[int, int] = {}
        caps = self.dev.capabilities(absinfo=True)
        caps.pop(e.EV_SYN, None)
        self.absinfo = dict(caps.get(e.EV_ABS, []))
        self.values = {code: info.value for code, info in self.absinfo.items()}
        self.ui = UInput(caps, name=self.dev.name, vendor=self.dev.info.vendor, product=self.dev.info.product,
                         version=self.dev.info.version, bustype=self.dev.info.bustype, phys=VIRTUAL_PHYS,
                         max_effects=max(len(caps.get(e.EV_FF, [])), 16))

    def stop(self) -> None:
        self.halt.set()
        self.join(1.5)

    def transform(self, code: int, value: int) -> list[tuple[int, int]]:
        """Traduce un eje físico en los valores que deben enviarse al virtual."""
        self.values[code] = value
        info = self.absinfo.get(code)
        name = AXES.get(code)
        if info is None or name is None:
            return [(code, value)]
        s = self.settings
        out = apply_curve(value, info.min, info.max, s.get(name, 50), centered=code == e.ABS_X)
        if s.get("combined") and code in (e.ABS_Y, e.ABS_Z):
            t, b = self.absinfo[e.ABS_Y], self.absinfo[e.ABS_Z]
            ty = apply_curve(self.values[e.ABS_Y], t.min, t.max, s.get("throttle", 50))
            br = apply_curve(self.values[e.ABS_Z], b.min, b.max, s.get("brake", 50))
            return [(e.ABS_Y, combine(ty, br, t.min, t.max)), (e.ABS_Z, b.min)]
        return [(code, out)]

    def _service_ff(self) -> None:
        for ev in self.ui.read():
            if ev.type == e.EV_UINPUT and ev.code == e.UI_FF_UPLOAD:
                upload = self.ui.begin_upload(ev.value)
                try:
                    virtual_id = upload.effect.id
                    upload.effect.id = self.ids.get(virtual_id, -1)
                    self.ids[virtual_id] = self.dev.upload_effect(upload.effect)
                    upload.effect.id = virtual_id
                    upload.retval = 0
                except OSError as exc:
                    upload.retval = -exc.errno
                self.ui.end_upload(upload)
            elif ev.type == e.EV_UINPUT and ev.code == e.UI_FF_ERASE:
                erase = self.ui.begin_erase(ev.value)
                try:
                    self.dev.erase_effect(self.ids.pop(erase.effect_id))
                    erase.retval = 0
                except (KeyError, OSError):
                    erase.retval = 0
                self.ui.end_erase(erase)
            elif ev.type == e.EV_FF:
                self.dev.write(e.EV_FF, self.ids.get(ev.code, ev.code), ev.value)

    def run(self) -> None:
        sel = selectors.DefaultSelector()
        try:
            self.dev.grab()
            sel.register(self.dev, selectors.EVENT_READ, "real")
            sel.register(self.ui.fd, selectors.EVENT_READ, "virtual")
            while not self.halt.is_set():
                for key, _ in sel.select(0.2):
                    if key.data == "virtual":
                        self._service_ff()
                        continue
                    for ev in self.dev.read():
                        if ev.type == e.EV_ABS:
                            for code, value in self.transform(ev.code, ev.value):
                                self.ui.write(e.EV_ABS, code, value)
                        elif ev.type == e.EV_SYN:
                            self.ui.syn()
                        elif ev.type != e.EV_MSC:
                            self.ui.write(ev.type, ev.code, ev.value)
        except OSError as exc:
            log.info("volante: %s", exc)
        finally:
            for real in self.ids.values():
                try:
                    self.dev.erase_effect(real)
                except OSError:
                    pass
            try:
                self.dev.ungrab()
            except OSError:
                pass
            self.ui.close()
            self.dev.close()


def damper_effect(strength: int) -> ff.Effect:
    coeff = int(strength * 0x7FFF / 100)
    cond = ff.Condition(right_saturation=0xFFFF, left_saturation=0xFFFF, right_coeff=coeff, left_coeff=coeff, deadband=0, center=0)
    return ff.Effect(e.FF_DAMPER, -1, 0, ff.Trigger(0, 0), ff.Replay(0, 0), ff.EffectType(ff_condition_effect=(ff.Condition * 2)(cond, cond)))
