from collections import defaultdict

from PySide6.QtCore import QDate, QTime, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from ..models import Employee, Rule, Shift
from ..schemas import RuleCreate
from ..services import employees_service, rules_service, shifts_service
from ..services.calendar_service import month_bounds
from . import theme
from .searchable_combo import SearchableComboBox

SCOPE_LABELS = {
    "single": "仅当天",
    "month": "整月",
    "workday": "整月工作日",
    "weekend": "整月周末",
}

DEMAND_TYPES = {
    "DAY_STAFF": "当天在岗人数（至少 N 人）",
    "SEGMENT_STAFF": "时段在岗人数（某时段至少 N 人）",
    "START_STAFF": "某时刻上班人数",
    "SHIFT_STAFF": "指定班次人数",
    "MAX_STAFF": "当天人数上限（至多 N 人）",
}

TIME_OP_LABELS = {"eq": "等于", "ge": "不早于", "le": "不晚于"}
COUNT_OP_LABELS = {"ge": "至少", "le": "至多"}
WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


class _CalendarTable(QTableWidget):
    """日历表格：支持用方向键选中日期、按 Enter 打开设置。"""

    day_activated = Signal(int, int)

    def keyPressEvent(self, event):  # noqa: N802 - 覆盖 Qt 命名
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            row, col = self.currentRow(), self.currentColumn()
            if row >= 0 and col >= 0:
                self.day_activated.emit(row, col)
                event.accept()
                return
        super().keyPressEvent(event)


def _time_widget(value: str = "08:00") -> QTimeEdit:
    edit = QTimeEdit()
    edit.setDisplayFormat("HH:mm")
    parsed = QTime.fromString(value, "HH:mm")
    if parsed.isValid():
        edit.setTime(parsed)
    return edit


