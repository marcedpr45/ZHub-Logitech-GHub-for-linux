"""Interfaz gráfica de ZHub."""
from __future__ import annotations

import argparse
import copy
import logging
import signal
import sys
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton, QButtonGroup, QScrollArea, QSlider, QSpinBox,
    QStackedWidget, QSystemTrayIcon, QTableWidget, QTabWidget, QVBoxLayout, QWidget,
)

from . import assets, ghub_import, wheel as wheel_io
from .devices import CATALOG, DeviceSpec, key_name
from .engine import Engine
from .kwin import WindowBridge
from .profiles import (
    default_mouse_settings, default_profile, device_cfg, load_config, new_profile, profile_by_id, save_config,
    scan_steam_games, window_info,
)

AUTOSTART = Path.home() / ".config/autostart/zhub.desktop"
QSS = """
* { font-size: 13px; color: #f2f3f5; }
QMainWindow, QDialog, QWidget#page, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget { background: #0b0c0e; }
QFrame#card { background: #17191d; border-radius: 4px; }
QFrame#device { background: #15171a; border: 1px solid #24272d; border-radius: 4px; }
QFrame#device:hover { border: 1px solid #00b8fc; }
QLabel#title { font-size: 16px; font-weight: 600; letter-spacing: 1px; }
QLabel#muted { color: #8d939e; }
QLabel#brand { font-size: 19px; font-weight: 700; color: #00b8fc; letter-spacing: 2px; }
QLabel#banner { background: #3a2a10; color: #ffcf8a; padding: 9px 12px; border-radius: 3px; }
QPushButton { background: #24272d; border: 0; border-radius: 3px; padding: 7px 13px; }
QPushButton:hover { background: #30343c; }
QPushButton#primary { background: #00b8fc; color: #04131b; font-weight: 600; }
QPushButton#nav { background: transparent; color: #8d939e; padding: 7px 10px; }
QPushButton#nav:hover { color: #ffffff; }
QPushButton#slot { background: #17191d; border: 1px solid #2c3037; font-size: 12px; padding: 4px 6px; }
QPushButton#slot:hover { border: 1px solid #00b8fc; }
QPushButton#segment:checked { background: #00b8fc; color: #04131b; font-weight: 600; }
QComboBox, QSpinBox, QLineEdit, QPlainTextEdit { background: #24272d; border: 0; border-radius: 3px; padding: 6px; }
QComboBox QAbstractItemView { background: #24272d; selection-background-color: #30343c; }
QListWidget, QTableWidget { background: transparent; border: 0; outline: 0; }
QListWidget::item { padding: 11px; }
QListWidget::item:selected { background: #17191d; color: #00b8fc; border-left: 2px solid #00b8fc; }
QTabWidget::pane { border: 0; }
QTabBar::tab { background: transparent; padding: 8px 16px; color: #8d939e; }
QTabBar::tab:selected { color: #ffffff; border-bottom: 2px solid #00b8fc; }
QHeaderView { background: #17191d; }
QHeaderView::section { background: #17191d; border: 0; color: #8d939e; padding: 6px; }
QTableWidget QLabel { background: transparent; }
QSlider::groove:horizontal { height: 4px; background: #33373f; }
QSlider::handle:horizontal { background: #00b8fc; width: 14px; margin: -6px 0; border-radius: 7px; }
QSlider::handle:horizontal:disabled { background: #4a4f58; }
QLabel:disabled { color: #5c616b; }
"""
ACTION_TYPES = [
    ("default", "Por defecto"), ("none", "Desactivado"), ("mouse", "Botón de ratón"), ("key", "Tecla o atajo"),
    ("sequence", "Macro"), ("wheel", "Rueda"), ("double_click", "Doble clic"), ("dpi", "DPI"),
    ("gshift", "G-Shift"), ("profile_cycle", "Ciclo de perfil"), ("report_rate_cycle", "Ciclo de tasa de respuesta"),
    ("run", "Ejecutar programa"),
]
_MOUSE = {1: "Izquierdo", 2: "Derecho", 3: "Central", 4: "Atrás", 5: "Adelante"}
_WHEEL = {"up": "Arriba", "down": "Abajo", "left": "Izquierda", "right": "Derecha"}
_DPI = {"cycle": "Ciclo", "up": "Subir", "down": "Bajar", "shift": "DPI Shift", "default": "Volver al predeterminado"}
_MODES = {"press": "Una vez al pulsar", "held": "Repetir mientras se mantiene", "toggle": "Alternar (repetir hasta volver a pulsar)"}


def pretty_key(name: str) -> str:
    return name.removeprefix("KEY_").removeprefix("BTN_").title()


def describe(a: dict | None) -> str:
    if a is None:
        return "Por defecto"
    t = a.get("type")
    if t == "mouse":
        return f"Clic {_MOUSE.get(a['button'], a['button']).lower()}"
    if t == "key":
        return " + ".join(pretty_key(k) for k in a["keys"]) or "Sin tecla"
    if t == "sequence":
        return f"Macro: {a.get('name') or 'sin nombre'}"
    if t == "wheel":
        return f"Rueda: {_WHEEL[a['dir']].lower()}"
    if t == "dpi":
        return f"DPI: {_DPI[a['op']].lower()}"
    if t == "run":
        return f"Ejecutar: {a['command']}"
    if t == "unsupported":
        return f"No portable: {a.get('label', '')}"
    return dict(ACTION_TYPES).get(t, str(t))


def steps_to_text(steps: list[dict]) -> str:
    lines = []
    for s in steps or []:
        if "delay" in s:
            lines.append(f"espera {s['delay']}")
        elif "key" in s:
            lines.append(f"{'pulsa' if s.get('tap') else 'baja' if s.get('down', True) else 'sube'} {s['key']}")
        elif "mouse" in s:
            lines.append(f"{'baja' if s.get('down', True) else 'sube'} raton {s['mouse']}")
    return "\n".join(lines)


def text_to_steps(text: str) -> list[dict]:
    """Convierte el texto del editor en pasos; lanza ValueError si una línea no es válida."""
    steps: list[dict] = []
    for n, raw in enumerate(text.splitlines(), 1):
        parts = raw.split()
        if not parts:
            continue
        try:
            if parts[0] == "espera":
                steps.append({"delay": int(parts[1])})
            elif parts[0] in ("baja", "sube") and parts[1] == "raton":
                steps.append({"mouse": int(parts[2]), "down": parts[0] == "baja"})
            elif parts[0] in ("baja", "sube", "pulsa") and parts[1].startswith("KEY_"):
                from evdev import ecodes
                ecodes.ecodes[parts[1]]
                steps.append({"key": parts[1], "tap": True} if parts[0] == "pulsa" else {"key": parts[1], "down": parts[0] == "baja"})
            else:
                raise ValueError
        except (ValueError, IndexError, KeyError):
            raise ValueError(f"Línea {n} no válida: «{raw}»") from None
    return steps


