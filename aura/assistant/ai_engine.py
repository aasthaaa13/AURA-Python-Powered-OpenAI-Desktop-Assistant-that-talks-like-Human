"""Pluggable AI backend.

`AIEngine` is the seam: the GUI only ever calls `process()`, so a different
model or a local runtime can be dropped in without touching the interface.
Four implementations ship here — OpenAI via the `openai` SDK (the default),
Ollama for local or self-hosted models (same wire protocol, different endpoint),
Claude via the Anthropic SDK, and an offline engine used when nothing is
configured, so the HUD is always usable.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

import config


@dataclass
class Turn:
    role: str      # "user" | "assistant"
    content: str


@dataclass
class AIResult:
    text: str
    engine: str
    ok: bool = True
    error: str | None = None


class AIEngine(abc.ABC):
    """Interface every backend implements."""

    name: str = "engine"

    @abc.abstractmethod
    def process(self, prompt: str, history: list[Turn] | None = None) -> AIResult:
        """Answer `prompt`. Called on a worker thread — may block."""

    @property
    def available(self) -> bool:
        return True

    @property
    def status(self) -> str:
        return "ONLINE" if self.available else "OFFLINE"


class OfflineEngine(AIEngine):
    """Deterministic fallback so the interface works with no API key.

    Keeps the app fully functional for local commands; anything needing real
    reasoning gets an honest "not connected" answer rather than a fabrication.
    """

    name = "offline"

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason or "No AI provider configured."

    @property
    def status(self) -> str:
        return "LOCAL"

    def process(self, prompt: str, history: list[Turn] | None = None) -> AIResult:
        text = prompt.strip().lower()
        if not text:
            return AIResult("Standing by.", self.name)
        if any(g in text for g in ("hello", "hi ", "hey", "good morning", "good evening")):
            return AIResult("Online and standing by. How can I help?", self.name)
        if "who are you" in text or "your name" in text:
            return AIResult(
                "I am AURA, running in local mode. Connect an API key for full reasoning.",
                self.name,
            )
        if "thank" in text:
            return AIResult("Any time.", self.name)
        return AIResult(
            f"I can't reason about that in local mode — {self.reason} "
            "Built-in commands (status, time, uptime, help) still work.",
            self.name,
        )


class OpenAIEngine(AIEngine):
    """OpenAI backend, via the official `openai` SDK.

    The client is created lazily so an import or auth failure surfaces as a UI
    error rather than preventing the app from starting.
    """

    name = "openai"

    def __init__(self, model: str | None = None) -> None:
        self.model = model or config.AI.openai_model
        self._client = None
        self._token_param = "max_completion_tokens"

    @property
    def available(self) -> bool:
        return not self._problem()

    # -- hooks the Ollama subclass overrides --------------------------------
    def _problem(self) -> str | None:
        return config.openai_key_problem()

    def _client_kwargs(self) -> dict:
        # Key comes from the environment — never hardcoded, never logged.
        return {"api_key": config.openai_api_key()}

    def _unreachable_message(self) -> str:
        return "Network error — could not reach the OpenAI API."

    def _missing_model_message(self) -> str:
        return (f"Model '{self.model}' was not found for this key. "
                "Set OPENAI_MODEL in .env to one your account can use.")

    def _auth_message(self) -> str:
        return "Authentication failed — check OPENAI_API_KEY."

    def _ensure_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(**self._client_kwargs())
        return self._client

    def _messages(self, prompt: str, history: list[Turn] | None) -> list[dict]:
        messages = [{"role": "system", "content": config.AI.system_prompt}]
        for turn in (history or [])[-config.AI.history_limit:]:
            if turn.content.strip():
                messages.append({"role": turn.role, "content": turn.content})
        messages.append({"role": "user", "content": prompt})
        return messages

    def process(self, prompt: str, history: list[Turn] | None = None) -> AIResult:
        problem = self._problem()
        if problem:
            return AIResult("", self.name, ok=False, error=problem)
        try:
            import openai
        except ImportError:
            return AIResult(
                "", self.name, ok=False,
                error="The 'openai' package is not installed (pip install openai).",
            )

        messages = self._messages(prompt, history)
        try:
            response = self._create(messages)
        except openai.AuthenticationError:
            return AIResult("", self.name, ok=False, error=self._auth_message())
        except openai.PermissionDeniedError:
            return AIResult("", self.name, ok=False,
                            error=f"This key is not allowed to use '{self.model}'.")
        except openai.RateLimitError:
            return AIResult("", self.name, ok=False,
                            error="Rate limited (or out of quota). Try again shortly.")
        except openai.NotFoundError:
            return AIResult("", self.name, ok=False, error=self._missing_model_message())
        except openai.APIConnectionError:
            return AIResult("", self.name, ok=False, error=self._unreachable_message())
        except openai.APIStatusError as exc:
            return AIResult("", self.name, ok=False,
                            error=f"{self.name} API error {exc.status_code}.")
        except Exception as exc:   # last resort: never crash the worker
            return AIResult("", self.name, ok=False, error=f"Unexpected AI error: {exc}")

        text = (response.choices[0].message.content or "").strip() if response.choices else ""
        if not text:
            return AIResult("", self.name, ok=False, error="The model returned no text.")
        return AIResult(text, self.name)

    def _create(self, messages: list[dict]):
        """Send the request, coping with either output-token parameter name.

        Reasoning models reject `max_tokens` and older ones predate
        `max_completion_tokens`, so try the current name and fall back once.
        The working name is remembered for subsequent calls.
        """
        import openai

        client = self._ensure_client()
        try:
            return client.chat.completions.create(
                model=self.model,
                messages=messages,
                **{self._token_param: config.AI.max_tokens},
            )
        except openai.BadRequestError as exc:
            other = ("max_tokens" if self._token_param == "max_completion_tokens"
                     else "max_completion_tokens")
            if self._token_param not in str(exc):
                raise
            self._token_param = other
            return client.chat.completions.create(
                model=self.model,
                messages=messages,
                **{other: config.AI.max_tokens},
            )


class OllamaEngine(OpenAIEngine):
    """Ollama backend — local by default, or a remote/cloud host with a key.

    Ollama serves an OpenAI-compatible API at `/v1`, so this reuses the OpenAI
    request path wholesale and only swaps the endpoint, credentials and the
    wording of failures.

    A **local** Ollama has no authentication, so no key is needed and none is
    checked; the SDK still requires the field to be non-empty, hence the
    placeholder. A key is only meaningful for Ollama's hosted service or a
    self-hosted instance behind an authenticating proxy — set OLLAMA_API_KEY
    and it is sent as the bearer token.
    """

    name = "ollama"

    def __init__(self, model: str | None = None) -> None:
        super().__init__(model or config.AI.ollama_model)
        # Ollama predates `max_completion_tokens`; start with the name it knows.
        self._token_param = "max_tokens"

    def _problem(self) -> str | None:
        # Nothing to validate up front: local needs no key, and a wrong key or a
        # stopped server surfaces on the request with a far more useful message.
        return None

    def _client_kwargs(self) -> dict:
        return {
            "base_url": config.ollama_base_url(),
            # Placeholder keeps the SDK happy when the server wants no auth.
            "api_key": config.ollama_api_key() or "ollama",
        }

    def _unreachable_message(self) -> str:
        host = config.ollama_base_url()
        if config.ollama_is_local():
            return (f"Could not reach Ollama at {host}. Start it with: ollama serve")
        return f"Could not reach the Ollama host at {host}."

    def _missing_model_message(self) -> str:
        if config.ollama_is_local():
            return (f"Model '{self.model}' is not pulled. Run: ollama pull {self.model}")
        return f"Model '{self.model}' is not available on this Ollama host."

    def _auth_message(self) -> str:
        if not config.ollama_api_key():
            return (
                "The Ollama host refused the request as unauthenticated. "
                "Set OLLAMA_API_KEY in .env."
            )
        return "Ollama rejected OLLAMA_API_KEY."


class ClaudeEngine(AIEngine):
    """Anthropic Claude backend.

    The SDK client is created lazily so an import or auth failure surfaces as a
    UI error rather than preventing the app from starting.
    """

    name = "claude"

    def __init__(self, model: str | None = None) -> None:
        self.model = model or config.AI.anthropic_model
        self._client = None
        self._init_error: str | None = None

    @property
    def available(self) -> bool:
        return config.anthropic_key_problem() is None and self._init_error is None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:
            self._init_error = "The 'anthropic' package is not installed."
            raise RuntimeError(self._init_error) from exc
        # Key comes from the environment — never hardcoded, never logged.
        self._client = anthropic.Anthropic(api_key=config.anthropic_api_key())
        return self._client

    def process(self, prompt: str, history: list[Turn] | None = None) -> AIResult:
        problem = config.anthropic_key_problem()
        if problem:
            return AIResult("", self.name, ok=False, error=problem)

        messages = []
        for turn in (history or [])[-config.AI.history_limit:]:
            if turn.content.strip():
                messages.append({"role": turn.role, "content": turn.content})
        messages.append({"role": "user", "content": prompt})

        try:
            import anthropic
        except ImportError:
            return AIResult(
                "", self.name, ok=False,
                error="The 'anthropic' package is not installed (pip install anthropic).",
            )

        try:
            client = self._ensure_client()
            response = client.messages.create(
                model=self.model,
                max_tokens=config.AI.max_tokens,
                system=config.AI.system_prompt,
                messages=messages,
            )
        except anthropic.AuthenticationError:
            return AIResult("", self.name, ok=False,
                            error="Authentication failed — check ANTHROPIC_API_KEY.")
        except anthropic.RateLimitError:
            return AIResult("", self.name, ok=False,
                            error="Rate limited by the API. Try again shortly.")
        except anthropic.NotFoundError:
            return AIResult("", self.name, ok=False,
                            error=f"Model '{self.model}' was not found for this key.")
        except anthropic.APIConnectionError:
            return AIResult("", self.name, ok=False,
                            error="Network error — could not reach the API.")
        except anthropic.APIStatusError as exc:
            return AIResult("", self.name, ok=False,
                            error=f"API error {exc.status_code}.")
        except Exception as exc:  # last resort: never crash the worker
            return AIResult("", self.name, ok=False, error=f"Unexpected AI error: {exc}")

        if response.stop_reason == "refusal":
            return AIResult(
                "I'm not able to answer that one.", self.name, ok=True,
            )
        text = "\n".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ).strip()
        if not text:
            return AIResult("", self.name, ok=False, error="The model returned no text.")
        return AIResult(text, self.name)


def create_engine() -> AIEngine:
    """Pick a backend from configuration, falling back to offline.

    `AURA_AI_PROVIDER=auto` (the default) uses whichever key is present,
    preferring OpenAI. Naming a provider explicitly never silently substitutes
    the other one — if its key is missing you get the offline engine and the
    reason, rather than an answer from a backend you did not choose.
    """
    provider = config.resolved_provider()
    if provider == "offline":
        problem = config.api_key_problem()
        return OfflineEngine(problem or "Offline mode is selected in configuration.")
    if provider == "ollama":
        return OllamaEngine()          # no key to validate; failures surface per-request
    if provider == "openai":
        problem = config.openai_key_problem()
        return OpenAIEngine() if problem is None else OfflineEngine(problem)
    if provider == "claude":
        problem = config.anthropic_key_problem()
        return ClaudeEngine() if problem is None else OfflineEngine(problem)
    return OfflineEngine(f"Unknown provider '{config.AI.provider}'.")
