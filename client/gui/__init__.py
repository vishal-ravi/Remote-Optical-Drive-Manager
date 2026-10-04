"""PySide6 application entry point."""

from __future__ import annotations

import logging
import os
import sys
import traceback

log = logging.getLogger("rodm.client.gui")


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("QT_LOGGING_RULES", "*.debug=false")
    from PySide6.QtWidgets import QApplication

    from common.logging_setup import setup_logging
    from .main_window import MainWindow

    setup_logging("rodm", level=logging.INFO, console=False)
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Remote Optical Drive Manager")
    app.setOrganizationName("rodm")
    app.setStyle("Fusion")

    def excepthook(exc_type, exc, tb):  # pragma: no cover - UI safety net
        message = "".join(traceback.format_exception(exc_type, exc, tb))
        log.error("unhandled exception:\n%s", message)
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.critical(
            None,
            "Unexpected error",
            f"{exc_type.__name__}: {exc}\n\nDetails were written to the log.",
        )

    sys.excepthook = excepthook

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
