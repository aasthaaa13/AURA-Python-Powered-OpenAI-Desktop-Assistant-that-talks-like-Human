"""The compact assistant window: an orb, a caption, and a way to talk to it.

Replaces the fullscreen HUD as the default interface. It deliberately shows no
telemetry — the point is that a small window is cheap to repaint, so animation
no longer competes with the audio path and the voice comes out clean.

The system monitor still runs in the background so spoken commands like
"status" keep working; it simply has nothing on screen.
"""

from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QColor, QLinearGradient, QPainter
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from assistant.assistant_manager import AssistantManager
from assistant.hotkey import GlobalHotkey
from config import (
    APP_NAME,
    HOTKEY,
    MINI_ALWAYS_ON_TOP,
    MINI_WINDOW_SIZE,
    STATE_COLORS,
    TARGET_FPS,
    THEME,
)
from system.system_monitor import SystemMonitor
from ui.orb import OrbWidget
from ui.widgets import display_font, mono_font

STYLESHEET = f"""
QWidget {{ color: {THEME.text}; background: transparent; }}
QLineEdit#miniInput {{
    background: rgba(8, 20, 34, 0.85);
    border: 1px solid {THEME.panel_border};
    border-radius: 8px;
    padding: 0 12px;
    color: {THEME.text};
    selection-background-color: {THEME.primary_soft};
}}
QLineEdit#miniInput:focus {{ border: 1px solid {THEME.primary}; }}
QPushButton#miniMic {{
    background: rgba(12, 34, 51, 0.9);
    border: 1px solid {THEME.panel_border};
    border-radius: 8px;
    color: {THEME.primary};
}}
QPushButton#miniMic:hover {{ border: 1px solid {THEME.primary}; }}
QPushButton#miniMic[listening="true"] {{
    background: rgba(52, 211, 153, 0.22);
    border: 1px solid {THEME.ok};
    color: {THEME.ok};
}}
"""


