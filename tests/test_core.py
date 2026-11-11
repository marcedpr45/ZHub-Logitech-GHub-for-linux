import os, subprocess, threading, time, unittest
from types import SimpleNamespace
from evdev import ecodes as e
from zhub import assets, g600, hidpp, profiles, ghub_import, wheel
from zhub.devices import G305, G600, hid_to_evdev, key_name
from zhub.engine import Engine, Session


class FakeOut:
    def __init__(self): self.log = []
    def key(self, c, d): self.log.append((c, d))
    def rel(self, c, v, sync=False): self.log.append(("rel", c, v))
    def syn(self): pass
    def release_all(self): pass


def setup(prof=None, spec=G305):
    cfg = profiles.default_config()
    if prof:
        cfg["profiles"][0]["devices"]["g305"] = prof
    eng = Engine(cfg, FakeOut())
    return eng, Session(eng, spec)


def ev(t, c, v): return SimpleNamespace(type=t, code=c, value=v)


class Hid(unittest.TestCase):
    def test_key_table(self):
        self.assertEqual(hid_to_evdev(4), e.KEY_A)
        self.assertEqual(hid_to_evdev(0x1e), e.KEY_1)
        self.assertEqual(hid_to_evdev(224), e.KEY_LEFTCTRL)
        self.assertEqual(key_name(e.KEY_A), "KEY_A")
        self.assertEqual(key_name(e.BTN_LEFT), "BTN_LEFT")

    def test_dpi_range(self):
        # 200, paso 50, hasta 400, fin
        d = bytes([0, 200, 0xE0, 50, 1, 144, 0, 0])
        self.assertEqual(hidpp.parse_dpi_list(d), [200, 250, 300, 350, 400])

    def test_rates(self):
        self.assertEqual(hidpp.parse_rate_flags(0b10001011), [125, 250, 500, 1000])