class KeyCapture(QLineEdit):
    """Campo que captura un atajo por código físico de tecla."""

    def __init__(self, keys: list[str]):
        super().__init__()
        self.keys = list(keys)
        self.setReadOnly(True)
        self.setPlaceholderText("Haz clic aquí y pulsa el atajo")
        self.setText(" + ".join(pretty_key(k) for k in self.keys))
        self.fresh = True

    def keyPressEvent(self, ev):
        code = ev.nativeScanCode() - 8
        if code <= 0 or ev.isAutoRepeat():
            return
        if self.fresh:
            self.keys, self.fresh = [], False
        name = key_name(code)
        if name not in self.keys:
            self.keys.append(name)
        self.setText(" + ".join(pretty_key(k) for k in self.keys))

    def keyReleaseEvent(self, ev):
        self.fresh = True


class MacroEditor(QPlainTextEdit):
    """Editor de pasos con grabación de teclas y tiempos."""

    def __init__(self):
        super().__init__()
        self.recording, self.last = False, 0
        self.setPlaceholderText("baja KEY_LEFTCTRL\nespera 10\npulsa KEY_E\nsube KEY_LEFTCTRL")

    def keyPressEvent(self, ev):
        self._record(ev, "baja") if self.recording else super().keyPressEvent(ev)

    def keyReleaseEvent(self, ev):
        self._record(ev, "sube") if self.recording else super().keyReleaseEvent(ev)

    def _record(self, ev, verb):
        code = ev.nativeScanCode() - 8
        if code <= 0 or ev.isAutoRepeat():
            return
        if self.last:
            self.appendPlainText(f"espera {max(1, ev.timestamp() - self.last)}")
        self.last = ev.timestamp()
        self.appendPlainText(f"{verb} {key_name(code)}")


