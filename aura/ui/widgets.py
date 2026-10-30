"""Reusable HUD widgets: glass panels, gauges, waveform, log and input bar.

Widgets that animate expose `tick(dt)`; `MainWindow` drives them from one timer.
"""

from __future__ import annotations

import math
import random

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPen,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from config import STATE_COLORS, THEME, WAVEFORM_BARS, WAVEFORM_DECAY
from ui.animations import Pulse, Smoothed


def display_font(size: int, *, bold: bool = False, spacing: float = 1.6) -> QFont:
    font = QFont()
    font.setFamilies(list(THEME.font_display))
    font.setPixelSize(size)
    font.setBold(bold)
    font.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
    return font


def mono_font(size: int, *, bold: bool = False) -> QFont:
    font = QFont()
    font.setFamilies(list(THEME.font_mono))
    font.setPixelSize(size)
    font.setBold(bold)
    return font


def _c(hex_color: str, alpha: float = 1.0) -> QColor:
    color = QColor(hex_color)
    color.setAlphaF(max(0.0, min(1.0, alpha)))
    return color


class SpacedLabel(QLabel):
    """A QLabel that reserves room for letter-spaced text.

    Qt's size hint under-measures fonts with `AbsoluteSpacing` — the gap after
    the final glyph is not counted — so the last character gets clipped. A
    fixed minimum width can't fix it either: at construction the widget has not
    met the screen yet, so its metrics are pre-DPI. Overriding `sizeHint` is the
    reliable fix because the layout queries it after the window is shown.
    """

    def __init__(self, text: str = "", padding: int = 24, parent: QWidget | None = None):
        super().__init__(text, parent)
        self._padding = padding

    def sizeHint(self):  # noqa: N802
        hint = super().sizeHint()
        needed = QFontMetrics(self.font()).horizontalAdvance(self.text()) + self._padding
        return QSize(max(hint.width(), needed), hint.height())

    def minimumSizeHint(self):  # noqa: N802
        return self.sizeHint()

    def setText(self, text: str) -> None:  # noqa: N802
        super().setText(text)
        self.updateGeometry()   # width differs per state; re-ask the layout


class GlassPanel(QFrame):
    """Translucent bordered panel with an optional title strip."""

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("glassPanel")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(14, 12, 14, 12)
        self._layout.setSpacing(8)

        if title:
            label = QLabel(title.upper())
            label.setObjectName("panelTitle")
            label.setFont(display_font(11, bold=True, spacing=2.6))
            self._layout.addWidget(label)

    def body(self) -> QVBoxLayout:
        return self._layout


class StatGauge(QWidget):
    """A labelled horizontal meter — CPU, RAM, disk."""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(42)
        self._label = label
        self._value = Smoothed(0.0, rate=6.0)
        self._text = "--"
        self._painted_text = ""
        self._available = True

    def set_value(self, percent: float | None, text: str | None = None) -> None:
        if percent is None:
            self._available = False
            self._text = text or "N/A"
            self._value.set(0.0)
            return
        self._available = True
        self._value.set(max(0.0, min(100.0, percent)) / 100.0)
        self._text = text if text is not None else f"{percent:.0f}%"

    def tick(self, dt: float) -> None:
        before = self._value.value
        self._value.tick(dt)
        # Skip the repaint when the bar has effectively settled — an idle gauge
        # otherwise damages its panel (and the backdrop) 30 times a second.
        if abs(self._value.value - before) > 1e-4 or self._text != self._painted_text:
            self._painted_text = self._text
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()

        painter.setFont(mono_font(11))
        painter.setPen(_c(THEME.text_dim))
        painter.drawText(QRectF(0, 0, w * 0.5, 16), Qt.AlignLeft | Qt.AlignVCenter,
                         self._label.upper())
        painter.setPen(_c(THEME.text if self._available else THEME.text_faint))
        painter.setFont(mono_font(12, bold=True))
        painter.drawText(QRectF(w * 0.5, 0, w * 0.5, 16),
                         Qt.AlignRight | Qt.AlignVCenter, self._text)

        track = QRectF(0, 24, w, 6)
        painter.setPen(Qt.NoPen)
        painter.setBrush(_c(THEME.grid, 0.9))
        painter.drawRoundedRect(track, 3, 3)

        if not self._available:
            return
        fill_w = max(0.0, w * self._value.value)
        if fill_w <= 0.5:
            return
        # Colour shifts toward warn/error as the meter fills.
        pct = self._value.value
        color = THEME.primary if pct < 0.7 else (THEME.warn if pct < 0.88 else THEME.error)
        gradient = QLinearGradient(0, 0, fill_w, 0)
        gradient.setColorAt(0.0, _c(color, 0.35))
        gradient.setColorAt(1.0, _c(color, 1.0))
        painter.setBrush(gradient)
        painter.drawRoundedRect(QRectF(0, 24, fill_w, 6), 3, 3)