class Bindings(unittest.TestCase):
    def test_default_passthrough(self):
        eng, s = setup()
        s.on_event(ev(e.EV_KEY, e.BTN_LEFT, 1)); s.on_event(ev(e.EV_KEY, e.BTN_LEFT, 0))
        self.assertEqual(eng.out.log, [(e.BTN_LEFT, True), (e.BTN_LEFT, False)])

    def test_remap_key_chord(self):
        eng, s = setup({"buttons": {"g4": {"type": "key", "keys": ["KEY_LEFTCTRL", "KEY_C"]}}})
        s.on_button("g4", True); s.on_button("g4", False)
        self.assertEqual(eng.out.log, [(e.KEY_LEFTCTRL, True), (e.KEY_C, True), (e.KEY_C, False), (e.KEY_LEFTCTRL, False)])

    def test_gshift_layer_and_release_uses_pressed_binding(self):
        eng, s = setup({"buttons": {"g5": {"type": "gshift"}}, "shifted": {"g4": {"type": "key", "keys": ["KEY_F"]}}})
        s.on_button("g5", True); s.on_button("g4", True); s.on_button("g5", False); s.on_button("g4", False)
        self.assertEqual(eng.out.log, [(e.KEY_F, True), (e.KEY_F, False)])

    def test_dpi_cycle_shift(self):
        eng, s = setup({"mouse": {"levels": [400, 800], "default": 800, "shift": 200, "report_rate": 1000}})
        s.apply_profile(); self.assertEqual(s.dpi, 800)
        s.on_button("g6", True); s.on_button("g6", False); self.assertEqual(s.dpi, 400)
        s.dpi_action("shift"); self.assertEqual(s.dpi, 200)

    def test_macro_tap(self):
        steps = [{"key": "KEY_A", "down": True}, {"delay": 1}, {"key": "KEY_A", "down": False}]
        eng, s = setup({"buttons": {"g4": {"type": "sequence", "press": steps}}})
        s.on_button("g4", True); time.sleep(0.05); s.on_button("g4", False)
        self.assertEqual(eng.out.log, [(e.KEY_A, True), (e.KEY_A, False)])
        self.assertEqual(s.loops, {})

    def test_macro_hold_and_toggle_stop(self):
        steps = [{"key": "KEY_A", "tap": True}, {"delay": 1}]
        eng, s = setup({"buttons": {"g4": {"type": "sequence", "held": steps}, "g5": {"type": "sequence", "toggle": steps}}})
        s.on_button("g4", True); time.sleep(0.03); s.on_button("g4", False); time.sleep(0.03)
        n = len(eng.out.log); self.assertGreater(n, 2)
        time.sleep(0.03); self.assertEqual(len(eng.out.log), n)
        s.on_button("g5", True); s.on_button("g5", False); time.sleep(0.03)
        s.on_button("g5", True); s.on_button("g5", False); time.sleep(0.03)
        n = len(eng.out.log); time.sleep(0.03); self.assertEqual(len(eng.out.log), n)

    def lib(self, **kw):
        from zhub import macros
        m = macros.new_macro("t", kw.pop("mode"))
        m.update(kw)
        eng, s = setup({"buttons": {"g4": {"type": "macro", "id": m["id"]}}})
        eng.profile["macros"] = [m]
        return eng, s

    def test_library_no_repeat_and_standard_delay(self):
        steps = [{"key": "KEY_A", "down": True}, {"delay": 500}, {"key": "KEY_A", "down": False}]
        eng, s = self.lib(mode="no_repeat", steps=steps, standard_delay=True, delay=1)
        t = time.monotonic(); s.on_button("g4", True); s.on_button("g4", False); time.sleep(0.05)
        self.assertLess(time.monotonic() - t, 0.4)  # el retardo grabado se sustituye por el estándar
        self.assertEqual(eng.out.log, [(e.KEY_A, True), (e.KEY_A, False)])

    def test_library_repeat_hold_stops_on_release(self):
        eng, s = self.lib(mode="repeat_hold", steps=[{"key": "KEY_B", "tap": True}, {"delay": 2}])
        s.on_button("g4", True); time.sleep(0.03); s.on_button("g4", False); time.sleep(0.02)
        n = len(eng.out.log); self.assertGreater(n, 2); time.sleep(0.03); self.assertEqual(len(eng.out.log), n)

    def test_library_toggle_needs_second_press(self):
        eng, s = self.lib(mode="toggle", steps=[{"key": "KEY_C", "tap": True}, {"delay": 2}])
        s.on_button("g4", True); s.on_button("g4", False); time.sleep(0.03)
        n = len(eng.out.log); time.sleep(0.02); self.assertGreater(len(eng.out.log), n)
        s.on_button("g4", True); s.on_button("g4", False); time.sleep(0.02)
        n = len(eng.out.log); time.sleep(0.03); self.assertEqual(len(eng.out.log), n)

    def test_library_sequence_phases(self):
        eng, s = self.lib(mode="sequence", press=[{"key": "KEY_LEFTCTRL", "down": True}],
                          held=[{"key": "KEY_D", "tap": True}, {"delay": 5}], release=[{"key": "KEY_LEFTCTRL", "down": False}])
        s.on_button("g4", True); time.sleep(0.03); s.on_button("g4", False); time.sleep(0.03)
        log = eng.out.log
        self.assertEqual(log[0], (e.KEY_LEFTCTRL, True)); self.assertEqual(log[-1], (e.KEY_LEFTCTRL, False))
        self.assertIn((e.KEY_D, True), log); self.assertEqual(s.loops, {})

    def test_text_and_run_steps(self):
        from zhub import macros
        out, ran = FakeOut(), []
        macros.Player(out, ran.append).run([{"text": "Hi!"}, {"run": "spectacle -r"}])
        self.assertEqual(out.log, [(e.KEY_LEFTSHIFT, True), (e.KEY_H, True), (e.KEY_H, False), (e.KEY_LEFTSHIFT, False),
                                   (e.KEY_I, True), (e.KEY_I, False),
                                   (e.KEY_LEFTSHIFT, True), (e.KEY_1, True), (e.KEY_1, False), (e.KEY_LEFTSHIFT, False)])
        self.assertEqual(ran, ["spectacle -r"])

    def test_ghub_macro_modes(self):
        seq = lambda *c: {"components": list(c)}
        k = lambda u, d: {"keyboard": {"hidUsage": str(u), "isDown": d}}
        held = ghub_import.macro_definition({"useSimpleActions": False, "heldSequence": seq(k(4, True), k(4, False)), "defaultDelay": 30}, "x", "Spam")
        self.assertEqual((held["mode"], held["delay"], held["id"]), ("repeat_hold", 30, "x"))
        tog = ghub_import.macro_definition({"useSimpleActions": False, "toggleSequence": seq(k(4, True))}, "y", "T")
        self.assertEqual(tog["mode"], "toggle")
        sq = ghub_import.macro_definition({"useSimpleActions": False, "pressSequence": seq(k(4, True)), "releaseSequence": seq(k(4, False))}, "z", "S")
        self.assertEqual(sq["mode"], "sequence"); self.assertEqual(sq["release"], [{"key": "KEY_A", "down": False}])

    def test_g600_soft_buttons(self):
        eng, s = setup(spec=G600)
        eng.profile["devices"]["g600"] = {"shifted": {"g9": {"type": "key", "keys": ["KEY_Z"]}}, "buttons": {"g8": {"type": "key", "keys": ["KEY_X"]}}}
        s.on_event(ev(e.EV_KEY, e.KEY_F13, 1)); self.assertTrue(s.gshift)
        s.on_event(ev(e.EV_KEY, e.KEY_1, 1)); s.on_event(ev(e.EV_KEY, e.KEY_1, 0))
        s.on_event(ev(e.EV_KEY, e.KEY_F13, 0)); self.assertFalse(s.gshift)
        s.on_event(ev(e.EV_KEY, e.KEY_F15, 1)); s.on_event(ev(e.EV_KEY, e.KEY_F15, 0))
        self.assertEqual(eng.out.log, [(e.KEY_Z, True), (e.KEY_Z, False), (e.KEY_X, True), (e.KEY_X, False)])


