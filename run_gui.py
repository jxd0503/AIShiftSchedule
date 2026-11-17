"""桌面应用入口：python run_gui.py"""
import sys

from PySide6.QtWidgets import QApplication

from backend.app.db import init_db
from backend.app.gui import theme
from backend.app.gui.main_window import MainWindow
from backend.app.services.optimizer import mark_pending_candidates_stale


def main() -> None:
    init_db()
    mark_pending_candidates_stale()
    app = QApplication(sys.argv)
    app.setApplicationName("AI 智能排班")
    app.setWindowIcon(theme.app_icon())
    theme.install(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