class ActionDialog(QDialog):
    def __init__(self, parent, title: str, action: dict | None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        self.result_action = action
        a = action or {}
        root = QVBoxLayout(self)
        self.kind = QComboBox()
        for key, label in ACTION_TYPES:
            self.kind.addItem(label, key)
        root.addWidget(self.kind)
        self.pages = QStackedWidget()
        root.addWidget(self.pages)
        self.page_index: dict[str, int] = {}

        self.mouse_btn = self._combo(_MOUSE, a.get("button"))
        self.wheel_dir = self._combo(_WHEEL, a.get("dir"))
        self.dpi_op = self._combo(_DPI, a.get("op"))
        self.capture = KeyCapture(a.get("keys", []) if a.get("type") == "key" else [])
        self.command = QLineEdit(a.get("command", ""))
        self.command.setPlaceholderText("p. ej. spectacle -r")
        self._page("mouse", self.mouse_btn)
        self._page("wheel", self.wheel_dir)
        self._page("dpi", self.dpi_op)
        self._page("key", self.capture)
        self._page("run", self.command)
        self._page("sequence", self._macro_page(a))
        blank = QWidget()
        self.pages.addWidget(blank)
        self.blank = self.pages.count() - 1

        self.kind.currentIndexChanged.connect(self._sync)
        current = "default" if action is None else a.get("type")
        idx = self.kind.findData(current)
        self.kind.setCurrentIndex(max(idx, 0))
        self._sync()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _combo(self, values: dict, current) -> QComboBox:
        combo = QComboBox()
        for key, label in values.items():
            combo.addItem(label, key)
        if current in values:
            combo.setCurrentIndex(combo.findData(current))
        return combo

    def _page(self, key: str, widget: QWidget) -> None:
        self.page_index[key] = self.pages.addWidget(widget)

    def _macro_page(self, a: dict) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        self.macro_name = QLineEdit(a.get("name", ""))
        self.macro_name.setPlaceholderText("Nombre de la macro")
        mode = "toggle" if a.get("toggle") else "held" if a.get("held") else "press"
        self.macro_mode = self._combo(_MODES, mode)
        self.editor = MacroEditor()
        self.editor.setPlainText(steps_to_text(a.get(mode, [])) if a.get("type") == "sequence" else "")
        self.record = QPushButton("Grabar teclas")
        self.record.setCheckable(True)
        self.record.toggled.connect(self._toggle_record)
        for w in (self.macro_name, self.macro_mode, self.editor, self.record):
            lay.addWidget(w)
        return page

    def _toggle_record(self, on: bool) -> None:
        self.editor.recording, self.editor.last = on, 0
        self.record.setText("Detener grabación" if on else "Grabar teclas")
        if on:
            self.editor.setFocus()

    def _sync(self) -> None:
        self.pages.setCurrentIndex(self.page_index.get(self.kind.currentData(), self.blank))

    def _accept(self) -> None:
        t = self.kind.currentData()
        if t == "default":
            self.result_action = None
        elif t == "mouse":
            self.result_action = {"type": t, "button": self.mouse_btn.currentData()}
        elif t == "wheel":
            self.result_action = {"type": t, "dir": self.wheel_dir.currentData()}
        elif t == "dpi":
            self.result_action = {"type": t, "op": self.dpi_op.currentData()}
        elif t == "key":
            if not self.capture.keys:
                QMessageBox.warning(self, "Falta el atajo", "Pulsa una tecla o combinación.")
                return
            self.result_action = {"type": t, "keys": self.capture.keys}
        elif t == "run":
            if not self.command.text().strip():
                return
            self.result_action = {"type": t, "command": self.command.text().strip()}
        elif t == "sequence":
            try:
                steps = text_to_steps(self.editor.toPlainText())
            except ValueError as exc:
                QMessageBox.warning(self, "Macro no válida", str(exc))
                return
            self.result_action = {"type": t, "name": self.macro_name.text().strip(), self.macro_mode.currentData(): steps}
        else:
            self.result_action = {"type": t}
        self.accept()


def card() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(18, 16, 18, 16)
    return frame, lay


def heading(text: str, name: str = "title") -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    return label


class DpiBar(QWidget):
    """Barra de velocidades DPI con los niveles, el predeterminado y el Shift."""

    def __init__(self, spec: DeviceSpec, settings):
        super().__init__()
        self.spec, self.settings = spec, settings
        self.setFixedHeight(78)

    def _x(self, dpi: int) -> float:
        span = self.spec.dpi_max - self.spec.dpi_min
        return 24 + (self.width() - 48) * (dpi - self.spec.dpi_min) / span

    def paintEvent(self, _):
        m = self.settings()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        y = 48
        p.setPen(QPen(QColor("#33373f"), 4, cap=Qt.PenCapStyle.RoundCap))
        p.drawLine(24, y, self.width() - 24, y)
        for dpi in m["levels"]:
            x = self._x(dpi)
            default = dpi == m["default"]
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#ff9d2e" if default else "#00b8fc"))
            if default:
                p.drawPolygon(QPolygonF([QPointF(x, y - 9), QPointF(x + 9, y), QPointF(x, y + 9), QPointF(x - 9, y)]))
            else:
                p.drawEllipse(QPointF(x, y), 7, 7)
            p.setPen(QColor("#ffffff"))
            p.drawText(QRectF(x - 40, 8, 80, 22), Qt.AlignmentFlag.AlignCenter, str(dpi))
        x = self._x(m["shift"])
        p.setPen(QPen(QColor("#ffe14d"), 3))
        p.drawLine(QPointF(x, y - 13), QPointF(x, y + 13))
        p.end()


class LiveWheel(QWidget):
    """Foto del volante que gira con el físico e indica los grados."""

    SIDE = 260

    def __init__(self, path: Path | None):
        super().__init__()
        self.pix = QPixmap(str(path) if path else "")
        if not self.pix.isNull():
            self.pix = self.pix.scaledToHeight(2 * self.SIDE, Qt.TransformationMode.SmoothTransformation)
        self.angle = 0.0
        self.setFixedHeight(self.SIDE + 34)

    def set_angle(self, angle: float) -> None:
        if angle != self.angle:
            self.angle = angle
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.save()
        p.translate(self.width() / 2, self.SIDE / 2)
        p.rotate(self.angle)
        p.drawPixmap(QRectF(-self.SIDE / 2, -self.SIDE / 2, self.SIDE, self.SIDE).toRect(), self.pix)
        p.restore()
        side = "Centro" if abs(self.angle) < 2 else f"{abs(self.angle):.0f}° {'derecha' if self.angle > 0 else 'izquierda'}"
        p.setPen(QColor("#ffffff"))
        p.drawText(QRectF(0, self.SIDE + 4, self.width(), 28), Qt.AlignmentFlag.AlignCenter, side)
        p.end()


class PedalBars(QWidget):
    """Recorrido de cada pedal; si están combinados, embrague y una barra centrada."""

    PEDALS = (("clutch", "Embrague"), ("brake", "Freno"), ("throttle", "Acelerador"))

    def __init__(self):
        super().__init__()
        self.values = {key: 0.0 for key, _ in self.PEDALS}
        self.combined = False
        self.setFixedHeight(240)
        self.setMinimumWidth(270)

    def set_values(self, values: dict[str, float], combined: bool = False) -> None:
        current = {key: values[key] for key, _ in self.PEDALS}
        if current != self.values or combined != self.combined:
            self.values, self.combined = current, combined
            self.update()

    def paint_combined(self, p: QPainter, column: float, top: int, track: int) -> None:
        net = self.values["throttle"] - self.values["brake"]
        percent = round(abs(net) * 100)
        x, mid, fill = column + (column - 34) / 2, top + track / 2, track / 2 * abs(net)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#33373f"))
        p.drawRoundedRect(QRectF(x, top, 34, track), 3, 3)
        p.setBrush(QColor("#00b8fc"))
        p.drawRect(QRectF(x, mid - fill if net > 0 else mid, 34, fill))
        p.setPen(QPen(QColor("#ffffff"), 1))
        p.drawLine(QPointF(x - 5, mid), QPointF(x + 39, mid))
        p.drawText(QRectF(column, 2, column, 24), Qt.AlignmentFlag.AlignCenter, f"{percent} %")
        name = "Combinados" if not percent else "Acelerador" if net > 0 else "Freno"
        p.setPen(QColor("#8d939e"))
        p.drawText(QRectF(column, top + track + 6, column, 24), Qt.AlignmentFlag.AlignCenter, name)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pedals = self.PEDALS[:1] if self.combined else self.PEDALS
        column = self.width() / (2 if self.combined else len(self.PEDALS))
        top, track = 30, self.height() - 62
        if self.combined:
            self.paint_combined(p, column, top, track)
        for i, (key, name) in enumerate(pedals):
            x = column * i + (column - 34) / 2
            fill = track * self.values[key]
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#33373f"))
            p.drawRoundedRect(QRectF(x, top, 34, track), 3, 3)
            p.setBrush(QColor("#00b8fc"))
            p.drawRoundedRect(QRectF(x, top + track - fill, 34, fill), 3, 3)
            p.setPen(QColor("#ffffff"))
            p.drawText(QRectF(column * i, 2, column, 24), Qt.AlignmentFlag.AlignCenter, f"{round(self.values[key] * 100)} %")
            p.setPen(QColor("#8d939e"))
            p.drawText(QRectF(column * i, top + track + 6, column, 24), Qt.AlignmentFlag.AlignCenter, name)
        p.end()


class DeviceCanvas(QWidget):
    """Foto del dispositivo con marcadores y etiquetas en las posiciones de G HUB."""

    def __init__(self, path: Path, meta: dict, slots: list[tuple[dict, str]], text, clicked):
        super().__init__()
        self.pix = QPixmap(str(path))
        self.origin = meta["origin"]
        self.slots, self.text = slots, text
        self.buttons: list[QPushButton] = []
        xs = [0, self.origin["width"]] + [a["label"]["x"] for a, _ in slots]
        ys = [0, self.origin["height"]] + [a["label"]["y"] for a, _ in slots]
        self.box = (min(xs), min(ys), max(xs), max(ys))
        for _, bid in slots:
            b = QPushButton(self)
            b.setObjectName("slot")
            b.clicked.connect(lambda _=False, i=bid: clicked(i))
            self.buttons.append(b)
        self.setMinimumHeight(430)
        self.refresh()

    def refresh(self) -> None:
        for button, (_, bid) in zip(self.buttons, self.slots):
            label = self.text(bid)
            button.setText(label if len(label) < 25 else label[:23] + "…")
            button.setToolTip(label)

    def _map(self, x: float, y: float) -> QPointF:
        x0, y0, x1, y1 = self.box
        k = min((self.width() - 190) / max(x1 - x0, 1), (self.height() - 40) / max(y1 - y0, 1))
        ox = (self.width() - (x1 - x0) * k) / 2
        oy = (self.height() - (y1 - y0) * k) / 2
        return QPointF(ox + (x - x0) * k, oy + (y - y0) * k)

    def resizeEvent(self, _):
        for button, (a, _) in zip(self.buttons, self.slots):
            pt = self._map(a["label"]["x"], a["label"]["y"])
            w, h = 172, 32
            button.setGeometry(int(min(max(pt.x() - w / 2, 0), self.width() - w)),
                               int(min(max(pt.y() - h / 2, 0), self.height() - h)), w, h)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        tl, br = self._map(0, 0), self._map(self.origin["width"], self.origin["height"])
        p.drawPixmap(QRectF(tl, br).toRect(), self.pix)
        for button, (a, _) in zip(self.buttons, self.slots):
            marker = self._map(a["marker"]["x"], a["marker"]["y"])
            p.setPen(QPen(QColor("#00b8fc"), 1))
            p.drawLine(marker, QPointF(button.geometry().center()))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#00b8fc"))
            p.drawEllipse(marker, 4, 4)
        p.end()


class DevicePage(QWidget):
    """Página de un dispositivo: barra lateral y sus secciones propias."""

    def __init__(self, win: "MainWindow", spec: DeviceSpec):
        super().__init__()
        self.win, self.spec = win, spec
        self.layer = "buttons"
        self.canvases: list[DeviceCanvas] = []
        self.table = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        back = QPushButton("←")
        back.setFixedWidth(42)
        back.clicked.connect(win.go_home)
        head.addWidget(back)
        head.addWidget(heading(spec.name))
        self.status = heading("", "muted")
        head.addSpacing(14)
        head.addWidget(self.status)
        head.addStretch(1)
        root.addLayout(head)
        self.banner = heading("", "banner")
        self.banner.setWordWrap(True)
        self.banner.hide()
        root.addWidget(self.banner)
        body = QHBoxLayout()
        self.rail = QListWidget()
        self.rail.setObjectName("rail")
        self.rail.setFixedWidth(176)
        self.stack = QStackedWidget()
        self.rail.currentRowChanged.connect(self.stack.setCurrentIndex)
        body.addWidget(self.rail)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)
        if spec.kind == "mouse":
            self.section("Sensibilidad", self.sensor_page())
            self.section("Asignaciones", self.assign_page())
            if spec.has_lighting:
                self.section("Iluminación", self.lighting_page())
        else:
            self.section("Volante", self.wheel_page())
            self.section("Pedales", self.pedals_page())
            self.axes: wheel_io.LiveAxes | None = None
            self.live_ticks, self.hw_range = 0, 900
            self.live_timer = QTimer(self)
            self.live_timer.timeout.connect(self.tick_live)
            self.live_timer.start(33)
        self.rail.setCurrentRow(0)

    def section(self, name: str, page: QWidget) -> None:
        self.rail.addItem(name)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidget(page)
        self.stack.addWidget(area)

    @property
    def cfg(self) -> dict:
        return device_cfg(self.win.editing, self.spec.id)

    def mouse(self) -> dict:
        return self.cfg.setdefault("mouse", default_mouse_settings())

    # ---- sensibilidad ----
    def sensor_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        frame, lay = card()
        lay.addWidget(heading("Velocidades DPI"))
        self.bar = DpiBar(self.spec, self.mouse)
        lay.addWidget(self.bar)
        self.levels_box = QGridLayout()
        lay.addLayout(self.levels_box)
        foot = QHBoxLayout()
        add = QPushButton("Añadir nivel")
        add.clicked.connect(self.add_level)
        foot.addWidget(add)
        foot.addStretch(1)
        foot.addWidget(QLabel("DPI Shift"))
        self.shift = self._dpi_spin(self.mouse()["shift"])
        self.shift.editingFinished.connect(self.save_shift)
        foot.addWidget(self.shift)
        lay.addLayout(foot)
        root.addWidget(frame)
        frame2, lay2 = card()
        lay2.addWidget(heading("Tasa de respuesta"))
        row = QHBoxLayout()
        group = QButtonGroup(self)
        current = self.mouse().get("report_rate", 1000)
        for hz in self.spec.rates:
            b = QPushButton(f"{hz} Hz")
            b.setObjectName("segment")
            b.setCheckable(True)
            b.setChecked(hz == current)
            b.clicked.connect(lambda _=False, v=hz: self.save_rate(v))
            group.addButton(b)
            row.addWidget(b)
        lay2.addLayout(row)
        root.addWidget(frame2)
        root.addStretch(1)
        self.render_levels()
        return page

    def _dpi_spin(self, value: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(self.spec.dpi_min, self.spec.dpi_max)
        spin.setSingleStep(self.spec.dpi_step)
        spin.setValue(value)
        return spin

    def render_levels(self) -> None:
        while self.levels_box.count():
            self.levels_box.takeAt(0).widget().deleteLater()
        m = self.mouse()
        group = QButtonGroup(self)
        for i, dpi in enumerate(m["levels"]):
            spin = self._dpi_spin(dpi)
            spin.editingFinished.connect(lambda i=i, s=spin: self.edit_level(i, s.value()))
            radio = QRadioButton("Predeterminado")
            radio.setChecked(dpi == m["default"])
            radio.clicked.connect(lambda _=False, i=i: self.set_default(i))
            group.addButton(radio)
            remove = QPushButton("Quitar")
            remove.setEnabled(len(m["levels"]) > 1)
            remove.clicked.connect(lambda _=False, i=i: self.remove_level(i))
            self.levels_box.addWidget(QLabel(f"Nivel {i + 1}"), i, 0)
            self.levels_box.addWidget(spin, i, 1)
            self.levels_box.addWidget(radio, i, 2)
            self.levels_box.addWidget(remove, i, 3)
        self.bar.update()

    def edit_level(self, i: int, value: int) -> None:
        m = self.mouse()
        if m["levels"][i] == value:
            return
        if m["default"] == m["levels"][i]:
            m["default"] = value
        m["levels"][i] = value
        self.bar.update()
        self.win.commit()

    def set_default(self, i: int) -> None:
        self.mouse()["default"] = self.mouse()["levels"][i]
        self.bar.update()
        self.win.commit()

    def add_level(self) -> None:
        m = self.mouse()
        if len(m["levels"]) < self.spec.max_levels:
            m["levels"].append(min(m["levels"][-1] * 2, self.spec.dpi_max))
            self.render_levels()
            self.win.commit()

    def remove_level(self, i: int) -> None:
        m = self.mouse()
        removed = m["levels"].pop(i)
        if m["default"] == removed:
            m["default"] = m["levels"][0]
        self.render_levels()
        self.win.commit()

    def save_shift(self) -> None:
        if self.mouse()["shift"] != self.shift.value():
            self.mouse()["shift"] = self.shift.value()
            self.bar.update()
            self.win.commit()

    def save_rate(self, hz: int) -> None:
        self.mouse()["report_rate"] = hz
        self.win.commit()

    # ---- asignaciones ----
    def assign_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        shift = QCheckBox("Editar capa G-Shift")
        shift.toggled.connect(self.toggle_layer)
        lay.addWidget(shift)
        views = []
        for im in assets.metadata(self.spec.id)["images"]:
            slots = [(a, bid) for a in im.get("assignments", []) if (bid := a["slotId"].split("_")[1]) in self.spec.buttons]
            path = assets.image(self.spec.id, im["key"])
            if slots and path:
                views.append((im["key"], DeviceCanvas(path, im, slots, self.slot_text, self.edit_binding)))
        if len(views) == 1:
            lay.addWidget(views[0][1], 1)
        elif views:
            tabs = QTabWidget()
            for key, canvas in views:
                tabs.addTab(canvas, "Lateral" if "side" in key else "Superior")
            lay.addWidget(tabs, 1)
        else:
            lay.addWidget(self.button_table(), 1)
        self.canvases = [c for _, c in views]
        return page

    def button_table(self) -> QWidget:
        """Lista de botones cuando no están disponibles las imágenes de G HUB."""
        self.table = QTableWidget(len(self.spec.buttons), 2)
        self.table.setHorizontalHeaderLabels(["Botón", "Asignación"])
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for row, (bid, label) in enumerate(self.spec.buttons.items()):
            self.table.setRowHeight(row, 40)
            self.table.setCellWidget(row, 0, QLabel(label))
            btn = QPushButton(self.slot_text(bid))
            btn.clicked.connect(lambda _=False, b=bid: self.edit_binding(b))
            self.table.setCellWidget(row, 1, btn)
        return self.table

    def slot_text(self, bid: str) -> str:
        if self.layer == "shifted" and bid not in self.cfg.get("shifted", {}):
            return "Igual que normal"
        action = self.cfg.get(self.layer, {}).get(bid) if self.layer == "shifted" else (self.cfg.get("buttons", {}).get(bid) or self.spec.defaults.get(bid))
        return describe(action)

    def toggle_layer(self, on: bool) -> None:
        self.layer = "shifted" if on else "buttons"
        self.render_buttons()

    def render_buttons(self) -> None:
        for c in self.canvases:
            c.refresh()
        if self.table:
            for row, bid in enumerate(self.spec.buttons):
                self.table.cellWidget(row, 1).setText(self.slot_text(bid))

    def edit_binding(self, bid: str) -> None:
        layer = self.layer
        dlg = ActionDialog(self, self.spec.buttons[bid], copy.deepcopy(self.cfg.get(layer, {}).get(bid)))
        if not dlg.exec():
            return
        action = dlg.result_action
        if bid == "g1" and layer == "buttons" and action and not self.keeps_left_click(action):
            QMessageBox.warning(self, "Te quedarías sin clic izquierdo", "Asigna antes el clic izquierdo a otro botón.")
            return
        bucket = self.cfg.setdefault(layer, {})
        if action is None:
            bucket.pop(bid, None)
        else:
            bucket[bid] = action
        self.render_buttons()
        self.win.commit()

    def keeps_left_click(self, replacement: dict) -> bool:
        left = {"type": "mouse", "button": 1}
        return replacement == left or any(a == left for b, a in self.cfg.get("buttons", {}).items() if b != "g1")

    # ---- iluminación ----
    def lighting(self) -> dict:
        return self.cfg.setdefault("lighting", {"effect": "cycle", "color": "#00b8fc", "period": 5000})

    def lighting_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        frame, lay = card()
        lay.addWidget(heading("Iluminación RGB"))
        form = QFormLayout()
        li = self.lighting()
        effect = QComboBox()
        for key, label in (("off", "Apagado"), ("fixed", "Color fijo"), ("breathing", "Respiración"), ("cycle", "Ciclo de colores")):
            effect.addItem(label, key)
        effect.setCurrentIndex(max(effect.findData(li.get("effect")), 0))
        effect.currentIndexChanged.connect(lambda: self.save_light("effect", effect.currentData()))
        form.addRow("Efecto", effect)
        form.addRow("Color", self.color_button("color", li.get("color", "#00b8fc")))
        form.addRow("Color con G-Shift", self.color_button("shift_color", li.get("shift_color", "#ff9d2e")))
        speed = QSlider(Qt.Orientation.Horizontal)
        speed.setRange(1, 15)
        speed.setValue(round(li.get("period", 5000) / 1000))
        speed.sliderReleased.connect(lambda: self.save_light("period", speed.value() * 1000))
        form.addRow("Duración del efecto (s)", speed)
        lay.addLayout(form)
        root.addWidget(frame)
        root.addStretch(1)
        return page

    def color_button(self, key: str, value: str) -> QPushButton:
        btn = QPushButton(value)
        btn.setStyleSheet(f"background: {value};")

        def pick():
            color = QColorDialog.getColor(QColor(btn.text()), self, "Elige un color")
            if color.isValid():
                btn.setText(color.name())
                btn.setStyleSheet(f"background: {color.name()};")
                self.save_light(key, color.name())

        btn.clicked.connect(pick)
        return btn

    def save_light(self, key: str, value) -> None:
        self.lighting()[key] = value
        self.win.commit()

    # ---- volante ----
    def wheel(self) -> dict:
        return self.cfg.setdefault("wheel", {"range": 900, "autocenter": 20, "gain": 100, "damping": 10, "sensitivity": 50,
                                              "throttle": 50, "brake": 50, "clutch": 50, "combined": False})

    def slider_row(self, form: QFormLayout, key: str, label: str, low: int, high: int, default: int) -> QSlider:
        row = QHBoxLayout()
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(low, high)
        slider.setValue(self.wheel().get(key, default))
        value = QLabel(str(slider.value()))
        value.setFixedWidth(42)
        slider.valueChanged.connect(lambda v: value.setText(str(v)))
        slider.sliderReleased.connect(lambda: self.save_wheel(key, slider.value()))
        row.addWidget(slider, 1)
        row.addWidget(value)
        form.addRow(label, row)
        return slider

    def save_wheel(self, key: str, value) -> None:
        self.wheel()[key] = value
        self.win.commit()

    def picture(self, key: str, height: int) -> QLabel:
        label = QLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        path = assets.image(self.spec.id, key)
        if path:
            label.setPixmap(QPixmap(str(path)).scaledToHeight(height, Qt.TransformationMode.SmoothTransformation))
        return label

    def wheel_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        self.live_wheel = LiveWheel(assets.image(self.spec.id, "wheel_image_front"))
        root.addWidget(self.live_wheel)
        frame, lay = card()
        lay.addWidget(heading("Ajustes del volante"))
        form = QFormLayout()
        self.slider_row(form, "range", "Radio de giro (°)", 180, 900, 900)
        centering = QCheckBox("Activar autocentrado (desactívalo si el juego centra el volante por su cuenta)")
        centering.setChecked(self.wheel().get("autocenter_on", True))
        form.addRow(centering)
        strength = self.slider_row(form, "autocenter", "Fuerza del autocentrado (%)", 0, 100, 20)
        strength.setEnabled(centering.isChecked())
        centering.toggled.connect(strength.setEnabled)
        centering.toggled.connect(lambda on: self.save_wheel("autocenter_on", on))
        self.slider_row(form, "gain", "Fuerza de respuesta (%)", 0, 100, 100)
        self.slider_row(form, "damping", "Amortiguación (%)", 0, 100, 10)
        self.slider_row(form, "sensitivity", "Sensibilidad (50 = lineal)", 0, 100, 50)
        lay.addLayout(form)
        root.addWidget(frame)
        root.addStretch(1)
        return page

    def pedals_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        live = QHBoxLayout()
        live.addWidget(self.picture("pedals_image", 240), 1)
        self.live_pedals = PedalBars()
        live.addWidget(self.live_pedals, 1)
        root.addLayout(live)
        frame, lay = card()
        lay.addWidget(heading("Pedales"))
        form = QFormLayout()
        self.slider_row(form, "throttle", "Acelerador (50 = lineal)", 0, 100, 50)
        self.slider_row(form, "brake", "Freno (50 = lineal)", 0, 100, 50)
        self.slider_row(form, "clutch", "Embrague (50 = lineal)", 0, 100, 50)
        lay.addLayout(form)
        combined = QCheckBox("Pedales combinados (acelerador y freno en un solo eje)")
        combined.setChecked(self.wheel().get("combined", False))
        combined.toggled.connect(lambda on: self.save_wheel("combined", on))
        lay.addWidget(combined)
        root.addWidget(frame)
        root.addStretch(1)
        return page

    def tick_live(self) -> None:
        if not self.axes or not self.isVisible():
            return
        try:
            values = self.axes.read()
        except OSError:
            self.axes = None
            return
        if self.live_ticks % 30 == 0:  # el giro real, no el pedido, para que los grados cuadren
            self.hw_range = wheel_io.current_range() or self.wheel().get("range", 900)
        self.live_ticks += 1
        self.live_wheel.set_angle(values["wheel"] * self.hw_range / 2)
        self.live_pedals.set_values(values, self.wheel().get("combined", False))

    def open_axes(self) -> None:
        path = self.win.engine.axes_path(self.spec.id) if self.win.engine else None
        if path:
            self.axes = wheel_io.LiveAxes(path)

    def refresh_status(self, st: dict | None) -> None:
        if st is None:
            return
        if self.spec.kind == "wheel" and not self.axes:
            self.open_axes()
        bits = []
        if st["battery"]:
            bits.append(f"Batería {st['battery']}")
        if st["dpi"]:
            bits.append(f"{st['dpi']} DPI")
        if st["rate"]:
            bits.append(f"{st['rate']} Hz")
        if st["firmware"]:
            bits.append(f"Firmware {st['firmware']}")
        if st["error"]:
            bits.append(f"⚠ {st['error']}")
        self.status.setText("  ·  ".join(bits))
        self.banner.setVisible(bool(st["update"]))
        self.banner.setText(update_text(self.spec.name, st))


def update_text(name: str, st: dict) -> str:
    return f"Firmware {st['update']} disponible para {name}. Actualízalo desde G HUB en Windows."


class DeviceCard(QFrame):
    """Tarjeta de un dispositivo conectado en la pantalla de inicio."""

    def __init__(self, win: "MainWindow", spec: DeviceSpec):
        super().__init__()
        self.win, self.spec = win, spec
        self.setObjectName("device")
        self.setFixedSize(280, 400)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QVBoxLayout(self)
        picture = QLabel()
        picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        path = assets.image(spec.id)
        if path:
            pix = QPixmap(str(path))
            picture.setPixmap(pix.scaled(240, 290, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            picture.setText(spec.name)
        lay.addWidget(picture, 1)
        lay.addWidget(heading(spec.name))
        self.status = heading("", "muted")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

    def mousePressEvent(self, _):
        self.win.open_device(self.spec.id)

    def refresh_status(self, st: dict | None) -> None:
        if not st:
            return
        text = f"Batería {st['battery']}" if st["battery"] else "Conectado"
        self.status.setText(text + ("  ·  Firmware disponible" if st["update"] else ""))


class MainWindow(QMainWindow):
    stateChanged = pyqtSignal()

    def __init__(self, cfg: dict, engine: Engine | None):
        super().__init__()
        self.cfg, self.engine = cfg, engine
        self.editing = default_profile(cfg)
        self.tray: QSystemTrayIcon | None = None
        self.notified: set[tuple[str, str]] = set()
        self.shown: list[str] = []
        self.cards: dict[str, DeviceCard] = {}
        self.page: DevicePage | None = None
        self.setWindowTitle("ZHub")
        self.resize(1160, 780)
        central = QWidget()
        central.setObjectName("page")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(22, 14, 22, 14)

        top = QHBoxLayout()
        top.addWidget(heading("ZHub", "brand"))
        top.addSpacing(16)
        for text, fn in (("Inicio", self.go_home), ("Perfiles", lambda: self.pages.setCurrentIndex(2)), ("Ajustes", lambda: self.pages.setCurrentIndex(3))):
            b = QPushButton(text)
            b.setObjectName("nav")
            b.clicked.connect(fn)
            top.addWidget(b)
        top.addStretch(1)
        self.profile_box = QComboBox()
        self.profile_box.setMinimumWidth(260)
        self.profile_box.activated.connect(self.pick_profile)
        top.addWidget(self.profile_box)
        top.addStretch(1)
        self.auto = QCheckBox("Cambio automático")
        self.auto.toggled.connect(self.set_auto)
        self.pin = QCheckBox("Fijar este perfil")
        self.pin.toggled.connect(self.set_pin)
        top.addWidget(self.auto)
        top.addWidget(self.pin)
        root.addLayout(top)

        self.live = heading("", "muted")
        root.addWidget(self.live)
        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)
        self.pages.addWidget(self.home_page())
        self.device_holder = QStackedWidget()
        self.pages.addWidget(self.device_holder)
        self.pages.addWidget(self.profiles_tab())
        self.pages.addWidget(self.settings_tab())

        self.stateChanged.connect(self.refresh_live)
        self.reload_profiles()
        self.refresh_live()

    # ---- inicio ----
    def home_page(self) -> QWidget:
        self.home = QStackedWidget()
        empty = heading("Enciende o conecta un dispositivo Logitech compatible", "muted")
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.home.addWidget(empty)
        holder = QWidget()
        self.card_row = QHBoxLayout(holder)
        self.card_row.setSpacing(20)
        self.home.addWidget(holder)
        return self.home

    def connected_ids(self) -> list[str]:
        if not self.engine:
            return list(CATALOG)
        online = {s["id"] for s in self.engine.status()}
        return [sid for sid in CATALOG if sid in online]

    def rebuild_home(self) -> None:
        while self.card_row.count():
            item = self.card_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.shown = self.connected_ids()
        self.cards = {sid: DeviceCard(self, CATALOG[sid]) for sid in self.shown}
        self.card_row.addStretch(1)
        for c in self.cards.values():
            self.card_row.addWidget(c)
        self.card_row.addStretch(1)
        self.home.setCurrentIndex(1 if self.shown else 0)
        if self.page and self.page.spec.id not in self.shown:
            self.go_home()

    def go_home(self) -> None:
        self.pages.setCurrentIndex(0)

    def open_device(self, sid: str) -> None:
        self.close_device()
        self.page = DevicePage(self, CATALOG[sid])
        self.device_holder.addWidget(self.page)
        self.device_holder.setCurrentWidget(self.page)
        self.pages.setCurrentIndex(1)
        self.refresh_live()

    def close_device(self) -> None:
        if self.page:
            self.device_holder.removeWidget(self.page)
            self.page.deleteLater()
            self.page = None

    def profiles_tab(self) -> QWidget:
        tab = QWidget()
        lay = QHBoxLayout(tab)
        side = QVBoxLayout()
        self.profile_list = QListWidget()
        self.profile_list.setFixedWidth(260)
        self.profile_list.currentRowChanged.connect(self.show_rules)
        side.addWidget(self.profile_list, 1)
        for text, fn in (("Nuevo perfil", self.add_profile), ("Duplicar", self.duplicate_profile), ("Eliminar", self.delete_profile), ("Añadir juegos de Steam", self.add_steam)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            side.addWidget(b)
        lay.addLayout(side)

        frame, body = card()
        body.addWidget(heading("Cuándo se activa este perfil"))
        form = QFormLayout()
        self.rule_name = QLineEdit()
        self.rule_exe = QLineEdit()
        self.rule_exe.setPlaceholderText("firefox, league of legends.exe, *.exe")
        self.rule_class = QLineEdit()
        self.rule_class.setPlaceholderText("org.kde.dolphin, steam_app_*")
        self.rule_steam = QLineEdit()
        self.rule_steam.setPlaceholderText("570, 730")
        self.rule_title = QLineEdit()
        self.rule_title.setPlaceholderText("Expresión regular sobre el título")
        form.addRow("Nombre", self.rule_name)
        form.addRow("Ejecutable", self.rule_exe)
        form.addRow("Clase de ventana", self.rule_class)
        form.addRow("Steam AppID", self.rule_steam)
        form.addRow("Título", self.rule_title)
        body.addLayout(form)
        row = QHBoxLayout()
        take = QPushButton("Usar la última ventana activa")
        take.clicked.connect(self.use_last_window)
        save = QPushButton("Guardar")
        save.setObjectName("primary")
        save.clicked.connect(self.save_rules)
        row.addWidget(take)
        row.addStretch(1)
        row.addWidget(save)
        body.addLayout(row)
        self.rule_hint = heading("", "muted")
        self.rule_hint.setWordWrap(True)
        body.addWidget(self.rule_hint)
        body.addStretch(1)
        lay.addWidget(frame, 1)
        return tab

    def settings_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        frame, body = card()
        body.addWidget(heading("Importar desde G HUB"))
        note = heading("Lee perfiles, botones, macros y sensibilidad de tu partición de Windows. Sustituye los perfiles actuales de ZHub.", "muted")
        note.setWordWrap(True)
        body.addWidget(note)
        imp = QPushButton("Importar configuración de G HUB")
        imp.setObjectName("primary")
        imp.clicked.connect(self.import_ghub)
        body.addWidget(imp, alignment=Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(frame)
        frame2, body2 = card()
        body2.addWidget(heading("Inicio"))
        self.autostart = QCheckBox("Iniciar ZHub con la sesión")
        self.autostart.setChecked(AUTOSTART.exists())
        self.autostart.toggled.connect(self.set_autostart)
        body2.addWidget(self.autostart)
        lay.addWidget(frame2)
        lay.addStretch(1)
        return tab

    # ---- estado ----
    def commit(self) -> None:
        save_config(self.cfg)
        if self.engine:
            self.engine.set_config(self.cfg)

    def reload_profiles(self) -> None:
        if not profile_by_id(self.cfg, self.editing["id"]):
            self.editing = default_profile(self.cfg)
        names = [p["name"] + (" (por defecto)" if p.get("default") else "") for p in self.cfg["profiles"]]
        self.profile_box.clear()
        self.profile_box.addItems(names)
        idx = self.cfg["profiles"].index(self.editing)
        self.profile_box.setCurrentIndex(idx)
        self.profile_list.blockSignals(True)
        self.profile_list.clear()
        self.profile_list.addItems(names)
        self.profile_list.setCurrentRow(idx)
        self.profile_list.blockSignals(False)
        self.auto.blockSignals(True)
        self.auto.setChecked(self.cfg.get("auto_switch", True))
        self.auto.blockSignals(False)
        self.sync_pin()
        self.show_rules(idx)
        if self.page:
            self.open_device(self.page.spec.id)

    def sync_pin(self) -> None:
        self.pin.blockSignals(True)
        self.pin.setChecked(self.cfg.get("persistent_profile") == self.editing["id"])
        self.pin.blockSignals(False)

    def pick_profile(self, idx: int) -> None:
        self.editing = self.cfg["profiles"][idx]
        self.reload_profiles()

    def set_auto(self, on: bool) -> None:
        self.cfg["auto_switch"] = on
        self.commit()

    def set_pin(self, on: bool) -> None:
        self.cfg["persistent_profile"] = self.editing["id"] if on else None
        self.commit()
        if self.engine:
            self.engine.set_window(self.engine.window)

    def refresh_live(self) -> None:
        if self.connected_ids() != self.shown:
            self.rebuild_home()
        status = {s["id"]: s for s in self.engine.status()} if self.engine else {}
        for sid, c in self.cards.items():
            c.refresh_status(status.get(sid))
        if self.page:
            self.page.refresh_status(status.get(self.page.spec.id))
        for sid, st in status.items():
            key = (sid, st["update"])
            if st["update"] and key not in self.notified and self.tray:
                self.notified.add(key)
                self.tray.showMessage("Actualización de firmware", update_text(CATALOG[sid].name, st))
        if self.engine:
            win = self.engine.window
            where = (win.proton_exe or win.exe or win.wm_class) if win else "sin detectar"
            self.live.setText(f"Perfil activo: {self.engine.profile['name']}  ·  Ventana: {where}")

    # ---- perfiles ----
    def show_rules(self, row: int) -> None:
        if row < 0 or row >= len(self.cfg["profiles"]):
            return
        p = self.cfg["profiles"][row]
        m = p["match"]
        self.rule_name.setText(p["name"])
        self.rule_exe.setText(", ".join(m.get("exe", [])))
        self.rule_class.setText(", ".join(m.get("wm_class", [])))
        self.rule_steam.setText(", ".join(str(a) for a in m.get("steam_appid", [])))
        self.rule_title.setText(m.get("title", ""))
        self.rule_hint.setText("El perfil por defecto se usa cuando ninguna otra regla coincide." if p.get("default") else "")

    def selected_rule_profile(self) -> dict:
        return self.cfg["profiles"][max(self.profile_list.currentRow(), 0)]

    def save_rules(self) -> None:
        p = self.selected_rule_profile()
        split = lambda w: [x.strip() for x in w.text().split(",") if x.strip()]
        p["name"] = self.rule_name.text().strip() or p["name"]
        p["match"] = {"exe": split(self.rule_exe), "wm_class": split(self.rule_class), "steam_appid": split(self.rule_steam), "title": self.rule_title.text().strip()}
        self.commit()
        self.reload_profiles()

    def use_last_window(self) -> None:
        win = self.engine.window if self.engine else None
        if not win:
            return
        self.rule_exe.setText(win.proton_exe or win.exe)
        self.rule_class.setText(win.wm_class)

    def add_profile(self) -> None:
        name, ok = QInputDialog.getText(self, "Nuevo perfil", "Nombre")
        if ok and name.strip():
            p = new_profile(name.strip())
            self.cfg["profiles"].append(p)
            self.editing = p
            self.commit()
            self.reload_profiles()

    def duplicate_profile(self) -> None:
        import uuid
        p = copy.deepcopy(self.selected_rule_profile())
        p.update(id=str(uuid.uuid4()), name=p["name"] + " (copia)", default=False)
        self.cfg["profiles"].append(p)
        self.commit()
        self.reload_profiles()

    def delete_profile(self) -> None:
        p = self.selected_rule_profile()
        if p.get("default"):
            QMessageBox.information(self, "Perfil por defecto", "El perfil por defecto no se puede eliminar.")
            return
        if QMessageBox.question(self, "Eliminar perfil", f"¿Eliminar «{p['name']}»?") == QMessageBox.StandardButton.Yes:
            self.cfg["profiles"].remove(p)
            if self.cfg.get("persistent_profile") == p["id"]:
                self.cfg["persistent_profile"] = None
            self.commit()
            self.reload_profiles()

    def add_steam(self) -> None:
        known = {str(a) for p in self.cfg["profiles"] for a in p["match"].get("steam_appid", [])}
        added = 0
        for game in scan_steam_games():
            if game["appid"] not in known:
                self.cfg["profiles"].append(new_profile(game["name"], {"exe": [], "wm_class": [], "steam_appid": [game["appid"]], "title": ""}))
                added += 1
        self.commit()
        self.reload_profiles()
        QMessageBox.information(self, "Steam", f"Perfiles añadidos: {added}")

    def import_ghub(self) -> None:
        found = ghub_import.find_settings()
        if found:
            path = found[0]
        else:
            name, _ = QFileDialog.getOpenFileName(self, "Busca settings.db de G HUB", str(Path.home()), "settings.db (settings.db)")
            if not name:
                return
            path = Path(name)
        if QMessageBox.question(self, "Importar", f"¿Reemplazar los perfiles actuales con los de\n{path}?") != QMessageBox.StandardButton.Yes:
            return
        try:
            new = ghub_import.import_ghub(path)
        except Exception as exc:  # el archivo procede de otra aplicación
            QMessageBox.critical(self, "No se pudo importar", str(exc))
            return
        self.cfg.clear()
        self.cfg.update(new)
        self.editing = default_profile(self.cfg)
        self.commit()
        self.reload_profiles()
        QMessageBox.information(self, "Importado", f"Perfiles importados: {len(self.cfg['profiles'])}")

    def set_autostart(self, on: bool) -> None:
        if on:
            AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
            script = Path(__file__).resolve().parent.parent / "zhub.py"
            AUTOSTART.write_text(f"[Desktop Entry]\nType=Application\nName=ZHub\nExec=python3 {script} --minimized\nX-KDE-autostart-after=panel\n")
        else:
            AUTOSTART.unlink(missing_ok=True)

    def closeEvent(self, ev):
        if QSystemTrayIcon.isSystemTrayAvailable() and self.engine:
            ev.ignore()
            self.hide()


def app_icon() -> QIcon:
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#4fd1b5"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    p.setPen(QColor("#10211d"))
    p.setFont(QFont("Sans", 32, QFont.Weight.Bold))
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "Z")
    p.end()
    return QIcon(pix)


def main() -> int:
    parser = argparse.ArgumentParser(prog="zhub")
    parser.add_argument("--minimized", action="store_true", help="arrancar en la bandeja")
    parser.add_argument("--no-engine", action="store_true", help="abrir solo la interfaz, sin capturar dispositivos")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")

    app = QApplication(sys.argv)
    app.setApplicationName("ZHub")
    app.setDesktopFileName("zhub")
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(QSS)
    app.setWindowIcon(app_icon())
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    tick = QTimer()  # permite a Python atender señales dentro del bucle de Qt
    tick.start(300)
    tick.timeout.connect(lambda: None)

    bridge = WindowBridge()
    if not bridge.claim():
        print("ZHub ya está en marcha.", file=sys.stderr)
        return 1
    try:
        assets.sync()
    except (OSError, ValueError, KeyError) as exc:
        logging.warning("recursos de G HUB no disponibles: %s", exc)
    cfg = load_config()
    engine = None if args.no_engine else Engine(cfg)
    win = MainWindow(cfg, engine)
    if engine:
        engine.on_change = win.stateChanged.emit
        engine.fw_refs = assets.firmware_refs()
        bridge.activeWindow.connect(lambda c, t, p: engine.set_window(window_info(c, t, p)))
        engine.start()
        bridge.start()

    tray = QSystemTrayIcon(app_icon())
    menu = QMenu()
    show_action = QAction("Abrir ZHub", menu)
    show_action.triggered.connect(lambda: (win.show(), win.raise_()))
    quit_action = QAction("Salir", menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(show_action)
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda r: r == QSystemTrayIcon.ActivationReason.Trigger and (win.show(), win.raise_()))
    tray.show()
    win.tray = tray
    win.refresh_live()
    if not args.minimized:
        win.show()
    code = app.exec()
    if engine:
        bridge.stop()
        engine.stop()
    return code


