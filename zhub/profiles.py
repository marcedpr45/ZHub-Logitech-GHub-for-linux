"""Modelo de configuración y resolución de perfil según la ventana activa."""
from __future__ import annotations

import fnmatch
import glob
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from .devices import CATALOG

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "zhub" / "config.json"


@dataclass(frozen=True)
class WindowInfo:
    wm_class: str = ""
    title: str = ""
    pid: int = 0
    exe: str = ""
    proton_exe: str = ""
    steam_appid: str = ""
    cwd: str = ""  # carpeta de trabajo: bajo Proton/Wine es la del juego
    exe_path: str = ""


def window_info(wm_class: str, title: str, pid: int) -> WindowInfo:
    """Completa la ventana con el ejecutable real y el .exe si corre bajo Proton/Wine."""
    exe, proton, steam, cwd, exe_path = "", "", "", "", ""
    if pid > 0:
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            pass
        try:
            exe_path = os.readlink(f"/proc/{pid}/exe")
            exe = os.path.basename(exe_path)
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                for arg in f.read().split(b"\0"):
                    text = arg.decode(errors="replace").replace("\\", "/")
                    if text.lower().endswith(".exe"):
                        proton = os.path.basename(text)
            with open(f"/proc/{pid}/environ", "rb") as f:
                env = dict(v.split(b"=", 1) for v in f.read().split(b"\0") if b"=" in v)
            steam = (env.get(b"STEAM_COMPAT_APP_ID") or env.get(b"SteamAppId") or b"").decode()
        except OSError:
            pass
    return WindowInfo(wm_class, title, pid, exe, proton, "" if steam == "0" else steam, cwd, exe_path)


def new_profile(name: str, match: dict | None = None, is_default: bool = False) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "default": is_default,
        "match": match or {"exe": [], "wm_class": [], "steam_appid": [], "title": "", "path": []},
        "devices": {},
    }


def default_config() -> dict:
    return {"auto_switch": True, "persistent_profile": None, "profiles": [new_profile("Escritorio", is_default=True)]}


def load_config(path: Path = CONFIG_PATH) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default_config()


def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=1, ensure_ascii=False))
    tmp.replace(path)


def default_profile(cfg: dict) -> dict:
    return next((p for p in cfg["profiles"] if p.get("default")), cfg["profiles"][0])


def profile_by_id(cfg: dict, pid: str | None) -> dict | None:
    return next((p for p in cfg["profiles"] if p["id"] == pid), None)


def matches(profile: dict, win: WindowInfo) -> bool:
    m = profile.get("match") or {}
    names = [n.lower() for n in (win.exe, win.proton_exe) if n]
    for pattern in m.get("exe", []):
        if any(fnmatch.fnmatch(n, pattern.lower()) for n in names):
            return True
    cls = win.wm_class.lower()
    if cls and any(fnmatch.fnmatch(cls, p.lower()) for p in m.get("wm_class", [])):
        return True
    from_class = re.fullmatch(r"steam_app_(\d+)", cls)
    ids = {win.steam_appid, from_class.group(1) if from_class else ""} - {""}
    if ids & {str(a) for a in m.get("steam_appid", [])}:
        return True
    for folder in m.get("path", []):
        folder = os.path.normpath(os.path.expanduser(folder))
        if folder.count("/") >= 2 and any(p == folder or p.startswith(folder + "/") for p in (win.cwd, win.exe_path) if p):
            return True
    title = m.get("title")
    if title:
        try:
            return re.search(title, win.title, re.I) is not None
        except re.error:
            return False
    return False


def resolve(cfg: dict, win: WindowInfo | None) -> dict:
    """Perfil persistente > primera coincidencia > perfil por defecto."""
    pinned = profile_by_id(cfg, cfg.get("persistent_profile"))
    if pinned:
        return pinned
    if cfg.get("auto_switch", True) and win is not None:
        for profile in cfg["profiles"]:
            if not profile.get("default") and matches(profile, win):
                return profile
    return default_profile(cfg)


def device_cfg(profile: dict, device_id: str) -> dict:
    return profile.setdefault("devices", {}).setdefault(device_id, {})


def default_mouse_settings() -> dict:
    return {"levels": [400, 800, 1600, 3200], "default": 800, "shift": 400, "report_rate": 1000}


def scan_steam_games(roots: list[str] | None = None) -> list[dict]:
    """Devuelve [{name, appid}] de los juegos instalados en las bibliotecas de Steam."""
    home = Path.home()
    roots = roots or [str(home / ".local/share/Steam"), str(home / ".steam/steam")]
    libraries = set(roots)
    for root in roots:
        vdf = Path(root) / "steamapps/libraryfolders.vdf"
        if vdf.exists():
            libraries.update(re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace")))
    found: dict[str, str] = {}
    for lib in libraries:
        for acf in glob.glob(f"{lib}/steamapps/appmanifest_*.acf"):
            text = Path(acf).read_text(errors="replace")
            appid, name = re.search(r'"appid"\s+"(\d+)"', text), re.search(r'"name"\s+"([^"]+)"', text)
            if appid and name and not re.search(r"proton|steamworks|steam linux runtime", name.group(1), re.I):
                found[appid.group(1)] = name.group(1)
    return [{"appid": a, "name": n} for a, n in sorted(found.items(), key=lambda kv: kv[1].lower())]


__all__ = ["CATALOG"]
