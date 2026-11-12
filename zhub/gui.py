"""Interfaz gráfica de ZHub."""
from __future__ import annotations

import argparse
import copy
import logging
import os
import signal
import sys
from pathlib import Path

from PyQt6.QtCore import QEvent, QLibraryInfo, QLocale, QPointF, QRectF, Qt, QTimer, QTranslator, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton, QButtonGroup, QScrollArea, QSlider, QSpinBox,
    QStackedWidget, QSystemTrayIcon, QTableWidget, QTabWidget, QVBoxLayout, QWidget,
)

from . import apps, assets, ghub_import, macros, wheel as wheel_io
from .devices import CATALOG, DeviceSpec, key_name
from .engine import Engine
from .kwin import WindowBridge
from .profiles import (
    default_mouse_settings, default_profile, device_cfg, load_config, new_profile, profile_by_id, save_config,
    window_info,
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
QListWidget#picker::item { padding: 8px 11px; }
QListWidget#picker::item:hover { background: #15171a; }
QListWidget#picker::item:disabled { color: #6b717c; }
QListWidget#picker::indicator { width: 18px; height: 18px; border: 2px solid #8d939e; border-radius: 3px; background: #0b0c0e; margin-right: 8px; }
QListWidget#picker::indicator:hover { border-color: #00b8fc; }
QListWidget#picker::indicator:checked { border: 2px solid #00b8fc; background: #00b8fc; image: url(CHECK_ICON); }
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
    ("macro", "Macro"), ("wheel", "Rueda"), ("double_click", "Doble clic"), ("dpi", "DPI"),
    ("gshift", "G-Shift"), ("profile_cycle", "Ciclo de perfil"), ("report_rate_cycle", "Ciclo de tasa de respuesta"),
    ("run", "Ejecutar programa"),
]
_MOUSE = {1: "Izquierdo", 2: "Derecho", 3: "Central", 4: "Atrás", 5: "Adelante"}
_WHEEL = {"up": "Arriba", "down": "Abajo", "left": "Izquierda", "right": "Derecha"}
_DPI = {"cycle": "Ciclo", "up": "Subir", "down": "Bajar", "shift": "DPI Shift", "default": "Volver al predeterminado"}


def pretty_key(name: str) -> str:
    return name.removeprefix("KEY_").removeprefix("BTN_").title()


def describe(a: dict | None, profile: dict | None = None) -> str:
    if a is None:
        return "Por defecto"
    t = a.get("type")
    if t == "macro":
        m = macros.find(profile or {}, a.get("id"))
        return f"Macro: {m['name']}" if m else "Macro eliminada"
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


