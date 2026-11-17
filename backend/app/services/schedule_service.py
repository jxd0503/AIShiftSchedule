# pyright: reportCallInDefaultInitializer=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""排班域应用服务：求解编排、手工排班、校验与导出，全部同进程可调用。

逻辑与 routers/schedule.py 保持一致，只是把 HTTPException 换成 DomainError，
把 Response 换成 (filename, bytes)。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlmodel import Session, delete, select

from ..db import engine
from ..errors import DomainError
from ..models import Assignment, Employee, Plan, Rule, Shift
from ..schemas import AssignmentUpdate
from . import exporter, optimizer, solver
from .calendar_service import (
    effective_units,
    month_bounds,
    shift_window_error,
    time_window_error,
    units_to_hours,
)
from .validator import evaluate

# 段 A 在时限内未出解时，给「证明不可行/求首个可行解」的额外兜底窗口（秒）
UNSOLVED_FALLBACK_SECONDS = 120.0


@dataclass
class SolveResult:
    status: str
    message: str
    report: dict[str, Any]
    phase: str
    task_id: int | None = None
    elapsed_seconds: float = 0.0
    remaining_seconds: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


def load_plan(plan_id: int, session: Session) -> Plan:
    plan = session.get(Plan, plan_id)
    if plan is None:
        raise DomainError("计划不存在")
    return plan


def load_context(
    plan_id: int, session: Session
) -> tuple[Plan, list[Employee], list[Shift], list[Rule], list[Assignment]]:
    plan = load_plan(plan_id, session)
    employees = list(
        session.exec(
            select(Employee)
            .where(Employee.plan_id == plan_id)
            .order_by(Employee.sort_order, Employee.id)  # pyright: ignore[reportArgumentType]
        ).all()
    )
    shifts = list(
        session.exec(
            select(Shift)
            .where(Shift.plan_id == plan_id)
            .order_by(Shift.sort_order, Shift.id)  # pyright: ignore[reportArgumentType]
        ).all()
    )
    rules = list(
        session.exec(
            select(Rule).where(Rule.plan_id == plan_id).order_by(Rule.id)  # pyright: ignore[reportArgumentType]
        ).all()
    )
    assignments = list(
        session.exec(
            select(Assignment)
            .where(Assignment.plan_id == plan_id)
            .order_by(Assignment.date)  # pyright: ignore[reportArgumentType]
        ).all()
    )
    return plan, employees, shifts, rules, assignments


def persist_solution(
    plan: Plan, session: Session, outcome: solver.SolveOutcome
) -> list[Assignment]:
    """把求解结果写入当前会话（不提交），返回新增的排班记录。"""
    assert plan.id is not None
    session.exec(delete(Assignment).where(Assignment.plan_id == plan.id))  # pyright: ignore[reportArgumentType, reportUnusedCallResult]
    session.flush()
    saved: list[Assignment] = []
    for item in outcome.assignments:
        record = Assignment(
            plan_id=plan.id,
            employee_id=item.employee_id,
            date=item.date,
            shift_id=item.shift_id,
            start_time=item.start_time,
            end_time=item.end_time,
            hours=item.hours,
            locked=item.locked,
            source=item.source,
        )
        session.add(record)
        saved.append(record)
    plan.status = "solved"
    plan.solve_report = json.dumps(outcome.report, ensure_ascii=False)
    plan.last_solve_at = datetime.now().isoformat(timespec="seconds")
    session.add(plan)
    return saved


def _current_objective(plan: Plan) -> float | None:
    if not plan.solve_report:
        return None
    try:
        report = json.loads(plan.solve_report)
    except json.JSONDecodeError:
        return None
    objective = report.get("objective")
    return float(objective) if isinstance(objective, (int, float)) else None


def _clear_schedule(plan: Plan, session: Session) -> None:
    assert plan.id is not None
    session.exec(delete(Assignment).where(Assignment.plan_id == plan.id))  # pyright: ignore[reportArgumentType, reportUnusedCallResult]
    plan.status = "draft"
    plan.solve_report = None
    plan.last_solve_at = None
    session.add(plan)
    session.commit()


def _persist_infeasible(
    plan: Plan, session: Session, outcome: solver.SolveOutcome
) -> None:
    assert plan.id is not None
    session.exec(delete(Assignment).where(Assignment.plan_id == plan.id))  # pyright: ignore[reportArgumentType, reportUnusedCallResult]
    plan.status = "draft"
    plan.solve_report = json.dumps(outcome.report, ensure_ascii=False)
    plan.last_solve_at = datetime.now().isoformat(timespec="seconds")
    session.add(plan)
    session.commit()