class StatusRow(QWidget):
    """A `LABEL ........ value` line with a coloured state dot."""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(22)
        self._label = label
        self._value = "--"
        self._color = THEME.text_dim

    def set_value(self, value: str, color: str | None = None) -> None:
        self._value = value
        self._color = color or THEME.text
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()

        painter.setBrush(_c(self._color, 0.95))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QRectF(0, h / 2 - 3, 6, 6))

        painter.setFont(mono_font(11))
        painter.setPen(_c(THEME.text_dim))
        painter.drawText(QRectF(14, 0, w * 0.55, h), Qt.AlignLeft | Qt.AlignVCenter,
                         self._label.upper())
        painter.setPen(_c(self._color))
        painter.setFont(mono_font(11, bold=True))
        painter.drawText(QRectF(w * 0.4, 0, w * 0.6, h),
                         Qt.AlignRight | Qt.AlignVCenter, self._value)


class WaveformWidget(QWidget):
    """Mirrored equaliser. Driven by real mic level, idles on a gentle drift."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(78)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._bars = [0.05] * WAVEFORM_BARS
        self._level = Smoothed(0.0, rate=14.0)
        self._idle = Pulse(speed=0.28)
        self._state = "IDLE"
        self._phase = 0.0
        # Per-bar oscillators give the spectrum a flowing shape instead of
        # uniform noise; fixed at construction so the pattern stays coherent.
        self._bar_speed = [1.1 + 2.9 * random.random() for _ in range(WAVEFORM_BARS)]
        self._bar_offset = [random.random() * math.tau for _ in range(WAVEFORM_BARS)]
        self._shutdown = False

    def set_state(self, state: str) -> None:
        self._state = state

    def start_shutdown(self) -> None:
        """Collapse the spectrum to a flat line as the app powers down."""
        self._shutdown = True
        self._state = "SHUTDOWN"

    def set_level(self, level: float) -> None:
        self._level.set(max(0.0, min(1.0, level)))

    def tick(self, dt: float) -> None:
        level = self._level.tick(dt)
        self._idle.tick(dt)
        self._phase += dt

        if self._shutdown:
            for i in range(len(self._bars)):
                self._bars[i] = max(0.0, self._bars[i] - dt * 2.2)
            self.update()
            return

        active = self._state in ("LISTENING", "SPEAKING", "PROCESSING")
        count = len(self._bars)
        decay = WAVEFORM_DECAY * dt

        for i in range(count):
            # Bell-shaped envelope: most energy in the middle, like a real EQ.
            centre = 1.0 - abs(i / (count - 1) - 0.5) * 2.0
            envelope = 0.15 + 0.85 * (centre ** 1.4)
            wave = 0.5 + 0.5 * math.sin(
                self._phase * self._bar_speed[i] * math.tau + self._bar_offset[i]
            )
            floor = (0.03 + 0.05 * wave) * (0.5 + 0.5 * envelope)

            if active:
                drive = level
                if self._state == "PROCESSING":
                    # No mic input while thinking — animate a scanning band instead.
                    drive = max(level, 0.45)
                target = drive * envelope * (0.25 + 0.75 * wave)
                target = max(target, floor)
            else:
                target = floor

            current = self._bars[i]
            # Rise instantly (transient attack), fall smoothly (visual decay).
            self._bars[i] = target if target > current else max(
                floor, current - decay * max(0.12, current)
            )
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        mid = h / 2.0
        count = len(self._bars)
        slot = w / count
        bar_w = max(2.0, slot * 0.5)
        color = QColor(STATE_COLORS.get(self._state, THEME.primary))

        painter.setPen(QPen(_c(THEME.panel_border, 0.55), 1.0))
        painter.drawLine(0, int(mid), w, int(mid))

        painter.setPen(Qt.NoPen)
        for i, value in enumerate(self._bars):
            x = i * slot + (slot - bar_w) / 2.0
            height = max(1.5, value * (mid - 6))
            gradient = QLinearGradient(0, mid - height, 0, mid + height)
            gradient.setColorAt(0.0, _c(color.name(), 0.15))
            gradient.setColorAt(0.5, _c(color.name(), 0.95))
            gradient.setColorAt(1.0, _c(color.name(), 0.15))
            painter.setBrush(gradient)
            painter.drawRoundedRect(
                QRectF(x, mid - height, bar_w, height * 2.0), bar_w / 2, bar_w / 2
            )


class LogPanel(QTextEdit):
    """Append-only activity log with severity colouring."""

    LEVEL_COLORS = {
        "info": THEME.text_dim,
        "user": THEME.primary_glow,
        "assistant": THEME.text,
        "system": THEME.primary_soft,
        "ok": THEME.ok,
        "warn": THEME.warn,
        "error": THEME.error,
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("logPanel")
        self.setReadOnly(True)
        self.setFont(mono_font(12))
        self.document().setMaximumBlockCount(500)   # bounded memory
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.WidgetWidth)

    def append_line(self, prefix: str, text: str, level: str = "info") -> None:
        color = self.LEVEL_COLORS.get(level, THEME.text_dim)
        safe = (
            text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        ).replace("\n", "<br>")
        html = (
            f'<div style="margin:2px 0;">'
            f'<span style="color:{THEME.text_faint};">{prefix}</span> '
            f'<span style="color:{color};">{safe}</span></div>'
        )
        self.append(html)
        self.moveCursor(QTextCursor.End)


class MicButton(QPushButton):
    """Microphone toggle that pulses while the assistant is listening."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("◉  MICROPHONE", parent)
        self.setObjectName("micButton")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFont(display_font(12, bold=True, spacing=2.0))
        self.setFixedHeight(44)
        self._pulse = Pulse(speed=1.1)
        self._active = False

    def sizeHint(self):  # noqa: N802
        # Same letter-spacing under-measurement as SpacedLabel.
        hint = super().sizeHint()
        needed = QFontMetrics(self.font()).horizontalAdvance(self.text()) + 46
        return QSize(max(hint.width(), needed), hint.height())

    def minimumSizeHint(self):  # noqa: N802
        return self.sizeHint()

    def set_active(self, active: bool) -> None:
        self._active = active
        self.setProperty("listening", "true" if active else "false")
        # Re-apply the stylesheet so the property selector takes effect.
        self.style().unpolish(self)
        self.style().polish(self)

    def tick(self, dt: float) -> None:
        if not self._active:
            return
        self._pulse.tick(dt)
        self.update()


class InputBar(QWidget):
    """Text entry + send, mirroring the microphone path."""

    submitted = Signal(str)
    mic_toggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.mic = MicButton()
        self.mic.toggled.connect(self.mic_toggled.emit)

        self.field = QLineEdit()
        self.field.setObjectName("commandInput")
        self.field.setPlaceholderText("Enter command or question…")
        self.field.setFont(mono_font(13))
        self.field.setFixedHeight(44)
        self.field.returnPressed.connect(self._submit)

        self.send = QPushButton("SEND")
        self.send.setObjectName("sendButton")
        self.send.setCursor(Qt.PointingHandCursor)
        self.send.setFont(display_font(12, bold=True, spacing=2.0))
        self.send.setFixedHeight(44)
        self.send.setFixedWidth(104)
        self.send.clicked.connect(self._submit)

        layout.addWidget(self.mic)
        layout.addWidget(self.field, 1)
        layout.addWidget(self.send)

    def _submit(self) -> None:
        text = self.field.text().strip()
        if not text:
            return
        self.field.clear()
        self.submitted.emit(text)

    def tick(self, dt: float) -> None:
        self.mic.tick(dt)