class StepEditor(QWidget):
    """Pasos de una macro: se graban o se añaden a mano, como en el editor de G HUB."""

    def __init__(self, steps: list[dict]):
        super().__init__()
        self.steps = copy.deepcopy(steps)
        self.recording, self.last = False, 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        tools = QHBoxLayout()
        self.record = QPushButton("● Grabar")
        self.record.setCheckable(True)
        self.record.toggled.connect(self.toggle_record)
        tools.addWidget(self.record)
        for label, fn in (("+ Tecla", self.add_key), ("+ Retardo", self.add_delay), ("+ Clic", self.add_click),
                          ("+ Texto", self.add_text), ("+ Programa", self.add_program)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            tools.addWidget(b)
        tools.addStretch(1)
        lay.addLayout(tools)
        self.list = QListWidget()
        self.list.setObjectName("steps")
        self.list.installEventFilter(self)
        self.list.itemDoubleClicked.connect(self.edit_item)
        lay.addWidget(self.list, 1)
        order = QHBoxLayout()
        for label, fn in (("Subir", lambda: self.move(-1)), ("Bajar", lambda: self.move(1)),
                          ("Eliminar", self.remove), ("Vaciar", self.clear)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            order.addWidget(b)
        order.addStretch(1)
        hint = heading("Doble clic en un retardo, texto o programa para cambiarlo", "muted")
        order.addWidget(hint)
        lay.addLayout(order)
        self.refresh()

    def refresh(self, select: int | None = None) -> None:
        self.list.clear()
        for step in self.steps:
            self.list.addItem(macros.describe_step(step))
        if select is not None and self.steps:
            self.list.setCurrentRow(min(max(select, 0), len(self.steps) - 1))

    def insert(self, new: list[dict]) -> None:
        row = self.list.currentRow()
        at = row + 1 if row >= 0 else len(self.steps)
        self.steps[at:at] = new
        self.refresh(at + len(new) - 1)

    def toggle_record(self, on: bool) -> None:
        self.recording, self.last = on, 0
        self.record.setText("■ Detener" if on else "● Grabar")
        if on:
            self.list.setFocus()

    def eventFilter(self, obj, ev):
        if self.recording and ev.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            code = ev.nativeScanCode() - 8
            if code > 0 and not ev.isAutoRepeat():
                new = [{"delay": max(1, ev.timestamp() - self.last)}] if self.last else []
                self.last = ev.timestamp()
                new.append({"key": key_name(code), "down": ev.type() == QEvent.Type.KeyPress})
                self.steps.extend(new)
                self.refresh(len(self.steps) - 1)
            return True
        return super().eventFilter(obj, ev)

    def add_key(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Añadir tecla o atajo")
        lay = QVBoxLayout(dlg)
        capture = KeyCapture([])
        lay.addWidget(capture)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        if dlg.exec() and capture.keys:
            self.insert([{"key": k, "down": True} for k in capture.keys] +
                        [{"key": k, "down": False} for k in reversed(capture.keys)])

    def add_delay(self) -> None:
        ms, ok = QInputDialog.getInt(self, "Añadir retardo", "Milisegundos", macros.DEFAULT_DELAY, 1, 600000)
        if ok:
            self.insert([{"delay": ms}])

    def add_click(self) -> None:
        names = list(_MOUSE.values())
        choice, ok = QInputDialog.getItem(self, "Añadir clic", "Botón", names, 0, False)
        if ok:
            button = list(_MOUSE)[names.index(choice)]
            self.insert([{"mouse": button, "down": True}, {"mouse": button, "down": False}])

    def ask_text(self, current: str = "") -> str | None:
        text, ok = QInputDialog.getText(self, "Texto", "Texto que se escribirá", text=current)
        if not ok or not text:
            return None
        if not macros.text_supported(text):
            bad = "".join(sorted({c for c in text if c not in macros.CHARS}))
            QMessageBox.warning(self, "Caracteres no admitidos",
                                f"Solo se escriben caracteres de la distribución de teclado US. Se omitirán: {bad}")
        return text

    def add_text(self) -> None:
        text = self.ask_text()
        if text:
            self.insert([{"text": text}])

    def add_program(self) -> None:
        cmd, ok = QInputDialog.getText(self, "Abrir programa", "Orden (por ejemplo: spectacle -r)")
        if ok and cmd.strip():
            self.insert([{"run": cmd.strip()}])

    def edit_item(self, item) -> None:
        row = self.list.row(item)
        step = self.steps[row]
        if "delay" in step:
            ms, ok = QInputDialog.getInt(self, "Retardo", "Milisegundos", step["delay"], 1, 600000)
            if ok:
                step["delay"] = ms
        elif "text" in step:
            text = self.ask_text(step["text"])
            if text:
                step["text"] = text
        elif "run" in step:
            cmd, ok = QInputDialog.getText(self, "Abrir programa", "Orden", text=step["run"])
            if ok and cmd.strip():
                step["run"] = cmd.strip()
        self.refresh(row)

    def move(self, delta: int) -> None:
        row = self.list.currentRow()
        to = row + delta
        if row < 0 or not 0 <= to < len(self.steps):
            return
        self.steps[row], self.steps[to] = self.steps[to], self.steps[row]
        self.refresh(to)

    def remove(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            del self.steps[row]
            self.refresh(row)

    def clear(self) -> None:
        self.steps = []
        self.refresh()


class MacroDialog(QDialog):
    """Crear o editar una macro con los tipos de G HUB."""

    def __init__(self, parent, macro: dict):
        super().__init__(parent)
        self.setWindowTitle("Macro")
        self.setMinimumSize(720, 560)
        self.macro = copy.deepcopy(macro)
        root = QVBoxLayout(self)
        self.name = QLineEdit(self.macro.get("name", ""))
        self.name.setPlaceholderText("Nombre de la macro")
        root.addWidget(self.name)
        modes = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        for key, label in macros.MODES.items():
            b = QPushButton(label)
            b.setObjectName("segment")
            b.setCheckable(True)
            b.setChecked(key == self.macro["mode"])
            b.clicked.connect(lambda _=False, k=key: self.set_mode(k))
            self.mode_group.addButton(b)
            modes.addWidget(b)
        root.addLayout(modes)
        self.help = heading("", "muted")
        self.help.setWordWrap(True)
        root.addWidget(self.help)
        delay = QHBoxLayout()
        self.standard = QCheckBox("Usar retardo estándar entre acciones (ignora los retardos grabados)")
        self.standard.setChecked(self.macro.get("standard_delay", False))
        self.delay = QSpinBox()
        self.delay.setRange(1, 10000)
        self.delay.setSuffix(" ms")
        self.delay.setValue(self.macro.get("delay", macros.DEFAULT_DELAY))
        self.delay.setEnabled(self.standard.isChecked())
        self.standard.toggled.connect(self.delay.setEnabled)
        delay.addWidget(self.standard)
        delay.addWidget(self.delay)
        delay.addStretch(1)
        root.addLayout(delay)
        self.single = StepEditor(self.macro.get("steps", []))
        self.phases = QTabWidget()
        self.phase_editors = {}
        for key, label in macros.PHASES.items():
            self.phase_editors[key] = StepEditor(self.macro.get(key, []))
            self.phases.addTab(self.phase_editors[key], label)
        self.editors = QStackedWidget()
        self.editors.addWidget(self.single)
        self.editors.addWidget(self.phases)
        root.addWidget(self.editors, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.set_mode(self.macro["mode"])

    def set_mode(self, mode: str) -> None:
        if mode == "sequence" and self.macro["mode"] != "sequence":
            empty = all(not ed.steps for ed in self.phase_editors.values())
            if empty and self.single.steps:  # lleva los pasos a «Al pulsar» para no perderlos
                self.phase_editors["press"].steps = copy.deepcopy(self.single.steps)
                self.phase_editors["press"].refresh()
        self.macro["mode"] = mode
        self.help.setText(macros.MODE_HELP[mode])
        self.editors.setCurrentIndex(1 if mode == "sequence" else 0)

    def _accept(self) -> None:
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(self, "Falta el nombre", "Ponle un nombre a la macro.")
            return
        self.macro.update(name=name, standard_delay=self.standard.isChecked(), delay=self.delay.value(),
                          steps=self.single.steps, **{k: ed.steps for k, ed in self.phase_editors.items()})
        self.accept()


class ActionDialog(QDialog):
    def __init__(self, parent, title: str, action: dict | None, profile: dict):
        super().__init__(parent)
        self.profile = profile
        self.library_changed = False
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
        self._page("macro", self._macro_page(a))
        blank = QWidget()
        self.pages.addWidget(blank)
        self.blank = self.pages.count() - 1

        self.kind.currentIndexChanged.connect(self._sync)
        current = "default" if action is None else "macro" if a.get("type") == "sequence" else a.get("type")
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
        self.legacy = macros.from_legacy(a) if a.get("type") == "sequence" else None
        self.macro_box = QComboBox()
        row = QHBoxLayout()
        for label, fn in (("Nueva macro…", self._new_macro), ("Editar…", self._edit_macro)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addWidget(self.macro_box)
        lay.addLayout(row)
        note = heading("Las macros son del perfil, como en G HUB: cada aplicación tiene las suyas.", "muted")
        note.setWordWrap(True)
        lay.addWidget(note)
        self._fill_macros(self.legacy["id"] if self.legacy else a.get("id"))
        return page

    def _choices(self) -> list[dict]:
        return macros.library(self.profile) + ([self.legacy] if self.legacy else [])

    def _fill_macros(self, select: str | None) -> None:
        self.macro_box.clear()
        for m in self._choices():
            self.macro_box.addItem(f"{m['name']} · {macros.MODES[m['mode']]}", m["id"])
        idx = self.macro_box.findData(select)
        if idx >= 0:
            self.macro_box.setCurrentIndex(idx)

    def _new_macro(self) -> None:
        dlg = MacroDialog(self, macros.new_macro(""))
        if dlg.exec():
            macros.library(self.profile).append(dlg.macro)
            self.library_changed = True
            self._fill_macros(dlg.macro["id"])

    def _edit_macro(self) -> None:
        mid = self.macro_box.currentData()
        m = next((x for x in self._choices() if x["id"] == mid), None)
        if not m:
            return
        dlg = MacroDialog(self, m)
        if dlg.exec():
            m.clear()
            m.update(dlg.macro)
            self.library_changed = True
            self._fill_macros(mid)

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
        elif t == "macro":
            mid = self.macro_box.currentData()
            if not mid:
                QMessageBox.warning(self, "Sin macro", "Crea o elige una macro.")
                return
            if self.legacy and mid == self.legacy["id"]:  # la macro antigua pasa a la biblioteca del perfil
                macros.library(self.profile).append(self.legacy)
            self.result_action = {"type": "macro", "id": mid}
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
            self.section("Macros", self.macros_page())
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
        return describe(action, self.win.editing)

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
        dlg = ActionDialog(self, self.spec.buttons[bid], copy.deepcopy(self.cfg.get(layer, {}).get(bid)), self.win.editing)
        ok = dlg.exec()
        if dlg.library_changed:
            self.refresh_macros()
        if not ok:
            if dlg.library_changed:
                self.win.commit()
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

    # ---- macros ----
    def macros_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        frame, box = card()
        box.addWidget(heading(f"Macros de «{self.win.editing['name']}»"))
        note = heading("Como en G HUB, las macros pertenecen al perfil de cada aplicación y se pueden asignar a cualquier botón.", "muted")
        note.setWordWrap(True)
        box.addWidget(note)
        self.macro_list = QListWidget()
        self.macro_list.setMinimumHeight(300)
        self.macro_list.itemDoubleClicked.connect(lambda _: self.edit_macro())
        box.addWidget(self.macro_list)
        row = QHBoxLayout()
        for label, fn, primary in (("Crear macro", self.create_macro, True), ("Editar", self.edit_macro, False),
                                   ("Duplicar", self.duplicate_macro, False), ("Eliminar", self.delete_macro, False)):
            b = QPushButton(label)
            if primary:
                b.setObjectName("primary")
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        box.addLayout(row)
        lay.addWidget(frame)
        lay.addStretch(1)
        self.refresh_macros()
        return page

    def refresh_macros(self) -> None:
        if not hasattr(self, "macro_list"):
            return
        row = self.macro_list.currentRow()
        self.macro_list.clear()
        for m in macros.library(self.win.editing):
            self.macro_list.addItem(f"{m['name']}  ·  {macros.MODES[m['mode']]}")
        if self.macro_list.count():
            self.macro_list.setCurrentRow(min(max(row, 0), self.macro_list.count() - 1))
        self.render_buttons()

    def selected_macro(self) -> dict | None:
        lib, row = macros.library(self.win.editing), self.macro_list.currentRow()
        return lib[row] if 0 <= row < len(lib) else None

    def create_macro(self) -> None:
        dlg = MacroDialog(self, macros.new_macro(""))
        if dlg.exec():
            macros.library(self.win.editing).append(dlg.macro)
            self.refresh_macros()
            self.macro_list.setCurrentRow(self.macro_list.count() - 1)
            self.win.commit()

    def edit_macro(self) -> None:
        m = self.selected_macro()
        if m:
            dlg = MacroDialog(self, m)
            if dlg.exec():
                m.clear()
                m.update(dlg.macro)
                self.refresh_macros()
                self.win.commit()

    def duplicate_macro(self) -> None:
        m = self.selected_macro()
        if m:
            twin = copy.deepcopy(m)
            twin.update(id=macros.new_macro("")["id"], name=f"{m['name']} (copia)")
            macros.library(self.win.editing).append(twin)
            self.refresh_macros()
            self.macro_list.setCurrentRow(self.macro_list.count() - 1)
            self.win.commit()

    def delete_macro(self) -> None:
        m = self.selected_macro()
        if not m:
            return
        if QMessageBox.question(self, "Eliminar macro", f"¿Eliminar «{m['name']}»? Los botones que la usan quedarán por defecto.") \
                != QMessageBox.StandardButton.Yes:
            return
        macros.library(self.win.editing).remove(m)
        for dev in self.win.editing.get("devices", {}).values():
            for layer in ("buttons", "shifted"):
                bucket = dev.get(layer, {})
                for bid in [b for b, a in bucket.items() if a.get("type") == "macro" and a.get("id") == m["id"]]:
                    del bucket[bid]
        self.refresh_macros()
        self.win.commit()

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
        control = QHBoxLayout()
        control.addWidget(QLabel("Quién controla el giro y la fuerza:"))
        group = QButtonGroup(page)
        for key, label in (("zhub", "ZHub"), ("game", "El juego")):
            b = QPushButton(label)
            b.setObjectName("segment")
            b.setCheckable(True)
            b.setChecked(self.wheel().get("control", "zhub") == key)
            b.clicked.connect(lambda _=False, k=key: set_control(k))
            group.addButton(b)
            control.addWidget(b)
        control.addStretch(1)
        lay.addLayout(control)
        control_help = heading("", "muted")
        control_help.setWordWrap(True)
        lay.addWidget(control_help)
        form = QFormLayout()
        managed = [self.slider_row(form, "range", "Radio de giro (°)", 180, 900, 900)]
        centering = QCheckBox("Activar autocentrado (desactívalo si el juego centra el volante por su cuenta)")
        centering.setChecked(self.wheel().get("autocenter_on", True))
        form.addRow(centering)
        strength = self.slider_row(form, "autocenter", "Fuerza del autocentrado (%)", 0, 100, 20)
        centering.toggled.connect(lambda on: strength.setEnabled(on and not wheel_io.game_controls(self.wheel())))
        centering.toggled.connect(lambda on: self.save_wheel("autocenter_on", on))
        managed += [centering, self.slider_row(form, "gain", "Fuerza de respuesta (%)", 0, 100, 100),
                    self.slider_row(form, "damping", "Amortiguación (%)", 0, 100, 10)]
        self.slider_row(form, "sensitivity", "Sensibilidad (50 = lineal)", 0, 100, 50)
        lay.addLayout(form)

        def show_control() -> None:
            game = wheel_io.game_controls(self.wheel())
            for widget in managed:
                widget.setEnabled(not game)
            strength.setEnabled(not game and centering.isChecked())
            control_help.setText(
                "El juego decide el giro, el autocentrado y la fuerza. ZHub deja el volante a 900° y sin "
                "resortes propios, y no lo vuelve a tocar. La sensibilidad y los pedales siguen aplicándose."
                if game else
                "ZHub aplica estos ajustes al entrar en el perfil y los vuelve a poner si el juego los cambia al arrancar.")

        def set_control(key: str) -> None:
            self.save_wheel("control", key)
            show_control()

        show_control()
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


class AddProfilesDialog(QDialog):
    """Elige apps y juegos instalados (Steam, Heroic, Lutris, menú) para crearles perfil."""

    FILTERS = ("Todo", "Juegos", "Aplicaciones", *apps.GAME_SOURCES)

    def __init__(self, parent, cfg: dict):
        super().__init__(parent)
        self.setWindowTitle("Añadir perfiles")
        self.resize(640, 620)
        self.entries = apps.scan_all()
        root = QVBoxLayout(self)
        note = heading("Marca las apps y juegos para los que quieres un perfil. Los juegos de Heroic y Lutris "
                       "se reconocen por su carpeta de instalación; las aplicaciones, por su ventana. Los que ya tienen "
                       "perfil solo aparecen al buscarlos.", "muted")
        note.setWordWrap(True)
        root.addWidget(note)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar…")
        self.search.textChanged.connect(self.apply_filter)
        self.source = QComboBox()
        self.source.addItems(self.FILTERS)
        self.source.currentIndexChanged.connect(self.apply_filter)
        top.addWidget(self.search, 1)
        top.addWidget(self.source)
        root.addLayout(top)
        self.list = QListWidget()
        self.list.setObjectName("picker")
        self.known = [apps.has_profile(cfg, entry) for entry in self.entries]
        for entry, known in zip(self.entries, self.known):
            item = QListWidgetItem(f"{entry['name']}   ·   {entry['source']}" + ("   — ya tiene un perfil" if known else ""))
            if known:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            else:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
            self.list.addItem(item)
        self.list.itemChanged.connect(self.update_count)
        root.addWidget(self.list, 1)
        row = QHBoxLayout()
        mark = QPushButton("Marcar los visibles")
        mark.clicked.connect(lambda: self.mark_visible(True))
        clear = QPushButton("Desmarcar")
        clear.clicked.connect(lambda: self.mark_visible(False))
        row.addWidget(mark)
        row.addWidget(clear)
        row.addStretch(1)
        self.create = QPushButton()
        self.create.setObjectName("primary")
        self.create.clicked.connect(self.accept)
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        row.addWidget(self.create)
        row.addWidget(cancel)
        root.addLayout(row)
        self.apply_filter()
        self.update_count()

    def visible(self, entry: dict) -> bool:
        wanted = self.source.currentText()
        if wanted == "Juegos" and entry["source"] == apps.APP_SOURCE:
            return False
        if wanted == "Aplicaciones" and entry["source"] != apps.APP_SOURCE:
            return False
        if wanted in apps.GAME_SOURCES and entry["source"] != wanted:
            return False
        return self.search.text().strip().lower() in entry["name"].lower()

    def apply_filter(self) -> None:
        searching = bool(self.search.text().strip())
        for i, entry in enumerate(self.entries):
            # Los que ya tienen perfil solo estorban: se muestran únicamente al buscarlos
            hidden = not self.visible(entry) or (self.known[i] and not searching)
            self.list.item(i).setHidden(hidden)

    def mark_visible(self, on: bool) -> None:
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        for i in range(self.list.count()):
            item = self.list.item(i)
            if (not item.isHidden() or not on) and item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(state)

    def chosen(self) -> list[dict]:
        return [e for i, e in enumerate(self.entries) if self.list.item(i).checkState() == Qt.CheckState.Checked
                and self.list.item(i).flags() & Qt.ItemFlag.ItemIsUserCheckable]

    def update_count(self, *_) -> None:
        n = len(self.chosen())
        self.create.setText(f"Crear {n} perfil{'es' if n != 1 else ''}")
        self.create.setEnabled(n > 0)


class MainWindow(QMainWindow):
    stateChanged = pyqtSignal()

    def __init__(self, cfg: dict, engine: Engine | None):
        super().__init__()
        self.cfg, self.engine = cfg, engine
        self.editing = default_profile(cfg)
        self.followed: str | None = None  # último perfil activo que siguió el selector
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

    def open_device(self, sid: str, section: int = 0) -> None:
        self.close_device()
        self.page = DevicePage(self, CATALOG[sid])
        self.page.rail.setCurrentRow(section)
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
        for text, fn in (("Nuevo perfil", self.add_profile), ("Duplicar", self.duplicate_profile), ("Eliminar", self.delete_profile), ("Añadir apps y juegos…", self.add_installed)):
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
        self.rule_path = QLineEdit()
        self.rule_path.setPlaceholderText("/ruta/al/juego; otra/carpeta (para Heroic, Lutris…)")
        form.addRow("Nombre", self.rule_name)
        form.addRow("Ejecutable", self.rule_exe)
        form.addRow("Clase de ventana", self.rule_class)
        form.addRow("Steam AppID", self.rule_steam)
        form.addRow("Título", self.rule_title)
        form.addRow("Carpeta", self.rule_path)
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
            self.open_device(self.page.spec.id, self.page.rail.currentRow())

    def follow_active(self) -> None:
        """Como G HUB: al cambiar el perfil activo, la ventana pasa a mostrarlo."""
        active = self.engine.profile
        if active["id"] == self.followed or QApplication.activeModalWidget():
            return  # con un diálogo abierto se espera a que se cierre
        self.followed = active["id"]
        if active["id"] != self.editing["id"] and profile_by_id(self.cfg, active["id"]):
            self.editing = profile_by_id(self.cfg, active["id"])
            self.reload_profiles()

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
        if self.engine:
            self.follow_active()
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
        self.rule_path.setText("; ".join(m.get("path", [])))
        self.rule_hint.setText("El perfil por defecto se usa cuando ninguna otra regla coincide." if p.get("default") else "")

    def selected_rule_profile(self) -> dict:
        return self.cfg["profiles"][max(self.profile_list.currentRow(), 0)]

    def save_rules(self) -> None:
        p = self.selected_rule_profile()
        split = lambda w: [x.strip() for x in w.text().split(",") if x.strip()]
        p["name"] = self.rule_name.text().strip() or p["name"]
        p["match"] = {"exe": split(self.rule_exe), "wm_class": split(self.rule_class), "steam_appid": split(self.rule_steam),
                      "title": self.rule_title.text().strip(),
                      "path": [x.strip() for x in self.rule_path.text().split(";") if x.strip()]}
        self.commit()
        self.reload_profiles()

    def use_last_window(self) -> None:
        win = self.engine.window if self.engine else None
        if not win:
            return
        self.rule_exe.setText(win.proton_exe or win.exe)
        self.rule_class.setText(win.wm_class)
        if win.proton_exe and not win.steam_appid and win.cwd:
            self.rule_path.setText(win.cwd)  # juego de Wine/Proton fuera de Steam: su carpeta

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

    def add_installed(self) -> None:
        dialog = AddProfilesDialog(self, self.cfg)
        if not dialog.exec():
            return
        for entry in dialog.chosen():
            self.cfg["profiles"].append(new_profile(entry["name"], apps.to_match(entry)))
        self.commit()
        self.reload_profiles()

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


CHECK_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path d="M3 8.5l3.2 3.2L13 4.8" '
             'fill="none" stroke="#04131b" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def style() -> str:
    """Hoja de estilo con la marca de las casillas (Qt solo la carga desde un archivo)."""
    icon = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "zhub-check.svg"
    try:
        if not icon.exists() or icon.read_text() != CHECK_SVG:
            icon.write_text(CHECK_SVG)
    except OSError:
        return QSS.replace("image: url(CHECK_ICON);", "")
    return QSS.replace("CHECK_ICON", str(icon))


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
    qt_text = QTranslator(app)  # botones estándar (Aceptar, Cancelar, Guardar) en el idioma del sistema
    if qt_text.load(QLocale.system(), "qtbase", "_", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(qt_text)
    app.setApplicationName("ZHub")
    app.setDesktopFileName("zhub")
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(style())
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