class DayDemandDialog(QDialog):
    """为某一天（或整月 / 工作日 / 周末）批量添加日需求规则。"""

    def __init__(
        self,
        parent: QWidget,
        plan_id: int,
        month: str,
        day: str,
        employees: list[Employee],
        shifts: list[Shift],
    ) -> None:
        super().__init__(parent)
        self.plan_id = plan_id
        self.month = month
        self.day = day
        self.setWindowTitle(f"日需求 · {day}")
        self.setMinimumWidth(460)

        self.scope_combo = QComboBox()
        for key, label in SCOPE_LABELS.items():
            self.scope_combo.addItem(label, key)

        self.type_combo = QComboBox()
        for key, label in DEMAND_TYPES.items():
            self.type_combo.addItem(label, key)

        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 99)
        self.count_spin.setValue(1)
        self.count_label = QLabel("人数")

        self.segment_start = _time_widget("08:00")
        self.segment_end = _time_widget("10:00")
        self.start_single = _time_widget("08:00")

        self.shift_combo = SearchableComboBox(placeholder="输入班次代码搜索…")
        self.shift_combo.set_items([(shift.name, shift.id, False) for shift in shifts])

        self.time_op = QComboBox()
        for key, label in TIME_OP_LABELS.items():
            self.time_op.addItem(label, key)
        self.count_op = QComboBox()
        for key, label in COUNT_OP_LABELS.items():
            self.count_op.addItem(label, key)

        self.form = QFormLayout()
        self._rows: dict[str, int] = {}
        self._build_rows()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.addLayout(self.form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        theme.set_role(buttons.button(QDialogButtonBox.Ok), "primary")

        self.type_combo.currentIndexChanged.connect(self._apply_type)
        self._apply_type()

    def _add(self, key: str, label: str | QWidget, widget: QWidget) -> None:
        self._rows[key] = self.form.rowCount()
        self.form.addRow(label, widget)

    def _build_rows(self) -> None:
        self._add("type", "需求类型", self.type_combo)
        self._add("scope", "应用范围", self.scope_combo)

        segment_row = QWidget()
        segment_lay = QHBoxLayout(segment_row)
        segment_lay.setContentsMargins(0, 0, 0, 0)
        segment_lay.addWidget(self.segment_start)
        segment_lay.addWidget(QLabel("至"))
        segment_lay.addWidget(self.segment_end)
        segment_lay.addStretch()
        self._add("segment", "时段", segment_row)

        self._add("start_single", "上班时刻", self.start_single)
        self._add("time_op", "时刻条件", self.time_op)
        self._add("count_op", "人数条件", self.count_op)
        self._add("shift", "班次", self.shift_combo)
        self._add("count", self.count_label, self.count_spin)

    def _visible(self, key: str, visible: bool) -> None:
        self.form.setRowVisible(self._rows[key], visible)

    def current_type(self) -> str:
        data = self.type_combo.currentData()
        return str(data) if data else "DAY_STAFF"

    def _apply_type(self) -> None:
        rule_type = self.current_type()
        self._visible("segment", rule_type == "SEGMENT_STAFF")
        self._visible("start_single", rule_type == "START_STAFF")
        self._visible("time_op", rule_type == "START_STAFF")
        self._visible("count_op", rule_type == "START_STAFF")
        self._visible("shift", rule_type == "SHIFT_STAFF")
        if rule_type == "MAX_STAFF":
            self.count_label.setText("人数上限")
            self.count_spin.setRange(0, 99)
        else:
            self.count_label.setText("人数")
            self.count_spin.setRange(1, 99)

    def payloads(self) -> list[RuleCreate]:
        month_start, month_end = month_bounds(self.month)
        scope = str(self.scope_combo.currentData())
        rule_type = self.current_type()
        weekdays = None
        if scope == "single":
            start = end = self.day
        else:
            start, end = month_start, month_end
            if scope == "workday":
                weekdays = "1,2,3,4,5"
            elif scope == "weekend":
                weekdays = "6,7"
        return [
            RuleCreate(
                type=rule_type,  # pyright: ignore[reportArgumentType]
                date_start=start,
                date_end=end,
                count=self.count_spin.value(),
                start_time=(
                    self.segment_start.time().toString("HH:mm")
                    if rule_type == "SEGMENT_STAFF"
                    else (
                        self.start_single.time().toString("HH:mm")
                        if rule_type == "START_STAFF"
                        else None
                    )
                ),
                end_time=(
                    self.segment_end.time().toString("HH:mm")
                    if rule_type == "SEGMENT_STAFF"
                    else None
                ),
                shift_id=(
                    self.shift_combo.currentData() if rule_type == "SHIFT_STAFF" else None
                ),
                time_op=self.time_op.currentData() if rule_type == "START_STAFF" else None,
                count_op=self.count_op.currentData() if rule_type == "START_STAFF" else None,
                weekdays=weekdays,
            )
        ]


class DayDemandTab(QWidget):
    """日需求：当月日历，点任意一天设置当天（或整月）的人数需求。"""

    def __init__(self, plan_id: int, month: str) -> None:
        super().__init__()
        self.plan_id = plan_id
        self.month = month
        self._rules_by_day: dict[str, list[Rule]] = {}
        self._employees: list[Employee] = []
        self._shifts: list[Shift] = []
        self._default_cell: tuple[int, int] | None = None
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        header = QHBoxLayout()
        title = QLabel("点击日历中的日期，或用方向键选中后按 Enter，可为当天或整月设置人数需求")
        title.setProperty("class", "hint")
        header.addWidget(title)
        header.addStretch()
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh)
        header.addWidget(refresh_btn)
        layout.addLayout(header)

        self.table = _CalendarTable(0, 7)
        self.table.setHorizontalHeaderLabels(WEEKDAY_NAMES)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignCenter)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.table.setFocusPolicy(Qt.StrongFocus)
        self.table.setToolTip("点击日期，或用方向键选择、按 Enter 打开日需求设置")
        self.table.cellClicked.connect(self._open_day)
        self.table.day_activated.connect(self._open_day)
        layout.addWidget(self.table, 1)

        legend = QLabel("提示：已有需求的日期会以浅粉色标记，悬停可查看具体需求。")
        legend.setProperty("class", "hint")
        layout.addWidget(legend)

        self._build_calendar()

    def set_month(self, month: str) -> None:
        if month == self.month:
            return
        self.month = month
        self._build_calendar()
        self.refresh()

    def _build_calendar(self) -> None:
        if not self.month or "-" not in self.month:
            self.table.setRowCount(0)
            self._default_cell = None
            return
        month_start, month_end = month_bounds(self.month)
        first = QDate.fromString(month_start, "yyyy-MM-dd")
        last = QDate.fromString(month_end, "yyyy-MM-dd")
        days = first.daysTo(last) + 1
        offset = first.dayOfWeek() - 1  # 周一为 0
        rows = (offset + days + 6) // 7
        self.table.setRowCount(rows)
        for row in range(rows):
            self.table.setRowHeight(row, 64)
        self._cells: dict[str, tuple[int, int]] = {}
        for position in range(days):
            current = first.addDays(position)
            index = offset + position
            row, col = divmod(index, 7)
            self._cells[current.toString("yyyy-MM-dd")] = (row, col)
        for col in (5, 6):
            header_item = self.table.horizontalHeaderItem(col)
            if header_item is not None:
                header_item.setForeground(QColor("#b91c1c"))
        self._default_cell = self._cells.get(month_start)

    def refresh(self) -> None:
        try:
            rules = rules_service.list_rules(self.plan_id)
            self._employees = employees_service.list_employees(self.plan_id)
            self._shifts = shifts_service.list_shifts(self.plan_id)
        except Exception as error:  # noqa: BLE001 - 加载失败统一提示
            QMessageBox.warning(self, "加载失败", str(error))
            return

        demand_types = set(DEMAND_TYPES)
        by_day: dict[str, list[Rule]] = defaultdict(list)
        for rule in rules:
            if not rule.valid or rule.type not in demand_types:
                continue
            start = QDate.fromString(rule.date_start, "yyyy-MM-dd")
            end = QDate.fromString(rule.date_end, "yyyy-MM-dd")
            if not start.isValid() or not end.isValid():
                continue
            current = start
            while current <= end:
                key = current.toString("yyyy-MM-dd")
                if key in self._cells:
                    by_day[key].append(rule)
                current = current.addDays(1)
        self._rules_by_day = by_day

        self.table.clearContents()
        for day, (row, col) in self._cells.items():
            day_rules = by_day.get(day, [])
            label = day[8:].lstrip("0") or day[8:]
            text = f"{label}\n{len(day_rules)} 项需求" if day_rules else label
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignTop | Qt.AlignLeft)
            if day_rules:
                item.setBackground(QColor("#fdf2f8"))
                item.setForeground(QColor("#9d174d"))
                item.setToolTip("\n".join(_describe(rule) for rule in day_rules))
            elif col >= 5:
                item.setForeground(QColor("#b91c1c"))
            self.table.setItem(row, col, item)
        # 默认把焦点放在 1 号，便于直接用方向键选择日期
        if self._default_cell is not None and self.table.currentRow() < 0:
            self.table.setCurrentCell(*self._default_cell)

    def _open_day(self, row: int, col: int) -> None:
        day = next(
            (key for key, pos in self._cells.items() if pos == (row, col)), None
        )
        if day is None:
            return
        dialog = DayDemandDialog(
            self, self.plan_id, self.month, day, self._employees, self._shifts
        )
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            rules_service.create_rules(self.plan_id, dialog.payloads())
        except rules_service.RuleConflictError as error:
            detail = rules_service.conflict_error_text(error.items)
            ret = QMessageBox.question(
                self,
                "约束冲突",
                f"{error}\n{detail}\n\n是否仍然写入？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ret != QMessageBox.Yes:
                return
            try:
                rules_service.create_rules(self.plan_id, dialog.payloads(), force=True)
            except Exception as retry_error:  # noqa: BLE001
                QMessageBox.warning(self, "保存失败", str(retry_error))
                return
        except Exception as error:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", str(error))
            return
        self.refresh()


def _describe(rule: Rule) -> str:
    label = DEMAND_TYPES.get(rule.type, rule.type)
    if rule.type == "SEGMENT_STAFF":
        return f"{rule.start_time}-{rule.end_time} {label} {rule.count}"
    if rule.type == "START_STAFF":
        return f"{rule.start_time} 上班{label} {rule.count}"
    if rule.type == "SHIFT_STAFF":
        return f"{label} {rule.count}"
    return f"{label} {rule.count}"