def proved_infeasible(plan: Plan) -> bool:
    if not plan.solve_report:
        return False
    try:
        report = json.loads(plan.solve_report)
    except json.JSONDecodeError:
        return False
    return report.get("status") == "INFEASIBLE"


def solve_first_phase(plan_id: int) -> SolveResult:
    """两阶段求解第一阶段：短时限找可行解并落库，随后注册后台优化。"""
    with Session(engine) as session:
        plan, employees, shifts, rules, current = load_context(plan_id, session)
        locked = [item for item in current if item.locked]
        config = optimizer.resolve_config(plan, session)

        optimizer.reset_terminal(plan_id)
        optimizer.stale_pending_candidates(plan_id, session)
        session.commit()

        task = optimizer.begin_first_phase(plan_id, config)
        hint_candidate = optimizer.hint_from_latest(session, plan, current)
        hint = hint_candidate.assignments if hint_candidate is not None else None
        hint_stale = bool(hint_candidate is not None and hint_candidate.stale)
        started = time.monotonic()
        deadline = started + config.first_phase_seconds

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        def run(
            mode: str,
            limit: float,
            hint_items: list[Assignment] | None,
            *,
            fix_hint: bool = False,
            allow_fix_hint: bool = True,
        ) -> solver.SolveOutcome:
            return solver.solve(
                plan,
                employees,
                shifts,
                rules,
                locked,
                time_limit=limit,
                random_seed=config.random_seed,
                num_search_workers=config.num_search_workers,
                objective_mode=mode,
                hint_assignments=hint_items,
                fix_hint=fix_hint,
                allow_fix_hint=allow_fix_hint,
                soft_weights=config.soft_weights,
            )

        outcome: solver.SolveOutcome | None = None
        # ① 复用：当前口径未变时，固定上一版排班让 presolve 直接判可行（通常几秒内）
        if hint is not None and not hint_stale and remaining() >= 2.0:
            reused = run(
                "feasibility",
                min(15.0, remaining()),
                hint,
                fix_hint=True,
                allow_fix_hint=True,
            )
            if reused.status in ("OPTIMAL", "FEASIBLE"):
                outcome = reused

        # ② 软月工时先拿结构可行解，再用「修复模式」以最少改动对齐硬月工时
        warm: solver.SolveOutcome | None = None
        if outcome is None and plan.monthly_hours_hard and remaining() >= 6.0:
            warm_limit = min(45.0, remaining() * 0.4)
            plan.monthly_hours_hard = False
            try:
                warm = run("feasibility", warm_limit, None)
            finally:
                plan.monthly_hours_hard = True
            if warm.status in ("OPTIMAL", "FEASIBLE") and remaining() >= 2.0:
                repaired = run("repair", remaining(), warm.assignments)
                if repaired.status in ("OPTIMAL", "FEASIBLE"):
                    outcome = repaired

        # ③ 兜底：硬月工时可行性（优先带提示；提示可能过期，去掉后再试一次）
        if outcome is None and remaining() >= 2.0:
            fallback_hint = (
                hint
                if hint is not None and not hint_stale
                else (
                    warm.assignments
                    if warm is not None and warm.status in ("OPTIMAL", "FEASIBLE")
                    else None
                )
            )
            outcome = run("feasibility", remaining(), fallback_hint, allow_fix_hint=False)
            if outcome.status == "INFEASIBLE" and remaining() >= 2.0:
                outcome = run("feasibility", remaining(), None)

        # ④ 仍未出解：用计划总预算再补一段（不含提示，避免被过期方向锁死）
        if outcome is None or outcome.status == "UNKNOWN":
            extra = min(
                UNSOLVED_FALLBACK_SECONDS,
                max(0.0, config.max_seconds - (time.monotonic() - started)),
            )
            if extra >= 2.0:
                outcome = run("feasibility", extra, None)
        if outcome is None:  # pragma: no cover - 所有分支都已给出结果
            outcome = run("feasibility", 2.0, None)
        elapsed = time.monotonic() - started

        if outcome.status in ("OPTIMAL", "FEASIBLE"):
            persist_solution(plan, session, outcome)
            session.commit()
            background = optimizer.start_after_first_phase(
                plan_id,
                session,
                config,
                elapsed,
                _current_objective(plan),
                task=task,
                hint_assignments=outcome.assignments,
            )
            remaining = (
                max(0.0, config.max_seconds - elapsed) if background is not None else 0.0
            )
            return SolveResult(
                status=outcome.status,
                message=outcome.message,
                report=outcome.report,
                phase="solved_first_solution",
                task_id=background.task_id if background is not None else task.task_id,
                elapsed_seconds=round(elapsed, 1),
                remaining_seconds=round(remaining, 1),
            )

        if outcome.status == "PRECHECK_FAILED":
            optimizer.abandon_task(task)
            _persist_infeasible(plan, session, outcome)
            return SolveResult(
                status=outcome.status,
                message=outcome.message,
                report=outcome.report,
                phase="precheck_failed",
                elapsed_seconds=round(elapsed, 1),
            )

        if outcome.status == "INFEASIBLE":
            optimizer.abandon_task(task)
            optimizer.mark_infeasible(plan_id, config)
            _persist_infeasible(plan, session, outcome)
            return SolveResult(
                status="INFEASIBLE",
                message=outcome.message,
                report=outcome.report,
                phase="infeasible",
                elapsed_seconds=round(elapsed, 1),
            )

        optimizer.abandon_task(task)
        _clear_schedule(plan, session)
        return SolveResult(
            status=outcome.status,
            message=outcome.message,
            report=outcome.report,
            phase="no_solution_yet",
            elapsed_seconds=round(elapsed, 1),
            remaining_seconds=round(config.max_seconds, 1),
        )


