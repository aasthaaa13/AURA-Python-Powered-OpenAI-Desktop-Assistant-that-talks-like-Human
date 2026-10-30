"""Central configuration for the AURA HUD.

Every tunable lives here so the UI, assistant and system layers can be changed
independently. Secrets are read from the environment (or a `.env` next to this
file) and are never hardcoded — see `.env.example`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is optional
    def load_dotenv(*_args, **_kwargs):  # type: ignore[misc]
        return False

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"

load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# --- identity ---------------------------------------------------------------
APP_NAME = "A.U.R.A"
APP_SUBTITLE = "SYSTEM ONLINE"
# Who the assistant addresses.
USER_NAME = _env("AURA_USER_NAME", "Aastha")

# Interface: "mini" is the small orb window (default), "hud" the old fullscreen
# dashboard. The orb exists because repainting a fullscreen retina window costs
# ~24 ms a frame and intermittently stalls audio playback, which is heard as a
# distorted voice; a small window makes that cost — and the problem — go away.
UI_MODE = (_env("AURA_UI_MODE", "mini") or "mini").lower()
MINI_WINDOW_SIZE = (
    max(260, int(_env_float("AURA_MINI_WIDTH", 340))),
    max(300, int(_env_float("AURA_MINI_HEIGHT", 430))),
)
MINI_ALWAYS_ON_TOP = _env_bool("AURA_MINI_ON_TOP", False)

# System-wide shortcut that starts listening from any app. macOS only, and it
# needs Accessibility permission — see assistant/hotkey.py. Blank disables it.
# Modifiers: fn, cmd, ctrl, alt, shift. Example: "ctrl+space", "cmd+shift+j".
HOTKEY = _env("AURA_HOTKEY", "ctrl+space")

# Fullscreen applies to the "hud" mode only; the orb window is never fullscreen.
START_FULLSCREEN = _env_bool("AURA_START_FULLSCREEN", True)
# Low enough that the responsive breakpoints in MainWindow are actually reachable.
WINDOW_MIN_SIZE = (660, 500)
WINDOW_START_SIZE = (1360, 860)


# --- theme ------------------------------------------------------------------
@dataclass(frozen=True)
class Theme:
    """Colours and type for the HUD. Hex strings, usable by QSS and QPainter."""

    bg: str = "#04070E"
    bg_deep: str = "#020409"
    panel: str = "#0A1526"
    panel_border: str = "#16394F"
    grid: str = "#0C2233"

    primary: str = "#22D3EE"          # main cyan
    primary_soft: str = "#0E7490"     # dim cyan for rings/edges
    primary_glow: str = "#67E8F9"     # highlight

    text: str = "#CBEEF9"
    text_dim: str = "#6E93A6"
    text_faint: str = "#3D5A6B"

    ok: str = "#34D399"
    warn: str = "#FBBF24"
    error: str = "#FB7185"
    speaking: str = "#A78BFA"

    # Preference order; the first family installed on the machine wins.
    font_display: tuple[str, ...] = (
        "Orbitron", "Rajdhani", "Eurostile", "Bank Gothic",
        "Avenir Next Condensed", "Helvetica Neue", "Arial",
    )
    font_mono: tuple[str, ...] = (
        "JetBrains Mono", "SF Mono", "Menlo", "Consolas",
        "DejaVu Sans Mono", "Courier New",
    )


THEME = Theme()

STATE_COLORS = {
    "SHUTDOWN": THEME.error,
    "IDLE": THEME.primary,
    "LISTENING": THEME.ok,
    "PROCESSING": THEME.warn,
    "SPEAKING": THEME.speaking,
    "ERROR": THEME.error,
}


# --- animation --------------------------------------------------------------
# Single shared clock drives every animation.
#
# The small orb window costs ~3.8 ms a frame against the fullscreen HUD's ~24 ms,
# because the cost is dominated by the window flush rather than by what is drawn.
# That headroom is why this can sit at 30 and no longer needs to be traded off
# against audio: measured with the orb painting at 30 fps, zero audio underruns
# across repeated playbacks, and speech timing matching the no-painting baseline.
# The old fullscreen HUD (AURA_UI_MODE=hud) still wants a lower value.
TARGET_FPS = max(5, int(_env_float("AURA_TARGET_FPS", 30)))

# Frame rate used while the audio path is live *and* unbuffered.
#
# The voice used to distort because streaming the OS voice puts a hard real-time
# deadline on playback, and painting a fullscreen retina window intermittently
# blew it. Throttling the UI only traded that off; the real fix was to stop
# streaming (see SpeechEngine._speak_rendered — render to a file, then play it
# through a buffer that absorbs the stalls). Measured after that change: zero
# audio underruns across repeated playbacks with the HUD painting at full rate.
#
# So this now applies only where a real-time deadline remains: while recording
# from the microphone, and on platforms without the buffered playback path.
AUDIO_FPS = max(2, int(_env_float("AURA_AUDIO_FPS", 16)))

# While audio is live, repaint the spectrum only every Nth frame. Its damage
# region spans the whole window, so it costs far more per repaint than the core.
WAVEFORM_AUDIO_DIVIDER = max(1, int(_env_float("AURA_WAVEFORM_DIVIDER", 4)))
# Largest core radius in logical pixels. Uncapped, a fullscreen retina window
# rasterises gradients over ~1100x1100 device px per frame and blows the budget.
MAX_CORE_RADIUS = 250.0

# Wireframe sphere at the centre. Vertices are Fibonacci-distributed and the
# edge list is built once; raising these raises the per-frame line count roughly
# linearly (points x neighbours), so profile before going much higher.
# 96 x 3 gives ~161 edges. Measured: mesh density barely moves the frame cost
# (72 points and 96 points both paint in ~16.5 ms), so this is chosen for looks.
# The frame cost is dominated by compositing the damaged area, not by the number
# of lines — which is why TARGET_FPS, not this, is the performance lever.
SPHERE_POINTS = 96
SPHERE_NEIGHBOURS = 3

# Power-down animation length, in seconds.
SHUTDOWN_SECONDS = 1.5
WAVEFORM_BARS = 56
WAVEFORM_DECAY = 6.0           # how fast bars fall back toward the floor


# --- system monitor ---------------------------------------------------------
MONITOR_INTERVAL_S = _env_float("AURA_MONITOR_INTERVAL", 1.5)


# --- assistant --------------------------------------------------------------
@dataclass
class AIConfig:
    # auto | openai | ollama | claude | offline. "auto" prefers whichever key is
    # present, OpenAI first; Ollama is never picked automatically because a local
    # server needs no key, so there is nothing to detect intent from.
    provider: str = field(default_factory=lambda: _env("AURA_AI_PROVIDER", "auto"))
    openai_model: str = field(
        default_factory=lambda: _env("OPENAI_MODEL", "gpt-4o-mini")
    )
    anthropic_model: str = field(
        default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-opus-5")
    )
    ollama_model: str = field(
        default_factory=lambda: _env("OLLAMA_MODEL", "llama3.2:1b")
    )
    max_tokens: int = 1024
    system_prompt: str = (
        "You are AURA, a concise heads-up-display assistant embedded in a desktop "
        "control panel. Answer in at most three short sentences unless the user asks "
        "for detail. Prefer plain text: the display renders no markdown."
    )
    history_limit: int = 12          # user+assistant messages kept for context


AI = AIConfig()

# Speech
SPEECH_ENABLED = _env_bool("AURA_SPEECH_ENABLED", True)
TTS_ENABLED = _env_bool("AURA_TTS_ENABLED", True)
LISTEN_MAX_SECONDS = _env_float("AURA_LISTEN_MAX_SECONDS", 8.0)
LISTEN_SILENCE_SECONDS = _env_float("AURA_LISTEN_SILENCE_SECONDS", 1.2)
LISTEN_SAMPLE_RATE = 16000       # what most recognisers expect
MIC_DEVICE = _env("AURA_INPUT_DEVICE")

# ElevenLabs is optional; without it TTS falls back to the OS voice.
ELEVENLABS_API_KEY = _env("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = _env("ELEVENLABS_VOICE_ID")
ELEVENLABS_MODEL_ID = _env("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")


def openai_api_key() -> str:
    return _env("OPENAI_API_KEY")


def ollama_api_key() -> str:
    """Only needed for Ollama's hosted service or an authenticating proxy."""
    return _env("OLLAMA_API_KEY")


