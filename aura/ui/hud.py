"""The central AI core: a rotating 3D wireframe sphere that reacts to the voice.

Vertices are distributed with a Fibonacci spiral, so they are evenly spaced with
no polar clustering, and the edge list is computed **once** at construction —
the sphere is rigid, so rotating it never changes which points are connected.
Each frame only rotates, displaces and projects the points, which keeps a
~110-vertex / ~230-edge wireframe well inside the frame budget.

Voice reactivity: `set_level()` drives a per-vertex radial displacement, so the
surface ripples outward with the audio rather than merely scaling.

The widget is purely presentational — it reads a state string and a level, and
never calls into the assistant. `tick(dt)` advances it; `paintEvent` draws it.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget

from config import (
    MAX_CORE_RADIUS,
    SHUTDOWN_SECONDS,
    SPHERE_NEIGHBOURS,
    SPHERE_POINTS,
    STATE_COLORS,
    THEME,
)
from ui.animations import Pulse, Smoothed, ease_in_out

# Camera distance in sphere radii. Larger = weaker perspective.
_CAMERA = 3.4
# Depth is bucketed so pens can be reused instead of allocated per edge.
_DEPTH_BUCKETS = 8


def _c(hex_color: str, alpha: float = 1.0) -> QColor:
    color = QColor(hex_color)
    color.setAlphaF(max(0.0, min(1.0, alpha)))
    return color


def _fibonacci_sphere(count: int) -> list[tuple[float, float, float]]:
    """Evenly spaced points on a unit sphere — no clustering at the poles."""
    points = []
    golden = math.pi * (3.0 - math.sqrt(5.0))
    for i in range(count):
        y = 1.0 - (i / max(1, count - 1)) * 2.0
        radius = math.sqrt(max(0.0, 1.0 - y * y))
        theta = golden * i
        points.append((math.cos(theta) * radius, y, math.sin(theta) * radius))
    return points


def _nearest_edges(points, neighbours: int) -> list[tuple[int, int]]:
    """Connect each vertex to its k nearest neighbours, de-duplicated.

    Computed once: the sphere is rigid, so rotation never changes connectivity.
    """
    edges: set[tuple[int, int]] = set()
    for i, (xi, yi, zi) in enumerate(points):
        distances = []
        for j, (xj, yj, zj) in enumerate(points):
            if i == j:
                continue
            dx, dy, dz = xi - xj, yi - yj, zi - zj
            distances.append((dx * dx + dy * dy + dz * dz, j))
        distances.sort()
        for _dist, j in distances[:neighbours]:
            edges.add((i, j) if i < j else (j, i))
    return sorted(edges)


class AICoreWidget(QWidget):
    """Wireframe sphere whose colour, speed and ripple track the assistant."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(220, 220)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)

        self._points = _fibonacci_sphere(SPHERE_POINTS)
        self._edges = _nearest_edges(self._points, SPHERE_NEIGHBOURS)
        # Per-vertex ripple phase, so the surface moves organically, not in unison.
        self._vertex_phase = [
            (i * 2.399963) % math.tau for i in range(len(self._points))
        ]
        self._vertex_speed = [
            1.4 + 2.2 * ((i * 0.6180339887) % 1.0) for i in range(len(self._points))
        ]

        self._state = "IDLE"
        self._yaw = 0.0
        self._pitch_phase = 0.0
        self._time = 0.0
        self._pulse = Pulse(speed=0.4)
        self._level = Smoothed(0.0, rate=13.0)
        self._speed_scale = Smoothed(1.0, rate=3.0)
        self._color = QColor(THEME.primary)
        self._target_color = QColor(THEME.primary)

        self._shutdown: float | None = None     # None until a shutdown starts
        self._pens: list[QPen] = []
        self._pen_color = ""

        # Scratch buffers reused every frame to avoid per-frame allocation.
        self._proj: list[tuple[float, float, float, float]] = [
            (0.0, 0.0, 0.0, 0.0)
        ] * len(self._points)
        self._edge_buckets: list[list] = [[] for _ in range(_DEPTH_BUCKETS)]
        self._vertex_buckets: list[list] = [[] for _ in range(_DEPTH_BUCKETS)]

    # -- state in -----------------------------------------------------------
    def set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self._target_color = QColor(STATE_COLORS.get(state, THEME.primary))
        self._speed_scale.set(
            {"IDLE": 1.0, "LISTENING": 1.8, "PROCESSING": 3.0,
             "SPEAKING": 2.2, "ERROR": 0.35}.get(state, 1.0)
        )

    def set_level(self, level: float) -> None:
        """Audio level 0..1 — ripples the surface and brightens the core."""
        self._level.set(max(0.0, min(1.0, level)))

    def start_shutdown(self) -> None:
        """Begin the power-down animation. Idempotent."""
        if self._shutdown is None:
            self._shutdown = 0.0
            self._target_color = QColor(THEME.error)

    @property
    def shutdown_complete(self) -> bool:
        return self._shutdown is not None and self._shutdown >= 1.0

    # -- animation ----------------------------------------------------------
    def tick(self, dt: float) -> None:
        if self.shutdown_complete:
            return   # frozen final frame: stop spending CPU on it

        if self._shutdown is not None:
            self._shutdown = min(1.0, self._shutdown + dt / SHUTDOWN_SECONDS)

        scale = self._speed_scale.tick(dt)
        if self._shutdown is not None:
            scale *= 1.0 + 4.0 * self._shutdown      # spin up as it collapses
        self._time += dt
        self._yaw = (self._yaw + 26.0 * scale * dt) % 360.0
        self._pitch_phase += dt * 0.35
        self._pulse.tick(dt)
        self._level.tick(dt)

        cur, tgt, k = self._color, self._target_color, min(1.0, dt * 4.0)
        self._color = QColor(
            int(cur.red() + (tgt.red() - cur.red()) * k),
            int(cur.green() + (tgt.green() - cur.green()) * k),
            int(cur.blue() + (tgt.blue() - cur.blue()) * k),
        )
        self.update(self._drawn_rect())

    def _drawn_rect(self) -> QRect:
        """Bounding box of everything paintEvent touches, plus a margin."""
        side = min(self.width(), self.height())
        base = min(side / 2.0 - 6.0, MAX_CORE_RADIUS)
        reach = base * 1.15 + 18.0
        cx, cy = self.width() / 2.0, self.height() / 2.0
        return QRect(
            int(cx - reach), int(cy - reach), int(reach * 2) + 1, int(reach * 2) + 1
        )

    # -- pens ---------------------------------------------------------------
    def _ensure_pens(self, color: QColor, fade: float) -> None:
        """One pen per depth bucket, rebuilt only when the colour changes."""
        signature = f"{color.name()}|{fade:.2f}"
        if signature == self._pen_color:
            return
        self._pen_color = signature
        self._pens = []
        for bucket in range(_DEPTH_BUCKETS):
            depth = bucket / (_DEPTH_BUCKETS - 1)
            pen = QPen(_c(color.name(), (0.07 + 0.5 * depth ** 2) * fade))
            pen.setWidthF(0.6 + 1.0 * depth)
            pen.setCapStyle(Qt.RoundCap)
            self._pens.append(pen)

    # -- painting -----------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        side = min(self.width(), self.height())
        cx, cy = self.width() / 2.0, self.height() / 2.0
        base = min(side / 2.0 - 6.0, MAX_CORE_RADIUS)
        if base <= 10:
            return

        collapse = 0.0 if self._shutdown is None else ease_in_out(self._shutdown)
        fade = 1.0 - collapse
        if fade <= 0.01:
            return   # fully powered down

        level = self._level.value
        radius = base * (0.74 + 0.07 * level) * (1.0 - collapse) ** 1.4
        color = self._color

        self._project(radius, level, collapse)
        self._draw_glow(painter, cx, cy, radius, color, level, fade)
        self._draw_edges(painter, cx, cy, color, fade)
        self._draw_vertices(painter, cx, cy, color, fade)
        self._draw_label(painter, cx, cy, base, fade)

    def _project(self, radius: float, level: float, collapse: float) -> None:
        """Rotate, ripple and perspective-project every vertex."""
        yaw = math.radians(self._yaw)
        cos_y, sin_y = math.cos(yaw), math.sin(yaw)
        # A slow tilt keeps the sphere from reading as a flat spinning disc.
        pitch = math.radians(18.0 * math.sin(self._pitch_phase))
        cos_p, sin_p = math.cos(pitch), math.sin(pitch)

        # Voice drives the ripple; a trace of it remains at idle so it breathes.
        amplitude = 0.02 + 0.34 * level
        proj = self._proj
        for i, (x, y, z) in enumerate(self._points):
            ripple = 1.0 + amplitude * math.sin(
                self._time * self._vertex_speed[i] + self._vertex_phase[i]
            )
            if collapse:
                # Vertices scatter slightly outward as the sphere dies.
                ripple += collapse * 0.35 * math.sin(self._vertex_phase[i] * 3.0)
            rx, ry, rz = x * ripple, y * ripple, z * ripple

            # Yaw about the vertical axis, then a gentle pitch.
            xr = rx * cos_y + rz * sin_y
            zr = -rx * sin_y + rz * cos_y
            yr = ry * cos_p - zr * sin_p
            zr = ry * sin_p + zr * cos_p

            perspective = _CAMERA / (_CAMERA - zr)
            # Clamp: the ripple pushes vertices past the unit sphere, so raw
            # depth can exceed 0..1 and overflow the bucket index downstream.
            depth = (zr + 1.0) * 0.5
            proj[i] = (
                xr * radius * perspective,
                -yr * radius * perspective,
                0.0 if depth < 0.0 else (1.0 if depth > 1.0 else depth),
                perspective,
            )

    def _draw_glow(self, painter, cx, cy, radius, color, level, fade) -> None:
        glow = radius * (0.62 + 0.10 * self._pulse.value + 0.16 * level)
        if glow <= 1.0:
            return
        centre = QPointF(cx, cy)
        gradient = QRadialGradient(centre, glow)
        gradient.setColorAt(0.0, _c(THEME.primary_glow, (0.34 + 0.30 * level) * fade))
        gradient.setColorAt(0.45, _c(color.name(), 0.16 * fade))
        gradient.setColorAt(1.0, _c(color.name(), 0.0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(gradient))
        painter.drawEllipse(centre, glow, glow)

    def _draw_edges(self, painter, cx, cy, color, fade) -> None:
        """Draw the edges grouped by depth bucket.

        The edge list is ordered by vertex index, so depth — and therefore the
        pen — changes on nearly every line; drawing in that order costs ~14 ms a
        frame in pen switches alone. Bucketing first drops that to eight pen
        changes. (Batching into QPainterPaths instead was measured slower still,
        at ~30 ms: many-subpath rasterisation costs more than the direct calls.)
        """
        self._ensure_pens(color, fade)
        proj = self._proj
        buckets = self._edge_buckets
        for bucket in buckets:
            bucket.clear()
        scale = _DEPTH_BUCKETS - 1
        for i, j in self._edges:
            xi, yi, di, _pi = proj[i]
            xj, yj, dj, _pj = proj[j]
            buckets[min(scale, int(((di + dj) * 0.5) * scale))].append(
                (cx + xi, cy + yi, cx + xj, cy + yj)
            )
        painter.setBrush(Qt.NoBrush)
        for index, bucket in enumerate(buckets):
            if not bucket:
                continue
            painter.setPen(self._pens[index])
            for x1, y1, x2, y2 in bucket:
                painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    def _draw_vertices(self, painter, cx, cy, color, fade) -> None:
        """Same bucketing for the dots — one brush change per depth band."""
        buckets = self._vertex_buckets
        for bucket in buckets:
            bucket.clear()
        scale = _DEPTH_BUCKETS - 1
        for x, y, depth, _persp in self._proj:
            buckets[min(scale, int(depth * scale))].append((cx + x, cy + y))

        painter.setPen(Qt.NoPen)
        name = color.name()
        # Back to front, so near dots sit on top of far ones.
        for index, bucket in enumerate(buckets):
            if not bucket:
                continue
            depth = index / scale
            size = 0.9 + 2.0 * depth ** 2
            painter.setBrush(_c(name, (0.18 + 0.78 * depth ** 2) * fade))
            for px, py in bucket:
                painter.drawEllipse(QPointF(px, py), size, size)

    def _draw_label(self, painter, cx, cy, base, fade) -> None:
        text = "OFFLINE" if self._shutdown is not None else self._state
        font = QFont()
        font.setFamilies(list(THEME.font_display))
        font.setPixelSize(max(9, int(base * 0.095)))
        font.setLetterSpacing(QFont.AbsoluteSpacing, 2.6)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(_c(THEME.text, 0.92 * fade))
        rect = QRectF(cx - base, cy + base * 0.80, base * 2, base * 0.26)
        painter.drawText(rect, Qt.AlignCenter, text)
