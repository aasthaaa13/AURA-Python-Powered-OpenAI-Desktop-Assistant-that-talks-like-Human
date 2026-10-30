# AURA — HUD interface + clap listener

Two programs that work together:

| | What it is | Run it with |
| --- | --- | --- |
| **`main.py`** | A futuristic **AURA-style HUD**: animated AI core, live system telemetry, voice input, AI responses and an activity log. | `python main.py` |
| **`aura.py`** | A background **double-clap listener** that opens the HUD (and your editor) when you clap twice. | `python aura.py` |

You can run the HUD on its own — the clap listener is only the hands-free launcher.

## Setup

```bash
python -m pip install -r requirements.txt
cp .env.example .env      # then fill in your keys (optional)
```

Everything except `PySide6` degrades gracefully: no API key runs the offline engine,
no microphone disables voice input, no `psutil` hides the telemetry gauges.

---

# 1. The HUD (`main.py`)

```bash
python main.py
```

It opens as a **small orb window**, parked near the bottom-right of the screen.

![The orb in its IDLE, LISTENING and SPEAKING states](assets/screenshot.png)

**Layout** — an animated circle, the current state, the last thing said, and a
microphone button with a text box. Nothing else: no telemetry, no log. The system
monitor still runs in the background, so spoken commands like `status` keep working —
it simply has nothing on screen.

**Why it is small.** Repainting a fullscreen window costs ~24 ms a frame on a retina
display and intermittently stalls audio playback, which is heard as a distorted voice.
The orb costs ~3.8 ms, so the problem disappears rather than being traded off against
frame rate — measured at **zero audio underruns** with the orb animating at 30 fps.

**The orb** is a circle whose edge ripples with the audio level and whose colour tracks
the assistant state. The motion is deliberately subtle — a circle that breathes, not a
blob. The level driving it is your **measured** microphone amplitude while listening,
and the measured waveform of the reply while speaking.

Click the orb (or press **Space**) to talk, **Enter** to focus the text box, **Ctrl+Q**
to quit. `AURA_MINI_ON_TOP=true` keeps it above other windows.

<details>
<summary>The old fullscreen HUD is still available</summary>

`AURA_UI_MODE=hud` brings back the full dashboard — telemetry gauges, activity log,
audio spectrum and the rotating 3D wireframe sphere. It looks better in a screenshot,
but it is the configuration that interferes with audio, so lower `AURA_TARGET_FPS`
(around 8–16) if you use it.
</details>

**Assistant states** drive the core's colour, speed and the spectrum:

| State | Meaning | Colour |
| --- | --- | --- |
| `IDLE` | Waiting for input | cyan |
| `LISTENING` | Recording from the microphone | green |
| `PROCESSING` | Running a command or querying the AI | amber |
| `SPEAKING` | Reading the reply aloud | violet |
| `ERROR` | Last operation failed (transient — returns to `IDLE`) | red |

## Commands

Type in the bar or speak them. These run locally — they never reach the AI:

`status` · `cpu` · `memory` · `disk` · `battery` · `network` · `time` · `date` ·
`uptime` · `sysinfo` · `engine` · `open <app or https URL>` · `sleep display` ·
`clear` · `quit` · `help`

`quit` (also `exit`, `shutdown`, `close`, `goodbye`) plays a power-down sequence — the
sphere spins up, collapses to nothing and goes red, the spectrum flattens, and the header
reads POWERING DOWN — then closes once the farewell has finished, with a 6-second
backstop so a wedged voice can never strand you. **Ctrl+Q** closes without speaking.

`open` matches app names the way people actually say them. Spacing, case and punctuation
are ignored, so **VS Code**, `vs code`, `VSCode`, `vs-code` and `visual studio code` are
all the same entry; the verb is matched anywhere in the sentence and `launch` / `start` /
`run` / `pull up` / `fire up` work too. So `aura open vs code`, `can you open chrome`
and `i want to open spotify` are all handled locally. 21 apps are allow-listed; anything
else is refused by name.

Anything else is sent to the AI engine.

**Safety.** AI-generated text can never execute anything. Every action is a named Python
handler behind an explicit pattern match, subprocesses are launched with argument lists
(never a shell), and `open` only accepts an allow-listed app or an `https` URL. Actions
with a visible side effect ask first and run only after you reply `yes`.

## Voice

Press **MICROPHONE** to record one utterance; it stops automatically on silence.
Pressing it **while AURA is talking cuts the reply off and starts listening immediately**
— no need to wait for it to finish.
Capture uses `sounddevice` (PortAudio) — **PyAudio is not required**. Transcription uses
Google's recogniser via `SpeechRecognition`, which needs a network connection.
Replies are spoken with the system voice (`say` on macOS, SAPI on Windows, `espeak` on
Linux), or with ElevenLabs if you configure a key.

