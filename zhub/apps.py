"""Aplicaciones y juegos instalados con los que crear perfiles.

Fuentes: Steam, Heroic (Epic, GOG, Amazon y juegos añadidos a mano), Lutris y
los accesos del menú (.desktop, incluidas las Flatpak). Los juegos de otros
launchers se reconocen por su carpeta de instalación: bajo Proton/Wine el
ejecutable real es wine, pero la carpeta de trabajo del juego es la suya.
"""
from __future__ import annotations

import configparser
import glob
import json
import os
import re
import shlex
import sqlite3
from pathlib import Path

from .profiles import scan_steam_games

GAME_SOURCES = ("Steam", "Epic", "GOG", "Amazon", "Heroic", "Lutris")
APP_SOURCE = "Aplicación"

_HOME = Path.home()
_HEROIC = [_HOME / ".config/heroic", _HOME / ".var/app/com.heroicgameslauncher.hgl/config/heroic"]
_LUTRIS = [_HOME / ".local/share/lutris", _HOME / ".var/app/net.lutris.Lutris/data/lutris"]
_DESKTOP_DIRS = [  # por orden de prioridad: el usuario sustituye al sistema
    _HOME / ".local/share/applications",
    _HOME / ".local/share/flatpak/exports/share/applications",
    Path("/var/lib/flatpak/exports/share/applications"),
    Path("/usr/local/share/applications"),
    Path("/usr/share/applications"),
]
# Ejecutables que no identifican a la aplicación (intérpretes y lanzadores)
_GENERIC_EXE = {"env", "sh", "bash", "python", "python3", "java", "wine", "wine64", "mono", "electron",
                "flatpak", "steam", "lutris", "heroic", "xdg-open", "gtk-launch", "kioclient", "kioclient5", "kioclient6",
                "appimagelauncher", "pkexec", "sudo", "exec"}
_GENERIC_GAME_EXE = re.compile(r"launcher|^start|setup|unins|crashreport", re.I)


def _entry(source: str, name: str, *, exe=(), wm_class=(), steam_appid=(), path=()) -> dict:
    return {"source": source, "name": name.strip(),
            "match": {"exe": [e for e in exe if e], "wm_class": [c for c in wm_class if c],
                      "steam_appid": [str(a) for a in steam_appid if a], "title": "",
                      "path": [_norm(p) for p in path if _usable_path(p)]}}


def _norm(path: str) -> str:
    return os.path.normpath(os.path.expanduser(str(path)))


def _usable_path(path) -> bool:
    """Una carpeta demasiado general (/, la carpeta personal) atraparía cualquier ventana."""
    if not path:
        return False
    p = _norm(path)
    return p.count("/") >= 2 and p != str(_HOME)


def _game_exe(path: str) -> str:
    name = os.path.basename(str(path).replace("\\", "/"))
    return "" if not name or _GENERIC_GAME_EXE.search(name) else name


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return None


# ---- Steam ----
def steam_games() -> list[dict]:
    return [_entry("Steam", g["name"], steam_appid=[g["appid"]]) for g in scan_steam_games()]


# ---- Heroic ----
def _titles(store_cache: Path, filename: str) -> dict[str, str]:
    data = _read_json(store_cache / filename) or {}
    games = data.get("games", data.get("library", [])) if isinstance(data, dict) else data
    return {g.get("app_name"): g.get("title") for g in games or [] if isinstance(g, dict) and g.get("app_name")}


def heroic_games(roots: list[Path] | None = None) -> list[dict]:
    found: list[dict] = []
    for root in roots or _HEROIC:
        if not root.is_dir():
            continue
        cache = root / "store_cache"
        epic = _read_json(root / "legendaryConfig/legendary/installed.json") or {}
        for app, g in epic.items():
            if isinstance(g, dict) and not g.get("is_dlc") and g.get("install_path"):
                found.append(_entry("Epic", g.get("title") or app, exe=[_game_exe(g.get("executable", ""))],
                                    path=[g["install_path"]]))
        gog_titles = _titles(cache, "gog_library.json")
        gog = _read_json(root / "gog_store/installed.json") or {}
        for g in gog.get("installed", []) if isinstance(gog, dict) else []:
            path, app = g.get("install_path"), str(g.get("appName", ""))
            if not path or g.get("is_dlc"):
                continue
            title = gog_titles.get(app)
            info = _read_json(Path(path) / f"goggame-{app}.info") or {}
            exe = ""
            for task in info.get("playTasks", []):
                if task.get("isPrimary") and task.get("path"):
                    exe = _game_exe(task["path"])
            found.append(_entry("GOG", title or info.get("name") or os.path.basename(path), exe=[exe], path=[path]))
        amazon_titles = _titles(cache, "nile_library.json")
        amazon = _read_json(root / "nile_config/nile/installed.json") or []
        for g in amazon if isinstance(amazon, list) else []:
            if g.get("path"):
                found.append(_entry("Amazon", amazon_titles.get(g.get("id")) or os.path.basename(g["path"]), path=[g["path"]]))
        side = _read_json(root / "sideload_apps/library.json") or {}
        for g in side.get("games", []) if isinstance(side, dict) else []:
            exe = (g.get("install") or {}).get("executable", "")
            if g.get("title") and exe:
                found.append(_entry("Heroic", g["title"], exe=[_game_exe(exe)], path=[os.path.dirname(exe)]))
    return found


