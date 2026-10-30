#!/usr/bin/env python3
"""
Desktop clap listener: reads the default microphone and runs a welcome flow when two
loud transients (a double clap) are detected within a short time window.

On a double clap it opens your editor (VS Code, or the first entry of EDITOR_CANDIDATES
found on this machine) and speaks a welcome line through ElevenLabs. Browser windows are
off by default: add entries to CHROME_TARGETS to open pages in Chrome as well.

Run:
  python -m pip install -r requirements.txt
  python aura.py

Tuning (constants below):
  SAMPLE_RATE   — usually 44100 or 48000; match your device if needed.
  BLOCK_MS      — analysis window size; smaller = snappier, noisier.
  SPIKE_RATIO   — how many times louder than the noise floor counts as a clap;
                    raise if false triggers; lower if claps are missed.
  COOLDOWN_S    — minimum seconds between double-clap triggers (debounce).
  MIN_DOUBLE_GAP_S / MAX_DOUBLE_GAP_S — allowed time between the two claps.
  RETRIGGER_RATIO — audio must fall below threshold * this before another hit counts.
  NOISE_FLOOR_ALPHA / NOISE_FLOOR_RISE_ALPHA — baseline adaptation down / up.
    The floor is seeded from a short mic measurement at startup, so SPIKE_RATIO is
    relative to your actual room noise rather than a hardcoded guess.
  MIN_RMS       — ignore spikes below this absolute level (float audio ~ [-1, 1]).
  RUN_ONCE_PER_PROCESS — if True the flow runs on the first double clap only.
  SONG_URI      — optional Spotify/YouTube URL opened before everything else.
                    Empty by default: only Chrome and VS Code are launched.
  CHROME_TARGETS — the sites opened in Chrome, one window each. Empty by default, so no
                    browser window opens; add entries to change that. `monitor` is a
                    1-based display index (sorted left-to-right, then top-to-bottom).
  EDITOR_CANDIDATES — editors to look for, in order. The first one installed is opened.
  OPEN_CHROME_FULLSCREEN — fullscreen on the chosen monitor. On Windows the new window
                    is located and snapped with SetWindowPos; on macOS/Linux the window
                    is opened sized to the monitor via Chrome's own flags.
  CHROME_SEPARATE_SITE_PROFILES — if True, uses a temp --user-data-dir per site (not your
    normal profile). Default False so the sites use your usual Chrome profile and logins;
    enable only if the windows keep opening on the same monitor and you accept a separate
    profile for automation.
  FOCUS_EXISTING_VSCODE_ON_DOUBLE_CLAP — reuse / focus a running VS Code instance.
  OPEN_NEW_VSCODE_ON_DOUBLE_CLAP — also open an extra new VS Code window.
  VSCODE_OPEN_FULLSCREEN — Windows only: send F11 after focus/launch (toggle off with F11).
  AURA_WELCOME_* — TTS after the apps open (ElevenLabs). Configure via environment or a
    `.env` file next to this script (ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, etc.).
    ELEVENLABS_API_KEY must be the key itself, which starts with `sk_` — not the key ID
    listed in the dashboard, which the API rejects.
    `pcm_*` formats are played directly; `mp3_*` needs a system player (afplay on macOS).
    PCM output requires a paid plan, so a PCM rejection retries once as mp3_44100_128.
    With AURA_WELCOME_CACHE_ENABLED, audio is saved under `.cache/aura_welcome/` (WAV)
    and replayed when phrase + voice + model + format match—no repeat API call. Delete that
    folder or set AURA_WELCOME_CACHE_ENABLED=False to force a fresh fetch.
  The assistant speaks in the background so the apps open without waiting for playback.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
import webbrowser
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

from dotenv import load_dotenv
import numpy as np
import sounddevice as sd

# --- tuning knobs -----------------------------------------------------------
SAMPLE_RATE = 44100
BLOCK_MS = 40
CHANNELS = 1

# How many times the room's noise floor a peak must reach to count as a clap.
# Lowered from 7.0: in a room measuring a 0.034 floor that demanded 0.24 RMS,
# which is a hard clap right at the mic. 4.0 asks for 0.14 there instead.
SPIKE_RATIO = 4.0
COOLDOWN_S = 0.45
MIN_DOUBLE_GAP_S = 0.05
MAX_DOUBLE_GAP_S = 0.35
RETRIGGER_RATIO = 0.55
NOISE_FLOOR_ALPHA = 0.992  # closer to 1 = slower adaptation while the room is quiet
NOISE_FLOOR_RISE_ALPHA = 0.9995  # much slower when the room gets louder
NOISE_FLOOR_MIN = 1e-6
# The startup measurement uses a low percentile, i.e. the quiet baseline between the
# bumps and voices that make up "ambient", which is what the loop converges to anyway.
NOISE_FLOOR_PERCENTILE = 10
MIN_RMS = 0.006  # never call anything quieter than this a clap (was 0.012)
# Ceiling on the computed threshold. A hot mic or a loud room can push
# noise_floor * SPIKE_RATIO past what a real clap reaches; without this the detector
# would go permanently deaf. Raise it if you get false triggers in a loud room.
MAX_RMS = 0.20
QUIET_GATE_MULT = 2.2  # below floor * this counts as "quiet" for fast adaptation
# Startup mic probe: if default input RMS stays below this, scan for a louder device.
INPUT_PROBE_S = 0.5
INPUT_SILENT_RMS = 0.001

# Run the whole flow on the first double clap only (restart the script to run again).
RUN_ONCE_PER_PROCESS = True

# Start AURA anyway if no double clap is heard within this many seconds of the
# listener coming up. A clap is then a way to start it *sooner*, not the only way.
# Set to 0 to wait for a clap indefinitely.
AUTO_START_AFTER_S = 7.0

# Optional, off by default: Spotify "spotify:track:ID" / https://open.spotify.com/track/...
# or a YouTube watch URL. Empty means only Chrome and VS Code are opened.
SONG_URI = ""


class ChromeTarget(NamedTuple):
    """One Chrome window: URL (overridable via env) on a 1-based monitor index."""

    env: tuple[str, ...]
    url: str
    monitor: int


# Pages a double clap opens in Chrome, one window each. Empty = no browser windows.
# To re-enable, add e.g.:
#   ChromeTarget(env=("CLAUDE_CODE_URL",), url="https://claude.ai/new", monitor=1),
CHROME_TARGETS: tuple[ChromeTarget, ...] = ()

OPEN_CHROME_FULLSCREEN = True
# False = default Chrome profile (your normal user, extensions, cookies). True = temp dirs per site.
CHROME_SEPARATE_SITE_PROFILES = False

# AURA HUD: the futuristic interface (main.py) opens on a double clap.
OPEN_AURA_HUD_ON_DOUBLE_CLAP = True

# Editor: off — a double clap opens the AURA HUD only.
# Set FOCUS_EXISTING_VSCODE_ON_DOUBLE_CLAP = True to also focus/launch your editor
# (the first entry of EDITOR_CANDIDATES installed on this machine).
FOCUS_EXISTING_VSCODE_ON_DOUBLE_CLAP = False
OPEN_NEW_VSCODE_ON_DOUBLE_CLAP = False
VSCODE_OPEN_FULLSCREEN = True
# Seconds to wait for a freshly launched editor window before sending F11 (Windows).
VSCODE_WINDOW_WAIT_S = 12.0


class EditorCandidate(NamedTuple):
    name: str
    macos_app: str  # .app bundle name under /Applications
    win_dir: str  # folder under Programs / Program Files on Windows
    win_exe: str  # executable (also the process name used to focus its window)
    cli: str  # command on PATH


# Checked in order; the first one installed is what a double clap opens. These are all
# VS Code builds, so the same launch, focus and fullscreen logic works for each.
EDITOR_CANDIDATES: tuple[EditorCandidate, ...] = (
    EditorCandidate("Visual Studio Code", "Visual Studio Code", "Microsoft VS Code", "Code.exe", "code"),
    EditorCandidate("Cursor", "Cursor", "cursor", "Cursor.exe", "cursor"),
    EditorCandidate("Antigravity IDE", "Antigravity IDE", "Antigravity", "Antigravity.exe", "antigravity"),
)

# Who AURA is greeting. Changing this changes the spoken line below.
AURA_USER_NAME = "Hasib"
AURA_WELCOME_ENABLED = True
AURA_WELCOME_PHRASE = (
    f"Welcome back, sir. How are you doing today, {AURA_USER_NAME}? "
    "All systems are online and standing by."
)
# Seconds after the apps launch before speaking (waited inside the speech thread).
AURA_WELCOME_DELAY_S = 1.0
# Save ElevenLabs PCM as WAV under .cache/aura_welcome/; replay skips the API when the key matches.
AURA_WELCOME_CACHE_ENABLED = True

load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("aura")


def block_samples() -> int:
    n = int(SAMPLE_RATE * BLOCK_MS / 1000)
    return max(n, 1)


def rms_mono(block: np.ndarray) -> float:
    if block.ndim > 1:
        block = np.mean(block.astype(np.float64), axis=1)
    else:
        block = block.astype(np.float64)
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(block**2)))


def target_url(target: ChromeTarget) -> str:
    """First non-empty env override for this target, else its built-in default."""
    for name in target.env:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return target.url


def url_label(url: str) -> str:
    return urlparse(url).netloc or url


# --- microphone selection ---------------------------------------------------
def _input_devices() -> list[tuple[int, dict]]:
    return [
        (i, dev)
        for i, dev in enumerate(sd.query_devices())
        if dev["max_input_channels"] >= 1
    ]


def _resolve_input_device_index(spec: str) -> int:
    spec = spec.strip()
    if spec.isdigit():
        idx = int(spec)
        try:
            sd.query_devices(idx)
        except (sd.PortAudioError, ValueError) as e:
            raise ValueError(f"No input device with index {idx}: {e}") from e
        return idx
    needle = spec.lower()
    for idx, dev in _input_devices():
        if needle in dev["name"].lower():
            return idx
    raise ValueError(f"No input device matches {spec!r}")


def _read_probe_levels(device: int, blocksize: int) -> list[float] | None:
    """RMS of each block over INPUT_PROBE_S, or None if the device will not open."""
    try:
        with sd.InputStream(
            device=device,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
        ) as stream:
            levels: list[float] = []
            deadline = time.monotonic() + INPUT_PROBE_S
            while time.monotonic() < deadline:
                data, _ = stream.read(blocksize)
                levels.append(rms_mono(data))
            return levels
    except (sd.PortAudioError, ValueError):
        return None


def _probe_input_max_rms(device: int, blocksize: int) -> float | None:
    levels = _read_probe_levels(device, blocksize)
    if levels is None:
        return None
    return max(levels, default=0.0)


def clap_threshold(noise_floor: float) -> float:
    return min(max(noise_floor * SPIKE_RATIO, MIN_RMS), MAX_RMS)


def measure_noise_floor(device: int, blocksize: int) -> float:
    """Quiet baseline of the room, as a low percentile of block RMS."""
    levels = _read_probe_levels(device, blocksize)
    if not levels:
        log.warning("Could not measure room noise; starting from %.6f.", NOISE_FLOOR_MIN)
        return NOISE_FLOOR_MIN
    levels.sort()
    idx = min(
        len(levels) - 1, int(len(levels) * NOISE_FLOOR_PERCENTILE / 100)
    )
    return max(levels[idx], NOISE_FLOOR_MIN)


def _choose_input_device(blocksize: int) -> int:
    log.info("Audio devices:\n%s", sd.query_devices())

    override = (os.environ.get("AURA_INPUT_DEVICE") or "").strip()
    if override:
        try:
            idx = _resolve_input_device_index(override)
        except ValueError as e:
            log.error("%s", e)
            log.error("Set AURA_INPUT_DEVICE to a device index or name substring.")
            raise SystemExit(1) from e
        name = sd.query_devices(idx)["name"]
        peak = _probe_input_max_rms(idx, blocksize)
        log.info("Using AURA_INPUT_DEVICE [%d]: %s", idx, name)
        if peak is None:
            log.warning("Could not open configured mic; trying anyway.")
        elif peak < INPUT_SILENT_RMS:
            log.warning(
                "Configured mic looks silent (probe rms=%.5f). "
                "Check your input level or try another AURA_INPUT_DEVICE.",
                peak,
            )
        else:
            log.info("Mic probe OK (rms=%.5f).", peak)
        return idx

    default = sd.default.device[0]
    if default is not None and default >= 0:
        default_name = sd.query_devices(default)["name"]
        peak = _probe_input_max_rms(default, blocksize)
        if peak is not None and peak >= INPUT_SILENT_RMS:
            log.info(
                "Using default microphone [%d]: %s (probe rms=%.5f)",
                default,
                default_name,
                peak,
            )
            return default
        log.warning(
            "Default mic [%d] %s is silent or unavailable (probe rms=%s); "
            "scanning other inputs...",
            default,
            default_name,
            f"{peak:.5f}" if peak is not None else "unopenable",
        )

    best_idx: int | None = None
    best_peak = -1.0
    for idx, _dev in _input_devices():
        if default is not None and idx == default:
            continue
        peak = _probe_input_max_rms(idx, blocksize)
        if peak is not None and peak > best_peak:
            best_peak = peak
            best_idx = idx

    if best_idx is not None and best_peak >= INPUT_SILENT_RMS:
        log.info(
            "Auto-selected microphone [%d]: %s (probe rms=%.5f)",
            best_idx,
            sd.query_devices(best_idx)["name"],
            best_peak,
        )
        return best_idx

    if default is not None and default >= 0:
        log.warning("No active mic found; falling back to default [%d].", default)
        return default
    inputs = _input_devices()
    if not inputs:
        log.error("No input devices found.")
        raise SystemExit(1)
    idx, dev = inputs[0]
    log.warning("No active mic found; falling back to [%d] %s.", idx, dev["name"])
    return idx


# --- ElevenLabs welcome -----------------------------------------------------
def _elevenlabs_pcm_sample_rate(output_format: str) -> int:
    override = (os.environ.get("ELEVENLABS_PCM_SAMPLE_RATE") or "").strip()
    if override.isdigit():
        return int(override)
    if output_format.startswith("pcm_"):
        try:
            return int(output_format.split("_", maxsplit=1)[1])
        except (ValueError, IndexError):
            pass
    return 24000


def elevenlabs_env_config() -> tuple[str, str, str, int]:
    """voice_id, model_id, output_format, pcm_sample_rate."""
    voice = (os.environ.get("ELEVENLABS_VOICE_ID") or "").strip()
    model = (os.environ.get("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2").strip()
    fmt = (os.environ.get("ELEVENLABS_OUTPUT_FORMAT") or "pcm_24000").strip()
    if not (fmt.startswith("pcm_") or fmt.startswith("mp3_")):
        log.warning(
            "ELEVENLABS_OUTPUT_FORMAT=%r is not a pcm_* or mp3_* format; "
            "using pcm_24000 instead.",
            fmt,
        )
        fmt = "pcm_24000"
    rate = _elevenlabs_pcm_sample_rate(fmt)
    return voice, model, fmt, rate


def elevenlabs_key_problem() -> str | None:
    """Human-readable reason the configured API key cannot work, or None if it looks valid."""
    key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not key:
        return (
            "ELEVENLABS_API_KEY is not set. Put it in the .env file next to aura.py."
        )
    if not key.startswith("sk_"):
        return (
            "ELEVENLABS_API_KEY does not start with 'sk_', so it is the key *ID* shown "
            "in the dashboard list, not the key itself. The API rejects it with "
            "'API key ID used as API key'. Go to "
            "https://elevenlabs.io/app/settings/api-keys, create or rotate a key, and "
            "copy the sk_... value shown once at creation into .env."
        )
    return None


def _elevenlabs_error_detail(e: Exception) -> str:
    """Pull the useful message out of an ElevenLabs ApiError instead of dumping headers."""
    body = getattr(e, "body", None)
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        msg = detail.get("message") or detail.get("status") or ""
        code = detail.get("code") or detail.get("status") or ""
        if msg:
            return f"{msg} (code={code})" if code else str(msg)
    if isinstance(detail, str):
        return detail
    status = getattr(e, "status_code", None)
    prefix = f"HTTP {status}: " if status else ""
    return f"{prefix}{type(e).__name__}: {str(e)[:200]}"


def _aura_welcome_cache_dir() -> Path:
    base = Path(__file__).resolve().parent
    override = (os.environ.get("AURA_WELCOME_CACHE_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return base / ".cache" / "aura_welcome"


def _aura_welcome_cache_path(
    text: str, voice_id: str, model_id: str, output_format: str
) -> Path:
    key = f"{text}|{voice_id}|{model_id}|{output_format}".encode()
    digest = hashlib.sha256(key).hexdigest()[:24]
    ext = "mp3" if output_format.startswith("mp3_") else "wav"
    return _aura_welcome_cache_dir() / f"{digest}.{ext}"


def _pcm16_to_float(raw: bytes) -> np.ndarray | None:
    """Raw little-endian 16-bit mono PCM to float32 in [-1, 1)."""
    if len(raw) < 2:
        return None
    if len(raw) % 2:
        log.warning("Dropping a trailing byte from odd-length PCM audio.")
        raw = raw[: len(raw) - 1]
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def _play_pcm(pcm_f: np.ndarray, sample_rate: int) -> bool:
    try:
        sd.play(pcm_f, sample_rate)
        sd.wait()
    except Exception as e:
        log.warning("Could not play welcome audio: %s", e)
        return False
    return True


def _play_pcm_wav_file(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as wf:
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            rate = wf.getframerate()
            if ch != 1 or sw != 2:
                log.warning("Unsupported cached WAV (channels=%s, width=%s).", ch, sw)
                return False
            raw = wf.readframes(wf.getnframes())
    except (OSError, wave.Error) as e:
        log.warning("Could not read cached welcome audio: %s", e)
        return False
    pcm_f = _pcm16_to_float(raw)
    if pcm_f is None or pcm_f.size == 0:
        return False
    return _play_pcm(pcm_f, rate)


def _save_pcm_wav_file(path: Path, pcm_bytes: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with wave.open(str(tmp), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        tmp.replace(path)
    except OSError:
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
        raise


def _save_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError:
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
        raise


def _mp3_player_command(path: Path) -> list[str] | None:
    """Command that plays an MP3 and exits, or None if no player is available."""
    if sys.platform == "darwin":
        afplay = shutil.which("afplay")
        return [afplay, str(path)] if afplay else None
    if sys.platform == "win32":
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if not ps:
            return None
        script = (
            "$p = New-Object -ComObject WMPlayer.OCX; "
            f"$p.URL = '{path}'; $p.controls.play(); "
            "while ($p.playState -ne 1) {{ Start-Sleep -Milliseconds 200 }}"
        )
        return [ps, "-NoProfile", "-NonInteractive", "-Command", script]
    for exe, args in (
        ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet"]),
        ("mpv", ["--no-video", "--really-quiet"]),
        ("mpg123", ["-q"]),
    ):
        found = shutil.which(exe)
        if found:
            return [found, *args, str(path)]
    return None


def _play_mp3_file(path: Path) -> bool:
    cmd = _mp3_player_command(path)
    if cmd is None:
        log.warning(
            "No MP3 player found for the welcome audio. Install one, or set "
            "ELEVENLABS_OUTPUT_FORMAT=pcm_24000 (played directly, no player needed)."
        )
        return False
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as e:
        log.warning("Could not play welcome audio: %s", e)
        return False
    if proc.returncode != 0:
        log.warning("Audio player exited with code %d.", proc.returncode)
        return False
    return True


def _play_cached_welcome(path: Path) -> bool:
    if path.suffix == ".mp3":
        return _play_mp3_file(path)
    return _play_pcm_wav_file(path)


def _elevenlabs_fetch(
    client, voice_id: str, text: str, model_id: str, output_format: str
) -> bytes | None:
    try:
        return b"".join(
            client.text_to_speech.convert(
                voice_id=voice_id,
                text=text,
                model_id=model_id,
                output_format=output_format,
            )
        )
    except Exception as e:
        log.warning(
            "ElevenLabs TTS failed (format=%s): %s",
            output_format,
            _elevenlabs_error_detail(e),
        )
        return None


def say_aura_welcome() -> None:
    if not AURA_WELCOME_ENABLED or not AURA_WELCOME_PHRASE.strip():
        return
    text = AURA_WELCOME_PHRASE.strip()
    vid, model_id, output_format, pcm_rate = elevenlabs_env_config()
    if not vid:
        log.warning("Set ELEVENLABS_VOICE_ID in the environment for ElevenLabs TTS.")
        return

    cache_path = _aura_welcome_cache_path(text, vid, model_id, output_format)
    if AURA_WELCOME_CACHE_ENABLED and cache_path.is_file():
        log.info("Playing welcome from cache: %s", cache_path)
        if _play_cached_welcome(cache_path):
            return
        log.warning("Cache unusable; fetching from ElevenLabs.")

    problem = elevenlabs_key_problem()
    if problem:
        log.error("Welcome speech skipped: %s", problem)
        return
    api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    try:
        from elevenlabs.client import ElevenLabs
    except ImportError:
        log.warning("Install dependencies: pip install -r requirements.txt")
        return

    client = ElevenLabs(api_key=api_key)
    raw = _elevenlabs_fetch(client, vid, text, model_id, output_format)
    if raw is None and output_format.startswith("pcm_"):
        # PCM output is a paid-tier feature; MP3 works on every plan.
        log.info("Retrying with mp3_44100_128 (PCM output may need a paid plan).")
        output_format = "mp3_44100_128"
        cache_path = _aura_welcome_cache_path(text, vid, model_id, output_format)
        raw = _elevenlabs_fetch(client, vid, text, model_id, output_format)
    if not raw:
        return

    if output_format.startswith("mp3_"):
        try:
            _save_bytes(cache_path, raw)
        except OSError as e:
            log.warning("Could not save welcome cache: %s", e)
            return
        log.info("Saved welcome audio to cache: %s", cache_path)
        _play_mp3_file(cache_path)
        return

    pcm_f = _pcm16_to_float(raw)
    if pcm_f is None or pcm_f.size == 0:
        log.warning("ElevenLabs returned unusable audio (%d bytes).", len(raw))
        return
    if AURA_WELCOME_CACHE_ENABLED:
        try:
            _save_pcm_wav_file(cache_path, raw, pcm_rate)
            log.info("Saved welcome audio to cache: %s", cache_path)
        except OSError as e:
            log.warning("Could not save welcome cache: %s", e)
    _play_pcm(pcm_f, pcm_rate)


def _speak_welcome_after_delay() -> None:
    delay = max(0.0, AURA_WELCOME_DELAY_S)
    if delay:
        time.sleep(delay)
    try:
        say_aura_welcome()
    except Exception as e:  # never let a background thread die on a bare traceback
        log.warning("Welcome speech failed: %s", e)


def play_song(uri: str) -> None:
    u = uri.strip()
    if not u:
        return
    try:
        if sys.platform == "win32":
            os.startfile(u)
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["open", u],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            webbrowser.open(u)
    except OSError as e:
        log.warning("Could not open SONG_URI: %s", e)


# --- monitors ---------------------------------------------------------------
def _win32_monitor_rects() -> list[tuple[int, int, int, int]]:
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    collected: list[tuple[int, int, int, int]] = []

    @ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(RECT),
        wintypes.LPARAM,
    )
    def _cb(_hm, _hdc, lprc, _lp):
        r = lprc.contents
        collected.append((int(r.left), int(r.top), int(r.right), int(r.bottom)))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, _cb, 0)
    return collected


def _macos_monitor_rects() -> list[tuple[int, int, int, int]]:
    import ctypes
    import ctypes.util

    class CGPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    class CGSize(ctypes.Structure):
        _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]

    class CGRect(ctypes.Structure):
        _fields_ = [("origin", CGPoint), ("size", CGSize)]

    lib = (
        ctypes.util.find_library("CoreGraphics")
        or "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
    )
    try:
        cg = ctypes.cdll.LoadLibrary(lib)
    except OSError as e:
        log.warning("Could not load CoreGraphics for monitor layout: %s", e)
        return []

    max_displays = 16
    cg.CGGetActiveDisplayList.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    cg.CGGetActiveDisplayList.restype = ctypes.c_int32
    cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
    cg.CGDisplayBounds.restype = CGRect

    ids = (ctypes.c_uint32 * max_displays)()
    count = ctypes.c_uint32()
    if cg.CGGetActiveDisplayList(max_displays, ids, ctypes.byref(count)) != 0:
        return []
    rects: list[tuple[int, int, int, int]] = []
    for i in range(count.value):
        r = cg.CGDisplayBounds(ids[i])
        left, top = int(r.origin.x), int(r.origin.y)
        rects.append((left, top, left + int(r.size.width), top + int(r.size.height)))
    return rects


def _sorted_monitor_rects() -> list[tuple[int, int, int, int]]:
    """Each monitor as (left, top, right, bottom), sorted left-to-right then top-to-bottom."""
    if sys.platform == "win32":
        rects = _win32_monitor_rects()
    elif sys.platform == "darwin":
        rects = _macos_monitor_rects()
    else:
        rects = []
    rects.sort(key=lambda t: (t[0], t[1]))
    return rects


def _monitor_bounds(one_based_index: int) -> tuple[int, int, int, int]:
    """Monitor N as (left, top, right, bottom), 1-based index."""
    rects = _sorted_monitor_rects()
    if not rects:
        return (0, 0, 1920, 1080)
    idx = max(0, one_based_index - 1)
    if idx >= len(rects):
        log.warning(
            "Monitor %d requested but only %d found; using last monitor.",
            one_based_index,
            len(rects),
        )
        idx = len(rects) - 1
    return rects[idx]


# --- Chrome -----------------------------------------------------------------
def _chrome_executable() -> str | None:
    if sys.platform == "win32":
        for base in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ):
            if not base:
                continue
            p = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
            if os.path.isfile(p):
                return p
    elif sys.platform == "darwin":
        for p in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            str(
                Path.home()
                / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            ),
        ):
            if os.path.isfile(p):
                return p
    return (
        shutil.which("google-chrome")
        or shutil.which("chrome")
        or shutil.which("chromium")
    )


def _chrome_window_size() -> tuple[int, int]:
    w = (os.environ.get("CHROME_WINDOW_WIDTH") or "1400").strip()
    h = (os.environ.get("CHROME_WINDOW_HEIGHT") or "900").strip()
    try:
        return (max(400, int(w)), max(300, int(h)))
    except ValueError:
        return (1400, 900)


def _chrome_site_user_data_dir(site_key: str) -> str:
    p = Path(tempfile.gettempdir()) / "clap-trigger-chrome" / site_key
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _chrome_new_window_wait_timeout_s() -> float:
    try:
        return max(
            3.0, float((os.environ.get("CHROME_NEW_WINDOW_WAIT_S") or "25").strip())
        )
    except ValueError:
        return 25.0


def _top_level_hwnds_for_exe_win32(exe_name: str, min_size: int) -> set[int]:
    """HWND ints for visible-or-minimized top-level windows owned by exe_name."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    GW_OWNER = 4
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    found: set[int] = set()
    needle = exe_name.lower()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd: wintypes.HWND, _lp: wintypes.LPARAM) -> bool:
        if user32.GetWindow(hwnd, GW_OWNER):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        if not user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == 0:
            return True
        hproc = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
        )
        if not hproc:
            return True
        try:
            buf = ctypes.create_unicode_buffer(4096)
            sz = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(sz)):
                return True
            exe_path = buf.value
        finally:
            kernel32.CloseHandle(hproc)
        if os.path.basename(exe_path).lower() != needle:
            return True
        r = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        if (r.right - r.left) < min_size or (r.bottom - r.top) < min_size:
            return True
        found.add(int(hwnd))
        return True

    user32.EnumWindows(_enum, 0)
    return found


