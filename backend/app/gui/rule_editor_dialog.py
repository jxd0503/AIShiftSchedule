from PySide6.QtCore import QDate, QTime
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from ..errors import DomainError
from ..models import TYPE_DEFAULT_HARD, Employee, Rule, Shift
from ..schemas import RuleCreate
from ..services.calendar_service import month_bounds
from ..services.rule_fields import (
    DAY_COUNT_TYPES,
    ROLE_TYPES,
    SINGLE_ROLE_TYPES,
    WEEKDAY_TYPES,
    count_bounds,
)
from . import theme
from .searchable_combo import SearchableComboBox

RULE_TYPE_LABELS: dict[str, str] = {
    "DAY_STAFF": "每天在岗人数",
    "SEGMENT_STAFF": "时段在岗人数",
    "START_STAFF": "某时刻上班人数",
    "END_STAFF": "某时刻下班人数",
    "SHIFT_STAFF": "指定班次人数",
    "MAX_STAFF": "当天人数上限",
    "REST": "指定休息日",
    "MUST_WORK": "指定上班日",
    "PAID_LEAVE": "有薪休假",
    "FIXED_SHIFT": "指定班次",
    "FIXED_TIME": "指定上下班时刻",
    "ALL_START_AT": "全员同一上班时刻",
    "SHIFT_HOURS": "班次工时条件",
    "MAX_REST_DAYS": "连续休息上限",
    "MIN_REST_DAYS": "最少休息天数",
    "MAX_WORK_DAYS": "最多上班天数",
    "MAX_CONSECUTIVE_WORK": "连续上班上限",
    "MONTHLY_HOURS": "月度总工时",
    "BAN_SHIFT": "禁止班次",
    "SHIFT_LIMIT": "班次限制（仅上班日）",
    "SINGLE_ROLE_SHIFT": "岗位单人班次",
    "WORK_WINDOW": "工作时段窗",
}

EMPLOYEE_TYPES = {
    "REST",
    "MUST_WORK",
    "PAID_LEAVE",
    "FIXED_SHIFT",
    "FIXED_TIME",
    "SHIFT_LIMIT",
    "WORK_WINDOW",
}
SHIFT_REQUIRED = {"FIXED_SHIFT", "SHIFT_STAFF", "BAN_SHIFT", "SINGLE_ROLE_SHIFT"}
TIME_SINGLE = {"FIXED_TIME", "ALL_START_AT", "START_STAFF"}

TIME_OP_LABELS = {"eq": "等于", "le": "不晚于", "ge": "不早于"}
COUNT_OP_LABELS = {"ge": "至少", "le": "至多"}
HOURS_OP_LABELS = {"eq": "等于", "ge": "不少于", "le": "不超过"}
PERIOD_LABELS = {"week": "每周", "month": "每月"}

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def type_label(rule_type: str) -> str:
    return RULE_TYPE_LABELS.get(rule_type, rule_type)


def _time_edit(value: str) -> QTimeEdit:
    edit = QTimeEdit()
    edit.setDisplayFormat("HH:mm")
    parsed = QTime.fromString(value, "HH:mm")
    if parsed.isValid():
        edit.setTime(parsed)
    return edit


def _count_label_for(rule_type: str) -> str | None:
    if rule_type in ("DAY_STAFF", "SEGMENT_STAFF", "SHIFT_STAFF", "END_STAFF"):
        return "人数"
    if rule_type == "START_STAFF":
        return "人数"
    if rule_type == "MAX_STAFF":
        return "人数上限"
    if rule_type == "SINGLE_ROLE_SHIFT":
        return "人数阈值（≤N 人时生效）"
    if rule_type == "PAID_LEAVE":
        return "休假天数"
    if rule_type == "MAX_REST_DAYS":
        return "连休上限（天）"
    if rule_type == "MAX_CONSECUTIVE_WORK":
        return "连续上班上限（天）"
    if rule_type == "MIN_REST_DAYS":
        return "最少休息（天）"
    if rule_type == "MAX_WORK_DAYS":
        return "最多上班（天）"
    if rule_type == "MONTHLY_HOURS":
        return "目标工时（小时）"
    return None


def _count_range(rule_type: str) -> tuple[int, int]:
    if rule_type == "PAID_LEAVE":
        return (1, 31)
    return count_bounds(rule_type)