# ---- Lutris ----
def _lutris_game_config(text: str) -> dict[str, str]:
    """Lee exe, prefix y working_dir del bloque game: del .yml sin depender de PyYAML."""
    values: dict[str, str] = {}
    block = re.search(r"^game:\n((?:[ \t]+.*\n?)*)", text, re.M)
    for line in (block.group(1) if block else "").splitlines():
        m = re.match(r"^[ \t]{1,4}(exe|prefix|working_dir):\s*(.+?)\s*$", line)
        if m:
            values[m.group(1)] = m.group(2).strip("'\"")
    return values


def lutris_games(roots: list[Path] | None = None) -> list[dict]:
    found: list[dict] = []
    for root in roots or _LUTRIS:
        db = root / "pga.db"
        if not db.exists():
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = con.execute("SELECT name, runner, directory, configpath, service, service_id FROM games WHERE installed = 1").fetchall()
            con.close()
        except sqlite3.Error:
            continue
        for name, runner, directory, configpath, service, service_id in rows:
            if runner == "steam" or service == "steam":
                continue  # ya están en la lista de Steam
            cfg: dict[str, str] = {}
            if configpath:
                try:
                    cfg = _lutris_game_config((root / "games" / f"{configpath}.yml").read_text(errors="replace"))
                except OSError:
                    pass
            exe, prefix = cfg.get("exe", ""), cfg.get("prefix") or directory or ""
            if exe and os.path.isabs(exe):
                folder = os.path.dirname(exe)
            elif exe and prefix:
                folder = os.path.join(prefix, os.path.dirname(exe))
            else:
                folder = cfg.get("working_dir") or directory or ""
            found.append(_entry("Lutris", name, exe=[_game_exe(exe)], path=[folder]))
    return found


# ---- aplicaciones del menú ----
def _launch_target(exec_line: str) -> tuple[str, str]:
    """(ejecutable, id de Flatpak) de una línea Exec."""
    try:
        args = shlex.split(re.sub(r"%[a-zA-Z]", "", exec_line))
    except ValueError:
        return "", ""
    while args and (args[0] == "env" or re.match(r"^\w+=", args[0])):
        args.pop(0)
    if not args:
        return "", ""
    if os.path.basename(args[0]) == "flatpak" and "run" in args:
        rest = [a for a in args[args.index("run") + 1:] if not a.startswith("-")]
        return "", rest[0] if rest else ""
    exe = os.path.basename(args[0])
    return ("" if exe in _GENERIC_EXE else exe), ""


def desktop_apps(dirs: list[Path] | None = None) -> list[dict]:
    seen: dict[str, dict] = {}
    for folder in dirs or _DESKTOP_DIRS:
        for file in sorted(glob.glob(f"{folder}/**/*.desktop", recursive=True)):
            app_id = os.path.relpath(file, folder)[:-8].replace("/", "-")
            if app_id in seen:
                continue
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            parser.optionxform = str
            try:
                parser.read(file, encoding="utf-8")
            except (configparser.Error, UnicodeDecodeError, OSError):
                continue
            if not parser.has_section("Desktop Entry"):
                continue
            d = parser["Desktop Entry"]
            seen[app_id] = {}  # los ocultos también tapan a los del sistema
            exec_line = d.get("Exec", "")
            if (d.get("Type", "Application") != "Application" or d.get("NoDisplay", "").lower() == "true"
                    or d.get("Hidden", "").lower() == "true" or d.get("Terminal", "").lower() == "true" or not exec_line
                    or re.search(r"steam://|heroic://|lutris:", exec_line)):
                continue  # los juegos de launchers ya salen en su fuente
            name = d.get("Name[es]") or d.get("Name") or app_id
            exe, flatpak_id = _launch_target(exec_line)
            classes = [d.get("StartupWMClass", ""), flatpak_id or app_id]
            seen[app_id] = _entry(APP_SOURCE, name, exe=[exe], wm_class=list(dict.fromkeys(c for c in classes if c)))
    return [e for e in seen.values() if e]


def scan_all() -> list[dict]:
    """Juegos primero y luego aplicaciones, cada grupo ordenado por nombre."""
    games: list[dict] = []
    for scan in (steam_games, heroic_games, lutris_games):
        try:
            games += scan()
        except OSError:
            continue
    try:
        apps = desktop_apps()
    except OSError:
        apps = []
    key = lambda e: e["name"].lower()
    return sorted(games, key=key) + sorted(apps, key=key)


def has_profile(cfg: dict, entry: dict) -> bool:
    """Ya hay un perfil para esta app o juego (mismo AppID, carpeta, clase o nombre)."""
    m = entry["match"]
    for p in cfg["profiles"]:
        pm = p.get("match") or {}
        if p["name"].lower() == entry["name"].lower():
            return True
        if set(m["steam_appid"]) & {str(a) for a in pm.get("steam_appid", [])}:
            return True
        if set(m["path"]) & {_norm(x) for x in pm.get("path", [])}:
            return True
        if not m["steam_appid"] and not m["path"] and set(c.lower() for c in m["wm_class"]) & {c.lower() for c in pm.get("wm_class", [])}:
            return True
    return False


def to_match(entry: dict) -> dict:
    return {k: list(v) if isinstance(v, list) else v for k, v in entry["match"].items()}
