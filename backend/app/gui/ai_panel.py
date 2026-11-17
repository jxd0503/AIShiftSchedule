from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..errors import DomainError
from ..models import Employee
from ..services import ai_service, employees_service, rules_service
from . import theme
from .ai_worker import AiWorker
from .rule_editor_dialog import type_label

COUNT_LABELS = {
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
HOURS_OP_LABELS = {"eq": "等于", "ge": "不少于", "le": "不超过"}
TIME_OP_LABELS = {"eq": "等于", "ge": "不早于", "le": "不晚于"}


class AiPanel(QWidget):
    """AI 助手：自然语言约束解析、排班微调与冲突修复建议。"""

    data_changed = Signal()

    def __init__(self, plan_id: int) -> None:
        super().__init__()
        self.plan_id = plan_id
        self._employees: list[Employee] = []
        self._worker: AiWorker | None = None
        self._parse_result: dict[str, Any] = {}
        self._adjust_result: dict[str, Any] = {}
        self._suggestions: list[dict[str, Any]] = []
        self._build_ui()
        self.refresh_context()

    # ------------------------------------------------------------------ 界面

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("InnerTabs")
        self.tabs.addTab(self._build_parse_tab(), "约束解析")
        self.tabs.addTab(self._build_adjust_tab(), "排班微调")
        self.tabs.addTab(self._build_conflict_tab(), "冲突修复")
        layout.addWidget(self.tabs)
        theme.disable_tab_wheel_scroll(self.tabs)

    def _build_parse_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        hint = QLabel(
            "用自然语言描述约束（可一次写多条），解析结果先进入预览，确认后才写入规则。"
        )
        hint.setProperty("class", "hint")
        layout.addWidget(hint)

        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("范围"))
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("全部人员", "global")
        self.scope_combo.addItem("指定人员", "employee")
        self.scope_combo.currentIndexChanged.connect(self._on_scope_changed)
        scope_row.addWidget(self.scope_combo)
        self.employee_combo = QComboBox()
        self.employee_combo.setEnabled(False)
        scope_row.addWidget(self.employee_combo)
        scope_row.addStretch()
        self.parse_btn = QPushButton("解析约束")
        self.parse_btn.setProperty("class", "primary")
        self.parse_btn.clicked.connect(self._run_parse)
        scope_row.addWidget(self.parse_btn)
        layout.addLayout(scope_row)

        self.parse_text = QPlainTextEdit()
        self.parse_text.setPlaceholderText(
            "例如：\n11 月 4 日张三休息\n每周三刘蕾都休息\n11 月每个工作日排 8 小时的班"
        )
        self.parse_text.setFixedHeight(110)
        layout.addWidget(self.parse_text)

        self.preview_table = QTableWidget(0, 7)
        self.preview_table.setHorizontalHeaderLabels(
            ["选择", "类型", "人员", "日期", "内容", "硬软", "状态"]
        )
        header = self.preview_table.horizontalHeader()
        for col in range(6):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        self.preview_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.preview_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.preview_table.verticalHeader().setVisible(False)
        self.preview_table.verticalHeader().setDefaultSectionSize(36)
        layout.addWidget(self.preview_table, 1)

        self.parse_status = QLabel()
        self.parse_status.setWordWrap(True)
        self.parse_status.setProperty("class", "hint")
        layout.addWidget(self.parse_status)

        actions = QHBoxLayout()
        select_btn = QPushButton("全选有效")
        select_btn.clicked.connect(lambda: self._set_all_checked(True))
        actions.addWidget(select_btn)
        clear_btn = QPushButton("全不选")
        clear_btn.clicked.connect(lambda: self._set_all_checked(False))
        actions.addWidget(clear_btn)
        actions.addStretch()
        apply_btn = QPushButton("写入所选")
        apply_btn.setProperty("class", "primary")
        apply_btn.clicked.connect(self._apply_previews)
        actions.addWidget(apply_btn)
        layout.addLayout(actions)
        return page

    def _build_adjust_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        hint = QLabel(
            "描述对排班结果不满意的地方，AI 会换算成约束的新增/修改/删除，并试算验证后才落库。"
        )
        hint.setProperty("class", "hint")
        layout.addWidget(hint)

        self.adjust_text = QPlainTextEdit()
        self.adjust_text.setPlaceholderText(
            "例如：\n11-12 只排 1 个人\n把每天 8 小时的硬要求放宽到 7 小时\n11-03 甲和乙都必须休息，但当天要 2 人上班"
        )
        self.adjust_text.setFixedHeight(110)
        layout.addWidget(self.adjust_text)

        actions = QHBoxLayout()
        parse_btn = QPushButton("解析调整")
        parse_btn.clicked.connect(self._run_adjust_parse)
        actions.addWidget(parse_btn)
        actions.addStretch()
        self.apply_adjust_btn = QPushButton("应用并重新求解")
        self.apply_adjust_btn.setProperty("class", "primary")
        self.apply_adjust_btn.setEnabled(False)
        self.apply_adjust_btn.clicked.connect(self._apply_adjustment)
        actions.addWidget(self.apply_adjust_btn)
        layout.addLayout(actions)

        self.adjust_table = QTableWidget(0, 3)
        self.adjust_table.setHorizontalHeaderLabels(["操作", "内容", "说明"])
        header = self.adjust_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.adjust_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.adjust_table.verticalHeader().setVisible(False)
        self.adjust_table.verticalHeader().setDefaultSectionSize(36)
        layout.addWidget(self.adjust_table, 1)

        self.adjust_status = QLabel()
        self.adjust_status.setWordWrap(True)
        self.adjust_status.setProperty("class", "hint")
        layout.addWidget(self.adjust_status)
        return page

    def _build_conflict_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self.conflict_label = QLabel()
        self.conflict_label.setWordWrap(True)
        self.conflict_label.setProperty("class", "hint")
        layout.addWidget(self.conflict_label)

        actions = QHBoxLayout()
        actions.addWidget(QLabel("方案条数"))
        self.limit_combo = QComboBox()
        self.limit_combo.addItem("1 条", 1)
        self.limit_combo.addItem("2 条", 2)
        actions.addWidget(self.limit_combo)
        suggest_btn = QPushButton("生成修复方案")
        suggest_btn.setProperty("class", "primary")
        suggest_btn.clicked.connect(self._run_suggestions)
        actions.addWidget(suggest_btn)
        actions.addStretch()
        self.apply_suggestion_btn = QPushButton("应用所选方案")
        self.apply_suggestion_btn.setEnabled(False)
        self.apply_suggestion_btn.clicked.connect(self._apply_suggestion)
        actions.addWidget(self.apply_suggestion_btn)
        layout.addLayout(actions)

        self.suggestion_table = QTableWidget(0, 3)
        self.suggestion_table.setHorizontalHeaderLabels(["方案", "诊断", "改动"])
        header = self.suggestion_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.suggestion_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.suggestion_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.suggestion_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.suggestion_table.verticalHeader().setVisible(False)
        self.suggestion_table.verticalHeader().setDefaultSectionSize(36)
        layout.addWidget(self.suggestion_table, 1)

        self.conflict_status = QLabel()
        self.conflict_status.setWordWrap(True)
        self.conflict_status.setProperty("class", "hint")
        layout.addWidget(self.conflict_status)
        return page

    # ------------------------------------------------------------------ 上下文

    def refresh_context(self) -> None:
        try:
            self._employees = employees_service.list_employees(self.plan_id)
        except Exception:  # noqa: BLE001 - 上下文刷新失败不阻断页面
            self._employees = []
        current = self.employee_combo.currentData()
        self.employee_combo.blockSignals(True)
        self.employee_combo.clear()
        for employee in self._employees:
            self.employee_combo.addItem(employee.name, employee.id)
        if self._employees:
            index = self.employee_combo.findData(current)
            self.employee_combo.setCurrentIndex(index if index >= 0 else 0)
        else:
            self.employee_combo.setCurrentIndex(-1)
        self.employee_combo.blockSignals(False)
        self._update_scope_state()
        try:
            summary = ai_service.conflict_summary(self.plan_id)
        except DomainError:
            self.conflict_label.setText("尚未求解，暂无冲突信息")
            return
        if summary["status"] is None:
            self.conflict_label.setText("尚未求解，暂无冲突信息")
            return
        lines = [f"最近一次求解状态：{summary['status']}，冲突 {summary['count']} 条。"]
        for conflict in summary["conflicts"][:8]:
            lines.append(f"· {conflict.get('message') or conflict.get('code')}")
        self.conflict_label.setText("\n".join(lines))

    def _on_scope_changed(self) -> None:
        if self.scope_combo.currentData() == "employee" and not self._employees:
            # 人员可能在「人员与班次」页签刚新增，重新加载后再判断
            self.refresh_context()
            return
        self.employee_combo.setEnabled(self.scope_combo.currentData() == "employee")

    def _update_scope_state(self) -> None:
        """无人员时禁用「指定人员」选项，避免下拉为空却可继续解析。"""
        has_employees = bool(self._employees)
        model = self.scope_combo.model()
        item = model.item(1) if hasattr(model, "item") else None
        if item is not None:
            item.setEnabled(has_employees)
        if not has_employees and self.scope_combo.currentData() == "employee":
            self.scope_combo.setCurrentIndex(0)
        self.employee_combo.setEnabled(
            has_employees and self.scope_combo.currentData() == "employee"
        )
        self.scope_combo.setToolTip(
            "" if has_employees else "当前计划还没有人员，「指定人员」范围暂不可用"
        )
        self.employee_combo.setToolTip(
            "" if has_employees else "当前计划还没有人员，请先在「人员与班次」中添加"
        )

    # ------------------------------------------------------------------ 通用

    def _set_busy(self, busy: bool) -> None:
        self.parse_btn.setEnabled(not busy)
        self.apply_adjust_btn.setEnabled(not busy and bool(self._adjust_result))
        self.apply_suggestion_btn.setEnabled(not busy and bool(self._suggestions))
        if busy:
            self.tabs.setTabText(self.tabs.currentIndex(), "处理中…")

    def _start(self, func, *args, on_ok) -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "提示", "当前有 AI 任务正在进行，请稍候")
            return
        self._set_busy(True)
        worker = AiWorker(func, *args)
        worker.succeeded.connect(on_ok)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._on_worker_done)
        self._worker = worker
        worker.start()

    def _on_worker_done(self) -> None:
        self._worker = None
        self._set_busy(False)
        self._restore_tab_titles()

    def _restore_tab_titles(self) -> None:
        self.tabs.setTabText(0, "约束解析")
        self.tabs.setTabText(1, "排班微调")
        self.tabs.setTabText(2, "冲突修复")

    def _on_failed(self, message: str) -> None:
        self._restore_tab_titles()
        QMessageBox.warning(self, "AI 操作失败", message)

    def shutdown(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(5000)

    # ------------------------------------------------------------------ 约束解析

    def _run_parse(self) -> None:
        text = self.parse_text.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "提示", "请先输入约束描述")
            return
        scope = str(self.scope_combo.currentData())
        employee_id = None
        if scope == "employee":
            employee_id = self.employee_combo.currentData()
            if employee_id is None:
                QMessageBox.information(self, "提示", "请先选择人员")
                return
        self.parse_status.setText("AI 正在解析，最长约 3 分钟…")
        self._start(
            ai_service.parse_constraints,
            self.plan_id,
            scope,
            employee_id,
            text,
            on_ok=self._on_parsed,
        )

    def _on_parsed(self, result: dict[str, Any]) -> None:
        self._parse_result = result
        rules = result.get("rules") or []
        self.preview_table.setRowCount(len(rules))
        for row, item in enumerate(rules):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            valid = bool(item.get("valid"))
            check.setCheckState(Qt.Checked if valid else Qt.Unchecked)
            self.preview_table.setItem(row, 0, check)
            values = [
                type_label(str(item.get("type") or "")),
                item.get("employee_name") or "全员",
                self._date_text(item),
                self._detail_text(item),
                "硬" if item.get("hard") else "软",
                "可写入" if valid else (item.get("note") or "无效，需手工调整"),
            ]
            for col, text in enumerate(values, start=1):
                self.preview_table.setItem(row, col, QTableWidgetItem(str(text)))

        lines = list(result.get("warnings") or [])
        if result.get("hint"):
            lines.append(str(result["hint"]))
        if result.get("mode") == "fallback":
            lines.insert(0, "模型响应超时，已使用本地关键词兜底，请逐条核对。")
        self.parse_status.setText(
            "\n".join(lines) if lines else f"解析完成，共 {len(rules)} 条候选。"
        )

    @staticmethod
    def _date_text(item: dict[str, Any]) -> str:
        start = item.get("date_start") or ""
        end = item.get("date_end") or ""
        if start and end and start != end:
            return f"{start}~{end}"
        return start or end or "全月"

    @staticmethod
    def _detail_text(item: dict[str, Any]) -> str:
        rule_type = str(item.get("type") or "")
        if rule_type == "SHIFT_HOURS":
            op = HOURS_OP_LABELS.get(str(item.get("hours_op") or "eq"), "")
            return f"{op} {item.get('work_hours') or 0:g} 小时"
        if rule_type == "START_STAFF" and item.get("start_time"):
            op = TIME_OP_LABELS.get(str(item.get("time_op") or "eq"), "")
            return f"{item['start_time']} {op}，{item.get('count') or 0} 人"
        if rule_type == "END_STAFF" and item.get("end_time"):
            op = TIME_OP_LABELS.get(str(item.get("time_op") or "eq"), "")
            return f"{item['end_time']} {op}，{item.get('count') or 0} 人"
        if item.get("shift_name"):
            return f"{item['shift_name']} {item.get('start_time') or ''}-{item.get('end_time') or ''}".strip()
        if item.get("start_time") and item.get("end_time"):
            return f"{item['start_time']}-{item['end_time']}"
        if item.get("count") is not None:
            template = COUNT_LABELS.get(rule_type)
            if template:
                return template.format(n=item["count"])
            return f"{item['count']} 人"
        if item.get("weekdays"):
            return f"星期 {item['weekdays']}"
        return item.get("raw_text") or ""

    def _set_all_checked(self, checked: bool) -> None:
        rules = self._parse_result.get("rules") or []
        for row, item in enumerate(rules):
            cell = self.preview_table.item(row, 0)
            if cell is None:
                continue
            if checked and not item.get("valid"):
                continue
            cell.setCheckState(Qt.Checked if checked else Qt.Unchecked)

    def _checked_previews(self) -> list[dict[str, Any]]:
        rules = self._parse_result.get("rules") or []
        picked: list[dict[str, Any]] = []
        for row, item in enumerate(rules):
            cell = self.preview_table.item(row, 0)
            if cell is not None and cell.checkState() == Qt.Checked:
                picked.append(item)
        return picked

    def _apply_previews(self) -> None:
        previews = self._checked_previews()
        if not previews:
            QMessageBox.information(self, "提示", "请勾选要写入的约束")
            return
        try:
            count = ai_service.apply_previews(self.plan_id, previews)
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
                count = ai_service.apply_previews(self.plan_id, previews, force=True)
            except DomainError as retry_error:
                QMessageBox.warning(self, "写入失败", str(retry_error))
                return
        except DomainError as error:
            QMessageBox.warning(self, "写入失败", str(error))
            return
        QMessageBox.information(self, "已写入", f"成功写入 {count} 条约束")
        self.data_changed.emit()

    # ------------------------------------------------------------------ 排班微调

    def _run_adjust_parse(self) -> None:
        text = self.adjust_text.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "提示", "请先输入要调整的内容")
            return
        self.adjust_status.setText("AI 正在解析，最长约 3 分钟…")
        self._start(
            ai_service.parse_adjustment,
            self.plan_id,
            text,
            on_ok=self._on_adjust_parsed,
        )

    def _on_adjust_parsed(self, result: dict[str, Any]) -> None:
        self._adjust_result = result
        rows: list[tuple[str, str, str]] = []
        for item in result.get("add") or []:
            rows.append(
                (
                    "新增",
                    type_label(str(item.get("type") or "")),
                    f"{item.get('employee_name') or '全员'} · {self._date_text(item)} · {self._detail_text(item)}",
                )
            )
        for item in result.get("update") or []:
            rows.append(("修改", str(item.get("label") or ""), str(item.get("summary") or "")))
        for item in result.get("remove") or []:
            rows.append(("删除", str(item.get("label") or ""), str(item.get("reason") or "")))
        self.adjust_table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for col, text in enumerate(values):
                self.adjust_table.setItem(row, col, QTableWidgetItem(text))
        warnings = list(result.get("warnings") or [])
        self.adjust_status.setText(
            "\n".join(warnings) if warnings else f"解析完成，共 {len(rows)} 项改动。"
        )
        self.apply_adjust_btn.setEnabled(bool(rows))

    def _apply_adjustment(self) -> None:
        result = self._adjust_result
        if not result:
            return
        ret = QMessageBox.question(
            self,
            "应用调整",
            "将写入规则改动并重新求解；若求解不可行会自动回滚。继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        remove_ids = [item["rule_id"] for item in result.get("remove") or []]
        self.adjust_status.setText("正在应用并重新求解…")
        self._start(
            ai_service.apply_adjustment,
            self.plan_id,
            list(result.get("add") or []),
            list(result.get("update") or []),
            remove_ids,
            on_ok=self._on_adjust_applied,
        )

    def _on_adjust_applied(self, result: dict[str, Any]) -> None:
        applied = "\n".join(result.get("applied") or [])
        if result.get("solved"):
            self.adjust_status.setText(f"已应用并重新求解：{result.get('message')}\n{applied}")
            QMessageBox.information(self, "应用成功", str(result.get("message") or "已重新求解"))
            self.data_changed.emit()
            return
        conflicts = result.get("conflicts") or []
        self.adjust_status.setText(
            f"未能通过校验，改动已回滚：{result.get('message')}\n{applied}"
        )
        QMessageBox.warning(
            self,
            "已回滚",
            f"{result.get('message')}\n剩余冲突 {len(conflicts)} 条，规则改动未保存。",
        )

    # ------------------------------------------------------------------ 冲突修复

    def _run_suggestions(self) -> None:
        limit = int(self.limit_combo.currentData() or 1)
        self.conflict_status.setText("AI 正在生成修复方案，最长约 2 分钟…")
        self._start(
            ai_service.suggest_conflicts,
            self.plan_id,
            limit,
            on_ok=self._on_suggestions,
        )

    def _on_suggestions(self, result: dict[str, Any]) -> None:
        self._suggestions = list(result.get("suggestions") or [])
        self.suggestion_table.setRowCount(len(self._suggestions))
        for row, suggestion in enumerate(self._suggestions):
            self.suggestion_table.setItem(
                row, 0, QTableWidgetItem(str(suggestion.get("title") or f"方案 {row + 1}"))
            )
            self.suggestion_table.setItem(
                row, 1, QTableWidgetItem(str(suggestion.get("diagnosis") or ""))
            )
            self.suggestion_table.setItem(
                row, 2, QTableWidgetItem("\n".join(suggestion.get("summary") or []))
            )
        warnings = list(result.get("warnings") or [])
        if warnings:
            self.conflict_status.setText("\n".join(warnings))
        elif self._suggestions:
            self.conflict_status.setText(f"已生成 {len(self._suggestions)} 条方案，选中后应用")
        else:
            self.conflict_status.setText("没有生成方案")
        self.apply_suggestion_btn.setEnabled(bool(self._suggestions))

    def _apply_suggestion(self) -> None:
        row = self.suggestion_table.currentRow()
        if not (0 <= row < len(self._suggestions)):
            QMessageBox.information(self, "提示", "请先选中一条方案")
            return
        suggestion = self._suggestions[row]
        operations = list(suggestion.get("operations") or [])
        if not operations:
            QMessageBox.information(self, "提示", "该方案没有可执行的改动")
            return
        ret = QMessageBox.question(
            self,
            "应用方案",
            f"应用「{suggestion.get('title')}」并重新求解？不可行会自动回滚。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        self.conflict_status.setText("正在应用方案并重新求解…")
        self._start(
            ai_service.apply_operations,
            self.plan_id,
            operations,
            on_ok=self._on_suggestion_applied,
        )

    def _on_suggestion_applied(self, result: dict[str, Any]) -> None:
        applied = "\n".join(result.get("applied") or [])
        if result.get("solved"):
            self.conflict_status.setText(f"方案已应用：{result.get('message')}\n{applied}")
            QMessageBox.information(self, "应用成功", str(result.get("message") or "已重新求解"))
            self.data_changed.emit()
            return
        conflicts = result.get("conflicts") or []
        self.conflict_status.setText(
            f"方案未通过校验，已回滚：{result.get('message')}\n{applied}"
        )
        QMessageBox.warning(
            self,
            "已回滚",
            f"{result.get('message')}\n剩余冲突 {len(conflicts)} 条，改动未保存。",
        )
