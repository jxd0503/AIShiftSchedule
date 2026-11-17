from typing import Any

from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import theme

STATUS_LABELS = {
    "OPTIMAL": "已找到最优排班",
    "FEASIBLE": "已找到可行排班",
    "INFEASIBLE": "硬约束无法同时满足",
    "PRECHECK_FAILED": "预检查未通过",
    "UNKNOWN": "求解超时",
    "CHECKED": "已完成校验",
}

STATUS_BANNERS = {
    "OPTIMAL": ("#ecfdf5", "#15803d", "#a7f3d0"),
    "FEASIBLE": ("#ecfdf5", "#15803d", "#a7f3d0"),
    "INFEASIBLE": ("#fef2f2", "#b91c1c", "#fecaca"),
    "PRECHECK_FAILED": ("#fef2f2", "#b91c1c", "#fecaca"),
    "UNKNOWN": ("#fffbeb", "#b45309", "#fde68a"),
    "CHECKED": ("#eff6ff", "#1d4ed8", "#bfdbfe"),
}
NEUTRAL_BANNER = ("#f9fafb", "#6b7280", "#e5e7eb")


def _banner_style(status: str) -> str:
    background, foreground, border = STATUS_BANNERS.get(status, NEUTRAL_BANNER)
    return (
        f"background: {background}; color: {foreground}; border: 1px solid {border};"
        " border-radius: 8px; padding: 9px 14px; font-weight: 600; font-size: 13px;"
    )


def _make_table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.verticalHeader().setVisible(False)
    header = table.horizontalHeader()
    for column in range(len(headers)):
        header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
    header.setStretchLastSection(True)
    return table


def _fill(table: QTableWidget, rows: list[list[str]]) -> None:
    table.setUpdatesEnabled(False)
    table.setRowCount(len(rows))
    for row_index, values in enumerate(rows):
        for column, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setToolTip(text)
            table.setItem(row_index, column, item)
    table.setUpdatesEnabled(True)


