"""Main HUD window: layout, styling and signal wiring.

Owns the single animation clock. Every widget that animates gets `tick(dt)` from
here, so frame cost is predictable and animation is independent of whatever the
assistant is doing on its worker threads.
"""

from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import QRectF, Qt, QTimer, Slot
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from assistant.assistant_manager import AssistantManager, AssistantState
from config import (
    APP_NAME,
    AUDIO_FPS,
    WAVEFORM_AUDIO_DIVIDER,
    STATE_COLORS,
    TARGET_FPS,
    THEME,
    WINDOW_MIN_SIZE,
    WINDOW_START_SIZE,
)
from system.system_info import format_duration, host_info
from system.system_monitor import SystemMonitor, SystemStats
from ui.hud import AICoreWidget
from ui.widgets import (
    GlassPanel,
    InputBar,
    LogPanel,
    StatGauge,
    StatusRow,
    SpacedLabel,
    WaveformWidget,
    display_font,
    mono_font,
)

STYLESHEET = f"""
QWidget {{
    color: {THEME.text};
    background: transparent;
}}
QFrame#glassPanel {{
    background: rgba(10, 21, 38, 0.55);
    border: 1px solid {THEME.panel_border};
    border-radius: 10px;
}}
QLabel#panelTitle {{
    color: {THEME.primary};
    padding-bottom: 2px;
    border-bottom: 1px solid {THEME.panel_border};
}}
QTextEdit#logPanel {{
    background: rgba(4, 10, 20, 0.66);
    border: 1px solid {THEME.panel_border};
    border-radius: 10px;
    padding: 8px 10px;
    selection-background-color: {THEME.primary_soft};
}}
QLineEdit#commandInput {{
    background: rgba(8, 20, 34, 0.85);
    border: 1px solid {THEME.panel_border};
    border-radius: 8px;
    padding: 0 14px;
    color: {THEME.text};
    selection-background-color: {THEME.primary_soft};
}}
QLineEdit#commandInput:focus {{
    border: 1px solid {THEME.primary};
}}
QPushButton#sendButton, QPushButton#micButton {{
    background: rgba(12, 34, 51, 0.9);
    border: 1px solid {THEME.panel_border};
    border-radius: 8px;
    color: {THEME.primary};
    padding: 0 16px;
}}
QPushButton#sendButton:hover, QPushButton#micButton:hover {{
    background: rgba(18, 52, 74, 0.95);
    border: 1px solid {THEME.primary};
}}
QPushButton#sendButton:pressed, QPushButton#micButton:pressed {{
    background: {THEME.primary_soft};
    color: {THEME.bg_deep};
}}
QPushButton#micButton[listening="true"] {{
    background: rgba(52, 211, 153, 0.22);
    border: 1px solid {THEME.ok};
    color: {THEME.ok};
}}
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 4px 0;
}}
QScrollBar::handle:vertical {{
    background: {THEME.panel_border};
    border-radius: 4px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {THEME.primary_soft}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QToolTip {{
    background: {THEME.panel};
    color: {THEME.text};
    border: 1px solid {THEME.primary_soft};
}}
"""


class HudBackground(QWidget):
    """Central widget painting the gradient backdrop and grid.

    The backdrop is static, but the panels above it are translucent, so every
    animated child repaint forces this to redraw underneath. Rasterising the
    gradient and grid each time costs ~45 ms at fullscreen retina — more than a
    whole frame budget. Caching it into a pixmap turns each frame into a blit.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cache: QPixmap | None = None

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._cache = None   # rebuilt lazily on the next paint

    def _build_cache(self) -> QPixmap:
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap(int(self.width() * ratio), int(self.height() * ratio))
        pixmap.setDevicePixelRatio(ratio)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, False)
        w, h = self.width(), self.height()

        backdrop = QLinearGradient(0, 0, 0, h)
        backdrop.setColorAt(0.0, QColor(THEME.bg))
        backdrop.setColorAt(1.0, QColor(THEME.bg_deep))
        painter.fillRect(0, 0, w, h, backdrop)

        grid = QColor(THEME.grid)
        grid.setAlphaF(0.55)
        painter.setPen(QPen(grid, 1))
        step = 44
        for x in range(0, w, step):
            painter.drawLine(x, 0, x, h)
        for y in range(0, h, step):
            painter.drawLine(0, y, w, y)
        painter.end()
        return pixmap

    def paintEvent(self, event) -> None:  # noqa: N802
        if self._cache is None or self._cache.isNull():
            self._cache = self._build_cache()
        painter = QPainter(self)
        # Draw the whole cache at the origin: the pixmap carries the device
        # pixel ratio, so this is a straight 1:1 copy and Qt clips it to the
        # damaged region for free. Passing an explicit device-pixel source rect
        # instead makes Qt *scale* the blit, which cost ~10 ms every frame.
        painter.drawPixmap(0, 0, self._cache)


class HeaderBar(QWidget):
    """Title, live status pill and clock."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(58)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)

        self.title = SpacedLabel(APP_NAME, padding=20)
        self.title.setFont(display_font(26, bold=True, spacing=6.0))
        self.title.setStyleSheet(f"color: {THEME.primary};")

        self.status = SpacedLabel("INITIALISING", padding=32)
        self.status.setFont(display_font(13, bold=True, spacing=3.0))
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setStyleSheet(f"color: {THEME.text_dim};")

        self.clock = SpacedLabel("--:--:--", padding=14)
        self.clock.setFont(mono_font(20, bold=True))
        self.clock.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.clock.setStyleSheet(f"color: {THEME.text};")

        layout.addWidget(self.title, 0)
        layout.addStretch(1)
        layout.addWidget(self.status, 0)
        layout.addStretch(1)
        layout.addWidget(self.clock, 0)

    def set_state(self, state: str) -> None:
        color = STATE_COLORS.get(state, THEME.primary)
        label = {"IDLE": "SYSTEM ONLINE", "SHUTDOWN": "POWERING DOWN"}.get(state, state)
        self.status.setText(label)   # SpacedLabel re-asks the layout for width
        self.status.setStyleSheet(f"color: {color};")

    def tick_clock(self) -> None:
        self.clock.setText(datetime.now().strftime("%H:%M:%S"))