class LiveValues(unittest.TestCase):
    def test_scale(self):
        from types import SimpleNamespace as N
        from zhub.wheel import LiveAxes
        axes = LiveAxes.__new__(LiveAxes)
        data = {e.ABS_X: N(value=65535, min=0, max=65535), e.ABS_Y: N(value=255, min=0, max=255), e.ABS_Z: N(value=0, min=0, max=255), e.ABS_RZ: N(value=51, min=0, max=255)}
        axes.dev = N(absinfo=data.__getitem__)
        self.assertEqual(axes.read(), {"wheel": 1.0, "throttle": 1.0, "brake": 0.0, "clutch": 0.2})


class FakeHid:
    def __init__(self):
        self.up, self.link, self.dpi, self.writes = True, None, 800, []
    def reset(self): pass
    def _check(self):
        if not self.up: raise hidpp.HidppError("apagado")
    def get_mode(self): self._check(); return 1
    def set_mode(self, m): self._check(); self.writes.append(("mode", m))
    def dpi_list(self): return [400, 800]
    def report_rates(self): return [125, 1000]
    def get_dpi(self): self._check(); return self.dpi
    def get_report_rate(self): return 1000
    def battery(self): return "90 %"
    def firmware(self): return "U1 1.2"
    def set_dpi(self, v): self.dpi = v; self.writes.append(("dpi", v))
    def set_report_rate(self, v): pass
    def ping(self): return self.up
    def poll(self): pass


