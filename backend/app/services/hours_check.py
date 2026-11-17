# pyright: reportExplicitAny=false, reportImplicitStringConcatenation=false
# 说明：补排候选与缺口汇总返回的是给前端直接消费的 JSON 结构，值类型天然异构；
# 刻意换行拼接的日志文案属于业务可读性写法，这里只关闭由此产生的类型提示，真实错误仍然保留。
from collections import Counter
from datetime import datetime
from typing import Any, cast

from sqlmodel import Session

from ..models import AdjustmentLog, Assignment, Employee, Plan, Rule, Shift
from .calendar_service import (
    effective_units,
    intersect_dates,
    month_dates,
    units_to_hours,
    window_violation,
)
from .rule_fields import monthly_hours_target


def _shift_hours(shift: Shift) -> float:
    return units_to_hours(effective_units(shift.work_hours, shift.start_time, shift.end_time))


def _counted_hours(item: Assignment, shift_map: dict[int, Shift]) -> float:
    """仅统计实际出勤与有薪休假（POFF 固定 8 小时），OFF 休息不计入工时。"""
    if item.shift_id is None:
        return max(0.0, float(item.hours))
    shift = shift_map.get(item.shift_id)
    if shift is None:
        return 0.0
    if shift.is_off:
        return 0.0
    return max(0.0, float(item.hours))


def _eligible(plan: Plan, employee: Employee, shift: Shift) -> bool:
    if not shift.enabled or shift.is_leave or shift.is_off:
        return False
    if shift.is_special and not employee.allow_special:
        return False
    hours = _shift_hours(shift)
    if hours <= 0 or hours > employee.max_daily_hours + 1e-6:
        return False
    return (
        window_violation(
            plan.coverage_soft_start_effective,
            plan.coverage_soft_end_effective,
            shift.start_time,
            shift.end_time,
            plan.latest_end_time,
        )
        is None
    )


def _blocked_dates(plan: Plan, employee_id: int, rules: list[Rule]) -> set[str]:
    """休息与有薪休假日期不可再排班，避免与既有规则冲突。"""
    blocked: set[str] = set()
    for rule in rules:
        if not rule.valid or rule.employee_id != employee_id:
            continue
        if rule.type in ("REST", "PAID_LEAVE"):
            blocked.update(intersect_dates(rule.date_start, rule.date_end, plan.month))
    return blocked


def _preferred_shifts(
    employee: Employee,
    shifts: list[Shift],
    assignments: list[Assignment],
    shift_map: dict[int, Shift],
) -> list[Shift]:
    """补排优先沿用本人常用班次，其次沿用全计划常用班次，最后按时长排序。"""
    own_count: Counter[int] = Counter()
    plan_count: Counter[int] = Counter()
    for item in assignments:
        if item.shift_id is None:
            continue
        shift = shift_map.get(item.shift_id)
        if shift is None or shift.is_leave or shift.is_off:
            continue
        plan_count[item.shift_id] += 1
        if item.employee_id == employee.id:
            own_count[item.shift_id] += 1

    ordered: list[Shift] = []
    seen: set[int] = set()
    for counter in (own_count, plan_count):
        for shift_id, _ in counter.most_common():
            shift = shift_map.get(shift_id)
            if shift is not None and shift.id not in seen:
                seen.add(cast(int, shift.id))
                ordered.append(shift)
    for shift in sorted(shifts, key=lambda item: (item.start_time, item.sort_order, item.id or 0)):
        if shift.id not in seen and not shift.is_leave and not shift.is_off:
            seen.add(cast(int, shift.id))
            ordered.append(shift)
    return ordered


def _rule_decision(
    _plan: Plan, employee: Employee, day: str, rules: list[Rule]
) -> tuple[set[int], set[str]]:
    """返回当天规则要求的班次与上班时间，用于补排班次的合规选择。"""
    shift_ids: set[int] = set()
    start_times: set[str] = set()
    for rule in rules:
        if not rule.valid or not rule.date_start <= day <= rule.date_end:
            continue
        if rule.type == "FIXED_SHIFT":
            if rule.employee_id != employee.id:
                continue
            if rule.shift_id:
                shift_ids.add(rule.shift_id)
        elif rule.type == "FIXED_TIME":
            if rule.employee_id != employee.id:
                continue
            if rule.start_time:
                start_times.add(rule.start_time)
        elif rule.type == "ALL_START_AT":
            if rule.start_time:
                start_times.add(rule.start_time)
    return shift_ids, start_times


def _pick_shift(
    plan: Plan,
    employee: Employee,
    day: str,
    ordered: list[Shift],
    rules: list[Rule],
) -> Shift | None:
    shift_ids, start_times = _rule_decision(plan, employee, day, rules)
    if shift_ids:
        candidates = [shift for shift in ordered if shift.id in shift_ids]
        return next(
            (shift for shift in candidates if _eligible(plan, employee, shift)), None
        )
    if start_times:
        candidates = [shift for shift in ordered if shift.start_time in start_times]
        return next(
            (shift for shift in candidates if _eligible(plan, employee, shift)), None
        )
    return next((shift for shift in ordered if _eligible(plan, employee, shift)), None)