def continue_solve(plan_id: int) -> SolveResult:
    with Session(engine) as session:
        plan, _employees, _shifts, _rules, current = load_context(plan_id, session)
        if proved_infeasible(plan):
            raise DomainError(
                "已证明无解：当前硬约束组合不可行，继续搜索不会产生可行解，请先放宽约束后重新求解"
            )
        config = optimizer.resolve_config(plan, session)
        task = optimizer.start_after_first_phase(
            plan_id,
            session,
            config,
            0.0,
            _current_objective(plan),
            start_stage="A",
        )
        if task is None:
            raise DomainError("没有可用的后台优化时长")
        return SolveResult(
            status="RUNNING",
            message="后台优化进行中",
            report=json.loads(plan.solve_report) if plan.solve_report else {},
            phase="background",
            task_id=task.task_id,
            remaining_seconds=round(config.max_seconds, 1),
        )


def solve_status(plan_id: int) -> dict[str, Any]:
    with Session(engine) as session:
        load_plan(plan_id, session)
        return optimizer.status_payload(plan_id, session)


def cancel_solve(plan_id: int) -> dict[str, Any]:
    with Session(engine) as session:
        load_plan(plan_id, session)
        cancelled = optimizer.cancel_task(plan_id)
        return {"cancelled": cancelled, **optimizer.status_payload(plan_id, session)}


def accept_candidate(plan_id: int, candidate_id: int) -> dict[str, Any]:
    with Session(engine) as session:
        plan = load_plan(plan_id, session)
        try:
            result = optimizer.accept_candidate(session, plan, candidate_id)
        except ValueError as error:
            raise DomainError(str(error)) from error
        assignments = list(
            session.exec(
                select(Assignment)
                .where(Assignment.plan_id == plan_id)
                .order_by(Assignment.date)  # pyright: ignore[reportArgumentType]
            ).all()
        )
        return {
            "candidate": result["candidate"],
            "report": result["report"],
            "assignments": assignments,
        }


def discard_candidate(plan_id: int, candidate_id: int) -> dict[str, Any]:
    with Session(engine) as session:
        plan = load_plan(plan_id, session)
        try:
            payload = optimizer.discard_candidate(session, plan, candidate_id)
        except ValueError as error:
            raise DomainError(str(error)) from error
        return {"candidate": payload}


def load_grid_data(
    plan_id: int,
) -> tuple[Plan, list[Employee], list[Shift], list[Assignment]]:
    """GUI 排班网格所需的上下文（脱离 Session 后仍可访问字段）。"""
    with Session(engine) as session:
        plan, employees, shifts, _rules, assignments = load_context(plan_id, session)
        session.expunge_all()
        return plan, employees, shifts, assignments


def list_assignments(plan_id: int) -> list[Assignment]:
    with Session(engine) as session:
        load_plan(plan_id, session)
        return list(
            session.exec(
                select(Assignment)
                .where(Assignment.plan_id == plan_id)
                .order_by(Assignment.date)  # pyright: ignore[reportArgumentType]
            ).all()
        )


