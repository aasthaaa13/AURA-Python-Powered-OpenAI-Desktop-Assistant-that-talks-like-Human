
#!/usr/bin/env python3
"""AURA HUD — entry point.

    python main.py

Starts the Qt application, applies the HUD palette and shows the main window.
Configuration and secrets come from the environment or a `.env` file next to
this script — see `.env.example`.
"""

from __future__ import annotations

import signal
import sys


def _check_dependencies() -> str | None:
    """Return a human-readable message if a hard requirement is missing."""
    try:
        import PySide6  # noqa: F401
    except ImportError:
        return (
            "PySide6 is required to run the interface.\n"
            "Install the dependencies with:\n"
            "    python -m pip install -r requirements.txt"
        )
    return None


def main() -> int:
    problem = _check_dependencies()
    if problem:
        print(problem, file=sys.stderr)
        return 1

    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QApplication

    from config import APP_NAME, START_FULLSCREEN, THEME, UI_MODE

    # High-DPI pixmaps are enabled by default in Qt 6 — no attribute needed.
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)

    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(THEME.bg))
    palette.setColor(QPalette.Base, QColor(THEME.bg_deep))
    palette.setColor(QPalette.Text, QColor(THEME.text))
    palette.setColor(QPalette.WindowText, QColor(THEME.text))
    palette.setColor(QPalette.Highlight, QColor(THEME.primary_soft))
    palette.setColor(QPalette.HighlightedText, QColor(THEME.bg_deep))
    app.setPalette(palette)

    # Ctrl+C in the launching terminal should close the window cleanly.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    if UI_MODE == "hud":
        from ui.main_window import MainWindow

        window = MainWindow()
        if START_FULLSCREEN:
            window.showFullScreen()   # F11 or Esc returns to a normal window
        else:
            window.show()
    else:
        from ui.mini_window import MiniWindow

        window = MiniWindow()
        _place_bottom_right(app, window)
        window.show()
    return app.exec()


def _place_bottom_right(app, window) -> None:
    """Park the orb out of the way, near the corner of the screen."""
    screen = app.primaryScreen()
    if screen is None:
        return
    area = screen.availableGeometry()
    margin = 32
    window.move(
        area.right() - window.width() - margin,
        area.bottom() - window.height() - margin,
    )


if __name__ == "__main__":
    sys.exit(main())