def _candidates(
    plan: Plan,
    employee: Employee,
    shifts: list[Shift],
    rules: list[Rule],
    assignments: list[Assignment],
    shift_map: dict[int, Shift],
) -> list[dict[str, Any]]:
    occupied = {item.date for item in assignments if item.employee_id == employee.id}
    blocked = _blocked_dates(plan, cast(int, employee.id), rules)
    ordered = _preferred_shifts(employee, shifts, assignments, shift_map)
    result: list[dict[str, Any]] = []
    for day in month_dates(plan.month):
        if day in occupied or day in blocked:
            continue
        shift = _pick_shift(plan, employee, day, ordered, rules)
        if shift is None:
            continue
        result.append(
            {
                "date": day,
                "shift_id": shift.id,
                "shift_name": shift.name,
                "start_time": shift.start_time,
                "end_time": shift.end_time,
                "hours": _shift_hours(shift),
            }
        )
    return result


def build_gap(
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    rules: list[Rule],
    assignments: list[Assignment],
) -> dict[str, Any]:
    shift_map = {cast(int, shift.id): shift for shift in shifts}
    rows: list[dict[str, Any]] = []
    for employee in employees:
        own = [item for item in assignments if item.employee_id == employee.id]
        standard = round(monthly_hours_target(employee, rules, plan.month)[0], 2)
        actual = round(sum(_counted_hours(item, shift_map) for item in own), 2)
        gap = round(max(0.0, standard - actual), 2) if standard > 0 else 0.0
        rows.append(
            {
                "employee_id": employee.id,
                "name": employee.name,
                "standard_hours": standard,
                "actual_hours": actual,
                "gap_hours": gap,
                "candidates": _candidates(plan, employee, shifts, rules, assignments, shift_map)
                if gap > 0
                else [],
            }
        )
    employee_ids = {employee.id for employee in employees}
    standard_total = round(
        sum(monthly_hours_target(employee, rules, plan.month)[0] for employee in employees), 2
    )
    actual_total = round(
        sum(
            _counted_hours(item, shift_map)
            for item in assignments
            if item.employee_id in employee_ids
        ),
        2,
    )
    gap_total = round(sum(row["gap_hours"] for row in rows), 2)
    return {
        "month": plan.month,
        "standard_hours": standard_total,
        "actual_hours": actual_total,
        "gap_hours": gap_total,
        "filled": gap_total <= 0.001,
        "employees": rows,
    }


def fill_gap(
    session: Session,
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    rules: list[Rule],
    assignments: list[Assignment],
    items: list[tuple[int, str]],
    note: str | None = None,
) -> tuple[list[Assignment], list[AdjustmentLog]]:
    employee_map = {employee.id: employee for employee in employees}
    shift_map = {cast(int, shift.id): shift for shift in shifts}
    month_days = set(month_dates(plan.month))
    created: list[Assignment] = []
    logs: list[AdjustmentLog] = []
    now = datetime.now().isoformat(timespec="seconds")
    assert plan.id is not None

    for employee_id, day in items:
        employee = employee_map.get(employee_id)
        if employee is None:
            raise ValueError("人员不存在或不属于该计划")
        if day not in month_days:
            raise ValueError(f"日期 {day} 不在计划月份内")
        if any(
            item.employee_id == employee_id and item.date == day
            for item in assignments
        ):
            raise ValueError(f"{employee.name} 在 {day} 已有排班，不能按休息日调整")
        if day in _blocked_dates(plan, employee_id, rules):
            raise ValueError(f"{employee.name} 在 {day} 已设置为休息或有薪休假，不能调整")
        ordered = _preferred_shifts(employee, shifts, assignments, shift_map)
        shift = _pick_shift(plan, employee, day, ordered, rules)
        if shift is None:
            raise ValueError(f"{employee.name} 在 {day} 没有符合时段与资格要求的班次")

        own = [item for item in assignments if item.employee_id == employee_id]
        standard = round(monthly_hours_target(employee, rules, plan.month)[0], 2)
        actual = round(sum(_counted_hours(item, shift_map) for item in own), 2)
        hours = _shift_hours(shift)
        assignment = Assignment(
            plan_id=plan.id,
            employee_id=employee_id,
            date=day,
            shift_id=shift.id,
            start_time=shift.start_time,
            end_time=shift.end_time,
            hours=hours,
            locked=False,
            source="manual",
        )
        log = AdjustmentLog(
            plan_id=plan.id,
            employee_id=employee_id,
            date=day,
            shift_id=shift.id,
            hours=hours,
            action="补足工时",
            detail=(
                f"标准 {standard:.2f} 小时，已排 {actual:.2f} 小时，"
                f"补排 {shift.name} {shift.start_time}-{shift.end_time} {hours:.2f} 小时"
                + (f"；备注：{note}" if note else "")
            ),
            created_at=now,
        )
        session.add(assignment)
        session.add(log)
        assignments.append(assignment)
        created.append(assignment)
        logs.append(log)

    session.flush()
    for item in created:
        session.refresh(item)
    for log in logs:
        session.refresh(log)
    return created, logs
