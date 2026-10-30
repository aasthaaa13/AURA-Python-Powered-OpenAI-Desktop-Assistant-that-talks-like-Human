"""Controlled command system.

Safety model: the assistant never executes free text. Every action is a named
Python handler behind an explicit pattern match, arguments are validated, and
subprocesses are launched with argument lists (never `shell=True`). Anything
with a visible side effect is marked `requires_confirmation` and only runs after
the user explicitly confirms — AI-generated text can never trigger it directly.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, NamedTuple

import config
from system.system_info import format_duration, host_info


@dataclass
class CommandResult:
    handled: bool
    text: str = ""
    level: str = "assistant"
    requires_confirmation: bool = False
    action: Callable[[], str] | None = None
    speak: bool = True
    ui_action: str | None = None       # e.g. "clear_log", handled by the window


NOT_HANDLED = CommandResult(handled=False)

class AppTarget(NamedTuple):
    """One launchable app and every name a user might call it."""

    label: str                  # what AURA calls it back
    macos: str | None           # .app bundle name for `open -a`
    windows: str | None         # what `start` should launch
    linux: str | None           # command expected on PATH
    aliases: tuple[str, ...]


# Allow-list of launchable apps. Speech recognition splits and re-spells names
# unpredictably ("VS Code", "vs code", "vs-code"), so aliases are matched after
# squashing case, spaces and punctuation — "VS Code" and "vscode" are the same key.
_APPS: tuple[AppTarget, ...] = (
    AppTarget("the browser", None, None, None,
              ("browser", "web browser", "default browser", "internet")),
    AppTarget("Google Chrome", "Google Chrome", "chrome", "google-chrome",
              ("chrome", "google chrome", "chrome browser", "google")),
    AppTarget("Brave Browser", "Brave Browser", "brave", "brave-browser",
              ("brave", "brave browser")),
    AppTarget("Safari", "Safari", None, None, ("safari",)),
    AppTarget("Visual Studio Code", "Visual Studio Code", "code", "code",
              ("vscode", "vs code", "visual studio code", "visual studio",
               "code", "vs", "vsc", "v s code", "microsoft vs code")),
    AppTarget("Cursor", "Cursor", "cursor", "cursor",
              ("cursor", "cursor ide", "cursor editor")),
    AppTarget("Antigravity IDE", "Antigravity IDE", "antigravity", "antigravity",
              ("antigravity", "antigravity ide", "anti gravity")),
    AppTarget("Terminal", "Terminal", "wt", "x-terminal-emulator",
              ("terminal", "console", "shell", "command line")),
    AppTarget("Finder", "Finder", "explorer", "nautilus",
              ("finder", "files", "file manager", "explorer")),
    AppTarget("Spotify", "Spotify", "spotify", "spotify",
              ("spotify", "music", "spotify app")),
    AppTarget("Notion", "Notion", "notion", "notion",
              ("notion", "notion app")),
    AppTarget("Obsidian", "Obsidian", "obsidian", "obsidian",
              ("obsidian", "obsidian app", "my notes", "notes")),
    AppTarget("Claude", "Claude", "claude", "claude",
              ("claude", "claude app", "claude desktop")),
    AppTarget("Perplexity", "Perplexity", "perplexity", "perplexity",
              ("perplexity", "perplexity app")),
    AppTarget("Telegram", "Telegram", "telegram", "telegram-desktop",
              ("telegram", "telegram app")),
    AppTarget("WhatsApp", "WhatsApp", "whatsapp", "whatsapp",
              ("whatsapp", "whats app", "what's app", "whatsapp app")),
    AppTarget("Todoist", "Todoist", "todoist", "todoist",
              ("todoist", "to doist", "todo list", "tasks")),
    AppTarget("Zoom", "zoom.us", "zoom", "zoom",
              ("zoom", "zoom us", "zoom app", "zoom meeting")),
    AppTarget("VLC", "VLC", "vlc", "vlc", ("vlc", "vlc player", "video player")),
    AppTarget("Microsoft Excel", "Microsoft Excel", "excel", "libreoffice",
              ("excel", "microsoft excel", "ms excel", "spreadsheet")),
    AppTarget("Android Studio", "Android Studio", "studio64", "android-studio",
              ("android studio", "androidstudio")),
)


def _squash(text: str) -> str:
    """Reduce a spoken or typed app name to a comparable key.

    'VS Code', 'vs-code' and 'vscode' all collapse to 'vscode', which is what
    makes recognition spacing irrelevant.
    """
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _build_alias_index() -> dict[str, AppTarget]:
    index: dict[str, AppTarget] = {}
    for app in _APPS:
        for alias in (*app.aliases, app.label):
            key = _squash(alias)
            if key:
                index.setdefault(key, app)
    return index


_ALIAS_INDEX = _build_alias_index()

# "Open X" is rarely the whole sentence when spoken — recognisers return things
# like "aura open vs code" or "can you open chrome". Match the verb anywhere
# and take the rest as the target, so these never fall through to the AI engine.
_OPEN_RE = re.compile(
    r"\b(?:open|launch|start|run|fire\s+up|bring\s+up|pull\s+up)\s+(.+)$",
    re.IGNORECASE,
)

# Filler that speech recognition tends to add around the app name.
_OPEN_FILLER = re.compile(
    r"^(?:up\s+|the\s+|my\s+|an?\s+)+|"
    r"\s+(?:app|application|please|for me|now|window)\s*$",
    re.IGNORECASE,
)

_URL_RE = re.compile(r"^https?://[\w.-]+(?::\d+)?(?:/[^\s]*)?$", re.IGNORECASE)


class CommandRouter:
    """Matches user text against the supported command set."""

    def __init__(self, stats_provider: Callable[[], object] | None = None,
                 engine_name: Callable[[], str] | None = None) -> None:
        self._stats = stats_provider or (lambda: None)
        self._engine_name = engine_name or (lambda: "unknown")

    # -- public API ---------------------------------------------------------
    def route(self, text: str) -> CommandResult:
        query = (text or "").strip().lower().rstrip("?!.")
        if not query:
            return NOT_HANDLED

        for matcher, handler in self._table():
            if matcher(query):
                return handler(query)
        return NOT_HANDLED

    def help_text(self) -> str:
        return (
            "Supported commands: status · cpu · memory · disk · battery · network · "
            "time · date · uptime · sysinfo · engine · open <app or https URL> · "
            "sleep display · clear · quit · help. "
            "Anything else is sent to the AI engine."
        )

    # -- table --------------------------------------------------------------
    def _table(self) -> list[tuple[Callable[[str], bool], Callable[[str], CommandResult]]]:
        def has(*words: str) -> Callable[[str], bool]:
            return lambda q: any(w in q for w in words)

        def exact(*words: str) -> Callable[[str], bool]:
            return lambda q: q in words

        return [
            (exact("help", "commands", "what can you do"), self._cmd_help),
            (exact("clear", "clear log", "cls"), self._cmd_clear),
            (exact("quit", "exit", "shut down", "shutdown", "close",
                   "goodbye", "power down"), self._cmd_quit),
            # Checked early: an explicit "open ..." should always win over a
            # metric keyword that happens to appear in the same sentence.
            (lambda q: _OPEN_RE.search(q) is not None, self._cmd_open),
            (has("system status", "status report", "all systems"), self._cmd_status),
            (exact("status"), self._cmd_status),
            (has("cpu"), self._cmd_cpu),
            (has("memory", "ram"), self._cmd_memory),
            (has("disk", "storage"), self._cmd_disk),
            (has("battery", "power level"), self._cmd_battery),
            (has("network", "internet", "online"), self._cmd_network),
            (has("uptime"), self._cmd_uptime),
            (has("what time", "the time", "current time"), self._cmd_time),
            (exact("time"), self._cmd_time),
            (has("what date", "the date", "today's date"), self._cmd_date),
            (exact("date"), self._cmd_date),
            (has("sysinfo", "system info", "what machine", "host info"), self._cmd_sysinfo),
            (has("which engine", "what engine", "ai status"), self._cmd_engine),
            (exact("engine"), self._cmd_engine),
            (has("sleep display", "sleep the display", "lock screen"), self._cmd_sleep_display),
        ]

    # -- handlers -----------------------------------------------------------
    def _cmd_help(self, _query: str) -> CommandResult:
        return CommandResult(True, self.help_text(), speak=False)

    def _cmd_clear(self, _query: str) -> CommandResult:
        return CommandResult(True, "Log cleared.", ui_action="clear_log", speak=False)

    def _cmd_quit(self, _query: str) -> CommandResult:
        # The window closes only after this reply has been spoken (see
        # AssistantManager._finish), so keep the farewell short — every extra
        # word is extra delay before the app actually goes away.
        return CommandResult(True, f"Goodbye, {config.USER_NAME}.", ui_action="quit")

    def _cmd_status(self, _query: str) -> CommandResult:
        stats = self._stats()
        if stats is None:
            return CommandResult(True, "System metrics are not available yet.")
        return CommandResult(True, stats.summary_for_assistant())

    def _metric(self, attr: str, label: str, fmt: str = "{:.0f} percent") -> CommandResult:
        stats = self._stats()
        value = getattr(stats, attr, None) if stats is not None else None
        if value is None:
            return CommandResult(True, f"{label} is not available on this machine.")
        return CommandResult(True, f"{label} is at {fmt.format(value)}.")

    def _cmd_cpu(self, _q: str) -> CommandResult:
        return self._metric("cpu_percent", "CPU load")

    def _cmd_memory(self, _q: str) -> CommandResult:
        return self._metric("memory_percent", "Memory usage")

    def _cmd_disk(self, _q: str) -> CommandResult:
        return self._metric("disk_percent", "Disk usage")

    def _cmd_battery(self, _q: str) -> CommandResult:
        stats = self._stats()
        if stats is None or stats.battery_percent is None:
            return CommandResult(True, "No battery is present — running on AC power.")
        state = "charging" if stats.battery_plugged else "discharging"
        return CommandResult(
            True, f"Battery at {stats.battery_percent:.0f} percent and {state}."
        )

    def _cmd_network(self, _q: str) -> CommandResult:
        stats = self._stats()
        if stats is None or stats.net_online is None:
            return CommandResult(True, "Network state is unknown.")
        return CommandResult(
            True, "Network interfaces are up." if stats.net_online
            else "No network interface is up."
        )

    def _cmd_uptime(self, _q: str) -> CommandResult:
        stats = self._stats()
        uptime = getattr(stats, "app_uptime", 0.0) if stats is not None else 0.0
        return CommandResult(True, f"Interface uptime is {format_duration(uptime)}.")

    def _cmd_time(self, _q: str) -> CommandResult:
        return CommandResult(True, f"The time is {datetime.now().strftime('%H:%M')}.")

    def _cmd_date(self, _q: str) -> CommandResult:
        return CommandResult(True, f"Today is {datetime.now().strftime('%A, %d %B %Y')}.")

    def _cmd_sysinfo(self, _q: str) -> CommandResult:
        info = host_info()
        cores = f"{info.cpu_cores} logical cores" if info.cpu_cores else "unknown core count"
        return CommandResult(
            True,
            f"Running on {info.hostname}: {info.summary}, {cores}, "
            f"Python {info.python}.",
        )

    def _cmd_engine(self, _q: str) -> CommandResult:
        return CommandResult(True, f"The active AI engine is {self._engine_name()}.")

    def _cmd_open(self, query: str) -> CommandResult:
        match = _OPEN_RE.search(query)
        target = (match.group(1) if match else "").strip()
        if not target:
            return CommandResult(True, "Tell me what to open.", level="warn")

        if _URL_RE.match(target):
            url = target
            return CommandResult(
                True,
                f"Open {url} in the browser?",
                requires_confirmation=True,
                action=lambda: _open_url(url),
            )

        # Strip filler, then match on the squashed key so spacing, case and
        # punctuation from speech recognition are all irrelevant.
        cleaned = _OPEN_FILLER.sub("", target).strip()
        app = _ALIAS_INDEX.get(_squash(cleaned)) or _ALIAS_INDEX.get(_squash(target))
        if app is None:
            names = ", ".join(sorted({a.label for a in _APPS}))
            return CommandResult(
                True,
                f"'{target}' is not on the allowed list. I can open: {names} — "
                "or any https URL.",
                level="warn",
            )
        # No confirmation for an allow-listed app: launching one is benign and
        # trivially reversible, and this only ever runs on the user's own typed
        # or spoken input — AI output never reaches the router. The reply is the
        # action's own result, so it reports failure honestly too.
        return CommandResult(True, "", action=lambda: _open_app(app))

    def _cmd_sleep_display(self, _q: str) -> CommandResult:
        return CommandResult(
            True,
            "Put the display to sleep?",
            requires_confirmation=True,
            action=_sleep_display,
        )


# -- side-effecting helpers (only ever reached after confirmation) -----------
def _open_url(url: str) -> str:
    try:
        webbrowser.open(url)
    except Exception as exc:
        return f"Could not open the browser: {exc}"
    return f"Opened {url}."


def _open_app(app: AppTarget) -> str:
    quiet = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    try:
        if sys.platform == "darwin":
            if app.macos is None:      # the generic "browser" entry
                webbrowser.open("about:blank")
                return "Opened the browser."
            result = subprocess.run(
                ["open", "-a", app.macos], capture_output=True, text=True, check=False
            )
            if result.returncode != 0:
                return f"{app.label} does not appear to be installed."
        elif sys.platform == "win32":
            if app.windows is None:
                webbrowser.open("about:blank")
                return "Opened the browser."
            subprocess.Popen(["cmd", "/c", "start", "", app.windows], **quiet)
        else:
            if app.linux is None:
                webbrowser.open("about:blank")
                return "Opened the browser."
            exe = shutil.which(app.linux)
            if not exe:
                return f"{app.label} was not found on this system."
            subprocess.Popen([exe], **quiet)
    except OSError as exc:
        return f"Could not launch {app.label}: {exc}"
    return f"Opened {app.label}."


def _sleep_display() -> str:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["pmset", "displaysleepnow"], stderr=subprocess.DEVNULL)
        elif sys.platform == "win32":
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command",
                 "(Add-Type '[DllImport(\"user32.dll\")]"
                 "public static extern int SendMessage(int hWnd, int hMsg, int wParam,"
                 " int lParam);' -Name W -PassThru)::SendMessage(-1,0x0112,0xF170,2)"],
                stderr=subprocess.DEVNULL,
            )
        else:
            exe = shutil.which("xset")
            if not exe:
                return "No display control tool is available."
            subprocess.Popen([exe, "dpms", "force", "off"], stderr=subprocess.DEVNULL)
    except OSError as exc:
        return f"Could not sleep the display: {exc}"
    return "Display going to sleep."


def platform_label() -> str:
    return f"{platform.system()} {platform.release()}".strip()
