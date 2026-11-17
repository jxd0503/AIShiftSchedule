from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .plan_list_view import PlanListView
from .settings_view import SettingsView
from .workers import wait_all
from .workspace_view import WorkspaceView


class MainWindow(QMainWindow):
    """主窗口：顶部品牌栏 + 计划列表 / 工作区 / 设置页切换。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AI 智能排班")
        self.resize(1000, 690)
        self.setMinimumSize(1000, 690)

        root = QWidget()
        root.setObjectName("PageRoot")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_header())

        self._stack = QStackedWidget()
        layout.addWidget(self._stack, 1)
        self.setCentralWidget(root)

        self.plan_list = PlanListView()
        self._stack.addWidget(self.plan_list)
        self.plan_list.open_plan_requested.connect(self._open_plan)
        self.plan_list.new_plan_created.connect(self._open_plan)
        self.plan_list.open_settings_requested.connect(self._open_settings)

        self.settings_view = SettingsView()
        self._stack.addWidget(self.settings_view)

        self._workspace: WorkspaceView | None = None

    # ------------------------------------------------------------------ 顶部栏
    def _build_header(self) -> QFrame:
        header = QFrame()
        header.setObjectName("AppHeader")
        header.setFixedHeight(56)

        row = QHBoxLayout(header)
        row.setContentsMargins(24, 0, 24, 0)
        row.setSpacing(12)

        logo = QLabel()
        pixmap = theme.logo_pixmap(22)
        if not pixmap.isNull():
            logo.setPixmap(pixmap)
        row.addWidget(logo)

        brand = QLabel("AI 智能排班")
        brand.setObjectName("AppBrand")
        row.addWidget(brand)
        row.addStretch()

        self.nav_plans = QPushButton("排班计划")
        self.nav_plans.setProperty("class", "nav")
        self.nav_plans.setCursor(Qt.PointingHandCursor)
        self.nav_plans.clicked.connect(self._show_plans)
        row.addWidget(self.nav_plans)

        self.nav_settings = QPushButton("模型设置")
        self.nav_settings.setProperty("class", "nav")
        self.nav_settings.setCursor(Qt.PointingHandCursor)
        self.nav_settings.clicked.connect(self._open_settings)
        row.addWidget(self.nav_settings)

        self._set_active_nav(self.nav_plans)
        return header

    def _set_active_nav(self, active: QPushButton) -> None:
        for button in (self.nav_plans, self.nav_settings):
            button.setProperty("active", "true" if button is active else "false")
            theme.repolish(button)

    # ------------------------------------------------------------------ 路由
    def _open_plan(self, plan_id: int) -> None:
        self._discard_workspace()
        self._workspace = WorkspaceView(plan_id)
        self._workspace.back_requested.connect(self._show_plans)
        self._stack.addWidget(self._workspace)
        self._stack.setCurrentWidget(self._workspace)
        self._set_active_nav(self.nav_plans)

    def _open_settings(self) -> None:
        self.settings_view.reload()
        self._stack.setCurrentWidget(self.settings_view)
        self._set_active_nav(self.nav_settings)

    def _show_plans(self) -> None:
        self._discard_workspace()
        self.plan_list.refresh()
        self._stack.setCurrentWidget(self.plan_list)
        self._set_active_nav(self.nav_plans)

    def _discard_workspace(self) -> None:
        if self._workspace is not None:
            self._workspace.shutdown()
            self._stack.removeWidget(self._workspace)
            self._workspace.deleteLater()
            self._workspace = None

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 回调命名
        if self._workspace is not None:
            self._workspace.shutdown()
        self.settings_view.shutdown()
        wait_all()
        super().closeEvent(event)