def set_assignment(
    plan_id: int,
    employee_id: int,
    day: str,
    shift_id: int,
    *,
    locked: bool | None = None,
) -> Assignment | None:
    """手工指定某人某天的班次；shift_id 对应 OFF/POFF 时等价于“清空为休息”。"""
    with Session(engine) as session:
        plan = load_plan(plan_id, session)
        employee = session.get(Employee, employee_id)
        if employee is None or employee.plan_id != plan.id:
            raise DomainError("人员不存在或不属于该计划")
        shift = session.get(Shift, shift_id)
        if shift is None or shift.plan_id != plan.id:
            raise DomainError("班次不存在或不属于该计划")
        error = shift_window_error(
            plan.coverage_soft_start_effective,
            plan.coverage_soft_end_effective,
            shift,
            plan.latest_end_time,
        )
        if error:
            raise DomainError(error)
        month_start, month_end = month_bounds(plan.month)
        if not month_start <= day <= month_end:
            raise DomainError("排班日期必须落在计划月份内")

        existing = session.exec(
            select(Assignment).where(
                Assignment.plan_id == plan_id,
                Assignment.employee_id == employee_id,
                Assignment.date == day,
            )
        ).first()
        if existing is not None:
            existing.shift_id = shift.id
            existing.start_time = shift.start_time
            existing.end_time = shift.end_time
            existing.hours = units_to_hours(
                effective_units(shift.work_hours, shift.start_time, shift.end_time)
            )
            existing.source = "manual"
            if locked is not None:
                existing.locked = locked
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return existing

        item = Assignment(
            plan_id=plan_id,
            employee_id=employee_id,
            date=day,
            shift_id=shift.id,
            start_time=shift.start_time,
            end_time=shift.end_time,
            hours=units_to_hours(
                effective_units(shift.work_hours, shift.start_time, shift.end_time)
            ),
            locked=bool(locked),
            source="manual",
        )
        session.add(item)
        session.commit()
        session.refresh(item)
        return item


def clear_assignment(plan_id: int, employee_id: int, day: str) -> None:
    with Session(engine) as session:
        load_plan(plan_id, session)
        row = session.exec(
            select(Assignment).where(
                Assignment.plan_id == plan_id,
                Assignment.employee_id == employee_id,
                Assignment.date == day,
            )
        ).first()
        if row is None:
            return
        session.delete(row)
        session.commit()


def update_assignment(assignment_id: int, payload: AssignmentUpdate) -> Assignment | None:
    with Session(engine) as session:
        assignment = session.get(Assignment, assignment_id)
        if assignment is None:
            raise DomainError("排班记录不存在")
        plan = session.get(Plan, assignment.plan_id)
        if plan is None:
            raise DomainError("计划不存在")

        if payload.shift_id is None and payload.start_time is None:
            session.delete(assignment)
            session.commit()
            return None

        if payload.shift_id is not None:
            shift = session.get(Shift, payload.shift_id)
            if shift is None or shift.plan_id != plan.id:
                raise DomainError("班次不存在或不属于该计划")
            error = shift_window_error(
                plan.coverage_soft_start_effective,
                plan.coverage_soft_end_effective,
                shift,
                plan.latest_end_time,
            )
            if error:
                raise DomainError(error)
            start_time = shift.start_time
            end_time = shift.end_time
            work_hours = shift.work_hours
            assignment.shift_id = shift.id
        else:
            if payload.start_time is None or payload.end_time is None:
                raise DomainError("自定义班次需要同时填写开始与结束时间")
            start_time = payload.start_time
            end_time = payload.end_time
            work_hours = None
            assignment.shift_id = None
            error = time_window_error(
                plan.coverage_soft_start_effective,
                plan.coverage_soft_end_effective,
                start_time,
                end_time,
                plan.latest_end_time,
            )
            if error:
                raise DomainError(error)

        assignment.start_time = start_time
        assignment.end_time = end_time
        assignment.hours = units_to_hours(
            effective_units(work_hours, start_time, end_time)
        )
        assignment.source = "manual"
        if payload.locked is not None:
            assignment.locked = payload.locked
        session.add(assignment)
        session.commit()
        session.refresh(assignment)
        return assignment


def validate_plan(plan_id: int) -> tuple[dict[str, Any], list[Assignment]]:
    with Session(engine) as session:
        plan, employees, shifts, rules, assignments = load_context(plan_id, session)
        report = evaluate(plan, employees, shifts, rules, assignments)
        return report, assignments


def export_workbook(plan_id: int) -> tuple[str, bytes]:
    with Session(engine) as session:
        plan, employees, shifts, rules, assignments = load_context(plan_id, session)
        report = evaluate(plan, employees, shifts, rules, assignments)
        content = exporter.build_workbook(plan, employees, shifts, assignments, report)
        return f"{plan.name}.xlsx", content
