# pyright: reportAny=false, reportUnknownMemberType=false
"""班次域应用服务：GUI 直接调用的纯函数，不依赖 HTTP 层。"""
import re

from sqlmodel import Session, select

from ..db import engine
from ..errors import DomainError
from ..models import Assignment, Plan, Rule, Shift
from . import shift_import
from .calendar_service import TIME_PATTERN, shift_window_error


def _validate_time_format(start_time: str, end_time: str) -> None:
    """服务层兜底校验：班次时间必须是 HH:MM，避免 '8:0' 被静默当成 08:00。"""
    for value, label in ((start_time, "开始时间"), (end_time, "结束时间")):
        if not isinstance(value, str) or not re.match(TIME_PATTERN, value):
            raise DomainError(f"班次{label}必须是 HH:MM 格式（00:00~23:59）")


def _validate_special(
    is_off: bool,
    is_leave: bool,
    work_hours: float | None,
    start_time: str | None = None,
    end_time: str | None = None,
) -> None:
    if is_off and is_leave:
        raise DomainError("同一班次不能同时为休息 OFF 与有薪休假 POFF")
    if is_off:
        if work_hours is not None and work_hours != 0:
            raise DomainError("休息 OFF 班次工时固定为 0 小时")
        if start_time is not None and start_time != "00:00":
            raise DomainError("休息 OFF 班次起始时间固定为 00:00")
        if end_time is not None and end_time != "00:00":
            raise DomainError("休息 OFF 班次终止时间固定为 00:00")
    if is_leave:
        if work_hours is not None and work_hours != 8:
            raise DomainError("有薪休假 POFF 班次工时固定为 8 小时")
        if start_time is not None and start_time != "00:00":
            raise DomainError("有薪休假 POFF 班次起始时间固定为 00:00")
        if end_time is not None and end_time != "00:00":
            raise DomainError("有薪休假 POFF 班次终止时间固定为 00:00")


def _load_plan(session: Session, plan_id: int) -> Plan:
    plan = session.get(Plan, plan_id)
    if plan is None:
        raise DomainError("计划不存在")
    return plan


def list_shifts(plan_id: int) -> list[Shift]:
    with Session(engine) as session:
        _load_plan(session, plan_id)
        return list(
            session.exec(
                select(Shift)
                .where(Shift.plan_id == plan_id)
                .order_by(Shift.sort_order, Shift.id)  # pyright: ignore[reportArgumentType]
            ).all()
        )


def create_shift(
    plan_id: int,
    *,
    name: str,
    start_time: str,
    end_time: str,
    work_hours: float,
    is_special: bool,
    enabled: bool,
) -> Shift:
    with Session(engine) as session:
        plan = _load_plan(session, plan_id)
        _validate_time_format(start_time, end_time)
        _validate_special(False, False, work_hours, start_time, end_time)
        count = len(session.exec(select(Shift).where(Shift.plan_id == plan_id)).all())
        shift = Shift(
            plan_id=plan_id,
            name=name,
            start_time=start_time,
            end_time=end_time,
            is_special=is_special,
            special_source="manual" if is_special else None,
            special_reason="人工设为盘点班" if is_special else None,
            is_leave=False,
            is_off=False,
            color=shift_import.COLORS[count % len(shift_import.COLORS)],
            sort_order=count,
            enabled=enabled,
            work_hours=work_hours,
            break_code=None,
            break_hours=None,
            base_code=None,
            variant=None,
        )
        error = shift_window_error(
            plan.coverage_soft_start_effective,
            plan.coverage_soft_end_effective,
            shift,
            plan.latest_end_time,
        )
        if error:
            raise DomainError(error)
        session.add(shift)
        session.commit()
        session.refresh(shift)
        return shift


def update_shift(
    shift_id: int,
    *,
    name: str,
    start_time: str,
    end_time: str,
    work_hours: float,
    is_special: bool,
    enabled: bool,
) -> Shift:
    with Session(engine) as session:
        shift = session.get(Shift, shift_id)
        if shift is None:
            raise DomainError("班次不存在")
        plan = _load_plan(session, shift.plan_id)
        _validate_time_format(start_time, end_time)
        shift.name = name
        shift.start_time = start_time
        shift.end_time = end_time
        shift.work_hours = work_hours
        shift.enabled = enabled
        if is_special != shift.is_special:
            shift.is_special = is_special
            shift.special_source = "manual"
            shift.special_reason = "人工设为盘点班" if is_special else "人工设为普通班"
        _validate_special(shift.is_off, shift.is_leave, work_hours, start_time, end_time)
        error = shift_window_error(
            plan.coverage_soft_start_effective,
            plan.coverage_soft_end_effective,
            shift,
            plan.latest_end_time,
        )
        if error:
            raise DomainError(error)
        session.add(shift)
        session.commit()
        session.refresh(shift)
        return shift


def delete_shift(shift_id: int) -> None:
    with Session(engine) as session:
        shift = session.get(Shift, shift_id)
        if shift is None:
            raise DomainError("班次不存在")
        rule_refs = session.exec(select(Rule).where(Rule.shift_id == shift_id)).all()
        assignment_refs = session.exec(
            select(Assignment).where(Assignment.shift_id == shift_id)
        ).all()
        if rule_refs or assignment_refs:
            raise DomainError("班次已被规则或排班引用，无法删除")
        session.delete(shift)
        session.commit()


def import_shifts(plan_id: int) -> list[Shift]:
    with Session(engine) as session:
        _load_plan(session, plan_id)
        rules = session.exec(select(Rule).where(Rule.plan_id == plan_id)).all()
        if any(rule.shift_id is not None for rule in rules):
            raise DomainError("已有规则引用班次，无法重新导入班次代码")
        assignments = session.exec(select(Assignment).where(Assignment.plan_id == plan_id)).all()
        if assignments:
            raise DomainError("已有排班记录，无法重新导入班次代码")

        existing = session.exec(select(Shift).where(Shift.plan_id == plan_id)).all()
        overrides = {
            shift.name: shift_import.SpecialOverride(shift.is_special, shift.special_reason)
            for shift in existing
            if shift.special_source == "manual"
        }
        for shift in existing:
            session.delete(shift)
        session.flush()

        templates = shift_import.load_shift_templates()
        shifts = shift_import.build_shifts(plan_id, templates, overrides)
        for shift in shifts:
            session.add(shift)
        session.commit()
        for shift in shifts:
            session.refresh(shift)
        return shifts