class RuleEditorDialog(QDialog):
    """完整规则编辑器：固定行 + 按类型显隐，避免重建控件。"""

    def __init__(
        self,
        parent: QWidget | None,
        month: str,
        employees: list[Employee],
        shifts: list[Shift],
        rule: Rule | None = None,
        default_type: str = "DAY_STAFF",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑约束" if rule else "新增约束")
        self.setMinimumWidth(500)
        self._month = month
        self._rows: dict[str, int] = {}
        self._widgets: list[QWidget] = []

        start_text, end_text = month_bounds(month)
        min_date = QDate.fromString(start_text, "yyyy-MM-dd")
        max_date = QDate.fromString(end_text, "yyyy-MM-dd")

        self.type_combo = QComboBox()
        for key, label in RULE_TYPE_LABELS.items():
            self.type_combo.addItem(f"{label}（{key}）", key)

        self.employee_combo = QComboBox()
        for employee in employees:
            self.employee_combo.addItem(employee.name, employee.id)
        if not employees:
            # 没有人员时不要留空白下拉框，明确提示去「人员与班次」补人
            self.employee_combo.setPlaceholderText("（暂无人员，请先在「人员与班次」中添加）")
            self.employee_combo.setCurrentIndex(-1)

        self.shift_combo = SearchableComboBox(placeholder="输入班次代码搜索…")
        self.shift_combo.set_items([(shift.name, shift.id, False) for shift in shifts])

        # 岗位/分组：可下拉选择现有人员岗位，也可直接输入新岗位
        self.role_combo = QComboBox()
        self.role_combo.setEditable(True)
        self.role_combo.setInsertPolicy(QComboBox.NoInsert)
        self.role_combo.addItem("（不限岗位）", None)
        self.role_combo.lineEdit().setPlaceholderText("输入或选择岗位")
        seen_roles: set[str] = set()
        for employee in employees:
            role = (employee.role or "").strip()
            if role and role not in seen_roles:
                seen_roles.add(role)
                self.role_combo.addItem(role, role)

        # 班次限制：勾选表示「只能上该班次」，不勾选表示「按上班时刻限制」
        self.limit_shift_check = QCheckBox("限定具体班次（不勾选则按上班时刻限制）")

        self.no_date_check = QCheckBox("不指定日期（整月随机分散）")

        self.date_start = QDateEdit(min_date)
        self.date_end = QDateEdit(max_date)
        for edit in (self.date_start, self.date_end):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
            edit.setMinimumDate(min_date)
            edit.setMaximumDate(max_date)

        self.weekday_boxes = [QCheckBox(name) for name in WEEKDAY_NAMES]

        self.time_single = _time_edit("08:00")
        self.segment_start = _time_edit("08:00")
        self.segment_end = _time_edit("10:00")
        self.end_single = _time_edit("17:00")

        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 99)
        self.count_label = QLabel("人数")

        self.hard_combo = QComboBox()
        self.hard_combo.addItem("硬约束（必须满足）", True)
        self.hard_combo.addItem("软约束（尽量满足）", False)

        self.hours_op = QComboBox()
        for key, label in HOURS_OP_LABELS.items():
            self.hours_op.addItem(label, key)
        self.work_hours = QDoubleSpinBox()
        self.work_hours.setRange(0.5, 24)
        self.work_hours.setDecimals(1)
        self.work_hours.setValue(8.0)

        self.time_op = QComboBox()
        for key, label in TIME_OP_LABELS.items():
            self.time_op.addItem(label, key)
        self.count_op = QComboBox()
        for key, label in COUNT_OP_LABELS.items():
            self.count_op.addItem(label, key)

        self.period_combo = QComboBox()
        for key, label in PERIOD_LABELS.items():
            self.period_combo.addItem(label, key)

        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("可选备注")

        self.form = QFormLayout()
        self._build_rows()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.addLayout(self.form)
        note_row = QHBoxLayout()
        note_row.addWidget(QLabel("备注"))
        note_row.addWidget(self.note_edit)
        layout.addLayout(note_row)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        theme.set_role(buttons.button(QDialogButtonBox.Ok), "primary")

        if rule is not None:
            self._load_rule(rule)
        else:
            index = self.type_combo.findData(default_type)
            if index >= 0:
                self.type_combo.setCurrentIndex(index)
        self.type_combo.currentIndexChanged.connect(self._apply_type)
        self.limit_shift_check.toggled.connect(self._apply_type)
        self.no_date_check.toggled.connect(self._apply_no_date)
        self._apply_type()

    # ---------------------------------------------------------------- 表单构建
    def _add(self, key: str, label: str | QWidget, widget: QWidget) -> None:
        self._rows[key] = self.form.rowCount()
        self.form.addRow(label, widget)
        self._widgets.append(widget)

    def _build_rows(self) -> None:
        self._add("type", "类型", self.type_combo)
        self._add("employee", "人员", self.employee_combo)
        self._add("role", "岗位", self.role_combo)
        self._add("no_date", "", self.no_date_check)

        date_row = QWidget()
        date_lay = QHBoxLayout(date_row)
        date_lay.setContentsMargins(0, 0, 0, 0)
        date_lay.addWidget(self.date_start)
        date_lay.addWidget(QLabel("至"))
        date_lay.addWidget(self.date_end)
        date_lay.addStretch()
        self._add("date", "日期范围", date_row)

        weekday_row = QWidget()
        weekday_lay = QHBoxLayout(weekday_row)
        weekday_lay.setContentsMargins(0, 0, 0, 0)
        for box in self.weekday_boxes:
            weekday_lay.addWidget(box)
        weekday_lay.addStretch()
        self._add("weekday", "限定星期", weekday_row)

        self._add("shift", "班次", self.shift_combo)
        self._add("limit_shift", "", self.limit_shift_check)
        self._add("time_single", "上班时刻", self.time_single)

        segment_row = QWidget()
        segment_lay = QHBoxLayout(segment_row)
        segment_lay.setContentsMargins(0, 0, 0, 0)
        segment_lay.addWidget(self.segment_start)
        segment_lay.addWidget(QLabel("至"))
        segment_lay.addWidget(self.segment_end)
        segment_lay.addStretch()
        self._add("segment", "时段", segment_row)

        self._add("end_single", "下班时刻", self.end_single)
        self._add("time_op", "时刻条件", self.time_op)
        self._add("count_op", "人数条件", self.count_op)
        self._add("count", self.count_label, self.count_spin)

        hours_row = QWidget()
        hours_lay = QHBoxLayout(hours_row)
        hours_lay.setContentsMargins(0, 0, 0, 0)
        hours_lay.addWidget(self.hours_op)
        hours_lay.addWidget(self.work_hours)
        hours_lay.addWidget(QLabel("小时"))
        hours_lay.addStretch()
        self._add("hours", "工时条件", hours_row)

        self._add("period", "统计周期", self.period_combo)
        self._add("hard", "硬软", self.hard_combo)

    def _visible(self, key: str, visible: bool) -> None:
        self.form.setRowVisible(self._rows[key], visible)

    def _apply_no_date(self, checked: bool) -> None:
        self.date_start.setEnabled(not checked)
        self.date_end.setEnabled(not checked)

    def _apply_type(self) -> None:
        rule_type = self.current_type()
        is_limit = rule_type == "SHIFT_LIMIT"
        limit_shift = is_limit and self.limit_shift_check.isChecked()
        self._visible("employee", rule_type in EMPLOYEE_TYPES)
        self._visible("role", rule_type in ROLE_TYPES or rule_type in SINGLE_ROLE_TYPES)
        self._visible("no_date", rule_type == "PAID_LEAVE")
        self._visible("date", True)
        self._visible("weekday", rule_type in WEEKDAY_TYPES)
        self._visible("shift", rule_type in SHIFT_REQUIRED or limit_shift)
        self._visible("limit_shift", is_limit)
        self._visible(
            "time_single",
            rule_type in TIME_SINGLE or (is_limit and not limit_shift),
        )
        self._visible("segment", rule_type in ("SEGMENT_STAFF", "WORK_WINDOW"))
        self._visible("end_single", rule_type in ("FIXED_TIME", "END_STAFF"))
        self._visible(
            "time_op", rule_type in ("START_STAFF", "END_STAFF", "SHIFT_LIMIT") and not limit_shift
        )
        self._visible("count_op", rule_type in ("START_STAFF", "END_STAFF"))
        self._visible("hours", rule_type == "SHIFT_HOURS")
        self._visible("period", rule_type in DAY_COUNT_TYPES)

        count_label = _count_label_for(rule_type)
        self._visible("count", count_label is not None)
        if count_label is not None:
            low, high = _count_range(rule_type)
            self.count_label.setText(count_label)
            self.count_spin.setRange(low, high)
        self._apply_no_date(self.no_date_check.isChecked())

    def current_type(self) -> str:
        data = self.type_combo.currentData()
        return str(data) if data else "DAY_STAFF"

    # ---------------------------------------------------------------- 载入与输出
    def _load_rule(self, rule: Rule) -> None:
        index = self.type_combo.findData(rule.type)
        if index >= 0:
            self.type_combo.setCurrentIndex(index)
        if rule.employee_id is not None:
            emp_index = self.employee_combo.findData(rule.employee_id)
            if emp_index >= 0:
                self.employee_combo.setCurrentIndex(emp_index)
        if rule.shift_id is not None:
            shift_index = self.shift_combo.findData(rule.shift_id)
            if shift_index >= 0:
                self.shift_combo.setCurrentIndex(shift_index)
            if rule.type == "SHIFT_LIMIT":
                self.limit_shift_check.setChecked(True)
        if rule.role:
            role_index = self.role_combo.findData(rule.role)
            if role_index >= 0:
                self.role_combo.setCurrentIndex(role_index)
            else:
                # 规则里的岗位可能已不在现有人员档案中，保留原值便于编辑
                self.role_combo.setCurrentText(rule.role)
        if rule.date_start:
            self.date_start.setDate(QDate.fromString(rule.date_start, "yyyy-MM-dd"))
        if rule.date_end:
            self.date_end.setDate(QDate.fromString(rule.date_end, "yyyy-MM-dd"))
        if rule.start_time:
            parsed = QTime.fromString(rule.start_time, "HH:mm")
            if parsed.isValid():
                self.time_single.setTime(parsed)
                self.segment_start.setTime(parsed)
        if rule.end_time:
            parsed_end = QTime.fromString(rule.end_time, "HH:mm")
            if parsed_end.isValid():
                self.segment_end.setTime(parsed_end)
                self.end_single.setTime(parsed_end)
        if rule.count is not None:
            self.count_spin.setValue(max(self.count_spin.minimum(), rule.count))
        if rule.work_hours is not None:
            self.work_hours.setValue(rule.work_hours)
        for combo, value in (
            (self.hours_op, rule.hours_op),
            (self.time_op, rule.time_op),
            (self.count_op, rule.count_op),
            (self.period_combo, rule.period),
        ):
            if value:
                idx = combo.findData(value)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
        self.hard_combo.setCurrentIndex(0 if rule.hard else 1)
        self.note_edit.setText(rule.note or "")
        if rule.weekdays:
            selected = {part.strip() for part in rule.weekdays.split(",")}
            for position, box in enumerate(self.weekday_boxes, start=1):
                box.setChecked(str(position) in selected)
        if rule.type == "PAID_LEAVE" and not rule.date_start:
            self.no_date_check.setChecked(True)

    def payload(self) -> RuleCreate:
        rule_type = self.current_type()
        if rule_type in EMPLOYEE_TYPES and self.employee_combo.count() == 0:
            raise DomainError("当前计划还没有人员，请先在「人员与班次」中添加人员")
        weekdays = None
        if rule_type in WEEKDAY_TYPES:
            picked = [
                str(index)
                for index, box in enumerate(self.weekday_boxes, start=1)
                if box.isChecked()
            ]
            weekdays = ",".join(picked) or None

        no_date = rule_type == "PAID_LEAVE" and self.no_date_check.isChecked()
        count_label = _count_label_for(rule_type)

        if rule_type in ("SEGMENT_STAFF", "WORK_WINDOW"):
            start_time = self.segment_start.time().toString("HH:mm")
            end_time = self.segment_end.time().toString("HH:mm")
        elif rule_type == "END_STAFF":
            start_time = None
            end_time = self.end_single.time().toString("HH:mm")
        elif rule_type == "SHIFT_LIMIT":
            if self.limit_shift_check.isChecked():
                start_time = None
                end_time = None
            else:
                start_time = self.time_single.time().toString("HH:mm")
                end_time = None
        elif rule_type in TIME_SINGLE:
            start_time = self.time_single.time().toString("HH:mm")
            end_time = (
                self.end_single.time().toString("HH:mm")
                if rule_type == "FIXED_TIME"
                else None
            )
        else:
            start_time = None
            end_time = None

        limit_uses_shift = rule_type == "SHIFT_LIMIT" and self.limit_shift_check.isChecked()
        role_text = self.role_combo.currentText().strip()
        if role_text == "（不限岗位）":
            role_text = ""

        return RuleCreate(
            type=rule_type,  # pyright: ignore[reportArgumentType]
            employee_id=(
                self.employee_combo.currentData() if rule_type in EMPLOYEE_TYPES else None
            ),
            date_start=None if no_date else self.date_start.date().toString("yyyy-MM-dd"),
            date_end=None if no_date else self.date_end.date().toString("yyyy-MM-dd"),
            shift_id=(
                self.shift_combo.currentData()
                if rule_type in SHIFT_REQUIRED or limit_uses_shift
                else None
            ),
            start_time=start_time,
            end_time=end_time,
            count=self.count_spin.value() if count_label is not None else None,
            hard=bool(self.hard_combo.currentData()),
            note=self.note_edit.text().strip() or None,
            work_hours=self.work_hours.value() if rule_type == "SHIFT_HOURS" else None,
            hours_op=self.hours_op.currentData() if rule_type == "SHIFT_HOURS" else None,
            weekdays=weekdays,
            time_op=(
                self.time_op.currentData()
                if rule_type in ("START_STAFF", "END_STAFF", "SHIFT_LIMIT") and not limit_uses_shift
                else None
            ),
            count_op=(
                self.count_op.currentData()
                if rule_type in ("START_STAFF", "END_STAFF")
                else None
            ),
            period=self.period_combo.currentData() if rule_type in DAY_COUNT_TYPES else None,
            role=(
                role_text
                if (rule_type in ROLE_TYPES or rule_type in SINGLE_ROLE_TYPES) and role_text
                else None
            ),
        )


def default_hard(rule_type: str) -> bool:
    return bool(TYPE_DEFAULT_HARD.get(rule_type, True))