## AI engine

`AIEngine` in `assistant/ai_engine.py` is the seam — implement `process()` and the whole
UI works against it unchanged. Four backends ship:

- **`OpenAIEngine`** — OpenAI via the `openai` SDK (`gpt-4o-mini` by default). Used
  automatically when `OPENAI_API_KEY` is set. **This is the default.**
- **`OllamaEngine`** — local or self-hosted models through Ollama. See below.
- **`ClaudeEngine`** — Anthropic Claude, for `ANTHROPIC_API_KEY`.
- **`OfflineEngine`** — the fallback with nothing configured. Built-in commands still
  work, and it says plainly that it cannot reason rather than inventing an answer.

`AURA_AI_PROVIDER=auto` (the default) picks whichever key is present, preferring
OpenAI. Naming a provider explicitly (`openai` / `ollama` / `claude` / `offline`) never
silently substitutes another — if it cannot run you get the offline engine and the reason
why, rather than an answer from a backend you did not choose.

### Using Ollama

> **A local Ollama does not use an API key.** It is an unauthenticated HTTP server on
> your own machine, so there is no key to create and none to paste. `OLLAMA_API_KEY`
> exists here only for Ollama's hosted service or a self-hosted instance behind an
> authenticating proxy; leave it blank for a normal local install.

```bash
ollama serve              # start the server (often already running)
ollama pull llama3.2:1b   # or any model you prefer
```

Then in `.env`:

```env
AURA_AI_PROVIDER=ollama
OLLAMA_MODEL=llama3.2:1b
```

That is the whole setup. Ollama serves an OpenAI-compatible API at `/v1`, so this reuses
the same request path and only swaps the endpoint — no extra dependency.

**Remote or hosted Ollama.** Point `OLLAMA_HOST` at the machine, and set
`OLLAMA_API_KEY` if it requires authentication (sent as the bearer token):

```env
AURA_AI_PROVIDER=ollama
OLLAMA_HOST=https://ollama.example.com
OLLAMA_API_KEY=your-token-here
```

`/v1` is appended automatically if you leave it off.

**When something is wrong it tells you what to run** rather than surfacing a raw HTTP
error — a stopped server gives *"Could not reach Ollama at … Start it with: ollama
serve"*, and a model you have not pulled gives *"Model 'x' is not pulled. Run: ollama
pull x"*.

If you see *"Model 'x' was not found for this key"* on OpenAI, set `OPENAI_MODEL` to a
model your account can use. The engine also copes with either output-token parameter
name, so both current chat models and reasoning models work without a config change.

## Configuration

Copy `.env.example` to `.env`. Every value is optional — the HUD starts without any of them.

| Variable | Purpose |
| --- | --- |
| `AURA_USER_NAME` | Who AURA greets (default `Hasib`). |
| `AURA_START_FULLSCREEN` | `false` starts windowed instead of fullscreen. |
| `OPENAI_API_KEY` | Enables the OpenAI engine. Starts with `sk-`. Without it the HUD runs offline. |
| `OPENAI_MODEL` | Model id (default `gpt-4o-mini`). |
| `AURA_AI_PROVIDER` | `auto` (default), `openai`, `ollama`, `claude`, or `offline`. |
| `OLLAMA_HOST` / `OLLAMA_MODEL` | Local or remote Ollama. **No API key needed locally.** |
| `OLLAMA_API_KEY` | Only for a hosted/proxied Ollama; blank for a local install. |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | Optional Claude backend instead of OpenAI. |
| `AURA_SPEECH_ENABLED` | `false` disables the microphone entirely. |
| `AURA_TTS_ENABLED` | `false` mutes spoken replies (text still appears). |
| `AURA_INPUT_DEVICE` | Mic override: device index or a substring of its name. |
| `AURA_LISTEN_MAX_SECONDS` / `AURA_LISTEN_SILENCE_SECONDS` | Recording length and the silence gap that ends it. |
| `AURA_MONITOR_INTERVAL` | Seconds between telemetry samples (default `1.5`). |
| `AURA_TARGET_FPS` | Animation frame rate (default `20`). See the note below. |
| `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID` | Optional ElevenLabs voice instead of the system voice. |

Colours, fonts and sphere density live in `config.py` (`THEME`, `SPHERE_POINTS`,
`MAX_CORE_RADIUS`).

### Painting, and why the voice used to distort