class Presence(unittest.TestCase):
    def make(self):
        eng, s = setup({"mouse": {"levels": [400, 1600], "default": 1600, "shift": 400, "report_rate": 1000}})
        s.hid = FakeHid(); eng.sessions["g305"] = s
        return eng, s

    def run_hw(self, s):
        while not s.hw.empty():
            fn, args = s.hw.get(); fn(*args)

    def test_offline_at_start_is_hidden(self):
        eng, s = self.make(); s.hid.up = False
        s.bring_up(); self.assertEqual(eng.status(), [])

    def test_bring_up_shows_and_applies(self):
        eng, s = self.make()
        s.bring_up(); self.run_hw(s)
        self.assertEqual(eng.status()[0]["firmware"], "U1 1.2")
        self.assertEqual(s.hid.dpi, 1600)
        self.assertIn(("mode", 2), s.hid.writes)

    def test_power_cycle_reapplies(self):
        eng, s = self.make()
        s.bring_up(); self.run_hw(s)
        s.hid.link = False; s.watch_link(); self.assertEqual(eng.status(), [])
        s.hid.dpi = 800; s.hid.writes.clear()   # el firmware vuelve a sus valores
        s.hid.link = True; s.watch_link(); self.run_hw(s)
        self.assertEqual(len(eng.status()), 1)
        self.assertEqual(s.hid.dpi, 1600)
        self.assertIn(("mode", 2), s.hid.writes)

    def test_ping_needs_two_failures(self):
        eng, s = self.make()
        s.bring_up(); self.run_hw(s); s.hid.up = False
        s.probe(); self.assertEqual(len(eng.status()), 1)
        s.probe(); self.assertEqual(eng.status(), [])

    def test_input_wakes(self):
        eng, s = self.make()
        s.hid.up = False; s.bring_up(); s.hid.up = True; s.last_bring = 0
        s.on_input(); self.run_hw(s)
        self.assertEqual(len(eng.status()), 1)

    def test_link_notification_parsing(self):
        h = hidpp.HidppDevice.__new__(hidpp.HidppDevice); h.devnum, h.link = 1, None
        h._note(bytes([0x10, 1, 0x41, 0x0c, 0x42, 0, 0])); self.assertIs(h.link, False)
        h._note(bytes([0x10, 1, 0x41, 0x0c, 0x02, 0, 0])); self.assertIs(h.link, True)


class G600Report(unittest.TestCase):
    def base(self):
        r = bytearray(154); r[0] = 0xF5; r[4] = 2; r[5] = 5; r[12] = 8; r[13] = 2; r[14:18] = bytes([8, 16, 32, 64])
        r[31 + 15:31 + 24] = bytes([0x17, 0, 0, 0x13, 0, 0, 0x14, 0, 0]); r[94 + 15:94 + 18] = bytes([0x17, 0, 0])
        return bytes(r)

    def test_build_matches_real_layout(self):
        s = {"levels": [800, 1600], "default": 1600, "shift": 400, "report_rate": 500}
        o = g600.build(self.base(), s, {"effect": "fixed", "color": "#102030", "period": 3000})
        self.assertEqual(o[1:4], bytes([0x10, 0x20, 0x30])); self.assertEqual(o[4], 0); self.assertEqual(o[5], 3)
        self.assertEqual(o[11], 1); self.assertEqual(o[12], 8); self.assertEqual(o[13], 2)
        self.assertEqual(o[14:19], bytes([16, 32, 8, 0, 0])); self.assertEqual(len(o), 154)
        self.assertEqual(o[46:55], bytes([0, 0, 0x68, 0, 0, 0x69, 0, 0, 0x6A]))
        self.assertEqual(o[109:112], bytes([0, 0, 0x68]))

    def test_only_touches_its_slot_and_rejects_unknown(self):
        other = bytearray(self.base()); other[0] = 0xF3
        with self.assertRaises(g600.G600Error):
            g600.validate(bytes(other))
        bad = bytearray(self.base()); bad[11] = 9
        with self.assertRaises(g600.G600Error):
            g600.build(bytes(bad), {"levels": [800], "default": 800, "shift": 400}, {})

    def test_import_gshift_modifier_is_ctrl(self):
        self.assertEqual(ghub_import.convert_card("0f82f693-5b78-4cf5-867e-011e01000000", {}), {"type": "key", "keys": ["KEY_LEFTCTRL", "KEY_1"]})