def _largest_hwnd_win32(hwnds: set[int]) -> int | None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    best: int | None = None
    best_area = -1
    for h in hwnds:
        r = wintypes.RECT()
        if user32.GetWindowRect(h, ctypes.byref(r)):
            area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
            if area > best_area:
                best_area = area
                best = h
    return best


def _wait_new_hwnd_win32(
    exe_name: str, before: set[int], timeout: float, min_size: int
) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.12)
        new = _top_level_hwnds_for_exe_win32(exe_name, min_size) - before
        if new:
            hwnd = _largest_hwnd_win32(new)
            if hwnd is not None:
                return hwnd
    return None


def _foreground_hwnd_win32(hwnd: int) -> None:
    import ctypes

    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    fg = user32.GetForegroundWindow()
    tid_tgt = user32.GetWindowThreadProcessId(hwnd, None)
    tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    if tid_fg and tid_tgt:
        user32.AttachThreadInput(tid_fg, tid_tgt, True)
    user32.SetForegroundWindow(hwnd)
    if tid_fg and tid_tgt:
        user32.AttachThreadInput(tid_fg, tid_tgt, False)


def _send_f11_win32(hwnd: int) -> None:
    """F11 toggles fullscreen in Chrome and VS Code (Electron)."""
    import ctypes

    user32 = ctypes.windll.user32
    KEYEVENTF_KEYUP = 0x0002
    VK_F11 = 0x7A
    _foreground_hwnd_win32(hwnd)
    user32.keybd_event(VK_F11, 0, 0, 0)
    user32.keybd_event(VK_F11, 0, KEYEVENTF_KEYUP, 0)


