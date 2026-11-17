from PySide6.QtCore import QTime, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QFrame,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from ..errors import DomainError
from ..models import Employee, Shift
from ..services import employees_service, plans_service, shifts_service
from . import theme


def _time_edit(value: str) -> QTimeEdit:
    edit = QTimeEdit()
    edit.setDisplayFormat("HH:mm")
    parsed = QTime.fromString(value, "HH:mm")
    if parsed.isValid():
        edit.setTime(parsed)
    return edit


class EmployeeDialog(QDialog):
    def __init__(self, parent: QWidget, employee: Employee | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑人员" if employee else "新增人员")
        self.setMinimumWidth(340)

        self.name_edit = QLineEdit(employee.name if employee else "")
        self.monthly_hours = QDoubleSpinBox()
        self.monthly_hours.setRange(0, 1000)
        self.monthly_hours.setDecimals(1)
        self.monthly_hours.setSingleStep(8)
        self.monthly_hours.setValue(employee.monthly_hours if employee else 160)

        self.max_daily_hours = QDoubleSpinBox()
        self.max_daily_hours.setRange(0, 24)
        self.max_daily_hours.setDecimals(1)
        self.max_daily_hours.setSingleStep(0.5)
        self.max_daily_hours.setValue(employee.max_daily_hours if employee else 10)

        self.allow_special = QCheckBox("允许安排盘点等特殊班")
        self.allow_special.setChecked(employee.allow_special if employee else False)

        # 岗位/分组：人数类规则可按岗位统计（如「主管」）
        self.role_edit = QLineEdit(employee.role if employee and employee.role else "")
        self.role_edit.setPlaceholderText("如：主管 / 一般员工（可留空）")

        form = QFormLayout()
        form.addRow("姓名", self.name_edit)
        form.addRow("岗位", self.role_edit)
        form.addRow("月工时（软目标）", self.monthly_hours)
        form.addRow("日工时上限", self.max_daily_hours)
        form.addRow("", self.allow_special)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.addLayout(form)
        layout.addWidget(buttons)
        theme.set_role(buttons.button(QDialogButtonBox.Ok), "primary")


class ShiftDialog(QDialog):
    def __init__(self, parent: QWidget, shift: Shift | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑班次" if shift else "新增班次")
        self.setMinimumWidth(340)
        self._locked = bool(shift and (shift.is_off or shift.is_leave))

        self.name_edit = QLineEdit(shift.name if shift else "")
        self.start_edit = _time_edit(shift.start_time if shift else "08:00")
        self.end_edit = _time_edit(shift.end_time if shift else "17:00")
        self.work_hours = QDoubleSpinBox()
        self.work_hours.setRange(0, 24)
        self.work_hours.setDecimals(2)
        self.work_hours.setSingleStep(0.5)
        self.work_hours.setValue(shift.work_hours if shift and shift.work_hours else 8.0)
        self.special = QCheckBox("盘点等特殊班")
        self.special.setChecked(shift.is_special if shift else False)
        self.enabled = QCheckBox("启用")
        self.enabled.setChecked(shift.enabled if shift else True)

        if self._locked:
            for widget in (self.start_edit, self.end_edit, self.work_hours):
                widget.setEnabled(False)
            self.special.setEnabled(False)

        form = QFormLayout()
        form.addRow("班次名称（代码）", self.name_edit)
        form.addRow("起始时间", self.start_edit)
        form.addRow("终止时间", self.end_edit)
        form.addRow("工时", self.work_hours)
        form.addRow("", self.special)
        form.addRow("", self.enabled)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.addLayout(form)
        layout.addWidget(buttons)
        theme.set_role(buttons.button(QDialogButtonBox.Ok), "primary")


class EmployeeSection(QGroupBox):
    def __init__(self, plan_id: int) -> None:
        super().__init__("人员")
        self.plan_id = plan_id
        self._employees: list[Employee] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(10)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        add_btn = QPushButton("新增")
        add_btn.setProperty("class", "primary")
        add_btn.clicked.connect(self._add)
        edit_btn = QPushButton("编辑")
        edit_btn.clicked.connect(self._edit)
        del_btn = QPushButton("删除")
        del_btn.clicked.connect(self._delete)
        toolbar.addWidget(add_btn)
        toolbar.addWidget(edit_btn)
        toolbar.addWidget(del_btn)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        self.empty_label = QLabel("还没有人员，点击上方「新增」添加第一位人员。")
        self.empty_label.setProperty("class", "hint")
        self.empty_label.setVisible(False)
        layout.addWidget(self.empty_label)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["姓名", "岗位", "月工时(软)", "日上限", "特殊班"])
        self.table.setToolTip(
            "月工时是软目标：求解器会尽量贴近，但不保证精确达成；"
            "若在「基本信息」开启「月工时按硬约束精确达成」，则会精确达成。"
        )
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.cellDoubleClicked.connect(lambda *_: self._edit())
        layout.addWidget(self.table)

        self.refresh()

    def refresh(self) -> None:
        try:
            self._employees = employees_service.list_employees(self.plan_id)
            plan = plans_service.get_plan(self.plan_id)
            hours_mode = "月工时(硬)" if plan.monthly_hours_hard else "月工时(软)"
        except DomainError as error:
            QMessageBox.warning(self, "加载失败", str(error))
            return
        self.table.setHorizontalHeaderLabels(
            ["姓名", "岗位", hours_mode, "日上限", "特殊班"]
        )
        self.table.setRowCount(len(self._employees))
        for row, employee in enumerate(self._employees):
            self.table.setItem(row, 0, QTableWidgetItem(employee.name))
            self.table.setItem(row, 1, QTableWidgetItem(employee.role or ""))
            self.table.setItem(row, 2, QTableWidgetItem(f"{employee.monthly_hours:g}"))
            self.table.setItem(row, 3, QTableWidgetItem(f"{employee.max_daily_hours:g}"))
            self.table.setItem(row, 4, QTableWidgetItem("是" if employee.allow_special else ""))
        self.table.setFixedHeight(
            self.table.horizontalHeader().height()
            + self.table.verticalHeader().defaultSectionSize() * max(1, self.table.rowCount())
            + self.table.frameWidth() * 2
            + 2
        )
        self.empty_label.setVisible(not self._employees)

    def _selected(self) -> Employee | None:
        row = self.table.currentRow()
        if 0 <= row < len(self._employees):
            return self._employees[row]
        return None

    def _add(self) -> None:
        dialog = EmployeeDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            employees_service.create_employee(
                self.plan_id,
                name=dialog.name_edit.text().strip(),
                monthly_hours=dialog.monthly_hours.value(),
                max_daily_hours=dialog.max_daily_hours.value(),
                allow_special=dialog.allow_special.isChecked(),
                role=dialog.role_edit.text().strip() or None,
            )
        except DomainError as error:
            QMessageBox.warning(self, "新增失败", str(error))
            return
        self.refresh()

    def _edit(self) -> None:
        employee = self._selected()
        if employee is None or employee.id is None:
            QMessageBox.information(self, "提示", "请先选中一行人员")
            return
        dialog = EmployeeDialog(self, employee)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            employees_service.update_employee(
                employee.id,
                name=dialog.name_edit.text().strip(),
                monthly_hours=dialog.monthly_hours.value(),
                max_daily_hours=dialog.max_daily_hours.value(),
                allow_special=dialog.allow_special.isChecked(),
                role=dialog.role_edit.text().strip() or None,
            )
        except DomainError as error:
            QMessageBox.warning(self, "保存失败", str(error))
            return
        self.refresh()

    def _delete(self) -> None:
        employee = self._selected()
        if employee is None or employee.id is None:
            QMessageBox.information(self, "提示", "请先选中一行人员")
            return
        ret = QMessageBox.question(
            self,
            "删除人员",
            f"确定删除「{employee.name}」？其相关规则与排班将一并删除。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        try:
            employees_service.delete_employee(employee.id)
        except DomainError as error:
            QMessageBox.warning(self, "删除失败", str(error))
            return
        self.refresh()


class ShiftSection(QGroupBox):
    def __init__(self, plan_id: int) -> None:
        super().__init__("班次")
        self.plan_id = plan_id
        self._shifts: list[Shift] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 8, 4, 4)
        layout.setSpacing(10)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        add_btn = QPushButton("新增")
        add_btn.setProperty("class", "primary")
        add_btn.clicked.connect(self._add)
        edit_btn = QPushButton("编辑")
        edit_btn.clicked.connect(self._edit)
        del_btn = QPushButton("删除")
        del_btn.clicked.connect(self._delete)
        import_btn = QPushButton("重新导入班次代码")
        import_btn.clicked.connect(self._import)
        toolbar.addWidget(add_btn)
        toolbar.addWidget(edit_btn)
        toolbar.addWidget(del_btn)
        toolbar.addStretch()
        toolbar.addWidget(import_btn)
        layout.addLayout(toolbar)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["班次", "起始", "终止", "工时", "标记"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(48)
        self.table.setFixedHeight(300)
        self.table.cellDoubleClicked.connect(lambda *_: self._edit())
        layout.addWidget(self.table)

        self.refresh()

    def refresh(self) -> None:
        try:
            self._shifts = shifts_service.list_shifts(self.plan_id)
        except DomainError as error:
            QMessageBox.warning(self, "加载失败", str(error))
            return
        self.table.setRowCount(len(self._shifts))
        for row, shift in enumerate(self._shifts):
            marks = []
            if shift.is_off:
                marks.append("休息")
            if shift.is_leave:
                marks.append("有薪假")
            if shift.is_special:
                marks.append("盘点")
            if not shift.enabled:
                marks.append("停用")
            self.table.setItem(row, 0, QTableWidgetItem(shift.name))
            self.table.setItem(row, 1, QTableWidgetItem(shift.start_time))
            self.table.setItem(row, 2, QTableWidgetItem(shift.end_time))
            self.table.setItem(
                row, 3, QTableWidgetItem(f"{shift.work_hours:g}" if shift.work_hours else "0")
            )
            self.table.setItem(row, 4, QTableWidgetItem("、".join(marks)))

    def _selected(self) -> Shift | None:
        row = self.table.currentRow()
        if 0 <= row < len(self._shifts):
            return self._shifts[row]
        return None

    def _add(self) -> None:
        dialog = ShiftDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            shifts_service.create_shift(
                self.plan_id,
                name=dialog.name_edit.text().strip(),
                start_time=dialog.start_edit.time().toString("HH:mm"),
                end_time=dialog.end_edit.time().toString("HH:mm"),
                work_hours=dialog.work_hours.value(),
                is_special=dialog.special.isChecked(),
                enabled=dialog.enabled.isChecked(),
            )
        except DomainError as error:
            QMessageBox.warning(self, "新增失败", str(error))
            return
        self.refresh()

    def _edit(self) -> None:
        shift = self._selected()
        if shift is None or shift.id is None:
            QMessageBox.information(self, "提示", "请先选中一行班次")
            return
        dialog = ShiftDialog(self, shift)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            shifts_service.update_shift(
                shift.id,
                name=dialog.name_edit.text().strip(),
                start_time=dialog.start_edit.time().toString("HH:mm"),
                end_time=dialog.end_edit.time().toString("HH:mm"),
                work_hours=dialog.work_hours.value(),
                is_special=dialog.special.isChecked(),
                enabled=dialog.enabled.isChecked(),
            )
        except DomainError as error:
            QMessageBox.warning(self, "保存失败", str(error))
            return
        self.refresh()

    def _delete(self) -> None:
        shift = self._selected()
        if shift is None or shift.id is None:
            QMessageBox.information(self, "提示", "请先选中一行班次")
            return
        try:
            shifts_service.delete_shift(shift.id)
        except DomainError as error:
            QMessageBox.warning(self, "删除失败", str(error))
            return
        self.refresh()

    def _import(self) -> None:
        ret = QMessageBox.question(
            self,
            "重新导入班次代码",
            "将用班次代码文件重建班次目录，人工修改的特殊班标记会保留。继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        try:
            shifts_service.import_shifts(self.plan_id)
        except (DomainError, FileNotFoundError, ValueError) as error:
            QMessageBox.warning(self, "导入失败", str(error))
            return
        self.refresh()


class StaffShiftTab(QWidget):
    """人员与班次：上下分栏，两个独立表格。"""

    def __init__(self, plan_id: int) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(20, 16, 20, 18)
        content_layout.setSpacing(12)

        self.employee_section = EmployeeSection(plan_id)
        self.shift_section = ShiftSection(plan_id)
        content_layout.addWidget(self.employee_section)
        content_layout.addWidget(self.shift_section)
        content_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        outer.addWidget(scroll)

    def refresh(self) -> None:
        self.employee_section.refresh()
        self.shift_section.refresh()