def ollama_host() -> str:
    return _env("OLLAMA_HOST", "http://localhost:11434").rstrip("/")


def ollama_base_url() -> str:
    """Ollama's OpenAI-compatible endpoint, which always lives under /v1."""
    host = ollama_host()
    return host if host.endswith("/v1") else f"{host}/v1"


def ollama_is_local() -> bool:
    host = ollama_host()
    return any(marker in host for marker in ("localhost", "127.0.0.1", "0.0.0.0", "::1"))


def anthropic_api_key() -> str:
    return _env("ANTHROPIC_API_KEY")


def resolved_provider() -> str:
    """Which backend the configuration actually selects: openai/claude/offline."""
    choice = (AI.provider or "auto").lower()
    if choice in ("openai", "gpt", "chatgpt"):
        return "openai"
    if choice in ("ollama", "local-llm", "llama"):
        return "ollama"
    if choice in ("claude", "anthropic"):
        return "claude"
    if choice in ("offline", "local", "none"):
        return "offline"
    # auto: use whichever key is present, preferring OpenAI.
    if openai_api_key():
        return "openai"
    if anthropic_api_key():
        return "claude"
    return "offline"


def _key_problem(name: str, key: str) -> str | None:
    if not key:
        return f"{name} is not set — running on the offline engine."
    if not key.startswith("sk-"):
        return f"{name} does not look like a key (expected an 'sk-' prefix)."
    return None


def openai_key_problem() -> str | None:
    return _key_problem("OPENAI_API_KEY", openai_api_key())


def anthropic_key_problem() -> str | None:
    return _key_problem("ANTHROPIC_API_KEY", anthropic_api_key())


def api_key_problem() -> str | None:
    """Why the selected engine cannot run, or None if its key looks usable."""
    provider = resolved_provider()
    if provider == "claude":
        return anthropic_key_problem()
    # openai, or nothing configured at all — name the one we would use first.
    return openai_key_problem()