def _snap_window_to_monitor_win32(
    hwnd: int,
    one_based_monitor: int,
    *,
    fullscreen: bool,
    windowed_size: tuple[int, int] | None,
) -> None:
    import ctypes

    ml, mt, mr, mb = _monitor_bounds(one_based_monitor)
    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    SW_SHOWMAXIMIZED = 3
    HWND_TOP = 0
    SWP_SHOWWINDOW = 0x0040
    SWP_FRAMECHANGED = 0x0020
    flags = SWP_SHOWWINDOW | SWP_FRAMECHANGED

    user32.ShowWindow(hwnd, SW_RESTORE)
    if fullscreen:
        w, h = mr - ml, mb - mt
        x, y = ml, mt
    else:
        w, h = windowed_size or _chrome_window_size()
        x = ml + max(0, (mr - ml - w) // 2)
        y = mt + max(0, (mb - mt - h) // 2)
    user32.SetWindowPos(hwnd, HWND_TOP, x, y, w, h, flags)

    if fullscreen:
        user32.ShowWindow(hwnd, SW_SHOWMAXIMIZED)
        _send_f11_win32(hwnd)


def open_url_in_chrome(
    url: str,
    *,
    monitor: int,
    fullscreen: bool,
    user_data_dir: str | None = None,
) -> None:
    u = url.strip()
    if not u:
        return
    label = url_label(u)
    chrome = _chrome_executable()
    if not chrome:
        log.warning("Chrome not found; opening %s in the default browser.", label)
        webbrowser.open(u)
        return

    left, top, right, bottom = _monitor_bounds(monitor)
    position = (left, top)
    size = (
        (max(320, right - left), max(240, bottom - top))
        if fullscreen
        else _chrome_window_size()
    )
    # On Windows the window is placed after launch with SetWindowPos, which is more
    # reliable than --start-fullscreen when a Chrome instance is already running.
    snap_after_launch = sys.platform == "win32"

    args = [chrome]
    if user_data_dir:
        args += [f"--user-data-dir={user_data_dir}", "--no-first-run"]
    args.append("--new-window")
    args.append(f"--window-position={position[0]},{position[1]}")
    args.append(f"--window-size={size[0]},{size[1]}")
    if fullscreen and not snap_after_launch:
        args.append("--start-fullscreen")
    args.append(u)

    popen_kw: dict = {
        "args": args,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        before = (
            _top_level_hwnds_for_exe_win32("chrome.exe", 80)
            if snap_after_launch
            else set()
        )
        subprocess.Popen(**popen_kw)
        log.info("Chrome: opened %s on monitor %d.", label, monitor)
    except OSError as e:
        log.warning("Could not open %s in Chrome: %s", label, e)
        return

    if not snap_after_launch:
        return
    hwnd = _wait_new_hwnd_win32(
        "chrome.exe", before, _chrome_new_window_wait_timeout_s(), 80
    )
    if hwnd is None:
        log.warning(
            "Chrome: timed out waiting for the new window (%s); check "
            "CHROME_NEW_WINDOW_WAIT_S or close extra Chrome instances.",
            label,
        )
        return
    _snap_window_to_monitor_win32(
        hwnd,
        monitor,
        fullscreen=fullscreen,
        windowed_size=None if fullscreen else _chrome_window_size(),
    )


def open_chrome_targets() -> None:
    for i, target in enumerate(CHROME_TARGETS):
        url = target_url(target)
        user_data = (
            _chrome_site_user_data_dir(f"site{i + 1}")
            if CHROME_SEPARATE_SITE_PROFILES
            else None
        )
        open_url_in_chrome(
            url,
            monitor=target.monitor,
            fullscreen=OPEN_CHROME_FULLSCREEN,
            user_data_dir=user_data,
        )


# --- AURA HUD -------------------------------------------------------------
_hud_process: subprocess.Popen | None = None


def open_aura_hud() -> None:
    """Launch the HUD (main.py) in its own process.

    Qt must own the main thread, so the interface cannot run inside this
    listener — it is spawned as a separate process instead. A second double
    clap re-focuses rather than starting a duplicate.
    """
    global _hud_process
    if not OPEN_AURA_HUD_ON_DOUBLE_CLAP:
        return

    script = Path(__file__).resolve().parent / "main.py"
    if not script.is_file():
        log.warning("AURA HUD not found (expected %s).", script)
        return

    if _hud_process is not None and _hud_process.poll() is None:
        log.info("AURA HUD is already running (pid %d).", _hud_process.pid)
        return

    popen_kw: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(script.parent),
    }
    if sys.platform == "win32":
        popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        _hud_process = subprocess.Popen([sys.executable, str(script)], **popen_kw)
    except OSError as e:
        log.warning("Could not launch the AURA HUD: %s", e)
        return
    log.info("AURA HUD launched (pid %d).", _hud_process.pid)


# --- editor (VS Code) -------------------------------------------------------
class FoundEditor(NamedTuple):
    name: str
    macos_app: str | None  # .app bundle, launched with `open`
    exe: str | None  # executable path, used on Windows/Linux
    win_exe_name: str  # process name for window focus/fullscreen on Windows


def _macos_app_path(app_name: str) -> str | None:
    for p in (
        f"/Applications/{app_name}.app",
        str(Path.home() / f"Applications/{app_name}.app"),
    ):
        if os.path.isdir(p):
            return p
    return None


def _win_editor_path(win_dir: str, exe_name: str) -> str | None:
    local = os.environ.get("LOCALAPPDATA", "")
    bases = []
    if local:
        bases.append(os.path.join(local, "Programs", win_dir))
    for base in (
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ):
        if base:
            bases.append(os.path.join(base, win_dir))
    for b in bases:
        p = os.path.join(b, exe_name)
        if os.path.isfile(p):
            return p
    return None


def find_editor() -> FoundEditor | None:
    """First entry of EDITOR_CANDIDATES present on this machine."""
    for cand in EDITOR_CANDIDATES:
        if sys.platform == "darwin":
            app = _macos_app_path(cand.macos_app)
            if app:
                return FoundEditor(cand.name, app, None, cand.win_exe)
        elif sys.platform == "win32":
            exe = _win_editor_path(cand.win_dir, cand.win_exe)
            if exe:
                return FoundEditor(cand.name, None, exe, cand.win_exe)
        exe = shutil.which(cand.cli)
        if exe:
            return FoundEditor(cand.name, None, exe, cand.win_exe)
    return None


def _launch_macos_app(app: str, new_window: bool) -> bool:
    # `open -a` focuses a running instance; `open -n -a` forces a second one.
    args = ["open"] + (["-n"] if new_window else []) + ["-a", app]
    try:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        log.warning("Could not launch %s: %s", app, e)
        return False
    return True


def _focus_existing_editor_win32(exe_name: str) -> bool:
    hwnd = _largest_hwnd_win32(_top_level_hwnds_for_exe_win32(exe_name, 200))
    if hwnd is None:
        return False
    _foreground_hwnd_win32(hwnd)
    return True


def open_editor_window() -> None:
    if not FOCUS_EXISTING_VSCODE_ON_DOUBLE_CLAP and not OPEN_NEW_VSCODE_ON_DOUBLE_CLAP:
        return

    editor = find_editor()
    if editor is None:
        log.warning(
            "No editor found. Looked for: %s. Install one, or add its entry to "
            "EDITOR_CANDIDATES in aura.py.",
            ", ".join(c.name for c in EDITOR_CANDIDATES),
        )
        return

    if editor.macos_app:
        if FOCUS_EXISTING_VSCODE_ON_DOUBLE_CLAP:
            _launch_macos_app(editor.macos_app, new_window=False)
        if OPEN_NEW_VSCODE_ON_DOUBLE_CLAP:
            _launch_macos_app(editor.macos_app, new_window=True)
        log.info("Opened %s.", editor.name)
        if VSCODE_OPEN_FULLSCREEN:
            log.info("Editor fullscreen is Windows-only; use Ctrl+Cmd+F on macOS.")
        return

    assert editor.exe is not None
    popen_kw: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW

    before: set[int] = set()
    launched = False
    try:
        if FOCUS_EXISTING_VSCODE_ON_DOUBLE_CLAP:
            focused = sys.platform == "win32" and _focus_existing_editor_win32(
                editor.win_exe_name
            )
            if not focused:
                if sys.platform == "win32":
                    before = _top_level_hwnds_for_exe_win32(editor.win_exe_name, 200)
                subprocess.Popen([editor.exe], **popen_kw)
                launched = True
        if OPEN_NEW_VSCODE_ON_DOUBLE_CLAP:
            if sys.platform == "win32" and not before:
                before = _top_level_hwnds_for_exe_win32(editor.win_exe_name, 200)
            subprocess.Popen([editor.exe, "-n"], **popen_kw)
            launched = True
        log.info("Opened %s.", editor.name)
    except OSError as e:
        log.warning("Could not start or focus %s: %s", editor.name, e)
        return

    if sys.platform != "win32" or not VSCODE_OPEN_FULLSCREEN:
        return
    if launched:
        # A cold start takes seconds, so poll instead of guessing a sleep.
        hwnd = _wait_new_hwnd_win32(
            editor.win_exe_name, before, VSCODE_WINDOW_WAIT_S, 200
        )
    else:
        hwnd = _largest_hwnd_win32(
            _top_level_hwnds_for_exe_win32(editor.win_exe_name, 200)
        )
    if hwnd is not None:
        _send_f11_win32(hwnd)
    else:
        log.warning("%s fullscreen: no window found to send F11.", editor.name)


# --- trigger ----------------------------------------------------------------
def run_double_clap_actions() -> None:
    """Run outside the mic loop so sleeps do not stall capture."""
    try:
        play_song(SONG_URI)
        open_chrome_targets()
        open_aura_hud()
        open_editor_window()
        if AURA_WELCOME_ENABLED and AURA_WELCOME_PHRASE.strip():
            threading.Thread(target=_speak_welcome_after_delay, daemon=True).start()
    except Exception as e:
        log.warning("Double-clap actions failed: %s", e)


def log_startup_summary() -> None:
    log.info(
        "Listening (double clap: %.2f–%.2fs apart, rate=%d, block=%d ms, "
        "spike_ratio=%.1f, cooldown=%.2fs). Ctrl+C to stop.",
        MIN_DOUBLE_GAP_S,
        MAX_DOUBLE_GAP_S,
        SAMPLE_RATE,
        BLOCK_MS,
        SPIKE_RATIO,
        COOLDOWN_S,
    )
    if SONG_URI.strip():
        log.info("Double clap opens this track: %s", SONG_URI.strip())

    monitors = _sorted_monitor_rects()
    if monitors:
        log.info(
            "%d monitor(s) detected: %s",
            len(monitors),
            ", ".join(
                f"[{i + 1}] {r - l}x{b - t}@({l},{t})"
                for i, (l, t, r, b) in enumerate(monitors)
            ),
        )
    if not CHROME_TARGETS:
        log.info("CHROME_TARGETS is empty — no browser windows will open.")
    for target in CHROME_TARGETS:
        url = target_url(target)
        log.info(
            "Chrome%s on monitor %d: %s",
            " fullscreen" if OPEN_CHROME_FULLSCREEN else "",
            target.monitor,
            url,
        )
    if not _chrome_executable():
        log.warning("Google Chrome not found; URLs will open in the default browser.")

    if OPEN_AURA_HUD_ON_DOUBLE_CLAP:
        hud = Path(__file__).resolve().parent / "main.py"
        if hud.is_file():
            log.info("Double clap will open the AURA HUD (%s).", hud.name)
        else:
            log.warning("AURA HUD enabled but %s is missing.", hud)
    if FOCUS_EXISTING_VSCODE_ON_DOUBLE_CLAP or OPEN_NEW_VSCODE_ON_DOUBLE_CLAP:
        editor = find_editor()
        if editor is None:
            log.warning(
                "No editor found on this machine (looked for: %s); "
                "the editor step will be skipped.",
                ", ".join(c.name for c in EDITOR_CANDIDATES),
            )
        else:
            log.info(
                "Double clap will open %s (%s).",
                editor.name,
                editor.macos_app or editor.exe,
            )
        if OPEN_NEW_VSCODE_ON_DOUBLE_CLAP:
            log.info("A new editor window will be opened as well.")
    if VSCODE_OPEN_FULLSCREEN and sys.platform == "win32":
        log.info("The editor will be sent F11 for fullscreen after focus/launch.")

    if AURA_WELCOME_ENABLED:
        voice, model, fmt, rate = elevenlabs_env_config()
        log.info(
            "After the apps + %.2fs: %r (ElevenLabs voice=%s, model=%s, format=%s, pcm_rate=%d)",
            AURA_WELCOME_DELAY_S,
            AURA_WELCOME_PHRASE.strip(),
            voice or "(unset)",
            model,
            fmt,
            rate,
        )
        problem = elevenlabs_key_problem()
        if problem:
            log.error("Welcome speech will NOT work: %s", problem)


def listen_loop(input_idx: int, blocksize: int, noise_floor: float) -> None:
    last_trigger = 0.0
    first_clap_time: float | None = None
    spike_armed = True
    have_run = False
    started = time.monotonic()

    with sd.InputStream(
        device=input_idx,
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="float32",
        blocksize=blocksize,
    ) as stream:
        while True:
            data, overflowed = stream.read(blocksize)
            if overflowed:
                log.warning("Input overflow; try a larger BLOCK_MS")

            level = rms_mono(data)
            threshold = clap_threshold(noise_floor)
            quiet_gate = noise_floor * QUIET_GATE_MULT

            # Adapt the baseline in both directions: quickly while the room is quiet,
            # slowly when it gets louder, and never on an actual spike.
            if level < quiet_gate:
                noise_floor = (
                    NOISE_FLOOR_ALPHA * noise_floor + (1.0 - NOISE_FLOOR_ALPHA) * level
                )
            elif level < threshold:
                noise_floor = (
                    NOISE_FLOOR_RISE_ALPHA * noise_floor
                    + (1.0 - NOISE_FLOOR_RISE_ALPHA) * level
                )
            noise_floor = max(noise_floor, NOISE_FLOOR_MIN)

            now = time.monotonic()

            # Safety net: don't leave the user waiting on a clap the mic may
            # never hear. If nothing has triggered within the window, start anyway.
            if (
                not have_run
                and AUTO_START_AFTER_S > 0
                and (now - started) >= AUTO_START_AFTER_S
            ):
                have_run = True
                last_trigger = now
                log.info(
                    "No double clap within %.0fs — starting AURA anyway "
                    "(noise_floor=%.5f, threshold=%.5f).",
                    AUTO_START_AFTER_S,
                    noise_floor,
                    threshold,
                )
                threading.Thread(target=run_double_clap_actions, daemon=True).start()
                if RUN_ONCE_PER_PROCESS:
                    return
                continue

            if level < threshold * RETRIGGER_RATIO:
                spike_armed = True

            if not (
                spike_armed
                and level >= threshold
                and (now - last_trigger) >= COOLDOWN_S
            ):
                continue
            spike_armed = False

            if first_clap_time is None:
                first_clap_time = now
                continue

            gap = now - first_clap_time
            if gap < MIN_DOUBLE_GAP_S:
                # Too close to be a second clap: the tail/echo of the first one.
                continue
            if gap > MAX_DOUBLE_GAP_S:
                # Too late to pair; treat this hit as the start of a new attempt.
                first_clap_time = now
                continue

            first_clap_time = None
            last_trigger = now
            if have_run and RUN_ONCE_PER_PROCESS:
                log.info(
                    "Double clap detected (gap=%.3fs) — already ran this session; "
                    "restart the script to run again.",
                    gap,
                )
                continue
            have_run = True
            log.info(
                "Double clap detected (gap=%.3fs, rms=%.5f, noise_floor=%.5f, "
                "threshold=%.5f) — opening Chrome and VS Code",
                gap,
                level,
                noise_floor,
                threshold,
            )
            threading.Thread(target=run_double_clap_actions, daemon=True).start()


def main() -> int:
    blocksize = block_samples()
    log_startup_summary()
    try:
        input_idx = _choose_input_device(blocksize)
        noise_floor = measure_noise_floor(input_idx, blocksize)
        threshold = clap_threshold(noise_floor)
        log.info(
            "Room noise floor %.5f -> clap threshold %.5f", noise_floor, threshold
        )
        if noise_floor * SPIKE_RATIO > MAX_RMS:
            log.warning(
                "This mic is hot (floor %.3f): the threshold was capped at MAX_RMS=%.2f. "
                "Lower your input gain if you get false triggers.",
                noise_floor,
                MAX_RMS,
            )
        listen_loop(input_idx, blocksize, noise_floor)
    except KeyboardInterrupt:
        log.info("Stopped.")
        return 0
    except sd.PortAudioError as e:
        log.error("Audio error: %s", e)
        log.error(
            "If PortAudio fails, install/repair drivers or try another SAMPLE_RATE."
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
