"""Imágenes y referencias de firmware tomadas de tu instalación de G HUB."""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
from pathlib import Path

from .devices import CATALOG
from .ghub_import import find_settings, read_settings

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "zhub" / "assets"
_DEPOTS = {"g305": "g305", "g600": "g600", "g923": "g923_xbox"}


def _depot_root() -> Path | None:
    roots = glob.glob("/run/media/*/*/ProgramData/LGHUB/depots/*") + glob.glob("/mnt/*/ProgramData/LGHUB/depots/*")
    return Path(sorted(roots)[-1]) if roots else None


def _models() -> dict[str, str]:
    """Modelo exacto (color) de cada dispositivo registrado en G HUB."""
    try:
        known = read_settings(find_settings()[0])["devices/known"]["knownList"]
    except (IndexError, KeyError, OSError):
        return {}
    return {d["baseModelId"]: d["modelId"] for d in known}


def parse_firmware(text: str) -> tuple[int, ...] | None:
    """Normaliza «RQM 68.01.B0014», «U1 39.02.B0050» o «139.02.50» para compararlas."""
    m = re.match(r"(?:[A-Za-z]+(\d*)\s*)?([0-9A-Fa-f]+)\.([0-9A-Fa-f]+)(?:\.B?([0-9A-Fa-f]+))?", text.strip())
    if not m:
        return None
    return (int((m.group(1) or "") + m.group(2)), int(m.group(3)), int(m.group(4) or 0))


def is_newer(available: str, installed: str) -> bool:
    a, b = parse_firmware(available), parse_firmware(installed)
    return bool(a and b and a > b)


def sync() -> None:
    """Copia a la caché lo que haga falta; sin Windows montado se usa la copia anterior."""
    root = _depot_root()
    if not root:
        return
    models = _models()
    firmware: dict[str, str] = {}
    for dev_id, depot in _DEPOTS.items():
        src = root / depot
        manifest = src / "manifest.json"
        if not manifest.exists():
            continue
        wanted = models.get(dev_id, depot)
        devices = json.loads(manifest.read_text())["devices"]
        entry = next((d for d in devices if d["modelId"] == wanted), devices[0])
        target = CACHE / dev_id
        target.mkdir(parents=True, exist_ok=True)
        index = {}
        for res in entry["resources"]:
            if (src / res["src"]).is_file():
                name = Path(res["src"]).name
                if not (target / name).exists() or (target / name).stat().st_size != (src / res["src"]).stat().st_size:
                    shutil.copyfile(src / res["src"], target / name)
                index[res["key"]] = name
        (target / "index.json").write_text(json.dumps(index))
    for dfu in root.glob("*/dfu.json"):
        for item in json.loads(dfu.read_text()).get("contents", []):
            for dev_id, spec in CATALOG.items():
                ids = {f"046d_{p:04x}" for p in spec.pids}
                if ids & {i["interfaceId"] for i in item.get("interfaceInfos", [])}:
                    firmware[dev_id] = item["version"]
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / "firmware.json").write_text(json.dumps(firmware))


def firmware_refs() -> dict[str, str]:
    try:
        return json.loads((CACHE / "firmware.json").read_text())
    except OSError:
        return {}


def image(dev_id: str, key: str = "device_image") -> Path | None:
    try:
        name = json.loads((CACHE / dev_id / "index.json").read_text())[key]
    except (OSError, KeyError):
        return None
    return CACHE / dev_id / name


def metadata(dev_id: str) -> dict:
    path = image(dev_id, "image_metadata")
    return json.loads(path.read_text()) if path else {"images": []}