class ReportPanel(QWidget):
    """排班报告：覆盖缺口 / 人数上限 / 工时 / 冲突 / 规则满足 / 连续上班。"""

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(8)

        self.status_label = QLabel("尚未求解或校验")
        self.status_label.setStyleSheet(_banner_style(""))
        layout.addWidget(self.status_label)

        self.detail_label = QLabel(
            "运行求解或校验后，这里会显示覆盖缺口、工时偏差与冲突。"
        )
        self.detail_label.setWordWrap(True)
        self.detail_label.setProperty("class", "hint")
        layout.addWidget(self.detail_label)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("InnerTabs")
        self.coverage_table = _make_table(["日期", "需求", "需要", "实际", "缺口", "未覆盖区间"])
        self.limits_table = _make_table(["日期", "规则", "上限", "实际", "超出"])
        self.hours_table = _make_table(
            ["姓名", "目标工时（软）", "实际工时", "差值", "是否达标", "说明"]
        )
        self.hours_table.setToolTip(
            "人员档案里的月工时是软目标；只有显式「月度总工时」规则才是硬指标。"
        )
        self.conflicts_table = _make_table(["代码", "说明", "相关规则"])
        self.conflicts_table.verticalHeader().setDefaultSectionSize(48)
        self.rule_table = _make_table(["规则", "约束类型", "状态", "达成情况"])
        self.rest_table = _make_table(["姓名", "出勤天数", "休息天数", "有薪休假", "最长连续上班"])

        self.tabs.addTab(self.coverage_table, "覆盖缺口")
        self.tabs.addTab(self.limits_table, "人数上限")
        self.tabs.addTab(self.hours_table, "工时")
        self.tabs.addTab(self.conflicts_table, "冲突")
        self.tabs.addTab(self.rule_table, "规则满足情况")
        self.tabs.addTab(self.rest_table, "连续上班")
        layout.addWidget(self.tabs)
        theme.disable_tab_wheel_scroll(self.tabs)

    def set_report(self, report: dict[str, Any] | None) -> None:
        if not report:
            self.status_label.setText("尚未求解或校验")
            self.status_label.setStyleSheet(_banner_style(""))
            self.detail_label.setText(
                "运行求解或校验后，这里会显示覆盖缺口、工时偏差与冲突。"
            )
            for table in (
                self.coverage_table,
                self.limits_table,
                self.hours_table,
                self.conflicts_table,
                self.rule_table,
                self.rest_table,
            ):
                _fill(table, [])
            self._update_tab_titles()
            return

        status = str(report.get("status") or "")
        label = STATUS_LABELS.get(status, status or "未知状态")
        self.status_label.setText(label)
        self.status_label.setStyleSheet(_banner_style(status))

        message = report.get("message") or ""
        objective = report.get("objective")
        detail = message
        if not detail:
            detail = (
                f"目标值 {objective:g}" if isinstance(objective, (int, float)) else "仅满足硬约束，未做质量优化"
            )
        breakdown = report.get("breakdown") or {}
        if breakdown:
            detail += (
                "　|　"
                f"超额班次 {breakdown.get('long_shifts', 0)} · "
                f"非8h班次 {breakdown.get('non_8h_shifts', 0)} · "
                f"软休未命中 {breakdown.get('soft_rest_missed_days', 0)} 天 · "
                f"覆盖缺口 {breakdown.get('coverage_gap_minutes', 0)} 分钟"
            )
        self.detail_label.setText(detail)

        coverage_rows = []
        for item in report.get("coverage") or []:
            gaps = "、".join(
                f"{gap.get('start')}-{gap.get('end')}" for gap in item.get("gaps") or []
            )
            coverage_rows.append(
                [
                    str(item.get("date", "")),
                    str(item.get("label", "")),
                    str(item.get("required", "")),
                    str(item.get("assigned", "")),
                    str(item.get("shortfall", "")),
                    gaps or "-",
                ]
            )
        _fill(self.coverage_table, coverage_rows)

        limits_rows = [
            [
                str(item.get("date", "")),
                str(item.get("label", "")),
                str(item.get("limit", "")),
                str(item.get("assigned", "")),
                str(item.get("over", "")),
            ]
            for item in report.get("limits") or []
        ]
        _fill(self.limits_table, limits_rows)

        hours_rows = []
        any_soft = any(item.get("soft_target", True) for item in report.get("hours") or [])
        self.hours_table.setHorizontalHeaderLabels(
            [
                "姓名",
                "目标工时（软）" if any_soft else "目标工时（硬）",
                "实际工时",
                "差值",
                "是否达标",
                "说明",
            ]
        )
        for item in report.get("hours") or []:
            target = item.get("target")
            actual = item.get("actual")
            delta = item.get("delta")
            soft = item.get("soft_target", True)
            hours_rows.append(
                [
                    str(item.get("name", "")),
                    f"{target:g}" if isinstance(target, (int, float)) else "-",
                    f"{actual:g}" if isinstance(actual, (int, float)) else "-",
                    f"{delta:+g}" if isinstance(delta, (int, float)) else "-",
                    "达标"
                    if item.get("matches_target")
                    else ("未达软目标" if soft else "未达硬指标"),
                    str(item.get("target_note") or "-"),
                ]
            )
        _fill(self.hours_table, hours_rows)

        conflict_rows = []
        for item in report.get("conflicts") or []:
            rule_ids = item.get("rule_ids") or []
            conflict_rows.append(
                [
                    str(item.get("code", "")),
                    str(item.get("message", "")),
                    "、".join(f"#{rule_id}" for rule_id in rule_ids) or "-",
                ]
            )
        _fill(self.conflicts_table, conflict_rows)

        rule_rows = [
            [
                str(item.get("label", "")),
                "硬约束" if item.get("hard") else "软约束",
                str(item.get("status", "")),
                str(item.get("note", "")),
            ]
            for item in report.get("rule_status") or []
        ]
        _fill(self.rule_table, rule_rows)

        rest_rows = [
            [
                str(item.get("name", "")),
                str(item.get("work_days", "")),
                str(item.get("rest_days", "")),
                str(item.get("paid_leave_days", "")),
                str(item.get("max_consecutive", "")),
            ]
            for item in report.get("rest_summary") or []
        ]
        _fill(self.rest_table, rest_rows)

        self._update_tab_titles()
        if conflict_rows:
            self.tabs.setCurrentWidget(self.conflicts_table)

    def _update_tab_titles(self) -> None:
        self.tabs.setTabText(0, f"覆盖缺口（{self.coverage_table.rowCount()}）")
        self.tabs.setTabText(1, f"人数上限（{self.limits_table.rowCount()}）")
        self.tabs.setTabText(2, f"工时（{self.hours_table.rowCount()}）")
        self.tabs.setTabText(3, f"冲突（{self.conflicts_table.rowCount()}）")
        self.tabs.setTabText(4, f"规则满足情况（{self.rule_table.rowCount()}）")
        self.tabs.setTabText(5, f"连续上班（{self.rest_table.rowCount()}）")
