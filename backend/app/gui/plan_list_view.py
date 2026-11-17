from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..errors import DomainError
from ..models import Plan
from ..services import plans_service
from . import theme
from .calendar_popup import MonthPopup

STATUS_LABELS = {"draft": "未排班", "solved": "已排班"}


class MonthPickerDialog(QDialog):
    """用月份选择器新建计划的对话框，返回 YYYY-MM。"""

    def __init__(self, parent: QWidget, current: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("新建计划")
        self.setMinimumWidth(320)

        initial = None
        if current:
            parsed = QDate.fromString(current + "-01", "yyyy-MM-dd")
            if parsed.isValid():
                initial = parsed

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        self.calendar = MonthPopup(initial)
        self.calendar.month_selected.connect(self.accept)
        layout.addWidget(self.calendar)

        footer = QHBoxLayout()
        footer.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        footer.addWidget(cancel_btn)
        layout.addLayout(footer)

    def month(self) -> str:
        return self.calendar.selectedMonth().toString("yyyy-MM")


class PlanListView(QWidget):
    open_plan_requested = Signal(int)
    new_plan_created = Signal(int)
    open_settings_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._plans: list[Plan] = []
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        header = QHBoxLayout()
        header.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(4)
        title = QLabel("排班计划")
        title.setObjectName("PageTitle")
        titles.addWidget(title)
        subtitle = theme.hint_label("管理每月排班计划，双击一行进入工作区")
        subtitle.setWordWrap(False)
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch()

        del_btn = QPushButton("删除计划")
        del_btn.setProperty("class", "danger")
        del_btn.clicked.connect(self._delete_plan)
        header.addWidget(del_btn)

        new_btn = QPushButton("新建计划")
        new_btn.setProperty("class", "primary")
        new_btn.clicked.connect(self._create_plan)
        header.addWidget(new_btn)
        layout.addLayout(header)

        card = theme.card_frame()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(0)
        self.table = QTableWidget(0, 3)
        self.table.setObjectName("CardTable")
        self.table.setHorizontalHeaderLabels(["名称", "月份", "状态"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setMinimumSectionSize(110)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for col in (1, 2):
            item = self.table.horizontalHeaderItem(col)
            if item is not None:
                item.setTextAlignment(Qt.AlignCenter)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.cellDoubleClicked.connect(self._open_selected)
        card_layout.addWidget(self.table)
        layout.addWidget(card, 1)

    def refresh(self) -> None:
        try:
            self._plans = plans_service.list_plans()
        except DomainError as error:
            QMessageBox.warning(self, "加载失败", str(error))
            return
        self.table.setRowCount(len(self._plans))
        for row, plan in enumerate(self._plans):
            self.table.setItem(row, 0, QTableWidgetItem(plan.name))
            month_item = QTableWidgetItem(plan.month)
            month_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 1, month_item)
            status = QTableWidgetItem(STATUS_LABELS.get(plan.status, plan.status or "未排班"))
            status.setTextAlignment(Qt.AlignCenter)
            if plan.status == "solved":
                status.setForeground(QColor(theme.SUCCESS_FG))
            else:
                status.setForeground(QColor(theme.TEXT_SECONDARY))
            self.table.setItem(row, 2, status)

    def _selected_plan_id(self) -> int | None:
        row = self.table.currentRow()
        if 0 <= row < len(self._plans):
            return self._plans[row].id
        return None

    def _open_selected(self, _row: int, _col: int) -> None:
        plan_id = self._selected_plan_id()
        if plan_id is not None:
            self.open_plan_requested.emit(plan_id)

    def _create_plan(self) -> None:
        dialog = MonthPickerDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        month = dialog.month()
        try:
            plan = plans_service.create_plan(month=month)
        except DomainError as error:
            QMessageBox.warning(self, "创建失败", str(error))
            return
        except Exception as error:  # noqa: BLE001 - 未预期异常也要给出可见提示
            QMessageBox.warning(self, "创建失败", f"创建计划时出错：{error}")
            return
        self.refresh()
        self.new_plan_created.emit(plan.id or -1)

    def _delete_plan(self) -> None:
        plan_id = self._selected_plan_id()
        if plan_id is None:
            QMessageBox.information(self, "提示", "请先选中要删除的计划")
            return
        name = next((p.name for p in self._plans if p.id == plan_id), "")
        ret = QMessageBox.question(
            self,
            "删除计划",
            f"确定删除「{name}」？其人员、班次、规则与排班将一并删除，不可恢复。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        try:
            plans_service.delete_plan(plan_id)
        except DomainError as error:
            QMessageBox.warning(self, "删除失败", str(error))
            return
        self.refresh()
