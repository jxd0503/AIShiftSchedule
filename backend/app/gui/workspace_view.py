from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..errors import DomainError
from ..services import employees_service, plans_service, rules_service
from . import theme
from .ai_panel import AiPanel
from .basic_info_tab import BasicInfoTab
from .constraints_tab import ConstraintsTab
from .day_demand_tab import DayDemandTab
from .schedule_tab import ScheduleTab
from .staff_shift_tab import StaffShiftTab


class WorkspaceView(QWidget):
    back_requested = Signal()

    def __init__(self, plan_id: int) -> None:
        super().__init__()
        self.plan_id = plan_id
        self._month = ""
        self._build_ui()
        self._refresh_header()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 10, 24, 10)
        layout.setSpacing(6)

        back = QPushButton("‹ 返回")
        back.setProperty("class", "link")
        back.setCursor(Qt.PointingHandCursor)
        back.clicked.connect(self.back_requested.emit)
        layout.addWidget(back, 0, Qt.AlignLeft)

        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(4)
        self.title = QLabel()
        self.title.setObjectName("PageTitle")
        titles.addWidget(self.title)
        self.meta = QLabel()
        self.meta.setProperty("class", "hint")
        titles.addWidget(self.meta)
        title_row.addLayout(titles)
        title_row.addStretch()

        self.status_pill = QLabel("未排班")
        self.status_pill.setObjectName("StatusPill")
        self.status_pill.setAlignment(Qt.AlignCenter)
        title_row.addWidget(self.status_pill)

        self.solve_btn = QPushButton("求解排班")
        self.solve_btn.setProperty("class", "primary")
        self.solve_btn.clicked.connect(self._solve_from_header)
        title_row.addWidget(self.solve_btn)

        self.export_btn = QPushButton("导出 Excel")
        self.export_btn.clicked.connect(self._export_from_header)
        title_row.addWidget(self.export_btn)
        layout.addLayout(title_row)

        self.tabs = QTabWidget()
        self.basic_tab = BasicInfoTab(self.plan_id)
        self.basic_tab.saved.connect(self._on_saved)
        self.tabs.addTab(self.basic_tab, "基本信息")
        self.staff_tab = StaffShiftTab(self.plan_id)
        self.tabs.addTab(self.staff_tab, "人员与班次")
        try:
            plan = plans_service.get_plan(self.plan_id)
            self._month = plan.month
        except DomainError:
            self._month = ""
        self.day_tab = DayDemandTab(self.plan_id, self._month)
        self.tabs.addTab(self.day_tab, "日需求")
        self.constraints_tab = ConstraintsTab(self.plan_id)
        self.tabs.addTab(self.constraints_tab, "约束录入")
        self.schedule_tab = ScheduleTab(self.plan_id)
        self.schedule_tab.status_changed.connect(self._refresh_header)
        self.tabs.addTab(self.schedule_tab, "排班结果")
        self.ai_tab = AiPanel(self.plan_id)
        self.ai_tab.data_changed.connect(self._on_ai_changed)
        self.tabs.addTab(self.ai_tab, "AI 助手")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs, 1)
        self.tabs.tabBar().installEventFilter(self)

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self.tabs.tabBar() and event.type() == QEvent.Wheel:
            return True
        return super().eventFilter(obj, event)

    def shutdown(self) -> None:
        self.schedule_tab.shutdown()
        self.ai_tab.shutdown()

    # ------------------------------------------------------------------ 顶部信息
    def _refresh_header(self) -> None:
        try:
            plan = plans_service.get_plan(self.plan_id)
        except DomainError as error:
            self.title.setText("加载失败")
            self.meta.setText(str(error))
            return

        self.title.setText(plan.name)
        employee_count = 0
        rule_count = 0
        try:
            employee_count = len(employees_service.list_employees(self.plan_id))
            rule_count = len(rules_service.list_rules(self.plan_id))
        except Exception:  # noqa: BLE001 - 顶部摘要失败不影响主流程
            pass
        overnight = "开启" if plan.overnight_next_rest else "关闭"
        self.meta.setText(
            f"{plan.month} · 覆盖 {plan.coverage_soft_start}-{plan.coverage_soft_end}"
            f" · 跨午夜次日休息{overnight}"
            f" · {employee_count} 人 · {rule_count} 条规则"
        )

        solved = plan.status == "solved"
        self.status_pill.setText("已排班" if solved else "未排班")
        self.status_pill.setProperty("state", "success" if solved else "neutral")
        theme.repolish(self.status_pill)
        self.solve_btn.setText("重新求解" if solved else "求解排班")
        has_schedule = self.schedule_tab.has_schedule
        self.export_btn.setEnabled(has_schedule)
        self.export_btn.setToolTip(
            "" if has_schedule else "当前还没有排班结果，请先求解或手工调整后再导出"
        )

    def _solve_from_header(self) -> None:
        self.tabs.setCurrentWidget(self.schedule_tab)
        self.schedule_tab.start_solve()

    def _export_from_header(self) -> None:
        self.tabs.setCurrentWidget(self.schedule_tab)
        self.schedule_tab.export_excel()

    # ------------------------------------------------------------------ 事件
    def _on_tab_changed(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if widget is self.ai_tab:
            self.ai_tab.refresh_context()
        elif widget is self.constraints_tab:
            # 人员/班次可能在「人员与班次」页签或其它窗口被修改，进入时重新加载
            self.constraints_tab.refresh()

    def _on_saved(self, plan) -> None:
        self._month = plan.month
        self.day_tab.set_month(plan.month)
        self.schedule_tab.reload()
        self._refresh_header()

    def _on_ai_changed(self) -> None:
        self.constraints_tab.refresh()
        self.schedule_tab.reload()
        self.ai_tab.refresh_context()
        self._refresh_header()
