"""Detección de la ventana activa en KDE Wayland mediante un script de KWin."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtClassInfo, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusInterface

log = logging.getLogger("zhub")
SERVICE = "io.zhub.Daemon"
PLUGIN = "zhub-active-window"
SCRIPT = f"""
function report(w) {{
    callDBus("{SERVICE}", "/Window", "io.zhub.Window", "Active",
             w ? String(w.resourceClass) : "", w ? String(w.caption) : "", w ? w.pid : 0);
}}
workspace.windowActivated.connect(report);
report(workspace.activeWindow);
"""


@pyqtClassInfo("D-Bus Interface", "io.zhub.Window")
class WindowBridge(QObject):
    activeWindow = pyqtSignal(str, str, int)

    def __init__(self):
        super().__init__()
        self.bus = QDBusConnection.sessionBus()

    def claim(self) -> bool:
        """Registra el servicio; False si ya hay otra instancia en marcha."""
        return self.bus.registerService(SERVICE)

    def start(self) -> bool:
        self.bus.registerObject("/Window", self, QDBusConnection.RegisterOption.ExportAllSlots)
        path = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "zhub-kwin.js"
        path.write_text(SCRIPT)
        scripting = QDBusInterface("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", self.bus)
        if not scripting.isValid():
            log.warning("KWin no disponible: sin detección de ventana activa")
            return False
        scripting.call("unloadScript", PLUGIN)
        reply = scripting.call("loadScript", str(path), PLUGIN)
        if reply.errorMessage() or not reply.arguments():
            log.warning("KWin rechazó el script: %s", reply.errorMessage())
            return False
        script = QDBusInterface("org.kde.KWin", f"/Scripting/Script{reply.arguments()[0]}", "org.kde.kwin.Script", self.bus)
        script.call("run")
        return True

    def stop(self) -> None:
        QDBusInterface("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", self.bus).call("unloadScript", PLUGIN)

    @pyqtSlot(str, str, int)
    def Active(self, wm_class: str, title: str, pid: int) -> None:
        self.activeWindow.emit(wm_class, title, pid)
