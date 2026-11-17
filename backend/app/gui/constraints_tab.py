from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
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

from ..models import Employee, Rule, Shift
from ..services import employees_service, plans_service, rules_service, shifts_service
from .rule_editor_dialog import RULE_TYPE_LABELS, RuleEditorDialog, type_label

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
TIME_OP_LABELS = {"eq": "等于", "ge": "不早于", "le": "不晚于"}
HOURS_OP_LABELS = {"eq": "等于", "ge": "不少于", "le": "不超过"}
COUNT_VALUE_LABELS = {
    "DAY_STAFF": "在岗 {n} 人",
    "SEGMENT_STAFF": "在岗 {n} 人",
    "START_STAFF": "上班 {n} 人",
    "END_STAFF": "下班 {n} 人",
    "SHIFT_STAFF": "班次 {n} 人",
    "MAX_STAFF": "上限 {n} 人",
    "PAID_LEAVE": "休假 {n} 天",
    "MAX_REST_DAYS": "连休上限 {n} 天",
    "MAX_CONSECUTIVE_WORK": "连休上限 {n} 天",
    "MIN_REST_DAYS": "至少休 {n} 天",
    "MAX_WORK_DAYS": "最多上 {n} 天",
    "SINGLE_ROLE_SHIFT": "≤{n} 人时必须上班次",
    "MONTHLY_HOURS": "目标 {n} 小时",
}


