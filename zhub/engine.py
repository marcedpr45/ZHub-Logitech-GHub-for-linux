"""Motor: captura los dispositivos, aplica el perfil activo y ejecuta las acciones."""
from __future__ import annotations

import logging
import os
import queue
import selectors
import shlex
import subprocess
import threading
import time
from typing import Callable

import evdev
from evdev import UInput, ecodes as e

from . import g600, hidpp, macros, wheel
from .assets import is_newer
from .devices import CATALOG, MOUSE_BUTTONS, DeviceSpec, key_code
from .profiles import WindowInfo, default_mouse_settings, resolve

log = logging.getLogger("zhub")
VIRTUAL_PREFIX = "ZHub"
LOGITECH = 0x046D
_BTN_RANGE = range(0x110, 0x118)
PROBE_SECONDS = 20
RANGE_REASSERT = (3, 7)  # esperas sucesivas: se reescribe a los 3 s y a los 10 s del cambio


class Output:
    """Ratón y teclado virtuales; recuerda lo pulsado para no dejar teclas atascadas."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pressed: set[int] = set()
        self.mouse = UInput(
            {
                e.EV_KEY: list(_BTN_RANGE),
                e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL, e.REL_WHEEL_HI_RES, e.REL_HWHEEL_HI_RES],
            },
            name=f"{VIRTUAL_PREFIX} Virtual Mouse",
        )
        keys = [c for c in range(1, 0x2FF) if c not in range(0x100, 0x160)]
        self.kbd = UInput({e.EV_KEY: keys}, name=f"{VIRTUAL_PREFIX} Virtual Keyboard")

    def key(self, code: int, down: bool) -> None:
        dev = self.mouse if code in _BTN_RANGE else self.kbd
        with self.lock:
            (self.pressed.add if down else self.pressed.discard)(code)
            dev.write(e.EV_KEY, code, int(down))
            dev.syn()

    def rel(self, code: int, value: int, sync: bool = False) -> None:
        with self.lock:
            self.mouse.write(e.EV_REL, code, value)
            if sync:
                self.mouse.syn()

    def syn(self) -> None:
        with self.lock:
            self.mouse.syn()

    def release_all(self) -> None:
        for code in list(self.pressed):
            self.key(code, False)

    def close(self) -> None:
        self.release_all()
        self.mouse.close()
        self.kbd.close()


class Session:
    """Estado de un dispositivo físico conectado."""

    def __init__(self, engine: "Engine", spec: DeviceSpec, paths: list[str] | None = None):
        self.engine, self.spec = engine, spec
        self.paths = paths or []
        self.down: dict[str, dict] = {}
        self.gshift = False
        self.loops: dict[str, threading.Event] = {}
        self.levels: list[int] = []
        self.dpi = 0
        self.rate = 0
        self.saved_dpi: int | None = None
        self.hid: hidpp.HidppDevice | None = None
        self.hid_error = ""
        self.battery = ""
        self.stop = threading.Event()
        self.online = False
        self.update = ""
        self.g600: g600.G600 | None = None
        self.transform: wheel.WheelTransform | None = None
        self.ff_dev: evdev.InputDevice | None = None
        self.damper_id: int | None = None
        self.range_for: str | None = None  # perfil cuyo giro se escribió por última vez
        self.range_wake = threading.Event()
        self.fails = 0
        self.firmware = ""
        self.last_input = 0.0
        self.last_bring = 0.0
        self.hw: queue.Queue = queue.Queue()
        self.ranges: dict = {}

    # ---- configuración activa ----
    def cfg(self) -> dict:
        return self.engine.profile.get("devices", {}).get(self.spec.id, {})

    def binding(self, button: str, shifted: bool) -> dict:
        cfg = self.cfg()
        if shifted and button in cfg.get("shifted", {}):
            return cfg["shifted"][button]
        return cfg.get("buttons", {}).get(button) or self.spec.defaults.get(button, {"type": "none"})

    # ---- entrada ----
    def on_event(self, ev) -> None:
        out = self.engine.out
        if ev.type == e.EV_KEY:
            if ev.value == 2:
                if ev.code not in self.spec.inputs:
                    out.key(ev.code, True)
                return
            button = self.spec.inputs.get(ev.code)
            if button:
                self.on_button(button, bool(ev.value))
            else:
                out.key(ev.code, bool(ev.value))
        elif ev.type == e.EV_REL:
            out.rel(ev.code, ev.value)
        elif ev.type == e.EV_SYN and ev.code == e.SYN_REPORT:
            out.syn()

    def on_button(self, button: str, pressed: bool) -> None:
        if pressed:
            if button in self.down:
                return
            action = self.binding(button, self.gshift)
            self.down[button] = action
            self.begin(button, action)
        else:
            action = self.down.pop(button, None)
            if action:
                self.end(button, action)

    def begin(self, button: str, a: dict) -> None:
        out, kind = self.engine.out, a.get("type")
        if kind == "mouse":
            out.key(e.ecodes[MOUSE_BUTTONS[a["button"]]], True)
        elif kind == "key":
            for k in a["keys"]:
                out.key(key_code(k), True)
        elif kind == "wheel":
            axis = (e.REL_WHEEL, e.REL_WHEEL_HI_RES) if a["dir"] in ("up", "down") else (e.REL_HWHEEL, e.REL_HWHEEL_HI_RES)
            sign = 1 if a["dir"] in ("up", "right") else -1
            out.rel(axis[0], sign)
            out.rel(axis[1], 120 * sign, sync=True)
        elif kind == "double_click":
            for down in (True, False, True, False):
                out.key(e.BTN_LEFT, down)
        elif kind in ("macro", "sequence"):
            m = macros.resolve(self.engine.profile, a)
            if m:
                self.start_macro(button, m)
        elif kind == "gshift":
            self.gshift = True
        elif kind == "dpi":
            self.dpi_action(a["op"])
        elif kind == "report_rate_cycle":
            self.cycle_rate()
        elif kind == "profile_cycle":
            self.engine.cycle_profile()
        elif kind == "run":
            subprocess.Popen(shlex.split(a["command"]), start_new_session=True)

    def end(self, button: str, a: dict) -> None:
        out, kind = self.engine.out, a.get("type")
        if kind == "mouse":
            out.key(e.ecodes[MOUSE_BUTTONS[a["button"]]], False)
        elif kind == "key":
            for k in reversed(a["keys"]):
                out.key(key_code(k), False)
        elif kind == "gshift":
            self.gshift = False
        elif kind == "dpi" and a["op"] == "shift" and self.saved_dpi is not None:
            self.write_dpi(self.saved_dpi)
            self.saved_dpi = None
        elif kind in ("macro", "sequence") and button in self.loops:
            m = macros.resolve(self.engine.profile, a)
            if m and m.get("mode") in ("repeat_hold", "sequence"):
                self.loops[button].set()

    # ---- macros ----
    def start_macro(self, button: str, m: dict) -> None:
        mode = m.get("mode", "no_repeat")
        standard = m.get("delay", macros.DEFAULT_DELAY) if m.get("standard_delay") else None
        player = macros.Player(self.engine.out)
        play = lambda steps, stop=None: player.run(steps, stop, standard)  # noqa: E731
        if mode == "toggle" and button in self.loops:
            self.loops.pop(button).set()
            return
        if button in self.loops:
            return
        stop = threading.Event()
        self.loops[button] = stop

        def loop(steps: list[dict]) -> None:
            while steps and not stop.is_set():
                play(steps, stop)
                stop.wait(0.001)

        def run() -> None:
            try:
                if mode == "no_repeat":
                    play(m.get("steps", []))
                elif mode in ("repeat_hold", "toggle"):
                    loop(m.get("steps", []))
                else:
                    play(m.get("press", []))
                    loop(m.get("held", []))
                    stop.wait()
                    play(m.get("release", []))
            finally:
                if self.loops.get(button) is stop:
                    del self.loops[button]

        threading.Thread(target=run, daemon=True).start()

    # ---- hardware ----
    def queue_hw(self, fn: Callable, *args) -> None:
        self.hw.put((fn, args))

    def hw_loop(self) -> None:
        while not self.stop.is_set():
            try:
                fn, args = self.hw.get(timeout=0.3)
            except queue.Empty:
                self.watch_link()
                continue
            try:
                fn(*args)
                self.hid_error = ""
            except (OSError, hidpp.HidppError, g600.G600Error) as exc:
                self.hid_error = str(exc)
                log.warning("%s: %s", self.spec.id, exc)
            self.engine.changed()

    def write_dpi(self, value: int) -> None:
        self.dpi = value
        if self.g600:
            self.queue_hw(self.set_g600_dpi, value)
        elif self.writable:
            self.queue_hw(self.hid.set_dpi, value)
        self.engine.changed()

    def mouse_settings(self) -> dict:
        return self.cfg().get("mouse") or default_mouse_settings()

    def dpi_action(self, op: str) -> None:
        m = self.mouse_settings()
        levels = m["levels"]
        if op == "shift":
            self.saved_dpi = self.dpi
            self.write_dpi(m["shift"])
        elif op == "default":
            self.write_dpi(m["default"])
        else:
            idx = levels.index(self.dpi) if self.dpi in levels else 0
            step = -1 if op == "down" else 1
            if op == "cycle":
                self.write_dpi(levels[(idx + 1) % len(levels)])
            else:
                self.write_dpi(levels[min(max(idx + step, 0), len(levels) - 1)])

    def cycle_rate(self) -> None:
        rates = self.ranges.get("rates") or [125, 250, 500, 1000]
        idx = rates.index(self.rate) if self.rate in rates else -1
        self.rate = rates[(idx + 1) % len(rates)]
        if self.writable:
            self.queue_hw(self.hid.set_report_rate, self.rate)
        self.engine.changed()

    def apply_profile(self) -> None:
        if self.spec.kind == "wheel":
            self.apply_wheel()
            return
        m = self.mouse_settings()
        if self.spec.has_lighting:
            self.dpi, self.rate = m["default"], m.get("report_rate", 1000)
            self.queue_hw(self.apply_g600)
            return
        if self.dpi != m["default"]:
            self.write_dpi(m["default"])
        if m.get("report_rate") and self.rate != m["report_rate"]:
            self.rate = m["report_rate"]
            if self.writable:
                self.queue_hw(self.hid.set_report_rate, self.rate)

    def ff_path(self) -> str | None:
        for path in self.paths:
            if e.EV_FF in evdev.InputDevice(path).capabilities():
                return path
        return None

    def apply_wheel(self) -> None:
        raw = self.cfg().get("wheel")
        path = self.ff_path()
        if not raw or not path:
            return
        game = wheel.game_controls(raw)
        w = wheel.force_settings(raw)
        self.hid_error = ""
        if "range" in w:
            profile_id = self.engine.profile["id"]
            # Si manda el juego, no se fuerza ni se reafirma: así no se pisa el giro
            # que el juego haya puesto al volver a él con Alt+Tab.
            fresh = profile_id != self.range_for and not game
            self.range_for = profile_id
            if game:
                self.range_wake.set()
            self.hid_error = wheel.write_range(w["range"], force=fresh)
            if fresh:
                self.reassert_range(profile_id, w["range"])
        want = wheel.needs_transform(w)
        if want and not self.transform:
            self.transform = wheel.WheelTransform(path, w)
            self.transform.start()
        elif self.transform and not want:
            self.transform.stop()
            self.transform = None
        elif self.transform:
            self.transform.settings = w
        try:
            if self.ff_dev is None:
                self.ff_dev = evdev.InputDevice(path)
            self.ff_dev.write(e.EV_FF, e.FF_AUTOCENTER, wheel.autocenter_level(w))
            if "gain" in w:
                self.ff_dev.write(e.EV_FF, e.FF_GAIN, int(w["gain"] * 0xFFFF / 100))
            if self.damper_id is not None:
                self.ff_dev.erase_effect(self.damper_id)
                self.damper_id = None
            if w.get("damping"):
                self.damper_id = self.ff_dev.upload_effect(wheel.damper_effect(w["damping"]))
                self.ff_dev.write(e.EV_FF, self.damper_id, 1)
        except OSError as exc:
            self.hid_error = str(exc)

    def reassert_range(self, profile_id: str, degrees: int) -> None:
        """Vuelve a escribir el giro mientras el juego recién enfocado inicializa el volante."""
        self.range_wake.set()  # cancela la espera del cambio de perfil anterior
        wake = self.range_wake = threading.Event()

        def run() -> None:
            for delay in RANGE_REASSERT:
                if wake.wait(delay) or self.stop.is_set() or self.engine.profile["id"] != profile_id:
                    return
                wheel.write_range(degrees, force=True)

        threading.Thread(target=run, daemon=True).start()

    def apply_g600(self) -> None:
        if self.g600:
            m = self.mouse_settings()
            if self.g600.apply(m, self.cfg().get("lighting") or {}):
                self.g600.select_mode(g600.SLOT_MODE)
            self.set_g600_dpi(m["default"])

    def set_g600_dpi(self, value: int) -> None:
        """El nivel se cambia en memoria volátil; no escribe en el ratón."""
        self.g600.set_level(g600.level_index(self.mouse_settings(), value))

    def note_firmware(self) -> None:
        reference = self.engine.fw_refs.get(self.spec.id, "")
        self.update = reference if reference and self.firmware and is_newer(reference, self.firmware) else ""
        self.engine.changed()

    def read_wheel_firmware(self) -> None:
        self.firmware = wheel.read_firmware(self.engine.fw_refs.get(self.spec.id, ""))
        self.note_firmware()

    @property
    def writable(self) -> bool:
        return self.hid is not None and self.online

    def open_hid(self) -> None:
        if not self.spec.hidpp:
            self.online = True
            if self.spec.id == "g600":
                self.firmware = hidpp.usb_version(0xC24A)
                try:
                    self.g600 = g600.G600.open()
                    self.g600.activate()
                except (OSError, g600.G600Error) as exc:
                    self.hid_error = f"ajustes del G600 no disponibles: {exc}"
            elif self.spec.kind == "wheel":
                # Antes de aplicar ajustes: el volante no atiende una consulta HID++ mientras recibe órdenes de fuerza
                self.read_wheel_firmware()
            return
        endpoint = hidpp.find_endpoint(self.spec.pids[0], self.spec.receivers)
        if not endpoint:
            self.online, self.hid_error = True, "no se encontró la interfaz HID++"
            return
        try:
            self.hid = hidpp.HidppDevice(*endpoint)
        except PermissionError:
            self.online, self.hid_error = True, "sin permiso sobre hidraw: instala la regla udev"
            return
        self.queue_hw(self.bring_up)

    def bring_up(self) -> None:
        """Prepara el dispositivo; un apagado lo devuelve a su memoria interna y hay que repetirlo."""
        self.last_bring = time.monotonic()
        hid = self.hid
        hid.reset()
        try:
            if hid.get_mode() not in (None, hidpp.MODE_HOST):
                hid.set_mode(hidpp.MODE_HOST)
            self.ranges = {"dpi": hid.dpi_list(), "rates": hid.report_rates()}
            self.dpi = hid.get_dpi()
            self.rate = hid.get_report_rate()
            self.battery = hid.battery()
            self.firmware = hid.firmware()
            self.note_firmware()
        except (OSError, hidpp.HidppError) as exc:
            log.info("%s no disponible: %s", self.spec.id, exc)
            self.set_offline()
            return
        was_online, self.online, self.fails = self.online, True, 0
        self.hid_error = ""
        if not was_online:
            log.info("%s conectado", self.spec.id)
        self.apply_profile()
        self.engine.changed()

    def set_offline(self) -> None:
        if self.online:
            log.info("%s desconectado", self.spec.id)
        self.online, self.hid_error = False, ""
        self.engine.changed()

    def probe(self) -> None:
        if not self.online:
            self.bring_up()
        elif self.hid.ping():
            self.fails = 0
            self.update_battery()
        else:
            self.fails += 1
            if self.fails >= 2:
                self.set_offline()

    def watch_link(self) -> None:
        """Atiende en el hilo de hardware los avisos de enlace del receptor."""
        if not self.hid:
            return
        self.hid.poll()
        link, self.hid.link = self.hid.link, None
        if link is True and not self.online:
            time.sleep(0.3)
            self.bring_up()
        elif link is False and self.online:
            self.set_offline()

    def on_input(self) -> None:
        """Cualquier evento físico demuestra que el dispositivo está activo."""
        now = time.monotonic()
        self.last_input = now
        if self.hid and not self.online and now - self.last_bring >= 1:
            self.last_bring = now
            self.queue_hw(self.bring_up)

    def close_hid(self) -> None:
        if self.hid:
            try:
                if self.online:
                    self.hid.set_mode(hidpp.MODE_ONBOARD)
                self.hid.close()
            except (OSError, hidpp.HidppError):
                pass
            self.hid = None

    def run(self) -> None:
        devs = [evdev.InputDevice(p) for p in self.paths]
        sel = selectors.DefaultSelector()
        grabbed = []
        hw_thread = None
        try:
            if self.spec.kind == "mouse":
                for d in devs:
                    d.grab()
                    grabbed.append(d)
                    sel.register(d, selectors.EVENT_READ)
            hw_thread = threading.Thread(target=self.hw_loop, daemon=True)
            hw_thread.start()
            self.open_hid()
            if not self.hid:
                self.apply_profile()
            self.engine.changed()
            last_probe = time.monotonic()
            while not self.stop.is_set():
                if grabbed:
                    for key, _ in sel.select(0.25):
                        self.on_input()
                        for ev in key.fileobj.read():
                            self.on_event(ev)
                else:
                    self.stop.wait(0.25)
                    if not all(evdev.util.is_device(p) for p in self.paths):
                        break
                now = time.monotonic()
                if self.hid and now - last_probe >= PROBE_SECONDS and now - self.last_input >= PROBE_SECONDS:
                    last_probe = now
                    self.queue_hw(self.probe)
        except OSError as exc:
            log.info("%s desconectado: %s", self.spec.id, exc)
        finally:
            for loop in list(self.loops.values()):
                loop.set()
            self.stop.set()
            for d in grabbed:
                try:
                    d.ungrab()
                except OSError:
                    pass
            if hw_thread is not None:
                hw_thread.join(1.5)
            self.close_hid()
            if self.transform:
                self.transform.stop()
            if self.g600:
                try:
                    self.g600.close()
                except OSError:
                    pass
            self.engine.detached(self)

    def update_battery(self) -> None:
        try:
            self.battery = self.hid.battery()
        except (OSError, hidpp.HidppError):
            pass

    def status(self) -> dict:
        return {
            "id": self.spec.id,
            "name": self.spec.name,
            "dpi": self.dpi,
            "rate": self.rate,
            "battery": self.battery,
            "error": self.hid_error,
            "firmware": self.firmware,
            "update": self.update,
            "ranges": self.ranges,
        }


class Engine:
    def __init__(self, cfg: dict, output: Output | None = None):
        self.cfg = cfg
        self.out = output
        self.window: WindowInfo | None = None
        self.profile = resolve(cfg, None)
        self.sessions: dict[str, Session] = {}
        self.lock = threading.RLock()
        self.running = threading.Event()
        self.on_change: Callable[[], None] = lambda: None
        self.cycled: str | None = None
        self.fw_refs: dict[str, str] = {}

    def changed(self) -> None:
        self.on_change()

    def start(self) -> None:
        if self.out is None:
            self.out = Output()
        self.running.set()
        threading.Thread(target=self._hotplug, daemon=True).start()

    def stop(self) -> None:
        self.running.clear()
        with self.lock:
            sessions = list(self.sessions.values())
        for s in sessions:
            s.stop.set()
        deadline = time.monotonic() + 2
        while self.sessions and time.monotonic() < deadline:
            time.sleep(0.02)
        if self.out:
            self.out.close()

    def detached(self, session: Session) -> None:
        with self.lock:
            if self.sessions.get(session.spec.id) is session:
                del self.sessions[session.spec.id]
        self.changed()

    def _scan(self) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue
            if dev.name.startswith(VIRTUAL_PREFIX) or dev.phys == wheel.VIRTUAL_PHYS:
                dev.close()
                continue
            for spec in CATALOG.values():
                if dev.info.vendor == LOGITECH and dev.info.product in spec.pids:
                    found.setdefault(spec.id, []).append(path)
            dev.close()
        return found

    def _hotplug(self) -> None:
        while self.running.is_set():
            for dev_id, paths in self._scan().items():
                with self.lock:
                    if dev_id in self.sessions:
                        continue
                    session = Session(self, CATALOG[dev_id], paths)
                    self.sessions[dev_id] = session
                threading.Thread(target=session.run, daemon=True).start()
            time.sleep(2)

    # ---- perfiles ----
    def set_window(self, win: WindowInfo | None) -> None:
        if win is not None and win.pid == os.getpid():
            return  # como G HUB: al abrir ZHub se mantiene el perfil del juego
        self.window = win
        self.cycled = None
        self._apply(resolve(self.cfg, win))

    def set_config(self, cfg: dict) -> None:
        self.cfg = cfg
        self._apply(self._current(), force=True)

    def _current(self) -> dict:
        from .profiles import profile_by_id

        return profile_by_id(self.cfg, self.cycled) or resolve(self.cfg, self.window)

    def _apply(self, profile: dict, force: bool = False) -> None:
        with self.lock:
            changed = force or profile["id"] != self.profile["id"]
            self.profile = profile
            sessions = list(self.sessions.values())
        if changed:
            for s in sessions:
                s.apply_profile()
            log.info("perfil activo: %s", profile["name"])
            self.changed()

    def cycle_profile(self) -> None:
        ids = [p for p in self.cfg["profiles"]]
        idx = next((i for i, p in enumerate(ids) if p["id"] == self.profile["id"]), 0)
        target = ids[(idx + 1) % len(ids)]
        self.cycled = target["id"]
        self._apply(target)

    def axes_path(self, device_id: str) -> str | None:
        with self.lock:
            session = self.sessions.get(device_id)
        return session.ff_path() if session else None

    def status(self) -> list[dict]:
        with self.lock:
            return [s.status() for s in self.sessions.values() if s.online]
