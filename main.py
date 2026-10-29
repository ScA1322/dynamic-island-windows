"""Dynamic Island for Windows - entry point.

Usage:
    python main.py                 run normally (tray + overlay)
    python main.py --exit-after N  quit automatically after N seconds (smoke test)
    python main.py --debug         verbose logging
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app import APP_DISPLAY_NAME, APP_NAME, __version__
from app.config import logs_dir
from app.logger import setup_logging
from app.single_instance import ActivationServer, SingleInstance, notify_running_instance

log = logging.getLogger("main")


def _install_excepthooks() -> None:
    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = hook
    threading.excepthook = lambda args: log.critical(
        "Unhandled exception in thread %s", args.thread.name if args.thread else "?",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=APP_NAME)
    parser.add_argument("--exit-after", type=float, default=0, help="quit after N seconds (smoke test)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    log_file = setup_logging(logs_dir(), logging.DEBUG if args.debug else logging.INFO)
    _install_excepthooks()

    qapp = QApplication(sys.argv[:1])
    qapp.setApplicationName(APP_NAME)
    qapp.setApplicationDisplayName(APP_DISPLAY_NAME)
    qapp.setApplicationVersion(__version__)
    qapp.setQuitOnLastWindowClosed(False)

    instance = SingleInstance()
    if not instance.acquire():
        log.info("Already running; asking the running instance to open its settings")
        notify_running_instance()
        return 0

    from app.application import IslandApplication  # heavy imports after the instance check

    log.info("%s %s started (log: %s)", APP_DISPLAY_NAME, __version__, log_file)
    activation = ActivationServer()
    activation.listen()
    island = IslandApplication(qapp)
    activation.activated.connect(island.show_settings)
    qapp.aboutToQuit.connect(island.shutdown)
    island.start()

    if args.exit_after > 0:
        QTimer.singleShot(int(args.exit_after * 1000), island.quit)

    try:
        code = qapp.exec()
    finally:
        island.shutdown()
        activation.close()
        instance.release()
    log.info("Exited with code %s", code)
    return code


if __name__ == "__main__":
    sys.exit(main())