class ConstraintsTab(QWidget):
    """约束录入：规则表格 + 手工新增/编辑/删除 + 冲突预检。"""

    def __init__(self, plan_id: int) -> None:
        super().__init__()
        self.plan_id = plan_id
        self._rules: list[Rule] = []
        self._employees: list[Employee] = []
        self._shifts: list[Shift] = []
        self._employee_names: dict[int, str] = {}
        self._shift_names: dict[int, str] = {}
        self._month = ""
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        toolbar.addWidget(QLabel("类型"))
        self.type_filter = QComboBox()
        self.type_filter.addItem("全部类型", None)
        for key, label in RULE_TYPE_LABELS.items():
            self.type_filter.addItem(label, key)
        self.type_filter.currentIndexChanged.connect(self.refresh)
        toolbar.addWidget(self.type_filter)

        toolbar.addWidget(QLabel("人员"))
        self.employee_filter = QComboBox()
        self.employee_filter.addItem("全部", None)
        self.employee_filter.currentIndexChanged.connect(self.refresh)
        toolbar.addWidget(self.employee_filter)

        toolbar.addStretch()
        add_btn = QPushButton("新增约束")
        add_btn.setProperty("class", "primary")
        add_btn.clicked.connect(self._add)
        toolbar.addWidget(add_btn)
        edit_btn = QPushButton("编辑")
        edit_btn.clicked.connect(self._edit)
        toolbar.addWidget(edit_btn)
        del_btn = QPushButton("删除")
        del_btn.clicked.connect(self._delete)
        toolbar.addWidget(del_btn)
        batch_btn = QPushButton("批量删除所选")
        batch_btn.clicked.connect(self._batch_delete)
        toolbar.addWidget(batch_btn)
        layout.addLayout(toolbar)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            ["ID", "类型", "人员", "日期", "星期", "班次/时段", "人数/天数", "硬软", "备注"]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(8, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.setSortingEnabled(False)
        self.table.cellDoubleClicked.connect(lambda *_: self._edit())
        layout.addWidget(self.table)

        self.summary = QLabel()
        self.summary.setProperty("class", "hint")
        layout.addWidget(self.summary)

    def refresh(self) -> None:
        try:
            plan = plans_service.get_plan(self.plan_id)
            self._month = plan.month
            self._employees = employees_service.list_employees(self.plan_id)
            self._shifts = shifts_service.list_shifts(self.plan_id)
        except Exception as error:  # noqa: BLE001
            QMessageBox.warning(self, "加载失败", str(error))
            return

        self._employee_names = {e.id: e.name for e in self._employees if e.id is not None}
        self._shift_names = {s.id: s.name for s in self._shifts if s.id is not None}
        current = self.employee_filter.currentData()
        self.employee_filter.blockSignals(True)
        self.employee_filter.clear()
        self.employee_filter.addItem("全部", None)
        self.employee_filter.addItem("未指定人员", -1)
        for employee in self._employees:
            self.employee_filter.addItem(employee.name, employee.id)
        index = self.employee_filter.findData(current)
        self.employee_filter.setCurrentIndex(index if index >= 0 else 0)
        self.employee_filter.blockSignals(False)

        rule_type = self.type_filter.currentData()
        employee_id = self.employee_filter.currentData()
        try:
            rules = rules_service.list_rules(
                self.plan_id,
                rule_type=str(rule_type) if rule_type else None,
                employee_id=None if employee_id in (None, -1) else int(employee_id),
            )
        except Exception as error:  # noqa: BLE001
            QMessageBox.warning(self, "加载失败", str(error))
            return
        if employee_id == -1:
            rules = [rule for rule in rules if rule.employee_id is None]
        self._rules = rules

        self.table.setRowCount(len(rules))
        # 深色/浅色主题下分别选用可读的硬/软标识颜色
        dark_table = self.table.palette().color(QPalette.Base).lightness() < 128
        hard_color = QColor("#f87171") if dark_table else QColor("#b91c1c")
        soft_color = QColor("#4ade80") if dark_table else QColor("#15803d")
        for row, rule in enumerate(rules):
            values = [
                str(rule.id or ""),
                type_label(rule.type),
                self._employee_names.get(rule.employee_id or 0, "全员"),
                self._date_text(rule),
                self._weekday_text(rule),
                self._shift_text(rule),
                self._value_text(rule),
                "硬" if rule.hard else "软",
                rule.note or rule.raw_text or "",
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col == 7:
                    item.setForeground(hard_color if rule.hard else soft_color)
                self.table.setItem(row, col, item)
        self.summary.setText(f"共 {len(rules)} 条约束（双击一行可编辑）")

    @staticmethod
    def _date_text(rule: Rule) -> str:
        if rule.date_start == rule.date_end:
            return rule.date_start
        return f"{rule.date_start}~{rule.date_end}"

    @staticmethod
    def _weekday_text(rule: Rule) -> str:
        if not rule.weekdays:
            return "每天"
        names = []
        for part in rule.weekdays.split(","):
            if part.strip().isdigit() and 1 <= int(part) <= 7:
                names.append(WEEKDAY_NAMES[int(part) - 1])
        return "、".join(names)

    def _shift_text(self, rule: Rule) -> str:
        if rule.shift_id:
            name = self._shift_names.get(rule.shift_id, f"#{rule.shift_id}")
            return f"{name} {rule.start_time}-{rule.end_time or ''}".strip()
        if rule.type == "SEGMENT_STAFF":
            return f"{rule.start_time}-{rule.end_time}"
        if rule.end_time and not rule.start_time:
            op = TIME_OP_LABELS.get(rule.time_op or "eq", "")
            return f"{op}{rule.end_time} 下班".strip()
        if rule.start_time:
            op = TIME_OP_LABELS.get(rule.time_op or "eq", "")
            suffix = " 上班" if rule.type in ("SHIFT_LIMIT", "END_STAFF") else ""
            return f"{op}{rule.start_time}{suffix}".strip()
        return ""

    @staticmethod
    def _value_text(rule: Rule) -> str:
        if rule.type == "SHIFT_HOURS":
            op = HOURS_OP_LABELS.get(rule.hours_op or "eq", "")
            return f"{op} {rule.work_hours or 0:g} 小时"
        if rule.count is None:
            return ""
        template = COUNT_VALUE_LABELS.get(rule.type)
        text = str(rule.count) if template is None else template.format(n=rule.count)
        if rule.role:
            text += f"（岗位 {rule.role}）"
        return text

    def _selected_rules(self) -> list[Rule]:
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        return [self._rules[row] for row in sorted(rows) if 0 <= row < len(self._rules)]

    def _reload_context(self) -> None:
        """重新加载月份/人员/班次，供打开编辑器前使用（人员可能刚在其它页签新增）。"""
        plan = plans_service.get_plan(self.plan_id)
        self._month = plan.month
        self._employees = employees_service.list_employees(self.plan_id)
        self._shifts = shifts_service.list_shifts(self.plan_id)
        self._employee_names = {e.id: e.name for e in self._employees if e.id is not None}
        self._shift_names = {s.id: s.name for s in self._shifts if s.id is not None}

    def _open_editor(self, rule: Rule | None) -> RuleEditorDialog | None:
        try:
            self._reload_context()
        except Exception as error:  # noqa: BLE001
            QMessageBox.warning(self, "加载失败", str(error))
            return None
        return RuleEditorDialog(
            self,
            month=self._month,
            employees=self._employees,
            shifts=self._shifts,
            rule=rule,
        )

    def _save(self, dialog: RuleEditorDialog, rule: Rule | None) -> bool:
        payload = dialog.payload()
        try:
            if rule is not None and rule.id is not None:
                rules_service.update_rule(rule.id, payload)
            else:
                rules_service.create_rules(self.plan_id, [payload])
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
                return False
            try:
                if rule is not None and rule.id is not None:
                    rules_service.update_rule(rule.id, payload, force=True)
                else:
                    rules_service.create_rules(self.plan_id, [payload], force=True)
            except Exception as retry_error:  # noqa: BLE001
                QMessageBox.warning(self, "保存失败", str(retry_error))
                return False
        except Exception as error:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", str(error))
            return False
        return True

    def _add(self) -> None:
        if not self._month:
            QMessageBox.warning(self, "无法新增", "计划尚未加载")
            return
        dialog = self._open_editor(None)
        if dialog is None:
            return
        if dialog.exec() != QDialog.Accepted:
            return
        if self._save(dialog, None):
            self.refresh()

    def _edit(self) -> None:
        rules = self._selected_rules()
        if not rules:
            QMessageBox.information(self, "提示", "请先选中一行约束")
            return
        rule = rules[0]
        dialog = self._open_editor(rule)
        if dialog is None:
            return
        if dialog.exec() != QDialog.Accepted:
            return
        if self._save(dialog, rule):
            self.refresh()

    def _delete(self) -> None:
        rules = self._selected_rules()
        if not rules:
            QMessageBox.information(self, "提示", "请先选中要删除的约束")
            return
        ret = QMessageBox.question(
            self,
            "删除约束",
            f"确定删除选中的 {len(rules)} 条约束？已生成的排班不会被删除。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        ids = [rule.id for rule in rules if rule.id is not None]
        try:
            _deleted, not_found = rules_service.delete_rules(self.plan_id, ids)
        except Exception as error:  # noqa: BLE001
            QMessageBox.warning(self, "删除失败", str(error))
            return
        if not_found:
            QMessageBox.information(
                self, "部分未找到", f"以下约束已不存在：{not_found}"
            )
        self.refresh()

    def _batch_delete(self) -> None:
        self._delete()
