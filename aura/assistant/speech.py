"""Speech input and output.

Recording uses `sounddevice` directly (no PyAudio dependency) and hands raw PCM
to `SpeechRecognition` for transcription, so the only native requirement is
PortAudio. Both halves degrade gracefully: if a microphone or a recogniser is
missing, the caller gets a descriptive error and the UI stays usable via text.

`listen()` and `speak()` block and must be called from a worker thread.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import config

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

try:
    import sounddevice as sd
except Exception:  # pragma: no cover - missing PortAudio raises OSError, not ImportError
    sd = None  # type: ignore[assignment]

try:
    import speech_recognition as sr
except ImportError:  # pragma: no cover
    sr = None  # type: ignore[assignment]

LevelCallback = Callable[[float], None]


def speech_envelope(t: float) -> float:
    """A speech-shaped amplitude for `t` seconds into an utterance.

    Used only when the platform voice gives us no samples to measure. Layers a
    syllable rate (~4.5 Hz) over a slower phrase rhythm, with occasional dips
    that read as breaths, so the core moves in time with speech instead of
    pulsing mechanically.
    """
    syllable = 0.50 + 0.50 * math.sin(math.tau * 4.5 * t)
    phrase = 0.72 + 0.28 * math.sin(math.tau * 1.3 * t + 1.0)
    detail = 0.86 + 0.14 * math.sin(math.tau * 7.7 * t + 2.0)
    breath = 1.0 if math.sin(math.tau * 0.55 * t) > -0.65 else 0.22
    return max(0.05, min(1.0, syllable * phrase * detail * breath + 0.12))


@dataclass
class ListenResult:
    text: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.text) and self.error is None


class SpeechEngine:
    """Microphone capture, transcription and text-to-speech."""

    def __init__(self) -> None:
        self._abort = threading.Event()
        self._tts_process: subprocess.Popen | None = None
        self._tts_lock = threading.Lock()

    # -- capability probes --------------------------------------------------
    @property
    def input_available(self) -> bool:
        if sd is None or np is None:
            return False
        try:
            return any(d["max_input_channels"] >= 1 for d in sd.query_devices())
        except Exception:
            return False

    @property
    def recognizer_available(self) -> bool:
        return sr is not None

    @property
    def playback_is_buffered(self) -> bool:
        """True when replies play through an audio buffer rather than live.

        Buffered playback absorbs the stalls that a repainting fullscreen window
        causes (measured: zero underruns at 24 fps), so the UI does not need to
        throttle itself while speaking. Live streaming through the OS voice does.
        """
        if sd is None or np is None:
            return False
        if config.ELEVENLABS_API_KEY and config.ELEVENLABS_VOICE_ID:
            return True
        return sys.platform == "darwin" and shutil.which("say") is not None

    def unavailable_reason(self) -> str | None:
        if np is None:
            return "numpy is not installed (pip install numpy)."
        if sd is None:
            return "sounddevice is unavailable — check PortAudio installation."
        if not self.input_available:
            return "No microphone was detected on this system."
        if sr is None:
            return "SpeechRecognition is not installed (pip install SpeechRecognition)."
        return None

    # -- capture ------------------------------------------------------------
    def cancel(self) -> None:
        """Ask an in-flight `listen()` to stop early."""
        self._abort.set()

    def _resolve_device(self):
        spec = config.MIC_DEVICE
        if not spec:
            return None
        if spec.isdigit():
            return int(spec)
        needle = spec.lower()
        try:
            for index, device in enumerate(sd.query_devices()):
                if device["max_input_channels"] >= 1 and needle in device["name"].lower():
                    return index
        except Exception:
            pass
        return None

    def listen(self, on_level: LevelCallback | None = None) -> ListenResult:
        """Record one utterance and transcribe it.

        Recording stops on a run of silence after speech is detected, or at
        `LISTEN_MAX_SECONDS`, whichever comes first.
        """
        reason = self.unavailable_reason()
        if reason:
            return ListenResult(error=reason)

        self._abort.clear()
        rate = config.LISTEN_SAMPLE_RATE
        block = int(rate * 0.05)                       # 50 ms analysis blocks
        max_blocks = int(config.LISTEN_MAX_SECONDS / 0.05)
        silence_blocks_needed = max(1, int(config.LISTEN_SILENCE_SECONDS / 0.05))

        frames: list[bytes] = []
        noise_floor = 0.0
        speech_started = False
        silent_run = 0

        try:
            with sd.InputStream(
                device=self._resolve_device(),
                samplerate=rate,
                channels=1,
                dtype="int16",
                blocksize=block,
            ) as stream:
                for index in range(max_blocks):
                    if self._abort.is_set():
                        break
                    data, _overflowed = stream.read(block)
                    frames.append(bytes(data))

                    samples = np.frombuffer(bytes(data), dtype="<i2").astype(np.float32)
                    level = float(np.sqrt(np.mean(samples ** 2))) / 32768.0 if samples.size else 0.0
                    if on_level is not None:
                        on_level(min(1.0, level * 12.0))

                    # First few blocks establish the room floor.
                    if index < 6:
                        noise_floor = max(noise_floor, level)
                        continue
                    threshold = max(noise_floor * 2.5, 0.012)

                    if level >= threshold:
                        speech_started = True
                        silent_run = 0
                    elif speech_started:
                        silent_run += 1
                        if silent_run >= silence_blocks_needed:
                            break
        except Exception as exc:
            return ListenResult(error=f"Microphone error: {exc}")
        finally:
            if on_level is not None:
                on_level(0.0)

        if self._abort.is_set():
            return ListenResult(error="Listening cancelled.")
        if not speech_started:
            return ListenResult(error="No speech detected — try again, closer to the mic.")

        return self._transcribe(b"".join(frames), rate)

    def _transcribe(self, pcm: bytes, rate: int) -> ListenResult:
        if not pcm:
            return ListenResult(error="Nothing was recorded.")
        try:
            recognizer = sr.Recognizer()
            audio = sr.AudioData(pcm, rate, 2)   # 16-bit mono
        except Exception as exc:
            return ListenResult(error=f"Could not prepare audio: {exc}")

        try:
            text = recognizer.recognize_google(audio)
        except sr.UnknownValueError:
            return ListenResult(error="Speech was not intelligible.")
        except sr.RequestError as exc:
            return ListenResult(error=f"Speech service unreachable: {exc}")
        except Exception as exc:
            return ListenResult(error=f"Recognition failed: {exc}")

        text = (text or "").strip()
        if not text:
            return ListenResult(error="Speech was not intelligible.")
        return ListenResult(text=text)

    # -- speech output ------------------------------------------------------
    def speak(self, text: str, on_level: LevelCallback | None = None) -> bool:
        """Speak `text`, reporting an amplitude 0..1 while it plays.

        Blocks until playback finishes, so call it from a worker thread. The
        level feed drives the on-screen core: it is the *measured* amplitude
        when we synthesised the audio ourselves (ElevenLabs), and a synthesised
        speech envelope when the OS voice plays it, since the platform gives no
        amplitude back.
        """
        text = (text or "").strip()
        if not text or not config.TTS_ENABLED:
            return False
        try:
            if self._speak_elevenlabs(text, on_level):
                return True
            if self._speak_rendered(text, on_level):
                return True
            return self._speak_system(text, on_level)
        finally:
            if on_level is not None:
                on_level(0.0)

    def stop_speaking(self) -> None:
        """Cut playback off immediately, whichever backend is talking."""
        with self._tts_lock:
            process = self._tts_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        if sd is not None:
            try:
                sd.stop()      # ends the ElevenLabs PCM playback path
            except Exception:
                pass

    def _speak_elevenlabs(self, text: str, on_level: LevelCallback | None = None) -> bool:
        if not (config.ELEVENLABS_API_KEY and config.ELEVENLABS_VOICE_ID):
            return False
        if np is None or sd is None:
            return False
        try:
            from elevenlabs.client import ElevenLabs
        except ImportError:
            return False
        try:
            client = ElevenLabs(api_key=config.ELEVENLABS_API_KEY)
            raw = b"".join(
                client.text_to_speech.convert(
                    voice_id=config.ELEVENLABS_VOICE_ID,
                    text=text,
                    model_id=config.ELEVENLABS_MODEL_ID,
                    output_format="pcm_24000",
                )
            )
        except Exception:
            return False   # fall through to the OS voice
        if not raw or len(raw) < 2:
            return False
        try:
            if len(raw) % 2:
                raw = raw[:-1]
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            sd.play(samples, 24000)
            if on_level is not None:
                self._report_playback_level(samples, 24000, on_level)
            sd.wait()
        except Exception:
            return False
        return True

    def _speak_rendered(self, text: str, on_level: LevelCallback | None = None) -> bool:
        """macOS: render to a file first, then play it through an audio buffer.

        Streaming `say` live puts a hard real-time deadline on the audio path,
        and painting a fullscreen retina window intermittently blows it — that is
        the stuttering, distorted voice. Rendering has no deadline, and the
        playback buffer then absorbs the same stalls. It also hands us the actual
        samples, so the core reacts to the real waveform rather than a synthesised
        envelope. Returns False to fall through to live playback if anything
        about this path is unavailable.
        """
        if sys.platform != "darwin" or sd is None or np is None:
            return False
        say = shutil.which("say")
        if not say:
            return False

        path = Path(tempfile.gettempdir()) / f"aura_tts_{os.getpid()}.wav"
        try:
            result = subprocess.run(
                [say, "-o", str(path), "--data-format=LEI16@22050", text],
                capture_output=True, check=False,
            )
            if result.returncode != 0 or not path.is_file():
                return False
            with wave.open(str(path), "rb") as wf:
                if wf.getsampwidth() != 2:
                    return False
                rate, channels = wf.getframerate(), wf.getnchannels()
                raw = wf.readframes(wf.getnframes())
        except (OSError, wave.Error, ValueError):
            return False
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

        if len(raw) < 2:
            return False
        samples = np.frombuffer(raw[: len(raw) - (len(raw) % 2)], dtype="<i2")
        samples = samples.astype(np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        if samples.size == 0:
            return False

        try:
            sd.play(samples, rate)
            if on_level is not None:
                self._report_playback_level(samples, rate, on_level)
            sd.wait()
        except Exception:
            return False
        return True

    def _report_playback_level(self, samples, rate: int, on_level: LevelCallback) -> None:
        """Feed the real RMS envelope of audio we generated, as it plays."""
        total = len(samples) / float(rate)
        window = max(1, rate // 25)          # ~40 ms of audio per reading
        started = time.monotonic()
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= total or self._abort.is_set():
                break
            index = int(elapsed * rate)
            chunk = samples[index:index + window]
            if chunk.size:
                rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
                on_level(min(1.0, rms * 4.5))
            time.sleep(0.04)

    def _speak_system(self, text: str, on_level: LevelCallback | None = None) -> bool:
        """Use the platform voice: `say` on macOS, PowerShell on Windows, espeak on Linux."""
        command: list[str] | None = None
        if sys.platform == "darwin" and shutil.which("say"):
            command = ["say", text]
        elif sys.platform == "win32":
            shell = shutil.which("powershell") or shutil.which("pwsh")
            if shell:
                safe = text.replace("'", "''")
                command = [
                    shell, "-NoProfile", "-NonInteractive", "-Command",
                    "Add-Type -AssemblyName System.Speech; "
                    "(New-Object System.Speech.Synthesis.SpeechSynthesizer)"
                    f".Speak('{safe}')",
                ]
        else:
            for exe in ("espeak-ng", "espeak", "spd-say"):
                found = shutil.which(exe)
                if found:
                    command = [found, text]
                    break
        if command is None:
            return self._speak_pyttsx3(text)

        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return False
        with self._tts_lock:
            self._tts_process = process
        try:
            if on_level is None:
                process.wait()
            else:
                # The OS voice reports no amplitude, so drive the display from a
                # synthesised speech envelope for as long as it is talking.
                started = time.monotonic()
                while process.poll() is None:
                    on_level(speech_envelope(time.monotonic() - started))
                    time.sleep(0.04)
        finally:
            with self._tts_lock:
                self._tts_process = None
        return process.returncode == 0

    @staticmethod
    def _speak_pyttsx3(text: str) -> bool:
        try:
            import pyttsx3
        except ImportError:
            return False
        try:
            engine = pyttsx3.init()
            engine.say(text)
            engine.runAndWait()
        except Exception:
            return False
        return True
