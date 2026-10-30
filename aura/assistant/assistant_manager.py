"""Assistant orchestration and state machine.

This is the only place the UI talks to. It owns the state machine
(IDLE → LISTENING → PROCESSING → SPEAKING → IDLE, with ERROR as a transient),
runs every slow operation — recognition, AI calls, speech — on a QThreadPool so
the GUI thread never blocks, and marshals results back through Qt signals.

The UI never imports the AI engine or the speech engine directly, so either can
be swapped without touching a widget.
"""

from __future__ import annotations

import traceback
from enum import Enum
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot

import config
from assistant.ai_engine import AIEngine, AIResult, Turn, create_engine
from assistant.commands import CommandResult, CommandRouter
from assistant.speech import ListenResult, SpeechEngine


class AssistantState(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    SPEAKING = "SPEAKING"
    ERROR = "ERROR"


class _TaskSignals(QObject):
    done = Signal(object)
    failed = Signal(str)
    level = Signal(float)


class _Task(QRunnable):
    """Runs one callable on the pool and reports back via signals."""

    def __init__(self, fn: Callable[..., object], *, pass_level: bool = False) -> None:
        super().__init__()
        self.signals = _TaskSignals()
        self._fn = fn
        self._pass_level = pass_level
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # noqa: D102 - QRunnable entry point
        try:
            if self._pass_level:
                result = self._fn(self.signals.level.emit)
            else:
                result = self._fn()
        except Exception as exc:  # a worker crash must never kill the app
            self.signals.failed.emit(f"{exc}\n{traceback.format_exc(limit=3)}")
            return
        self.signals.done.emit(result)


class AssistantManager(QObject):
    """Coordinates speech, commands, and the AI engine."""

    state_changed = Signal(str)
    message = Signal(str, str, str)     # speaker, text, level
    log = Signal(str, str)              # text, level
    error = Signal(str)
    level_changed = Signal(float)
    listening_changed = Signal(bool)
    ui_action = Signal(str)             # e.g. "clear_log"

    def __init__(self, stats_provider: Callable[[], object] | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(4)

        self.engine: AIEngine = create_engine()
        self.speech = SpeechEngine()
        self.router = CommandRouter(
            stats_provider=stats_provider,
            engine_name=lambda: self.engine.name,
        )

        self._state = AssistantState.IDLE
        self._history: list[Turn] = []
        self._pending_confirm: CommandResult | None = None
        self._queue: list[str] = []
        self._continuous = False
        self._busy = False
        self._shutting_down = False
        self._quit_pending = False
        # Bumped whenever a reply is cut off, so a stale worker finishing later
        # cannot clobber the state of whatever replaced it.
        self._speech_generation = 0

    # -- introspection ------------------------------------------------------
    @property
    def state(self) -> AssistantState:
        return self._state

    @property
    def busy(self) -> bool:
        return self._busy

    def engine_status(self) -> tuple[str, str]:
        return self.engine.name.upper(), self.engine.status

    def mic_problem(self) -> str | None:
        if not config.SPEECH_ENABLED:
            return "Voice input is disabled in configuration."
        return self.speech.unavailable_reason()

    def greet(self) -> None:
        """Opening line, emitted once the window is up."""
        name, status = self.engine_status()
        self.log.emit(f"AI engine: {name} ({status})", "system")
        problem = config.api_key_problem()
        if problem:
            self.log.emit(problem, "warn")
        mic = self.mic_problem()
        if mic:
            self.log.emit(f"Voice input unavailable — {mic}", "warn")
        else:
            self.log.emit("Voice input ready.", "ok")
        self.message.emit(
            "AURA",
            f"Welcome back, {config.USER_NAME}. All systems online — how can I help?",
            "assistant",
        )

    # -- state --------------------------------------------------------------
    def _set_state(self, state: AssistantState) -> None:
        if state == self._state:
            return
        self._state = state
        self.state_changed.emit(state.value)

    def _fail(self, text: str) -> None:
        self._busy = False
        self.error.emit(text)
        self.log.emit(text, "error")
        self._set_state(AssistantState.ERROR)
        # ERROR is transient: settle back to IDLE so the HUD stays usable,
        # and let anything the user queued behind the failure through.
        self._finish()

    # -- text entry ---------------------------------------------------------
    @Slot(str)
    def submit_text(self, text: str) -> None:
        text = (text or "").strip()
        if not text or self._shutting_down:
            return
        if self._busy:
            # Queue rather than drop — typing ahead while AURA speaks is normal.
            self._queue.append(text)
            self.log.emit(f"Queued ({len(self._queue)} waiting).", "system")
            return

        self.message.emit("USER", text, "user")

        if self._pending_confirm is not None:
            self._resolve_confirmation(text)
            return

        result = self.router.route(text)
        if result.handled:
            self._handle_command(result)
            return

        self._history.append(Turn("user", text))
        self._ask_engine(text)

    # -- confirmation flow --------------------------------------------------
    def _resolve_confirmation(self, text: str) -> None:
        pending, self._pending_confirm = self._pending_confirm, None
        answer = text.strip().lower()
        if answer in ("y", "yes", "confirm", "do it", "go ahead", "ok", "okay", "affirmative"):
            if pending is None or pending.action is None:
                self._respond("Nothing was pending.", speak=False)
                return
            self.log.emit("Confirmed — executing.", "ok")
            self._busy = True
            self._set_state(AssistantState.PROCESSING)
            task = _Task(pending.action)
            task.signals.done.connect(self._on_action_done)
            task.signals.failed.connect(self._on_task_failed)
            self._pool.start(task)
        else:
            self.log.emit("Cancelled.", "warn")
            self._respond("Cancelled — nothing was changed.", speak=False)

    @Slot(object)
    def _on_action_done(self, result: object) -> None:
        self._busy = False
        self._respond(str(result) if result else "Done.")

    # -- commands -----------------------------------------------------------
    def _handle_command(self, result: CommandResult) -> None:
        if result.ui_action == "quit":
            # Hold the shutdown until the farewell has been delivered — but never
            # longer than this, so a wedged voice can't strand the user in an app
            # they asked to close.
            self._quit_pending = True
            # Tell the UI to wind down now: nothing on screen matters any more,
            # and the idle animation otherwise competes with the farewell.
            self.ui_action.emit("quitting")
            QTimer.singleShot(6000, self._force_quit)
        elif result.ui_action:
            self.ui_action.emit(result.ui_action)
        if result.requires_confirmation:
            self._pending_confirm = result
            self._respond(f"{result.text}  (reply 'yes' to confirm)", speak=result.speak)
            return
        if result.action is not None:
            # Runs without asking. Off the GUI thread, since launching an app
            # waits on the OS; the reply is whatever the action reports back.
            self._busy = True
            self._set_state(AssistantState.PROCESSING)
            task = _Task(result.action)
            task.signals.done.connect(self._on_action_done)
            task.signals.failed.connect(self._on_task_failed)
            self._pool.start(task)
            return
        self._respond(result.text, speak=result.speak, level=result.level)

    # -- AI -----------------------------------------------------------------
    def _ask_engine(self, prompt: str) -> None:
        self._busy = True
        self._set_state(AssistantState.PROCESSING)
        self.log.emit(f"Querying {self.engine.name} engine…", "system")
        history = list(self._history[:-1])   # exclude the prompt we just appended
        task = _Task(lambda: self.engine.process(prompt, history))
        task.signals.done.connect(self._on_engine_done)
        task.signals.failed.connect(self._on_task_failed)
        self._pool.start(task)

    @Slot(object)
    def _on_engine_done(self, result: object) -> None:
        self._busy = False
        if not isinstance(result, AIResult):
            self._fail("The AI engine returned an unexpected result.")
            return
        if not result.ok:
            self._fail(result.error or "The AI engine failed.")
            return
        self._history.append(Turn("assistant", result.text))
        if len(self._history) > config.AI.history_limit * 2:
            del self._history[: len(self._history) - config.AI.history_limit * 2]
        self._respond(result.text)

    # -- responses ----------------------------------------------------------
    def _respond(self, text: str, *, speak: bool = True, level: str = "assistant") -> None:
        text = (text or "").strip()
        if not text:
            self._set_state(AssistantState.IDLE)
            return
        self.message.emit("AURA", text, level)

        if speak and config.TTS_ENABLED:
            self._speak(text)
        else:
            self._finish()

    def _speak(self, text: str) -> None:
        self._busy = True
        self._set_state(AssistantState.SPEAKING)
        generation = self._speech_generation
        # pass_level feeds the live amplitude to the core while it talks.
        task = _Task(lambda emit: self.speech.speak(text, on_level=emit), pass_level=True)
        task.signals.level.connect(
            lambda value, g=generation: self._on_speech_level(value, g)
        )
        task.signals.done.connect(
            lambda _result, g=generation: self._on_speak_done(g)
        )
        task.signals.failed.connect(self._on_task_failed)
        self._pool.start(task)

    def _on_speech_level(self, value: float, generation: int) -> None:
        if generation == self._speech_generation:
            self.level_changed.emit(value)

    def _on_speak_done(self, generation: int) -> None:
        if generation != self._speech_generation:
            return   # this reply was interrupted; whatever replaced it owns the state
        self._busy = False
        self.level_changed.emit(0.0)
        self._finish()

    def _finish(self) -> None:
        """Return to IDLE, then drain one queued input or resume continuous mode."""
        self._set_state(AssistantState.IDLE)
        if self._shutting_down:
            return
        if self._quit_pending:
            # The farewell has been spoken; anything queued behind it is moot.
            self._quit_pending = False
            self._queue.clear()
            self.ui_action.emit("quit")
            return

        if self._queue:
            # Defer through the event loop so a long queue can't recurse deeply.
            nxt = self._queue.pop(0)
            QTimer.singleShot(0, lambda: self.submit_text(nxt))
            return
        self._maybe_continue()

    # -- voice input --------------------------------------------------------
    @Slot()
    def start_listening(self) -> None:
        if self._shutting_down:
            return
        problem = self.mic_problem()
        if problem:
            self._fail(problem)
            self.listening_changed.emit(False)
            return

        if self._state == AssistantState.SPEAKING:
            # Barge-in: pressing the mic mid-sentence means "stop talking and
            # listen to me now", so cut the reply off rather than queueing.
            self.log.emit("Interrupted — listening.", "warn")
            self._interrupt_speech()
        elif self._busy:
            self.log.emit("Busy — cannot listen right now.", "warn")
            self.listening_changed.emit(False)
            return

        self._busy = True
        self._set_state(AssistantState.LISTENING)
        self.listening_changed.emit(True)
        self.log.emit("Listening…", "ok")

        task = _Task(self.speech.listen, pass_level=True)
        task.signals.level.connect(self.level_changed.emit)
        task.signals.done.connect(self._on_listen_done)
        task.signals.failed.connect(self._on_task_failed)
        self._pool.start(task)

    def _interrupt_speech(self) -> None:
        """Stop playback now and drop the reply that was being spoken."""
        self._speech_generation += 1     # makes the in-flight speak() result stale
        self.speech.stop_speaking()
        self._busy = False
        self.level_changed.emit(0.0)

    @Slot()
    def cancel_listening(self) -> None:
        self.speech.cancel()
        self._continuous = False
        self.listening_changed.emit(False)

    @Slot(object)
    def _on_listen_done(self, result: object) -> None:
        self._busy = False
        self.level_changed.emit(0.0)
        self.listening_changed.emit(False)

        if not isinstance(result, ListenResult):
            self._fail("Speech recognition returned an unexpected result.")
            return
        if not result.ok:
            message = result.error or "Nothing was heard."
            if message == "Listening cancelled.":
                self.log.emit(message, "warn")
                self._set_state(AssistantState.IDLE)
                return
            self._fail(message)
            return

        self._set_state(AssistantState.IDLE)
        self.submit_text(result.text)

    def set_continuous(self, enabled: bool) -> None:
        self._continuous = bool(enabled)
        self.log.emit(
            f"Continuous listening {'enabled' if enabled else 'disabled'}.", "system"
        )
        if enabled and not self._busy:
            self.start_listening()

    def _maybe_continue(self) -> None:
        if self._continuous and not self._shutting_down and not self._busy:
            self.start_listening()

    def _force_quit(self) -> None:
        """Backstop for the deferred quit if speech never returns."""
        if not self._quit_pending:
            return
        self._quit_pending = False
        self.speech.stop_speaking()
        self.log.emit("Shutdown timeout — closing now.", "warn")
        self.ui_action.emit("quit")

    # -- failures -----------------------------------------------------------
    @Slot(str)
    def _on_task_failed(self, message: str) -> None:
        self._busy = False
        self.listening_changed.emit(False)
        self._fail(f"Background task failed: {message.splitlines()[0]}")

    # -- lifecycle ----------------------------------------------------------
    def shutdown(self) -> None:
        self._shutting_down = True
        self._continuous = False
        self._queue.clear()
        self.speech.cancel()
        self.speech.stop_speaking()
        self._pool.waitForDone(2000)
