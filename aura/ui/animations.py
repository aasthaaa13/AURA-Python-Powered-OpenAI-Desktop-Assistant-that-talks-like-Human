"""Animation primitives shared by the HUD widgets.

Everything here is frame-based: `MainWindow` owns one QTimer and calls `tick(dt)`
on each animated widget, so animation cost stays bounded no matter how many
panels are on screen. Nothing in this module touches the assistant or the
network, so animation never blocks on Python work.
"""

from __future__ import annotations

import math


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * clamp(t)


def ease_in_out(t: float) -> float:
    t = clamp(t)
    return t * t * (3.0 - 2.0 * t)


def ease_out_cubic(t: float) -> float:
    t = clamp(t)
    return 1.0 - (1.0 - t) ** 3


class Smoothed:
    """A value that chases a target at a frame-rate-independent rate.

    `rate` is roughly "how many times closer per second"; higher snaps faster.
    """

    __slots__ = ("value", "target", "rate")

    def __init__(self, value: float = 0.0, rate: float = 8.0) -> None:
        self.value = value
        self.target = value
        self.rate = rate

    def set(self, target: float) -> None:
        self.target = target

    def jump(self, value: float) -> None:
        self.value = self.target = value

    def tick(self, dt: float) -> float:
        # Exponential approach: stable for any dt, unlike a fixed-step lerp.
        alpha = 1.0 - math.exp(-self.rate * max(dt, 0.0))
        self.value += (self.target - self.value) * alpha
        return self.value


class Pulse:
    """A looping 0..1 oscillator used for glow and breathing effects."""

    __slots__ = ("phase", "speed")

    def __init__(self, speed: float = 1.0, phase: float = 0.0) -> None:
        self.speed = speed
        self.phase = phase

    def tick(self, dt: float) -> float:
        self.phase = (self.phase + dt * self.speed) % 1.0
        return self.value

    @property
    def value(self) -> float:
        # Sine mapped to 0..1 — smooth at the loop point, unlike a sawtooth.
        return 0.5 + 0.5 * math.sin(self.phase * math.tau)


class Spinner:
    """A continuously rotating angle in degrees."""

    __slots__ = ("angle", "speed")

    def __init__(self, speed: float = 30.0, angle: float = 0.0) -> None:
        self.speed = speed
        self.angle = angle

    def tick(self, dt: float, scale: float = 1.0) -> float:
        self.angle = (self.angle + self.speed * scale * dt) % 360.0
        return self.angle