class MainWindow(QMainWindow):
    """The HUD."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} — AI Command Interface")
        self.setMinimumSize(*WINDOW_MIN_SIZE)
        self.resize(*WINDOW_START_SIZE)
        self.setStyleSheet(STYLESHEET)

        self.monitor = SystemMonitor()
        self.assistant = AssistantManager(stats_provider=lambda: self.monitor.latest)

        self._build_ui()
        self._connect()

        self._wave_divider = 1
        self._wave_frames = 0
        self._metrics_signature: tuple = ()
        self._metrics_settle_frames = 60   # ~2s of close watching after startup
        self._last_frame = time.monotonic()
        self._frame_timer = QTimer(self)
        self._frame_timer.timeout.connect(self._on_frame)
        self._frame_timer.start(int(1000 / TARGET_FPS))

        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._on_second)
        self._clock_timer.start(1000)

        self.monitor.start()
        QTimer.singleShot(300, self.assistant.greet)

    # -- construction -------------------------------------------------------
    def _build_ui(self) -> None:
        root = HudBackground()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 14, 18, 16)
        outer.setSpacing(12)

        self.header = HeaderBar()
        outer.addWidget(self.header)

        # --- middle row: status | core | ai status
        middle = QHBoxLayout()
        middle.setSpacing(14)

        self.left_panel = self._build_status_panel()
        self.core = AICoreWidget()
        self.core.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.right_panel = self._build_ai_panel()

        middle.addWidget(self.left_panel, 3)
        middle.addWidget(self.core, 5)
        middle.addWidget(self.right_panel, 3)
        outer.addLayout(middle, 5)

        # --- waveform
        wave_panel = GlassPanel("Audio Spectrum")
        self.waveform = WaveformWidget()
        wave_panel.body().addWidget(self.waveform)
        outer.addWidget(wave_panel, 0)

        # --- log
        log_panel = GlassPanel("Activity Log")
        self.log = LogPanel()
        log_panel.body().addWidget(self.log)
        outer.addWidget(log_panel, 3)

        # --- input
        self.input_bar = InputBar()
        outer.addWidget(self.input_bar, 0)

    def _build_status_panel(self) -> GlassPanel:
        panel = GlassPanel("System Status")
        body = panel.body()

        self.gauge_cpu = StatGauge("CPU")
        self.gauge_mem = StatGauge("Memory")
        self.gauge_disk = StatGauge("Disk")
        for gauge in (self.gauge_cpu, self.gauge_mem, self.gauge_disk):
            body.addWidget(gauge)

        self.row_net = StatusRow("Network")
        self.row_batt = StatusRow("Power")
        self.row_uptime = StatusRow("Uptime")
        for row in (self.row_net, self.row_batt, self.row_uptime):
            body.addWidget(row)

        body.addStretch(1)
        return panel

    def _build_ai_panel(self) -> GlassPanel:
        panel = GlassPanel("AI Status")
        body = panel.body()

        self.row_engine = StatusRow("Engine")
        self.row_state = StatusRow("State")
        self.row_voice = StatusRow("Voice In")
        self.row_tts = StatusRow("Voice Out")
        self.row_host = StatusRow("Host")
        for row in (self.row_engine, self.row_state, self.row_voice,
                    self.row_tts, self.row_host):
            body.addWidget(row)

        hint = QLabel(
            "Type a command or press the mic.\n"
            "Try: status · time · uptime · help\n"
            "Type 'quit' to close.\n\n"
            "F11 fullscreen · Esc windowed\n"
            "Enter focus · Ctrl+Q quit"
        )
        hint.setFont(mono_font(11))
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {THEME.text_faint};")
        body.addSpacing(6)
        body.addWidget(hint)
        body.addStretch(1)

        info = host_info()
        self.row_host.set_value(info.hostname[:18], THEME.text_dim)
        name, status = self.assistant.engine_status()
        self.row_engine.set_value(f"{name} · {status}", THEME.primary)
        self.row_state.set_value("IDLE", THEME.primary)
        mic_problem = self.assistant.mic_problem()
        self.row_voice.set_value(
            "READY" if mic_problem is None else "UNAVAILABLE",
            THEME.ok if mic_problem is None else THEME.warn,
        )
        import config as _config
        self.row_tts.set_value(
            "ENABLED" if _config.TTS_ENABLED else "MUTED",
            THEME.ok if _config.TTS_ENABLED else THEME.text_dim,
        )
        return panel

    # -- wiring -------------------------------------------------------------
    def _connect(self) -> None:
        self.input_bar.submitted.connect(self.assistant.submit_text)
        self.input_bar.mic_toggled.connect(self._on_mic_toggled)

        self.assistant.state_changed.connect(self._on_state)
        self.assistant.message.connect(self._on_message)
        self.assistant.log.connect(self._on_log)
        self.assistant.error.connect(self._on_error)
        self.assistant.level_changed.connect(self._on_level)
        self.assistant.listening_changed.connect(self._on_listening_changed)
        self.assistant.ui_action.connect(self._on_ui_action)

        self.monitor.stats_ready.connect(self._on_stats)
        self.monitor.failed.connect(lambda msg: self._on_log(msg, "warn"))

    # -- frame clock --------------------------------------------------------
    def _on_frame(self) -> None:
        now = time.monotonic()
        dt = min(0.1, now - self._last_frame)   # clamp so a stall can't jump animation
        self._last_frame = now

        if not self.isVisible() or self.isMinimized():
            return   # don't burn CPU animating an invisible window

        # Watch text metrics closely while the window settles onto its screen,
        # then hand the job to the once-a-second tick.
        if self._metrics_settle_frames > 0:
            self._metrics_settle_frames -= 1
            self._refresh_text_metrics()

        self.core.tick(dt)
        self._wave_frames += 1
        if self._wave_frames % self._wave_divider == 0:
            # Scale dt by the divider so the bars still fall at the right speed.
            self.waveform.tick(dt * self._wave_divider)
        self.input_bar.tick(dt)
        for gauge in (self.gauge_cpu, self.gauge_mem, self.gauge_disk):
            gauge.tick(dt)

        if self.core.shutdown_complete:
            # Power-down finished: freeze the frame so the farewell that is still
            # being spoken gets the CPU to itself.
            self._frame_timer.stop()

    def _apply_frame_rate(self, state: str) -> None:
        """Slow the animation while audio is live.

        Painting a fullscreen retina window competes with the audio pipeline
        hard enough to distort the voice; see AUDIO_FPS in config.py for the
        measurements. Only the states that actually carry audio are throttled,
        so the HUD stays fluid the rest of the time.
        """
        if self.core.shutdown_complete:
            return
        # Recording is always live, so it stays sensitive to painting. Playback
        # only needs the throttle when it is streamed rather than buffered.
        audio_live = state == "LISTENING" or (
            state == "SPEAKING" and not self.assistant.speech.playback_is_buffered
        )
        fps = AUDIO_FPS if audio_live else TARGET_FPS
        # The spectrum spans the window, so its repaint costs far more than the
        # core's; thinning it out is what buys the core its higher rate.
        self._wave_divider = WAVEFORM_AUDIO_DIVIDER if audio_live else 1
        interval = max(1, int(1000 / fps))
        if self._frame_timer.interval() != interval:
            self._frame_timer.setInterval(interval)

    def _on_second(self) -> None:
        self.header.tick_clock()
        self._refresh_text_metrics()   # catches a move to a differently-scaled display
        stats = self.monitor.latest
        self.row_uptime.set_value(format_duration(stats.app_uptime), THEME.text_dim)

    # -- slots --------------------------------------------------------------
    @Slot(object)
    def _on_stats(self, stats: SystemStats) -> None:
        self.gauge_cpu.set_value(stats.cpu_percent)
        self.gauge_mem.set_value(stats.memory_percent, stats.memory_text)
        self.gauge_disk.set_value(stats.disk_percent, stats.disk_text)

        online = stats.net_online
        self.row_net.set_value(
            stats.network_text,
            THEME.ok if online else (THEME.error if online is False else THEME.text_dim),
        )
        if stats.battery_percent is None:
            self.row_batt.set_value("AC POWER", THEME.text_dim)
        else:
            low = stats.battery_percent < 20 and not stats.battery_plugged
            self.row_batt.set_value(
                stats.battery_text, THEME.warn if low else THEME.ok
            )

    @Slot(str)
    def _on_state(self, state: str) -> None:
        self._apply_frame_rate(state)
        self.core.set_state(state)
        self.waveform.set_state(state)
        self.header.set_state(state)
        self.row_state.set_value(state, STATE_COLORS.get(state, THEME.primary))

    @Slot(str, str, str)
    def _on_message(self, speaker: str, text: str, level: str) -> None:
        self.log.append_line(f"{speaker}:", text, level)

    @Slot(str, str)
    def _on_log(self, text: str, level: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.append_line(f"[{stamp}]", text, level)

    @Slot(str)
    def _on_error(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.append_line(f"[{stamp}] !", text, "error")

    @Slot(float)
    def _on_level(self, level: float) -> None:
        self.core.set_level(level)
        self.waveform.set_level(level)

    @Slot(bool)
    def _on_listening_changed(self, listening: bool) -> None:
        self.input_bar.mic.set_active(listening)
        if not listening and self.input_bar.mic.isChecked():
            # Recording finished on its own — release the toggle without recursing.
            self.input_bar.mic.blockSignals(True)
            self.input_bar.mic.setChecked(False)
            self.input_bar.mic.blockSignals(False)

    @Slot(bool)
    def _on_mic_toggled(self, checked: bool) -> None:
        if checked:
            # Works mid-reply too: start_listening cuts the speech off first.
            self.assistant.start_listening()
        else:
            self.assistant.cancel_listening()

    @Slot(str)
    def _on_ui_action(self, action: str) -> None:
        if action == "clear_log":
            self.log.clear()
        elif action == "quitting":
            # Play the power-down sequence. It collapses to nothing and then
            # freezes, which also hands the CPU back to the spoken farewell.
            self.core.start_shutdown()
            self.waveform.start_shutdown()
            self.header.set_state("SHUTDOWN")
            self.row_state.set_value("SHUTDOWN", THEME.error)
            self.input_bar.setEnabled(False)
        elif action == "quit":
            # Closing the last window ends the app; closeEvent does the cleanup.
            self.close()

    # -- text metrics -------------------------------------------------------
    def _measured_widgets(self) -> tuple:
        """Widgets whose width is derived from measured text."""
        return (self.header.title, self.header.status, self.header.clock,
                self.input_bar.mic)

    def _refresh_text_metrics(self) -> None:
        """Re-run the layout when measured text widths change.

        Font metrics shift after the window lands on its screen (HiDPI scaling)
        and again if it is dragged to a display with a different scale factor.
        Qt does not re-run the layout for that, so letter-spaced labels clip.
        Watching the measurement itself is self-correcting — no guessed delay,
        and moving between displays is handled for free.
        """
        widgets = self._measured_widgets()
        signature = tuple(
            QFontMetrics(w.font()).horizontalAdvance(w.text()) for w in widgets
        )
        if signature == self._metrics_signature:
            return
        self._metrics_signature = signature
        for widget in widgets:
            widget.updateGeometry()
        self.header.layout().activate()
        self.input_bar.layout().activate()

    # -- keyboard -----------------------------------------------------------
    def keyPressEvent(self, event) -> None:  # noqa: N802
        """F11 toggles fullscreen, Esc leaves it — never trap the user."""
        key = event.key()
        if key == Qt.Key_Q and event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier):
            self.close()
            return
        if key == Qt.Key_F11:
            self.toggle_fullscreen()
            return
        if key == Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
            self.resize(*WINDOW_START_SIZE)
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self.input_bar.field.setFocus()
            return
        super().keyPressEvent(event)

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self.resize(*WINDOW_START_SIZE)
        else:
            self.showFullScreen()

    # -- responsiveness -----------------------------------------------------
    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        width = self.width()
        # Collapse side panels on narrow windows so the core stays usable.
        self.right_panel.setVisible(width >= 900)
        self.left_panel.setVisible(width >= 720)
        self.waveform.setVisible(self.height() >= 560)

    # -- lifecycle ----------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802
        self._frame_timer.stop()
        self._clock_timer.stop()
        self.assistant.shutdown()
        self.monitor.stop()
        self.monitor.wait(2000)
        super().closeEvent(event)