class MiniWindow(QWidget):
    """Small always-available assistant window."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setStyleSheet(STYLESHEET)
        self.setMinimumSize(260, 300)
        self.resize(*MINI_WINDOW_SIZE)
        if MINI_ALWAYS_ON_TOP:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        # The monitor has no display here, but keeps "status"-type commands live.
        self.monitor = SystemMonitor()
        self.assistant = AssistantManager(stats_provider=lambda: self.monitor.latest)

        self._build_ui()
        self._connect()

        self._closing = False
        self._last_frame = time.monotonic()
        self._frame_timer = QTimer(self)
        self._frame_timer.timeout.connect(self._on_frame)
        self._frame_timer.start(int(1000 / TARGET_FPS))

        self.hotkey = GlobalHotkey(HOTKEY, self)
        self.hotkey.triggered.connect(self._on_hotkey)
        self._hotkey_error = self.hotkey.start()

        self.monitor.start()
        QTimer.singleShot(300, self.assistant.greet)
        QTimer.singleShot(600, self._report_hotkey)

    # -- construction -------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        self.orb = OrbWidget()
        self.orb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.orb, 1)

        self.state_label = QLabel("IDLE")
        self.state_label.setAlignment(Qt.AlignCenter)
        self.state_label.setFont(display_font(11, bold=True, spacing=3.0))
        self.state_label.setStyleSheet(f"color: {THEME.primary};")
        layout.addWidget(self.state_label)

        self.caption = QLabel("")
        self.caption.setAlignment(Qt.AlignCenter)
        self.caption.setWordWrap(True)
        self.caption.setFont(mono_font(11))
        self.caption.setMinimumHeight(46)
        self.caption.setStyleSheet(f"color: {THEME.text_dim};")
        layout.addWidget(self.caption)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.mic = QPushButton("◉")
        self.mic.setObjectName("miniMic")
        self.mic.setCheckable(True)
        self.mic.setCursor(Qt.PointingHandCursor)
        self.mic.setFixedSize(38, 34)
        self.mic.setToolTip("Listen (also: click the orb)")
        self.field = QLineEdit()
        self.field.setObjectName("miniInput")
        self.field.setPlaceholderText("Ask or command…")
        self.field.setFont(mono_font(12))
        self.field.setFixedHeight(34)
        row.addWidget(self.mic)
        row.addWidget(self.field, 1)
        layout.addLayout(row)

    # -- wiring -------------------------------------------------------------
    def _connect(self) -> None:
        self.field.returnPressed.connect(self._submit)
        self.mic.toggled.connect(self._on_mic_toggled)

        self.assistant.state_changed.connect(self._on_state)
        self.assistant.message.connect(self._on_message)
        self.assistant.error.connect(self._on_error)
        self.assistant.level_changed.connect(self.orb.set_level)
        self.assistant.listening_changed.connect(self._on_listening_changed)
        self.assistant.ui_action.connect(self._on_ui_action)
        # Warnings worth surfacing (no missing mic, no key) still reach the caption.
        self.assistant.log.connect(self._on_log)

    def _submit(self) -> None:
        text = self.field.text().strip()
        if not text:
            return
        self.field.clear()
        self.assistant.submit_text(text)

    # -- frame clock --------------------------------------------------------
    def _on_frame(self) -> None:
        now = time.monotonic()
        dt = min(0.1, now - self._last_frame)
        self._last_frame = now
        if not self.isVisible() or self.isMinimized():
            return
        self.orb.tick(dt)
        if self.orb.shutdown_complete:
            self._frame_timer.stop()
            if self._closing:
                self.close()

    # -- slots --------------------------------------------------------------
    @Slot(str)
    def _on_state(self, state: str) -> None:
        self.orb.set_state(state)
        self.state_label.setText(state)
        self.state_label.setStyleSheet(
            f"color: {STATE_COLORS.get(state, THEME.primary)};"
        )

    @Slot(str, str, str)
    def _on_message(self, speaker: str, text: str, level: str) -> None:
        colour = THEME.text if speaker == "AURA" else THEME.text_dim
        self.caption.setStyleSheet(f"color: {colour};")
        self.caption.setText(text if speaker == "AURA" else f"“{text}”")

    @Slot(str, str)
    def _on_log(self, text: str, level: str) -> None:
        if level in ("warn", "error"):
            self.caption.setStyleSheet(f"color: {THEME.warn};")
            self.caption.setText(text)

    @Slot(str)
    def _on_error(self, text: str) -> None:
        self.caption.setStyleSheet(f"color: {THEME.error};")
        self.caption.setText(text)

    @Slot(bool)
    def _on_listening_changed(self, listening: bool) -> None:
        self.mic.setProperty("listening", "true" if listening else "false")
        self.mic.style().unpolish(self.mic)
        self.mic.style().polish(self.mic)
        if not listening and self.mic.isChecked():
            self.mic.blockSignals(True)
            self.mic.setChecked(False)
            self.mic.blockSignals(False)

    @Slot(bool)
    def _on_mic_toggled(self, checked: bool) -> None:
        if checked:
            self.assistant.start_listening()   # interrupts a reply in progress
        else:
            self.assistant.cancel_listening()

    @Slot(str)
    def _on_ui_action(self, action: str) -> None:
        if action == "clear_log":
            self.caption.clear()
        elif action == "quitting":
            self.orb.start_shutdown()
            self.state_label.setText("POWERING DOWN")
            self.state_label.setStyleSheet(f"color: {THEME.error};")
            self.field.setEnabled(False)
            self.mic.setEnabled(False)
        elif action == "quit":
            # Let the power-down animation finish first — with speech muted the
            # farewell returns almost immediately, which would otherwise cut it off.
            self._closing = True
            if self.orb._shutdown is None or self.orb.shutdown_complete:
                self.close()

    # -- global hotkey ------------------------------------------------------
    @Slot()
    def _on_hotkey(self) -> None:
        """Fired from any app: raise the orb and start listening."""
        if self.assistant.state.value == "LISTENING":
            self.assistant.cancel_listening()
            return
        self.show()
        self.raise_()
        self.activateWindow()
        self.mic.setChecked(True)      # drives start_listening, interrupts a reply

    def _report_hotkey(self) -> None:
        if not HOTKEY:
            return
        if self._hotkey_error:
            self.caption.setStyleSheet(f"color: {THEME.warn};")
            self.caption.setText(self._hotkey_error)
        else:
            self.setToolTip(f"Press {HOTKEY} anywhere to talk")

    # -- interaction --------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        # Clicking the orb toggles listening — the primary way to talk to it.
        if self.orb.geometry().contains(event.position().toPoint()):
            self.mic.setChecked(not self.mic.isChecked())
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key_Q and event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier):
            self.close()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter) and not self.field.hasFocus():
            self.field.setFocus()
            return
        if key == Qt.Key_Space and not self.field.hasFocus():
            self.mic.setChecked(not self.mic.isChecked())
            return
        super().keyPressEvent(event)

    # -- painting -----------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        backdrop = QLinearGradient(0, 0, 0, self.height())
        backdrop.setColorAt(0.0, QColor(THEME.bg))
        backdrop.setColorAt(1.0, QColor(THEME.bg_deep))
        painter.fillRect(self.rect(), backdrop)

    # -- lifecycle ----------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802
        self._frame_timer.stop()
        self.hotkey.stop()
        self.assistant.shutdown()
        self.monitor.stop()
        self.monitor.wait(2000)
        super().closeEvent(event)
