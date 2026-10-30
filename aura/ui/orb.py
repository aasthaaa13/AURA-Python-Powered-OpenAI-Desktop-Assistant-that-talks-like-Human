"""The orb: a single animated circle that reacts to the voice.

Deliberately cheap to draw. The full HUD had to repaint a fullscreen retina
window, which cost ~24 ms a frame and intermittently stalled audio playback —
heard as a distorted voice. This widget lives in a small window, so a frame
costs a fraction of that and the animation can run smoothly without competing
with the audio path.

Presentational only: it reads an assistant state string and an audio level, and
never calls into the assistant. `tick(dt)` advances it, `paintEvent` draws it.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget

from config import SHUTDOWN_SECONDS, STATE_COLORS, THEME
from ui.animations import Pulse, Smoothed, ease_in_out

# Harmonics that deform the outline: (lobes, speed, share of the wobble).
# Deliberately no 2-lobe term — that one reads as an egg rather than a circle.
# Higher orders at small amplitude look like a surface rippling instead.
_HARMONICS = ((3, 0.53, 0.40), (4, -0.37, 0.34), (6, 0.29, 0.26))
_OUTLINE_STEPS = 72          # points around the blob; plenty at this size


def _c(hex_color: str, alpha: float = 1.0) -> QColor:
    color = QColor(hex_color)
    color.setAlphaF(max(0.0, min(1.0, alpha)))
    return color


class OrbWidget(QWidget):
    """A soft glowing circle whose edge ripples with the audio level."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(140, 140)

        self._state = "IDLE"
        self._time = 0.0
        self._breathe = Pulse(speed=0.22)
        self._level = Smoothed(0.0, rate=11.0)
        self._wobble = Smoothed(0.0, rate=5.0)     # eases the deformation in/out
        self._color = QColor(THEME.primary)
        self._target_color = QColor(THEME.primary)
        self._shutdown: float | None = None

    # -- state in -----------------------------------------------------------
    def set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self._target_color = QColor(STATE_COLORS.get(state, THEME.primary))
        # How much the outline is allowed to deform in each state.
        self._wobble.set(
            {"IDLE": 0.10, "LISTENING": 0.85, "PROCESSING": 0.55,
             "SPEAKING": 1.0, "ERROR": 0.2}.get(state, 0.10)
        )

    def set_level(self, level: float) -> None:
        self._level.set(max(0.0, min(1.0, level)))

    def start_shutdown(self) -> None:
        if self._shutdown is None:
            self._shutdown = 0.0
            self._target_color = QColor(THEME.error)

    @property
    def shutdown_complete(self) -> bool:
        return self._shutdown is not None and self._shutdown >= 1.0

    # -- animation ----------------------------------------------------------
    def tick(self, dt: float) -> None:
        if self.shutdown_complete:
            return
        if self._shutdown is not None:
            self._shutdown = min(1.0, self._shutdown + dt / SHUTDOWN_SECONDS)

        self._time += dt
        self._breathe.tick(dt)
        self._level.tick(dt)
        self._wobble.tick(dt)

        cur, tgt, k = self._color, self._target_color, min(1.0, dt * 4.0)
        self._color = QColor(
            int(cur.red() + (tgt.red() - cur.red()) * k),
            int(cur.green() + (tgt.green() - cur.green()) * k),
            int(cur.blue() + (tgt.blue() - cur.blue()) * k),
        )
        self.update()

    # -- painting -----------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        centre = QPointF(self.width() / 2.0, self.height() / 2.0)
        span = min(self.width(), self.height())
        if span < 40:
            return

        collapse = 0.0 if self._shutdown is None else ease_in_out(self._shutdown)
        fade = 1.0 - collapse
        if fade <= 0.01:
            return

        level = self._level.value
        breathe = self._breathe.value
        # Base radius breathes slowly and swells with the voice.
        radius = span * 0.34 * (1.0 + 0.03 * breathe + 0.10 * level)
        radius *= (1.0 - collapse) ** 1.3
        if radius < 2.0:
            return
        colour = self._color

        self._draw_glow(painter, centre, radius, colour, level, fade)
        self._draw_blob(painter, centre, radius, colour, level, fade)
        self._draw_ring(painter, centre, span, colour, breathe, fade)

    def _outline(self, centre: QPointF, radius: float, level: float) -> QPainterPath:
        """The circle, gently deformed by a few low harmonics."""
        # Kept small on purpose: this should stay a circle that moves, not a
        # blob. Idle retains a trace so it never looks frozen.
        amount = (0.008 + 0.042 * level) * self._wobble.value
        path = QPainterPath()
        for i in range(_OUTLINE_STEPS + 1):
            theta = (i / _OUTLINE_STEPS) * math.tau
            offset = 0.0
            for lobes, speed, share in _HARMONICS:
                offset += share * math.sin(lobes * theta + self._time * speed * math.tau)
            r = radius * (1.0 + amount * offset)
            point = QPointF(
                centre.x() + math.cos(theta) * r,
                centre.y() + math.sin(theta) * r,
            )
            if i == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
        path.closeSubpath()
        return path

    def _draw_glow(self, painter, centre, radius, colour, level, fade) -> None:
        outer = radius * (1.9 + 0.25 * level)
        gradient = QRadialGradient(centre, outer)
        gradient.setColorAt(0.0, _c(colour.name(), 0.30 * fade))
        gradient.setColorAt(0.35, _c(colour.name(), 0.12 * fade))
        gradient.setColorAt(1.0, _c(colour.name(), 0.0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(gradient))
        painter.drawEllipse(centre, outer, outer)

    def _draw_blob(self, painter, centre, radius, colour, level, fade) -> None:
        path = self._outline(centre, radius, level)

        body = QRadialGradient(centre, radius)
        body.setColorAt(0.0, _c(THEME.primary_glow, (0.55 + 0.35 * level) * fade))
        body.setColorAt(0.55, _c(colour.name(), 0.42 * fade))
        body.setColorAt(1.0, _c(colour.name(), 0.10 * fade))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(body))
        painter.drawPath(path)

        edge = QPen(_c(colour.name(), 0.95 * fade), 2.0)
        edge.setJoinStyle(Qt.RoundJoin)
        painter.setPen(edge)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)

    def _draw_ring(self, painter, centre, span, colour, breathe, fade) -> None:
        """A thin halo ring, fixed in place, that breathes independently."""
        r = span * 0.44 * (1.0 + 0.015 * breathe)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(_c(colour.name(), 0.22 * fade), 1.2))
        painter.drawEllipse(centre, r, r)
