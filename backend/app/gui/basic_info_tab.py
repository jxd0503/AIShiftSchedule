from PySide6.QtCore import QDate, QTime, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from ..errors import DomainError
from ..models import Plan
from ..services import plans_service
from . import theme
from .calendar_popup import MonthPopupDialog


def _to_hhmm(edit: QTimeEdit) -> str:
    return edit.time().toString("HH:mm")


def _set_hhmm(edit: QTimeEdit, value: str) -> None:
    t = QTime.fromString(value, "HH:mm")
    if t.isValid():
        edit.setTime(t)


class BasicInfoTab(QWidget):
    saved = Signal(object)  # Plan

    def __init__(self, plan_id: int) -> None:
        super().__init__()
        self.plan_id = plan_id
        self._plan: Plan | None = None
        self._build_ui()
        self._load()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 6, 20, 8)
        outer.setSpacing(8)

        card = theme.card_frame()
        card.setMinimumWidth(560)
        card.setMaximumWidth(780)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(24, 12, 24, 12)
        card_layout.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(5)
        form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)

        self.name_edit = QLineEdit()
        self.name_edit.setMinimumWidth(300)
        self.month_btn = QPushButton()
        self.month_btn.setFixedWidth(150)
        self.month_btn.setCursor(Qt.PointingHandCursor)
        self.month_btn.clicked.connect(self._pick_month)
        self._month_date = QDate.currentDate()

        self.soft_start = QTimeEdit()
        self.soft_start.setDisplayFormat("HH:mm")
        self.core_start = QTimeEdit()
        self.core_start.setDisplayFormat("HH:mm")
        self.core_end = QTimeEdit()
        self.core_end.setDisplayFormat("HH:mm")
        self.soft_end = QTimeEdit()
        self.soft_end.setDisplayFormat("HH:mm")
        for edit in (self.soft_start, self.core_start, self.core_end, self.soft_end):
            edit.setFixedWidth(130)

        self.core_min = QSpinBox()
        self.core_min.setRange(1, 999)
        self.max_consecutive = QSpinBox()
        self.max_consecutive.setRange(1, 31)
        for spin in (self.core_min, self.max_consecutive):
            spin.setFixedWidth(110)

        self.overnight_next_rest = QCheckBox("跨午夜班次日自动休息")
        self.monthly_hours_hard = QCheckBox("月工时按硬约束精确达成")
        self.monthly_hours_hard.setToolTip(
            "开启后，人员档案中的月工时必须精确达成（不再允许软偏离）；"
            "若因休息/每日上限/连续上班上限无法达成，求解前会给出可达工时与缺口。"
        )

        self.solve_first_phase = QSpinBox()
        self.solve_first_phase.setRange(1, 3600)
        self.solve_max = QSpinBox()
        self.solve_max.setRange(1, 7200)
        self.solve_first_phase.setFixedWidth(110)
        self.solve_max.setFixedWidth(110)

        form.addRow("名称", self.name_edit)
        form.addRow("月份", self.month_btn)
        form.addRow("软覆盖起点", self.soft_start)
        form.addRow("核心覆盖起点", self.core_start)
        form.addRow("核心覆盖终点", self.core_end)
        form.addRow("软覆盖终点", self.soft_end)
        form.addRow("核心最少人数", self.core_min)
        form.addRow("连续上班上限", self.max_consecutive)
        form.addRow("", self.monthly_hours_hard)
        form.addRow("", self.overnight_next_rest)
        form.addRow("段A求解秒数", self.solve_first_phase)
        form.addRow("段B保底秒数（持续优化，仅无解时兜底）", self.solve_max)
        card_layout.addLayout(form)

        save_row = QHBoxLayout()
        save_btn = QPushButton("保存")
        save_btn.setProperty("class", "primary")
        save_btn.clicked.connect(self._save)
        save_row.addWidget(save_btn)
        save_row.addStretch()
        card_layout.addLayout(save_row)

        # 空间不足时整卡滚动，避免输入框被压缩导致文字被裁切
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(card)
        outer.addWidget(scroll, 1)

    def _load(self) -> None:
        try:
            plan = plans_service.get_plan(self.plan_id)
        except DomainError as error:
            QMessageBox.warning(self, "加载失败", str(error))
            return
        self._plan = plan
        self.name_edit.setText(plan.name)
        month_date = QDate.fromString(plan.month + "-01", "yyyy-MM-dd")
        self._month_date = month_date if month_date.isValid() else QDate.currentDate()
        self._update_month_button()
        _set_hhmm(self.soft_start, plan.coverage_soft_start)
        _set_hhmm(self.core_start, plan.coverage_start)
        _set_hhmm(self.core_end, plan.coverage_end)
        _set_hhmm(self.soft_end, plan.coverage_soft_end)
        self.core_min.setValue(plan.core_min_count)
        self.max_consecutive.setValue(plan.max_consecutive_days)
        self.monthly_hours_hard.setChecked(plan.monthly_hours_hard)
        self.overnight_next_rest.setChecked(plan.overnight_next_rest)
        self.solve_first_phase.setValue(plan.solve_first_phase_seconds or 180)
        self.solve_max.setValue(plan.solve_max_seconds or 900)

    def _update_month_button(self) -> None:
        self.month_btn.setText(self._month_date.toString("yyyy-MM"))

    def _pick_month(self) -> None:
        chosen = MonthPopupDialog.pick(self.month_btn, self._month_date)
        if chosen is not None and chosen.isValid():
            self._month_date = chosen
            self._update_month_button()

    def _save(self) -> None:
        name = self.name_edit.text().strip()
        month = self._month_date.toString("yyyy-MM")
        if not name:
            QMessageBox.warning(self, "校验失败", "计划名称不能为空")
            return
        try:
            plan = plans_service.update_plan(
                self.plan_id,
                name=name,
                month=month,
                coverage_soft_start=_to_hhmm(self.soft_start),
                coverage_start=_to_hhmm(self.core_start),
                coverage_end=_to_hhmm(self.core_end),
                coverage_soft_end=_to_hhmm(self.soft_end),
                core_min_count=self.core_min.value(),
                max_consecutive_days=self.max_consecutive.value(),
                monthly_hours_hard=self.monthly_hours_hard.isChecked(),
                overnight_next_rest=self.overnight_next_rest.isChecked(),
                solve_first_phase_seconds=self.solve_first_phase.value(),
                solve_max_seconds=self.solve_max.value(),
            )
        except DomainError as error:
            QMessageBox.warning(self, "保存失败", str(error))
            return
        self._plan = plan
        QMessageBox.information(self, "已保存", "基本信息已保存")
        self.saved.emit(plan)