class Firmware(unittest.TestCase):
    def test_compare(self):
        self.assertEqual(assets.parse_firmware("RQM 68.01.B0014"), (68, 1, 14))
        self.assertEqual(assets.parse_firmware("U1 39.02.B0050"), (139, 2, 50))
        self.assertTrue(assets.is_newer("139.02.50", "U1 38.09.B0001"))
        self.assertFalse(assets.is_newer("139.02.50", "U1 39.02.B0050"))
        self.assertFalse(assets.is_newer("139.02.50", ""))
        self.assertFalse(assets.is_newer("139.02.50", "139.02"))
        self.assertTrue(assets.is_newer("139.03.10", "139.02"))

    def test_autocenter_toggle(self):
        self.assertEqual(wheel.autocenter_level({"autocenter": 100}), 0xFFFF)
        self.assertEqual(wheel.autocenter_level({"autocenter": 100, "autocenter_on": False}), 0)
        self.assertEqual(wheel.autocenter_level({}), int(20 * 0xFFFF / 100))

    def test_usb_version_recovers_major(self):
        self.assertEqual(wheel.full_version("39.02", "139.02.50"), "139.02")
        self.assertEqual(wheel.full_version("38.09", "139.02.50"), "38.09")
        self.assertEqual(wheel.full_version("39.02", ""), "39.02")


class WheelRange(unittest.TestCase):
    def test_profile_change_rewrites_stale_range(self):
        """Proton puede cambiar el giro por hidraw: el valor del controlador no es fiable."""
        from zhub import engine as engine_mod
        from zhub.devices import G923
        cfg = profiles.default_config()
        game = profiles.new_profile("FlatOut 2", {"exe": [], "wm_class": [], "steam_appid": ["2990"], "title": ""})
        cfg["profiles"].append(game)
        cfg["profiles"][0]["devices"]["g923"] = {"wheel": {"range": 900}}
        game["devices"]["g923"] = {"wheel": {"range": 541}}
        eng = Engine(cfg, FakeOut())
        s = Session(eng, G923)
        writes = []
        old = (wheel.write_range, Session.ff_path, engine_mod.RANGE_REASSERT)
        wheel.write_range = lambda d, force=False: writes.append((d, force)) or ""
        Session.ff_path = lambda self: "/nonexistent"
        engine_mod.RANGE_REASSERT = (0.05,)
        try:
            eng.sessions["g923"] = s
            eng.set_window(profiles.WindowInfo("steam_app_2990", "FlatOut 2"))
            self.assertEqual(writes[0], (541, True))
            time.sleep(0.2)
            self.assertEqual(writes[1], (541, True))  # se reafirma tras iniciar el juego
            eng.set_config(cfg)  # mismo perfil: no hace falta forzar
            self.assertEqual(writes[-1], (541, False))
        finally:
            wheel.write_range, Session.ff_path, engine_mod.RANGE_REASSERT = old
            s.stop.set()


    def test_game_control_leaves_wheel_neutral(self):
        from zhub import engine as engine_mod
        from zhub.devices import G923
        cfg = profiles.default_config()
        game = profiles.new_profile("AC", {"exe": [], "wm_class": [], "steam_appid": ["244210"], "title": ""})
        cfg["profiles"].append(game)
        game["devices"]["g923"] = {"wheel": {"range": 540, "autocenter_on": True, "autocenter": 80,
                                             "damping": 30, "gain": 60, "control": "game"}}
        eng = Engine(cfg, FakeOut())
        s = Session(eng, G923)
        writes = []
        old = (wheel.write_range, Session.ff_path, engine_mod.RANGE_REASSERT)
        wheel.write_range = lambda d, force=False: writes.append((d, force)) or ""
        Session.ff_path = lambda self: "/nonexistent"
        engine_mod.RANGE_REASSERT = (0.05,)
        try:
            eng.sessions["g923"] = s
            eng.set_window(profiles.WindowInfo("steam_app_244210", "AC"))
            time.sleep(0.2)
            self.assertEqual(writes, [(900, False)])  # sin forzar ni reafirmar
            w = wheel.force_settings(game["devices"]["g923"]["wheel"])
            self.assertEqual(wheel.autocenter_level(w), 0)
            self.assertEqual((w["gain"], w["damping"]), (100, 0))
        finally:
            wheel.write_range, Session.ff_path, engine_mod.RANGE_REASSERT = old
            s.stop.set()


