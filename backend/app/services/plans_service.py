# pyright: reportAny=false, reportUnknownMemberType=false
"""计划域应用服务：GUI 直接调用的纯函数，不依赖 HTTP 层。

把原 routers/plans.py 的业务逻辑抽到这里，供 PySide6 GUI 同进程复用。
会话用 Session(engine) 上下文管理，业务校验失败抛 DomainError。
"""
from datetime import datetime

from sqlmodel import Session, select

from ..db import engine
from ..errors import DomainError
from ..models import Assignment, Employee, Plan, Rule, Shift
from . import shift_import
from .calendar_service import month_bounds, parse_hhmm


def list_plans() -> list[Plan]:
    with Session(engine) as session:
        return list(
            session.exec(
                select(Plan).order_by(Plan.month.desc(), Plan.id.desc())  # pyright: ignore[reportAttributeAccessIssue]
            ).all()
        )


def get_plan(plan_id: int) -> Plan:
    with Session(engine) as session:
        plan = session.get(Plan, plan_id)
        if plan is None:
            raise DomainError("计划不存在")
        return plan


def ensure_coverage(plan: Plan) -> None:
    if parse_hhmm(plan.coverage_start) >= parse_hhmm(plan.coverage_end):
        raise DomainError("覆盖结束时间必须晚于开始时间")


def create_default_config(plan: Plan, session: Session) -> None:
    assert plan.id is not None
    templates = shift_import.load_shift_templates()
    for shift in shift_import.build_shifts(plan.id, templates):
        session.add(shift)
    date_start, date_end = month_bounds(plan.month)
    # 硬核心：核心时段内必须有 core_min_count 人（必须满足）
    session.add(
        Rule(
            plan_id=plan.id,
            type="SEGMENT_STAFF",
            date_start=date_start,
            date_end=date_end,
            start_time=plan.coverage_start,
            end_time=plan.coverage_end,
            count=plan.core_min_count,
            hard=True,
            raw_text="全月核心覆盖（硬）",
            source="manual",
        )
    )
    # 软早段：软起点到核心起点之间，偏好有人但不强制
    if plan.coverage_soft_start < plan.coverage_start:
        session.add(
            Rule(
                plan_id=plan.id,
                type="SEGMENT_STAFF",
                date_start=date_start,
                date_end=date_end,
                start_time=plan.coverage_soft_start,
                end_time=plan.coverage_start,
                count=1,
                hard=False,
                raw_text="全月覆盖（软·早段）",
                source="manual",
            )
        )
    # 软晚段：核心终点到软终点之间，偏好有人但不强制
    if plan.coverage_end < plan.coverage_soft_end:
        session.add(
            Rule(
                plan_id=plan.id,
                type="SEGMENT_STAFF",
                date_start=date_start,
                date_end=date_end,
                start_time=plan.coverage_end,
                end_time=plan.coverage_soft_end,
                count=1,
                hard=False,
                raw_text="全月覆盖（软·晚段）",
                source="manual",
            )
        )


def create_plan(
    month: str,
    name: str | None = None,
    coverage_start: str = "07:30",
    coverage_end: str = "17:00",
    coverage_soft_start: str = "07:00",
    coverage_soft_end: str = "19:00",
    core_min_count: int = 1,
    max_consecutive_days: int = 5,
    monthly_hours_hard: bool = False,
    latest_end_time: str | None = None,
    overnight_next_rest: bool = True,
) -> Plan:
    name = name or f"{month[:4]}年{int(month[5:])}月排班"
    with Session(engine) as session:
        plan = Plan(
            name=name,
            month=month,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            coverage_soft_start=coverage_soft_start,
            coverage_soft_end=coverage_soft_end,
            core_min_count=core_min_count,
            max_consecutive_days=max_consecutive_days,
            monthly_hours_hard=monthly_hours_hard,
            latest_end_time=latest_end_time or None,
            overnight_next_rest=overnight_next_rest,
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
        ensure_coverage(plan)
        session.add(plan)
        session.flush()
        create_default_config(plan, session)
        session.commit()
        session.refresh(plan)
        return plan


def update_plan(
    plan_id: int,
    *,
    name: str,
    month: str,
    coverage_start: str,
    coverage_end: str,
    coverage_soft_start: str,
    coverage_soft_end: str,
    core_min_count: int,
    max_consecutive_days: int,
    monthly_hours_hard: bool,
    latest_end_time: str | None,
    overnight_next_rest: bool,
    solve_first_phase_seconds: int | None,
    solve_max_seconds: int | None,
) -> Plan:
    with Session(engine) as session:
        plan = session.get(Plan, plan_id)
        if plan is None:
            raise DomainError("计划不存在")
        if month != plan.month:
            has_rules = (
                session.exec(select(Rule).where(Rule.plan_id == plan_id)).first() is not None
            )
            has_assign = (
                session.exec(select(Assignment).where(Assignment.plan_id == plan_id)).first()
                is not None
            )
            if has_rules or has_assign:
                raise DomainError("计划内已有规则或排班，月份不可修改")
            plan.month = month
        plan.name = name
        plan.coverage_start = coverage_start
        plan.coverage_end = coverage_end
        plan.coverage_soft_start = coverage_soft_start
        plan.coverage_soft_end = coverage_soft_end
        plan.core_min_count = core_min_count
        plan.max_consecutive_days = max_consecutive_days
        plan.monthly_hours_hard = monthly_hours_hard
        # 空串与 null 都表示清除自定义值，回到「跟随软覆盖结束时间」
        plan.latest_end_time = latest_end_time or None
        plan.overnight_next_rest = overnight_next_rest
        plan.solve_first_phase_seconds = solve_first_phase_seconds
        plan.solve_max_seconds = solve_max_seconds
        ensure_coverage(plan)
        session.add(plan)
        session.commit()
        session.refresh(plan)
        return plan


def delete_plan(plan_id: int) -> None:
    with Session(engine) as session:
        plan = session.get(Plan, plan_id)
        if plan is None:
            raise DomainError("计划不存在")
        for model in (Assignment, Rule, Employee, Shift):
            for row in session.exec(select(model).where(model.plan_id == plan_id)).all():
                session.delete(row)
        session.delete(plan)
        session.commit()
