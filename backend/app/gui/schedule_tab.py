import json
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..errors import DomainError
from ..services import plans_service, schedule_service
from . import theme
from .report_panel import ReportPanel
from .schedule_grid import ScheduleGrid
from .solve_worker import SolveWorker

STAGE_LABELS = {
    "A": "段 A · 求可行解",
    "B": "段 B · 质量优化",
    "infeasible": "已证明无解",
    "done": "已完成",
    "idle": "空闲",
}

BREAKDOWN_LABELS = {
    "long_shifts": "超额班次",
    "non_8h_shifts": "非 8h 班次",
    "soft_rest_missed_days": "软休未命中",
    "coverage_gap_minutes": "覆盖缺口",
    "rest_over_limit": "连休超限",
}


def _breakdown_text(breakdown: dict[str, Any] | None) -> str:
    if not breakdown:
        return "指标计算中"
    return " · ".join(
        f"{label} {breakdown.get(key, 0)}" for key, label in BREAKDOWN_LABELS.items()
    )


class ScheduleTab(QWidget):
    """排班结果：求解 / 后台优化状态 / 排班网格 / 报告。"""

    status_changed = Signal()

    def __init__(self, plan_id: int) -> None:
        super().__init__()
        self.plan_id = plan_id
        self._worker: SolveWorker | None = None
        self._status: dict[str, Any] = {}
        self._candidate_id: int | None = None
        self.has_schedule = False
        try:
            self._month = plans_service.get_plan(plan_id).month
        except DomainError:
            self._month = ""
        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._poll)

        self.reload()
        self._restore_status()

    # ---------------------------------------------------------------- 界面
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.solve_btn = QPushButton("求解排班")
        self.solve_btn.setProperty("class", "primary")
        self.solve_btn.clicked.connect(self._start_solve)
        self.continue_btn = QPushButton("继续搜索")
        self.continue_btn.clicked.connect(self._continue_solve)
        self.cancel_btn = QPushButton("停止优化")
        self.cancel_btn.clicked.connect(self._cancel)
        self.validate_btn = QPushButton("校验当前排班")
        self.validate_btn.clicked.connect(self._validate)
        self.export_btn = QPushButton("导出 Excel")
        self.export_btn.clicked.connect(self._export)
        refresh_btn = QPushButton("刷新")
        theme.set_role(refresh_btn, "link")
        refresh_btn.clicked.connect(self.reload)
        for button in (
            self.solve_btn,
            self.continue_btn,
            self.cancel_btn,
            self.validate_btn,
            self.export_btn,
            refresh_btn,
        ):
            toolbar.addWidget(button)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        progress_row = QHBoxLayout()
        progress_row.setSpacing(12)
        self.stage_label = QLabel("尚未求解")
        self.stage_label.setStyleSheet("font-weight: 600;")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setMaximumWidth(260)
        self.elapsed_label = QLabel("")
        self.elapsed_label.setProperty("class", "hint")
        progress_row.addWidget(self.stage_label)
        progress_row.addWidget(self.progress)
        progress_row.addWidget(self.elapsed_label)
        progress_row.addStretch()
        layout.addLayout(progress_row)

        self.banner = QFrame()
        self.banner.setVisible(False)
        self.banner.setStyleSheet(
            "QFrame { background: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 8px; }"
        )
        banner_layout = QHBoxLayout(self.banner)
        banner_layout.setContentsMargins(12, 8, 12, 8)
        self.banner_label = QLabel()
        self.banner_label.setWordWrap(True)
        self.banner_label.setStyleSheet("color: #15803d; border: none;")
        banner_layout.addWidget(self.banner_label, 1)
        self.accept_btn = QPushButton("应用更优解")
        self.accept_btn.setProperty("class", "primary")
        self.accept_btn.clicked.connect(self._accept_candidate)
        self.discard_btn = QPushButton("忽略")
        self.discard_btn.clicked.connect(self._discard_candidate)
        banner_layout.addWidget(self.accept_btn)
        banner_layout.addWidget(self.discard_btn)
        layout.addWidget(self.banner)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)
        self.grid = ScheduleGrid(self.plan_id, self._month)
        self.grid.changed.connect(self._on_grid_changed)
        self.report = ReportPanel()
        self.report.setFixedHeight(380)
        content_layout.addWidget(self.grid)
        content_layout.addWidget(self.report)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

    # ---------------------------------------------------------------- 数据
    def _plan(self):
        return plans_service.get_plan(self.plan_id)

    # ---------------------------------------------------------------- 外部入口
    def start_solve(self) -> None:
        if self.solve_btn.isEnabled():
            self._start_solve()

    def export_excel(self) -> None:
        if self.export_btn.isEnabled():
            self._export()

    def reload(self) -> None:
        try:
            plan, employees, shifts, assignments = schedule_service.load_grid_data(
                self.plan_id
            )
        except Exception as error:  # noqa: BLE001
            QMessageBox.warning(self, "加载失败", str(error))
            return
        self._month = plan.month
        self.grid.set_month(self._month)
        self.grid.set_data(employees, shifts, assignments)
        self.has_schedule = bool(assignments)
        self.report.set_report(_load_report(plan))
        self.solve_btn.setText("重新求解" if plan.status == "solved" else "求解排班")
        self._update_buttons()
        self.status_changed.emit()

    def _restore_status(self) -> None:
        try:
            self._apply_status(schedule_service.solve_status(self.plan_id))
        except Exception:  # noqa: BLE001
            return
        if self._status.get("phase") == "background":
            self._timer.start()

    # ---------------------------------------------------------------- 求解
    def _start_solve(self) -> None:
        if self._worker is not None:
            return
        self._worker = SolveWorker(self.plan_id)
        self._worker.solved.connect(self._on_solved)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_worker_finished)
        self.stage_label.setText("段 A · 求可行解（正在求解…）")
        self.progress.setRange(0, 0)
        self._update_buttons()
        self._timer.start()
        self._worker.start()

    def _continue_solve(self) -> None:
        try:
            schedule_service.continue_solve(self.plan_id)
        except DomainError as error:
            QMessageBox.warning(self, "无法继续", str(error))
            return
        self._timer.start()
        self._poll()
        self.reload()

    def _cancel(self) -> None:
        try:
            schedule_service.cancel_solve(self.plan_id)
        except Exception as error:  # noqa: BLE001
            QMessageBox.warning(self, "取消失败", str(error))
            return
        self._poll()

    def _on_worker_finished(self) -> None:
        self._worker = None
        self._update_buttons()

    def _on_solved(self, result) -> None:
        self.report.set_report(result.report)
        self.reload()
        if result.phase == "solved_first_solution":
            self.stage_label.setText("已找到可行解，后台持续优化中…")
            self._timer.start()
            self._poll()
        elif result.phase == "infeasible":
            self.stage_label.setText("已证明无解")
            QMessageBox.warning(
                self,
                "无解",
                f"{result.message}\n\n请检查硬约束（覆盖、工时、休息）是否互相矛盾。",
            )
        elif result.phase == "precheck_failed":
            self.stage_label.setText("预检查未通过")
            QMessageBox.warning(self, "预检查未通过", result.message)
        else:
            self.stage_label.setText("未在时限内找到可行解")
            QMessageBox.information(
                self,
                "暂未求出可行解",
                f"{result.message}\n\n可以点击「继续搜索」再给求解器一些时间。",
            )
        self._update_buttons()

    def _on_failed(self, message: str) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.stage_label.setText("求解失败")
        QMessageBox.warning(self, "求解失败", message)

    # ---------------------------------------------------------------- 轮询
    def _poll(self) -> None:
        try:
            status = schedule_service.solve_status(self.plan_id)
        except Exception:  # noqa: BLE001
            self._timer.stop()
            return
        self._apply_status(status)
        if status.get("phase") != "background" and self._worker is None:
            self._timer.stop()
            if status.get("phase") == "done" and not status.get("candidates"):
                self.stage_label.setText("后台优化已结束")
            self.reload()

    def _apply_status(self, status: dict[str, Any]) -> None:
        self._status = status
        stage = str(status.get("stage") or "idle")
        phase = status.get("phase")
        continuous = bool(status.get("continuous"))
        optimal = bool(status.get("optimal"))
        if self._worker is not None:
            self.stage_label.setText("段 A · 求可行解（正在求解…）")
            self.progress.setRange(0, 0)
        else:
            if optimal:
                self.stage_label.setText("已找到最优解")
            elif continuous and phase == "background":
                self.stage_label.setText("段 B · 持续优化中（找到更优解会提示，可随时停止）")
            else:
                self.stage_label.setText(STAGE_LABELS.get(stage, stage))
            budget = float(status.get("budget_seconds") or 0)
            elapsed = float(status.get("elapsed_seconds") or 0)
            if phase == "background" and continuous:
                # 无固定预算：用不确定进度条表示「一直在跑」
                self.progress.setRange(0, 0)
            elif phase == "background" and budget > 0:
                self.progress.setRange(0, 100)
                self.progress.setValue(min(100, int(elapsed / budget * 100)))
            elif phase == "idle":
                self.progress.setRange(0, 100)
                self.progress.setValue(0)
            else:
                self.progress.setRange(0, 100)
                self.progress.setValue(100 if stage in ("done", "infeasible") else 0)

        elapsed = float(status.get("elapsed_seconds") or 0)
        budget = float(status.get("budget_seconds") or 0)
        parts = [f"{elapsed:.0f} 秒"]
        if budget > 0 and phase == "background" and not continuous:
            parts.append(f"/ {budget:.0f} 秒")
        objective = status.get("objective")
        if isinstance(objective, (int, float)):
            parts.append(f"目标值 {objective:g}")
        bound = status.get("best_bound")
        if isinstance(bound, (int, float)):
            parts.append(f"下界 {bound:g}")
        since = status.get("since_last_improvement_seconds")
        if phase == "background" and isinstance(since, (int, float)) and since >= 15:
            parts.append(f"已 {since:.0f} 秒无改进")
        self.elapsed_label.setText(" · ".join(parts))

        candidates = status.get("candidates") or []
        if candidates:
            self._candidate_id = candidates[0].get("id")
            current = _load_report(self._plan()) or {}
            current_breakdown = current.get("breakdown") or {}
            candidate_breakdown = candidates[0].get("breakdown") or {}
            changed = [
                f"{label} {current_breakdown.get(key, 0)}→{candidate_breakdown.get(key, 0)}"
                for key, label in BREAKDOWN_LABELS.items()
                if current_breakdown.get(key, 0) != candidate_breakdown.get(key, 0)
            ]
            current_objective = current.get("objective")
            candidate_objective = candidates[0].get("objective")
            score = ""
            if isinstance(current_objective, (int, float)) and isinstance(
                candidate_objective, (int, float)
            ):
                score = f"（目标值 {current_objective:g} → {candidate_objective:g}）"
            prefix = "已找到最优解" if optimal else "发现更优解"
            action = "应用最优解" if optimal else "应用更优解"
            self.banner_label.setText(
                f"{prefix}{score}："
                + ("、".join(changed) if changed else "各项指标持平")
                + f"。点击「{action}」即可替换当前排班。"
            )
            self.accept_btn.setText(action)
            self.accept_btn.setVisible(True)
            self.banner.setVisible(True)
        else:
            self._candidate_id = None
            if optimal:
                # 已证明最优但没有更优候选：提示无需再优化
                self.banner_label.setText("当前排班已是最优解，无需再优化。")
                self.accept_btn.setVisible(False)
                self.banner.setVisible(True)
            else:
                self.banner.setVisible(False)
        self._update_buttons()

    def _update_buttons(self) -> None:
        phase = self._status.get("phase")
        solving = self._worker is not None
        self.solve_btn.setEnabled(not solving and phase != "background")
        self.cancel_btn.setEnabled(phase == "background")
        self.validate_btn.setEnabled(not solving)
        self.export_btn.setEnabled(not solving and self.has_schedule)
        self.export_btn.setToolTip(
            "" if self.has_schedule else "当前还没有排班结果，请先求解或手工调整后再导出"
        )

    # ---------------------------------------------------------------- 候选
    def _accept_candidate(self) -> None:
        if self._candidate_id is None:
            return
        try:
            schedule_service.accept_candidate(self.plan_id, self._candidate_id)
        except DomainError as error:
            QMessageBox.warning(self, "采用失败", str(error))
            return
        self.banner.setVisible(False)
        self.reload()
        self._poll()

    def _discard_candidate(self) -> None:
        if self._candidate_id is None:
            return
        try:
            schedule_service.discard_candidate(self.plan_id, self._candidate_id)
        except DomainError as error:
            QMessageBox.warning(self, "忽略失败", str(error))
            return
        self.banner.setVisible(False)
        self._poll()

    # ---------------------------------------------------------------- 其它
    def shutdown(self) -> None:
        """离开工作区时停止轮询并断开后台线程信号，避免回调悬空对象。"""
        self._timer.stop()
        if self._worker is not None:
            for signal, slot in (
                (self._worker.solved, self._on_solved),
                (self._worker.failed, self._on_failed),
                (self._worker.finished, self._on_worker_finished),
            ):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
            self._worker.wait(2000)
            self._worker = None

    def _on_grid_changed(self) -> None:
        self.reload()

    def _validate(self) -> None:
        try:
            report, _assignments = schedule_service.validate_plan(self.plan_id)
        except Exception as error:  # noqa: BLE001
            QMessageBox.warning(self, "校验失败", str(error))
            return
        self.report.set_report(report)
        self.stage_label.setText("已完成校验")

    def _export(self) -> None:
        if not self.has_schedule:
            QMessageBox.information(
                self, "暂无可导出内容", "当前还没有排班结果，请先求解或手工调整。"
            )
            return
        try:
            filename, content = schedule_service.export_workbook(self.plan_id)
        except Exception as error:  # noqa: BLE001
            QMessageBox.warning(self, "导出失败", str(error))
            return
        path, _selected = QFileDialog.getSaveFileName(
            self, "导出 Excel", filename, "Excel 工作簿 (*.xlsx)"
        )
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            with open(path, "wb") as handle:
                handle.write(content)
        except OSError as error:
            QMessageBox.warning(self, "保存失败", str(error))
            return
        QMessageBox.information(self, "导出成功", f"已保存到：\n{path}")


def _load_report(plan) -> dict[str, Any] | None:
    if not plan.solve_report:
        return None
    try:
        return json.loads(plan.solve_report)
    except json.JSONDecodeError:
        return None
