from datetime import date, timedelta

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..errors import DomainError
from ..models import Assignment, Employee, Shift
from ..schemas import AssignmentUpdate
from ..services import schedule_service
from ..services.calendar_service import month_bounds
from . import theme
from .searchable_combo import SearchableComboBox

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

ROLE_KIND = Qt.UserRole + 1
ROLE_CODE = Qt.UserRole + 2
ROLE_SUB = Qt.UserRole + 3
ROLE_BG = Qt.UserRole + 4
ROLE_FG = Qt.UserRole + 5
ROLE_LOCKED = Qt.UserRole + 6

KIND_WORK = "work"
KIND_OFF = "off"
KIND_LEAVE = "leave"
KIND_EMPTY = "empty"
KIND_CUSTOM = "custom"


def _mix(color: QColor, target: QColor, ratio: float) -> QColor:
    """按 ratio 把 color 向 target 混合，越接近 1 越像 target。"""
    ratio = max(0.0, min(1.0, ratio))
    return QColor(
        round(color.red() * (1 - ratio) + target.red() * ratio),
        round(color.green() * (1 - ratio) + target.green() * ratio),
        round(color.blue() * (1 - ratio) + target.blue() * ratio),
    )


def _shift_colors(shift: Shift) -> tuple[QColor, QColor]:
    base = QColor(shift.color or theme.PRIMARY)
    if not base.isValid():
        base = QColor(theme.PRIMARY)
    background = _mix(base, QColor("#ffffff"), 0.86)
    foreground = _mix(base, QColor("#000000"), 0.28)
    return background, foreground


def month_days(month: str) -> list[tuple[str, str, bool]]:
    """返回 (日期, 星期标签, 是否周末)。月份为空时返回空列表。"""
    if not month or "-" not in month:
        return []
    start_text, end_text = month_bounds(month)
    start = date.fromisoformat(start_text)
    end = date.fromisoformat(end_text)
    days: list[tuple[str, str, bool]] = []
    current = start
    while current <= end:
        weekday = current.weekday()  # 周一=0
        days.append((current.isoformat(), WEEKDAY_NAMES[weekday], weekday >= 5))
        current += timedelta(days=1)
    return days