On a **fullscreen retina** display each painted frame costs ~24 ms, dominated by the
window flush at 3360×2100 — not by how much is drawn, so mesh density and core size
barely move it. Streaming the OS voice puts a hard real-time deadline on playback, and
that painting intermittently blew it: a stuttering, distorted voice, which went away as
soon as another window took focus and the HUD stopped repainting.

Throttling the frame rate only traded that off. The actual fix is in
`SpeechEngine._speak_rendered`: **render the speech to a file first, then play it through
an audio buffer** that absorbs the stalls. Rendering has no deadline, and playback is now
immune — measured at zero audio underruns with the HUD painting at full rate. It also
hands the core the real waveform, so the sphere reacts to measured amplitude rather than
a synthesised envelope.

So `AURA_TARGET_FPS` (24) now applies while speaking too. `AURA_AUDIO_FPS` (16) and
the spectrum throttle are left for the cases that *are* still live: recording from the
microphone, and platforms without the buffered playback path.

## Architecture

```text
main.py              entry point: QApplication, palette, window
config.py            theme, tunables, env/secret loading
ui/
  main_window.py     layout, styling, signal wiring, the single animation clock
  hud.py             animated AI core (rings, orbits, reactor)
  animations.py      easing, smoothing, oscillators
  widgets.py         glass panels, gauges, spectrum, log, input bar
assistant/
  assistant_manager.py  state machine + QThreadPool orchestration  ← the only UI seam
  ai_engine.py          AIEngine interface, Claude + offline backends
  speech.py             microphone capture, transcription, text-to-speech
  commands.py           allow-listed command routing and confirmations
system/
  system_monitor.py  psutil polling on a QThread → SystemStats signal
  system_info.py     static host facts and formatting
```

**Nothing slow runs on the GUI thread.** System polling lives on its own `QThread`;
recognition, AI calls and speech run on a `QThreadPool`; results return as Qt signals.
Animation is driven by one 30 fps timer that pauses when the window is hidden, so a slow
AI response never stutters the interface. Typing while AURA is busy queues the input
rather than dropping it.

---

# 2. The clap listener (`aura.py`)

```bash
python aura.py
```

Listens for a **double clap** and then opens the HUD and your editor.

| Step | Controlled by |
| ---- | ------------- |
| AURA HUD | `OPEN_AURA_HUD_ON_DOUBLE_CLAP` — launches `main.py` in its own process |
| Your editor | `FOCUS_EXISTING_VSCODE_ON_DOUBLE_CLAP` / `OPEN_NEW_VSCODE_ON_DOUBLE_CLAP` — **both off by default**, so only the HUD opens |
| Welcome speech | `AURA_WELCOME_ENABLED`, `AURA_WELCOME_PHRASE` |
| Chrome windows | `CHROME_TARGETS` — **empty by default**, so no browser opens. Add entries to open pages. |
| Spotify/YouTube track | `SONG_URI` — **empty by default**, set it to also open a track |

Works on **macOS** and **Windows**. Window placement across multiple monitors is supported
on both (CoreGraphics on macOS, the Win32 API on Windows); F11 fullscreen for VS Code is
Windows-only.

### Which editor opens

`EDITOR_CANDIDATES` lists editors in priority order — Visual Studio Code, then Cursor,
then Antigravity IDE. The **first one installed** is what a double clap opens; they are
all VS Code builds, so the same launch and focus logic works for each. The startup log
prints which one was found. Reorder the list to change the preference.

The flow runs **once per process** by default (`RUN_ONCE_PER_PROCESS = True`); later double
claps are detected and logged but do not re-open anything. Set it to `False` to fire every time.

## Environment variables (clap listener)