class OwnWindow(unittest.TestCase):
    def test_focusing_zhub_keeps_game_profile(self):
        cfg = profiles.default_config()
        game = profiles.new_profile("FlatOut 2", {"exe": [], "wm_class": [], "steam_appid": ["2990"], "title": ""})
        cfg["profiles"].append(game)
        eng = Engine(cfg, FakeOut())
        eng.set_window(profiles.WindowInfo("steam_app_2990", "FlatOut 2", 1))
        eng.set_window(profiles.WindowInfo("zhub", "ZHub", os.getpid()))
        self.assertEqual(eng.profile["name"], "FlatOut 2")
        eng.set_window(profiles.WindowInfo("org.kde.dolphin", "Dolphin", 2))
        self.assertEqual(eng.profile["name"], "Escritorio")


class WheelMath(unittest.TestCase):
    def test_curve(self):
        self.assertEqual(wheel.apply_curve(500, 0, 1000, 50), 500)
        self.assertLess(wheel.apply_curve(500, 0, 1000, 25), 500)
        self.assertGreater(wheel.apply_curve(500, 0, 1000, 75), 500)
        self.assertEqual(wheel.apply_curve(0, -100, 100, 25, centered=True), 0)
        self.assertEqual(wheel.apply_curve(100, -100, 100, 25, centered=True), 100)

    def test_combined(self):
        self.assertEqual(wheel.combine(0, 0, 0, 1000), 500)
        self.assertEqual(wheel.combine(1000, 0, 0, 1000), 1000)
        self.assertEqual(wheel.combine(0, 1000, 0, 1000), 0)

    def test_needs_transform(self):
        self.assertFalse(wheel.needs_transform({"sensitivity": 50, "throttle": 50}))
        self.assertTrue(wheel.needs_transform({"brake": 70}))
        self.assertTrue(wheel.needs_transform({"combined": True}))


class Resolve(unittest.TestCase):
    def cfg(self):
        c = profiles.default_config()
        self.lol = profiles.new_profile("LoL", {"exe": ["league of legends.exe"], "wm_class": [], "steam_appid": [], "title": ""})
        self.dota = profiles.new_profile("Dota", {"exe": [], "wm_class": [], "steam_appid": ["570"], "title": ""})
        self.t = profiles.new_profile("T", {"exe": [], "wm_class": ["org.kde.*"], "steam_appid": [], "title": "x["})
        c["profiles"] += [self.lol, self.dota, self.t]
        return c

    def test_rules(self):
        c = self.cfg()
        w = profiles.WindowInfo
        self.assertEqual(profiles.resolve(c, w(proton_exe="League of Legends.exe"))["name"], "LoL")
        self.assertEqual(profiles.resolve(c, w(wm_class="steam_app_570"))["name"], "Dota")
        self.assertEqual(profiles.resolve(c, w(wm_class="org.kde.dolphin"))["name"], "T")
        self.assertEqual(profiles.resolve(c, w(title="abc"))["name"], "Escritorio")

    def test_persistent_and_auto(self):
        c = self.cfg(); w = profiles.WindowInfo(wm_class="steam_app_570")
        c["auto_switch"] = False
        self.assertEqual(profiles.resolve(c, w)["name"], "Escritorio")
        c["persistent_profile"] = self.lol["id"]
        self.assertEqual(profiles.resolve(c, w)["name"], "LoL")


class SteamDetection(unittest.TestCase):
    def test_native_and_proton_by_environment(self):
        for var in ("SteamAppId", "STEAM_COMPAT_APP_ID"):
            child = subprocess.Popen(["sleep", "5"], env={**os.environ, var: "570"})
            time.sleep(0.05)
            win = profiles.window_info("some-native-game", "", child.pid)
            child.kill(); child.wait()
            self.assertEqual(win.steam_appid, "570")
            p = profiles.new_profile("Dota", {"exe": [], "wm_class": [], "steam_appid": ["570"], "title": ""})
            self.assertTrue(profiles.matches(p, win))

    def test_proton_exe_from_command_line(self):
        child = subprocess.Popen(["bash", "-c", "exec -a 'C:\\\\Games\\\\Foo Bar.exe' sleep 5"])
        time.sleep(0.05)
        win = profiles.window_info("", "", child.pid)
        child.kill(); child.wait()
        self.assertEqual(win.proton_exe, "Foo Bar.exe")


if __name__ == "__main__":
    unittest.main()