class ScheduleHeader(QHeaderView):
    """排班网格表头：周末列浅红底，仅保留底部细线。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Horizontal, parent)
        self.weekend_columns: set[int] = set()
        self.setFixedHeight(44)
        self.setSectionsClickable(False)

    def paintSection(self, painter: QPainter, rect, logical_index: int) -> None:  # noqa: N802
        painter.save()
        if logical_index in self.weekend_columns:
            painter.fillRect(rect, QColor("#fee2e2"))
        else:
            painter.fillRect(rect, QColor("#ffffff"))
        text = self.model().headerData(logical_index, Qt.Horizontal, Qt.DisplayRole) or ""
        font = painter.font()
        font.setPixelSize(11)
        font.setBold(False)
        painter.setFont(font)
        if logical_index in self.weekend_columns:
            painter.setPen(QColor("#9f1239"))
        else:
            painter.setPen(QColor(theme.TEXT_SECONDARY))
        painter.drawText(rect.adjusted(0, 4, 0, -3), Qt.AlignCenter, str(text))
        painter.setPen(QColor(theme.BORDER))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.restore()


class ShiftCellDelegate(QStyledItemDelegate):
    """班次单元格：浅色圆角胶囊 + 时间/工时小字，白底细横线。"""

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802
        painter.save()
        rect = option.rect
        painter.fillRect(rect, QColor("#ffffff"))
        painter.setPen(QColor("#f1f2f4"))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())

        kind = index.data(ROLE_KIND)
        if kind is None:
            # 员工列 / 合计列
            font = painter.font()
            font.setPixelSize(12)
            font.setBold(index.column() == 0)
            painter.setFont(font)
            painter.setPen(QColor(theme.TEXT if index.column() == 0 else theme.TEXT_SECONDARY))
            align = (Qt.AlignVCenter | Qt.AlignLeft) if index.column() == 0 else Qt.AlignCenter
            padding = rect.adjusted(10, 0, -6, 0) if index.column() == 0 else rect
            painter.drawText(padding, align, str(index.data(Qt.DisplayRole) or ""))
            painter.restore()
            return

        code = str(index.data(ROLE_CODE) or "")
        sub = str(index.data(ROLE_SUB) or "")
        background = QColor(index.data(ROLE_BG) or "#f3f4f6")
        foreground = QColor(index.data(ROLE_FG) or theme.TEXT_SECONDARY)
        locked = bool(index.data(ROLE_LOCKED))

        pill = QRectF(rect.center().x() - 23, rect.top() + 6, 46, 20)
        painter.setPen(Qt.NoPen)
        painter.setBrush(background)
        painter.drawRoundedRect(pill, 5, 5)
        if locked:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(theme.PRIMARY), 1.4))
            painter.drawRoundedRect(pill.adjusted(0.7, 0.7, -0.7, -0.7), 5, 5)

        font = painter.font()
        font.setPixelSize(10)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(foreground)
        painter.drawText(pill, Qt.AlignCenter, code)

        if sub:
            font.setPixelSize(9)
            font.setBold(False)
            painter.setFont(font)
            painter.setPen(QColor(theme.TEXT_SECONDARY))
            sub_rect = QRectF(rect.left() + 2, pill.bottom() + 1, rect.width() - 4, 24)
            painter.drawText(sub_rect, Qt.AlignHCenter | Qt.AlignTop, sub)
        painter.restore()

    def sizeHint(self, option, index):  # noqa: N802
        if index.data(ROLE_KIND) is None:
            return QSize(80, 44)
        sub = str(index.data(ROLE_SUB) or "")
        return QSize(64, 60 if sub else 40)


class CellDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        employee_name: str,
        day: str,
        shifts: list[Shift],
        assignment: Assignment | None,
    ) -> None:
        super().__init__(parent)
        self.assignment = assignment
        self.setWindowTitle("调整排班")
        self.setMinimumWidth(420)

        self.shift_combo = SearchableComboBox(placeholder="输入班次代码搜索…")
        self.shift_combo.append_item("休息（清空当天排班）", 0, pinned=True)
        if assignment is not None and assignment.shift_id is None:
            self.shift_combo.append_item("保留当前自定义时间", -2, pinned=True)
        for shift in shifts:
            if not shift.enabled and (assignment is None or shift.id != assignment.shift_id):
                continue
            if shift.is_off:
                label = f"{shift.name}（休息 · 0h）"
            elif shift.is_leave:
                label = f"{shift.name}（有薪休假 · 8h）"
            else:
                label = f"{shift.name}  {shift.start_time}-{shift.end_time}"
            self.shift_combo.append_item(label, shift.id)
        if assignment is not None and assignment.shift_id is not None:
            index = self.shift_combo.findData(assignment.shift_id)
            if index >= 0:
                self.shift_combo.setCurrentIndex(index)

        self.locked_check = QCheckBox("锁定（重新求解时保留当天安排）")
        self.locked_check.setChecked(bool(assignment and assignment.locked))

        form = QFormLayout()
        form.addRow("班次", self.shift_combo)
        form.addRow("", self.locked_check)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        header = QLabel(f"{employee_name} · {day}")
        header.setStyleSheet("font-weight: 600;")
        layout.addWidget(header)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        theme.set_role(buttons.button(QDialogButtonBox.Ok), "primary")
        layout.addWidget(buttons)

    def choice(self) -> int:
        data = self.shift_combo.currentData()
        return int(data) if data is not None else 0


class ScheduleGrid(QWidget):
    """员工 × 日期 的排班网格；点单元格可手工调整。"""

    changed = Signal()

    def __init__(self, plan_id: int, month: str) -> None:
        super().__init__()
        self.plan_id = plan_id
        self.month = month
        self._employees: list[Employee] = []
        self._shifts: list[Shift] = []
        self._shift_map: dict[int, Shift] = {}
        self._assignments: dict[tuple[int, str], Assignment] = {}
        self._days = month_days(month)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.hint = QLabel("")
        self.hint.setProperty("class", "hint")
        layout.addWidget(self.hint)

        self.table = QTableWidget()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setMinimumSectionSize(44)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setItemDelegate(ShiftCellDelegate(self.table))
        self.table.setHorizontalHeader(ScheduleHeader(self.table))
        self.table.cellClicked.connect(self._open_cell)
        layout.addWidget(self.table)
        self._setup_columns()

    def set_month(self, month: str) -> None:
        if month == self.month and self.table.columnCount():
            return
        self.month = month
        self._days = month_days(month)
        self._setup_columns()

    def _fit_height(self) -> None:
        header_h = self.table.horizontalHeader().height()
        body_h = sum(
            self.table.verticalHeader().sectionSize(row)
            for row in range(self.table.rowCount())
        )
        self.table.setFixedHeight(header_h + body_h + self.table.frameWidth() * 2 + 2)

    def _setup_columns(self) -> None:
        header = self.table.horizontalHeader()
        self.table.setColumnCount(len(self._days) + 2)
        headers = ["员工"] + [f"{day[5:]}\n{weekday}" for day, weekday, _ in self._days] + ["合计"]
        self.table.setHorizontalHeaderLabels(headers)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(len(self._days) + 1, QHeaderView.ResizeToContents)
        weekend_columns: set[int] = set()
        for column in range(1, len(self._days) + 1):
            header.setSectionResizeMode(column, QHeaderView.Fixed)
            self.table.setColumnWidth(column, 64)
            if self._days[column - 1][2]:
                weekend_columns.add(column)
        if isinstance(header, ScheduleHeader):
            header.weekend_columns = weekend_columns
            header.update()

    def set_data(
        self,
        employees: list[Employee],
        shifts: list[Shift],
        assignments: list[Assignment],
    ) -> None:
        self._employees = employees
        self._shifts = shifts
        self._shift_map = {shift.id: shift for shift in shifts if shift.id is not None}
        self._assignments = {
            (item.employee_id, item.date): item for item in assignments
        }

        self.table.setUpdatesEnabled(False)
        self.table.clearContents()
        self.table.setRowCount(len(self._employees))
        for row, employee in enumerate(self._employees):
            name_item = QTableWidgetItem(employee.name)
            name_item.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(row, 0, name_item)
            total = 0.0
            for column, (day, _weekday, _weekend) in enumerate(self._days, start=1):
                item = self._assignments.get((employee.id, day))
                cell = QTableWidgetItem()
                if item is None:
                    cell.setData(ROLE_KIND, KIND_EMPTY)
                    cell.setData(ROLE_CODE, "休")
                    cell.setData(ROLE_BG, "#f3f4f6")
                    cell.setData(ROLE_FG, theme.TEXT_SECONDARY)
                    cell.setToolTip("当天未排班")
                else:
                    shift = self._shift_map.get(item.shift_id) if item.shift_id else None
                    self._apply_assignment_roles(cell, item, shift)
                    cell.setToolTip(
                        f"{item.start_time}-{item.end_time} · {item.hours:g}h"
                        + ("（已锁定）" if item.locked else "")
                    )
                    total += item.hours
                self.table.setItem(row, column, cell)
            total_item = QTableWidgetItem(f"{total:.1f}h")
            total_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, len(self._days) + 1, total_item)
        self.table.setUpdatesEnabled(True)
        self.table.resizeRowsToContents()
        self._fit_height()
        self.hint.setText(
            f"共 {len(self._employees)} 人 · {len(self._days)} 天 · "
            f"{len(self._assignments)} 条排班；点单元格可手工调整"
            if self._employees
            else "还没有人员，请先在「人员与班次」中添加"
        )

    def _apply_assignment_roles(
        self, cell: QTableWidgetItem, item: Assignment, shift: Shift | None
    ) -> None:
        cell.setData(ROLE_LOCKED, bool(item.locked))
        if shift is None:
            cell.setData(ROLE_KIND, KIND_CUSTOM)
            cell.setData(ROLE_CODE, "自定义")
            cell.setData(ROLE_SUB, f"{item.start_time}-{item.end_time}\n{item.hours:g}h")
            cell.setData(ROLE_BG, "#f3f4f6")
            cell.setData(ROLE_FG, theme.TEXT)
            return
        if shift.is_off:
            cell.setData(ROLE_KIND, KIND_OFF)
            cell.setData(ROLE_CODE, shift.name or "OFF")
            cell.setData(ROLE_SUB, "休息\n0h")
            cell.setData(ROLE_BG, "#e5e7eb")
            cell.setData(ROLE_FG, "#4b5563")
            return
        if shift.is_leave:
            cell.setData(ROLE_KIND, KIND_LEAVE)
            cell.setData(ROLE_CODE, shift.name or "假")
            cell.setData(ROLE_SUB, f"有薪假\n{item.hours:g}h")
            cell.setData(ROLE_BG, "#dbeafe")
            cell.setData(ROLE_FG, "#1d4ed8")
            return
        background, foreground = _shift_colors(shift)
        cell.setData(ROLE_KIND, KIND_WORK)
        cell.setData(ROLE_CODE, shift.name)
        cell.setData(ROLE_SUB, f"{item.start_time}-{item.end_time}\n{item.hours:g}h")
        cell.setData(ROLE_BG, background.name())
        cell.setData(ROLE_FG, foreground.name())

    def _open_cell(self, row: int, column: int) -> None:
        if not self._employees or row >= len(self._employees):
            return
        if column < 1 or column > len(self._days):
            return
        employee = self._employees[row]
        day = self._days[column - 1][0]
        assignment = self._assignments.get((employee.id, day))

        dialog = CellDialog(self, employee.name, day, self._shifts, assignment)
        if dialog.exec() != QDialog.Accepted:
            return
        choice = dialog.choice()
        locked = dialog.locked_check.isChecked()
        try:
            if choice == 0:
                schedule_service.clear_assignment(self.plan_id, employee.id, day)
            elif choice == -2:
                if assignment is not None:
                    schedule_service.update_assignment(
                        assignment.id,
                        AssignmentUpdate(
                            shift_id=None,
                            start_time=assignment.start_time,
                            end_time=assignment.end_time,
                            locked=locked,
                        ),
                    )
            else:
                schedule_service.set_assignment(
                    self.plan_id, employee.id, day, choice, locked=locked
                )
        except DomainError as error:
            QMessageBox.warning(self, "调整失败", str(error))
            return
        self.changed.emit()