The script loads a **`.env` file** in the same folder as `aura.py` (via `python-dotenv`).
You can also set variables in the shell. The HUD reads the same file — its own variables
are listed under [Configuration](#configuration) above.

> The Chrome URL variables below only take effect if you add entries back to
> `CHROME_TARGETS`, which is empty by default.

### Required (ElevenLabs welcome line)

| Variable | Purpose |
| -------- | ------- |
| `ELEVENLABS_API_KEY` | The API key itself — it **starts with `sk_`**. |
| `ELEVENLABS_VOICE_ID` | Voice ID from the ElevenLabs app (My Voices / library). |

Without these, the welcome speech is skipped (the apps still open).

> **The `sk_` prefix matters.** The [API keys page](https://elevenlabs.io/app/settings/api-keys)
> lists each key by its **key ID** (a long hex string). That ID is *not* the key — the API
> rejects it with `API key ID used as API key`. The real key starts with `sk_` and is shown
> **only once**, when you create or rotate it. If you no longer have it, rotate the key and
> copy the new `sk_...` value. The script checks this at startup and tells you if it is wrong.

### Optional

| Variable | Purpose |
| -------- | ------- |
| `ELEVENLABS_MODEL_ID` | TTS model (default in code: `eleven_multilingual_v2`). |
| `ELEVENLABS_OUTPUT_FORMAT` | `pcm_*` (default `pcm_24000`) plays directly with no external player. `mp3_*` needs a system player (`afplay` on macOS, PowerShell on Windows, `ffplay`/`mpv`/`mpg123` on Linux). PCM output requires a paid ElevenLabs plan, so if the API rejects it the script retries once as `mp3_44100_128`. |
| `ELEVENLABS_PCM_SAMPLE_RATE` | Override PCM sample rate if it differs from the format name. |
| `AURA_WELCOME_CACHE_DIR` | Custom folder for cached welcome WAV (default: `.cache/aura_welcome/` under the project). |
| `AURA_INPUT_DEVICE` | Optional mic override: **integer** index or **substring** of the device name. If unset, the script uses the system default; when that mic is silent, it auto-picks the loudest working input. List devices: `python -c "import sounddevice as sd; print(sd.query_devices())"`. |
| `CLAUDE_CODE_URL` | URL for the first Chrome window (default: `https://claude.ai/new`). |
| `TASARADAR_URL` | URL for the second Chrome window. `BINANCE_BTC_URL` is read as a fallback; the built-in default is the Binance BTC/USDT page. |
| `CHROME_NEW_WINDOW_WAIT_S` | Seconds to wait for a new Chrome window on Windows (default `25`). |
| `CHROME_WINDOW_WIDTH` / `CHROME_WINDOW_HEIGHT` | Windowed Chrome size when `OPEN_CHROME_FULLSCREEN` is `False`. |

Example `.env`:

```env
ELEVENLABS_API_KEY=your_key_here
ELEVENLABS_VOICE_ID=your_voice_id_here
```

## Run

```bash
python aura.py
```

Allow microphone access if your OS prompts you. Stop with **Ctrl+C**.

On startup the script prints the detected monitors, the URLs it will open, and the measured
room noise floor with the resulting clap threshold — useful for tuning.

## Tuning

Edit the constants at the top of `aura.py`:

| Constant      | Effect                                                            |
| ------------- | ----------------------------------------------------------------- |
| `SPIKE_RATIO` | How many times above the measured room noise counts as a clap. Increase for false triggers; decrease if claps are missed. |
| `COOLDOWN_S`  | Minimum time between two triggers.                                |
| `BLOCK_MS`    | Larger = slightly less CPU, a bit less precise timing.            |
| `MIN_RMS`     | Absolute floor on how loud a block must be (helps in very quiet rooms). |
| `MAX_RMS`     | Ceiling on the computed threshold, so a hot mic or loud room cannot push it past what a real clap reaches. Raise it if a loud room causes false triggers. |
| `MIN_DOUBLE_GAP_S` / `MAX_DOUBLE_GAP_S` | Allowed spacing between the two claps. |
| `SAMPLE_RATE` | Try `48000` if your device does not like `44100`.                 |

The noise floor is **measured from your mic at startup** and then adapts (fast downward,
slowly upward), so `SPIKE_RATIO` is relative to your actual room rather than a fixed guess.

## Troubleshooting

- **Wrong or quiet mic:** On startup the script probes your default input. If it is silent, it **auto-selects** the loudest working mic. To force a specific device, set `AURA_INPUT_DEVICE` in `.env` (index or name substring from `sounddevice.query_devices()`).
- **PortAudio / audio errors:** Update audio drivers or try another `SAMPLE_RATE`.
- **No reaction to claps:** Lower `SPIKE_RATIO` slightly, or clap closer to the mic. The startup log shows the exact threshold in use.
- **Too many triggers:** Raise `SPIKE_RATIO` or `COOLDOWN_S`.
- **VS Code does not open:** The startup log prints which editor was found, or warns if none was. Install VS Code from [code.visualstudio.com](https://code.visualstudio.com/), or add your editor to `EDITOR_CANDIDATES`.
- **Chrome opens on the wrong screen:** Check the monitor list printed at startup and adjust the `monitor` value in `CHROME_TARGETS`. If windows still land on one screen, set `CHROME_SEPARATE_SITE_PROFILES = True` (uses a separate Chrome profile per site).
- **No welcome speech:** The startup log states the exact reason. Most often the key is the dashboard **key ID** rather than the `sk_...` key — see the note above. API errors are logged with the message ElevenLabs returned.
