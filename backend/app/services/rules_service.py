# pyright: reportAny=false, reportUnknownMemberType=false
"""规则域应用服务：把 routers/rules.build_rule 与冲突预检搬到同进程可调用层。

GUI 与（后续移除的）HTTP 层共用同一套口径：校验失败抛 DomainError，
冲突阻断抛 RuleConflictError（携带冲突明细）。
"""
from __future__ import annotations

import random
import re
from datetime import date, timedelta
from typing import Any, NoReturn

from sqlmodel import Session, select

from ..db import engine
from ..errors import DomainError
from ..models import TYPE_DEFAULT_HARD, Assignment, Employee, Plan, Rule, Shift
from ..schemas import RuleCreate
from . import rule_checker
from .calendar_service import TIME_PATTERN, intersect_dates, month_bounds, month_dates
from .rule_fields import (
    DAY_COUNT_TYPES,
    ROLE_TYPES,
    SINGLE_ROLE_TYPES,
    WEEKDAY_TYPES,
    count_bounds,
    normalize_role,
    normalize_weekdays,
)


class RuleConflictError(DomainError):
    """规则冲突：携带与 HTTP 409 同构的冲突明细，GUI 可展示并支持强制写入。"""

    def __init__(self, message: str, items: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.items = items


def _bad(message: str) -> NoReturn:
    raise DomainError(message)


def _load_plan(session: Session, plan_id: int) -> Plan:
    plan = session.get(Plan, plan_id)
    if plan is None:
        raise DomainError("计划不存在")
    return plan


def _check_time(value: str | None, label: str) -> None:
    if value is None or not re.match(TIME_PATTERN, value):
        _bad(f"{label}必须是 HH:MM 格式")


def _validate_range(payload: RuleCreate, plan: Plan) -> tuple[str, str]:
    month_start, month_end = month_bounds(plan.month)
    if not payload.date_start or not payload.date_end:
        _bad("规则日期不能为空")
    try:
        start = date.fromisoformat(payload.date_start)
        end = date.fromisoformat(payload.date_end)
    except ValueError:
        _bad("规则日期格式不正确")
    if start.isoformat() < month_start or end.isoformat() > month_end:
        _bad(f"规则日期必须落在 {month_start} 至 {month_end} 之间")
    if start > end:
        _bad("规则开始日期不能晚于结束日期")
    return start.isoformat(), end.isoformat()


def rest_hints(plan: Plan, payloads: list[RuleCreate]) -> dict[int, set[str]]:
    """收集同一批次内待写入的 REST 区间，供 PAID_LEAVE 落库前挑选假期日期。"""
    hints: dict[int, set[str]] = {}
    for item in payloads:
        if item.type != "REST" or item.employee_id is None:
            continue
        if not item.date_start or not item.date_end:
            continue
        try:
            days = intersect_dates(item.date_start, item.date_end, plan.month)
        except ValueError:
            continue
        hints.setdefault(item.employee_id, set()).update(days)
    return hints


def _random_leave_dates(
    plan: Plan,
    session: Session,
    employee: Employee,
    days: int,
    rest_hint: set[str] | None = None,
) -> list[str]:
    """在当月随机挑 days 个分散日期，优先落在该员工同期 REST 区间内。"""
    dates = month_dates(plan.month)
    days = max(1, min(days, len(dates)))
    rest_days: set[str] = set(rest_hint or ())
    occupied: set[str] = set()
    rules = session.exec(
        select(Rule).where(Rule.plan_id == plan.id, Rule.employee_id == employee.id)
    ).all()
    for rule in rules:
        if not rule.valid:
            continue
        rule_dates = intersect_dates(rule.date_start, rule.date_end, plan.month)
        if rule.type == "REST":
            rest_days.update(rule_dates)
        if rule.type in ("MUST_WORK", "PAID_LEAVE", "FIXED_SHIFT", "FIXED_TIME"):
            occupied.update(rule_dates)
    picked: list[str] = []
    tiers = [
        [day for day in dates if day in rest_days and day not in occupied],
        [day for day in dates if day not in occupied],
        list(dates),
    ]
    for pool in tiers:
        if len(picked) >= days:
            break
        available = [day for day in pool if day not in picked]
        picked.extend(random.sample(available, min(days - len(picked), len(available))))
    return sorted(picked)


def _paid_leave_intervals(
    payload: RuleCreate,
    plan: Plan,
    session: Session,
    employee: Employee,
    rest_hint: set[str] | None = None,
) -> tuple[list[tuple[str, str]], int, str | None]:
    """按「天数 + 日期」确定有薪休假日集合，返回 (区间列表, 实际天数, 说明)。"""
    target = max(1, min(payload.count or 1, 31))
    has_start = bool(payload.date_start)
    has_end = bool(payload.date_end)
    explicit_days = payload.count is not None and payload.count >= 1
    _, month_end = month_bounds(plan.month)

    if not has_start and not has_end:
        days = _random_leave_dates(plan, session, employee, target, rest_hint)
        return [(day, day) for day in days], len(days), None

    if has_start and (not has_end or payload.date_end == payload.date_start):
        start_text, _ = _validate_range(
            payload.model_copy(update={"date_end": payload.date_start}), plan
        )
        start = date.fromisoformat(start_text)
        end = min(start + timedelta(days=target - 1), date.fromisoformat(month_end))
        return [(start_text, end.isoformat())], (end - start).days + 1, None

    start_text, end_text = _validate_range(payload, plan)
    start = date.fromisoformat(start_text)
    end = date.fromisoformat(end_text)
    span = (end - start).days + 1
    if not explicit_days or span == target:
        return [(start_text, end_text)], span, None
    if span > target:
        offset = random.randint(0, span - target)
        picked = start + timedelta(days=offset)
        return (
            [(picked.isoformat(), (picked + timedelta(days=target - 1)).isoformat())],
            target,
            None,
        )
    return [(start_text, end_text)], span, f"区间只有 {span} 天，已按 {span} 天休假"


def build_rule(
    payload: RuleCreate,
    plan: Plan,
    session: Session,
    source: str,
    rest_hint: set[str] | None = None,
) -> list[Rule]:
    rule_type = payload.type
    employee = None
    if payload.employee_id is not None:
        employee = session.get(Employee, payload.employee_id)
        if employee is None or employee.plan_id != plan.id:
            _bad("规则引用的人员不属于该计划")
    shift = None
    if payload.shift_id is not None:
        shift = session.get(Shift, payload.shift_id)
        if shift is None or shift.plan_id != plan.id:
            _bad("规则引用的班次不属于该计划")

    count = payload.count
    # REST / MUST_WORK 允许不指定人员：employee_id=None 表示全员生效
    if rule_type in ("PAID_LEAVE", "FIXED_SHIFT", "FIXED_TIME", "SHIFT_LIMIT"):
        if employee is None:
            _bad("该规则类型必须指定人员")
    if rule_type in (
        "DAY_STAFF",
        "SEGMENT_STAFF",
        "SHIFT_STAFF",
        "START_STAFF",
        "END_STAFF",
        "ALL_START_AT",
        "MAX_STAFF",
        "SINGLE_ROLE_SHIFT",
    ):
        employee = None
    if rule_type == "MAX_STAFF":
        shift = None
    if rule_type in (
        "SHIFT_HOURS",
        "MAX_REST_DAYS",
        "MIN_REST_DAYS",
        "MAX_WORK_DAYS",
        "MAX_CONSECUTIVE_WORK",
    ):
        shift = None
    if rule_type == "FIXED_SHIFT" and shift is None:
        _bad("指定班次规则必须选择班次")
    if rule_type == "SHIFT_STAFF" and shift is None:
        _bad("盘点班次规则必须选择班次")
    if rule_type == "BAN_SHIFT" and shift is None:
        _bad("禁止班次规则必须选择班次")
    if rule_type == "SINGLE_ROLE_SHIFT" and shift is None:
        _bad("岗位单人班次规则必须选择班次")
    if rule_type in ("FIXED_TIME", "ALL_START_AT", "START_STAFF"):
        _check_time(payload.start_time, "上班时刻")
    if rule_type == "SEGMENT_STAFF":
        _check_time(payload.start_time, "时段开始时间")
        _check_time(payload.end_time, "时段结束时间")
    if rule_type == "END_STAFF":
        _check_time(payload.end_time, "下班时刻")
    if rule_type == "SHIFT_LIMIT":
        if payload.shift_id is None and not payload.start_time and not payload.end_time:
            _bad("班次限制规则必须选择班次，或填写上班/下班时刻")
        if payload.start_time:
            _check_time(payload.start_time, "上班时刻")
        if payload.end_time:
            _check_time(payload.end_time, "下班时刻")
    if rule_type == "WORK_WINDOW":
        _check_time(payload.start_time, "工作时段开始时间")
        _check_time(payload.end_time, "工作时段结束时间")
        assert payload.start_time and payload.end_time
        if payload.start_time >= payload.end_time:
            _bad("工作时段结束时间必须晚于开始时间")

    time_op: str | None = None
    time_op_note: str | None = None
    if rule_type in ("START_STAFF", "END_STAFF", "SHIFT_LIMIT"):
        time_op = payload.time_op or ("ge" if rule_type == "SHIFT_LIMIT" else "eq")
    elif payload.time_op and payload.time_op != "eq":
        time_op_note = f"{rule_type} 不支持时刻条件，time_op 已忽略"

    count_op: str | None = None
    count_op_note: str | None = None
    if rule_type in ("START_STAFF", "END_STAFF"):
        count_op = payload.count_op or "ge"
    elif payload.count_op and payload.count_op != "ge":
        count_op_note = f"{rule_type} 不支持人数条件，count_op 已忽略"

    work_hours: float | None = None
    hours_op: str | None = None
    if rule_type == "SHIFT_HOURS":
        if payload.work_hours is None:
            _bad("班次工时规则必须填写工时阈值")
        work_hours = float(payload.work_hours)
        if not 0 < work_hours <= 24:
            _bad("班次工时规则的工时阈值必须大于 0 且不超过 24 小时")
        hours_op = payload.hours_op or "eq"
        if hours_op not in ("eq", "ge", "le"):
            _bad("工时比较方式只能是 eq（等于）、ge（不少于）、le（不超过）")

    period: str | None = None
    period_note: str | None = None
    if rule_type in DAY_COUNT_TYPES:
        period = payload.period or "month"
    elif payload.period:
        period_note = f"{rule_type} 不支持统计周期，period 已忽略"

    if rule_type in ("DAY_STAFF", "SEGMENT_STAFF", "SHIFT_STAFF", "START_STAFF", "END_STAFF"):
        _, high = count_bounds(rule_type)
        if count is None or not 1 <= count <= high:
            _bad("该规则类型必须填写不小于 1 的人数")
    elif rule_type == "MAX_STAFF":
        _, high = count_bounds(rule_type)
        if count is None or not 0 <= count <= high:
            _bad("人数上限规则必须填写 0~99 的人数")
    elif rule_type == "SINGLE_ROLE_SHIFT":
        _, high = count_bounds(rule_type)
        if count is None:
            count = 1
        if not 1 <= count <= high:
            _bad("岗位单人班次规则的人数阈值需为 1~99")
    elif rule_type == "MAX_REST_DAYS":
        _, high = count_bounds(rule_type)
        if count is None or not 1 <= count <= high:
            _bad("连续休息上限规则必须填写 1~15 天的连休上限")
    elif rule_type == "MAX_CONSECUTIVE_WORK":
        _, high = count_bounds(rule_type)
        if count is None or not 1 <= count <= high:
            _bad("连续上班上限规则必须填写 1~31 天的连续上班上限")
    elif rule_type == "MONTHLY_HOURS":
        _, high = count_bounds(rule_type)
        if count is None or not 1 <= count <= high:
            _bad("月度工时规则必须填写 1~744 小时的目标工时")
    elif rule_type in DAY_COUNT_TYPES:
        _, high = count_bounds(rule_type)
        if count is None or not 1 <= count <= high:
            label = "最少休息天数" if rule_type == "MIN_REST_DAYS" else "最多上班天数"
            _bad(f"{label}规则必须填写 1~31 天的整数")
    elif rule_type != "PAID_LEAVE":
        count = None

    if rule_type in (
        "REST",
        "MUST_WORK",
        "FIXED_SHIFT",
        "MAX_STAFF",
        "SHIFT_HOURS",
        "MAX_REST_DAYS",
        "MIN_REST_DAYS",
        "MAX_WORK_DAYS",
        "MAX_CONSECUTIVE_WORK",
        "BAN_SHIFT",
    ):
        payload_start = None
    else:
        payload_start = payload.start_time

    weekdays, weekday_note = (None, None)
    if payload.weekdays:
        if rule_type in WEEKDAY_TYPES:
            weekdays, weekday_note = normalize_weekdays(payload.weekdays)
        else:
            weekday_note = f"{rule_type} 不支持按星期限定，星期设置已忽略"

    role: str | None = None
    role_note: str | None = None
    if rule_type in ROLE_TYPES or rule_type in SINGLE_ROLE_TYPES:
        role, role_note = normalize_role(payload.role)
        if rule_type in SINGLE_ROLE_TYPES and not role:
            _bad("岗位单人班次规则必须填写岗位")
    elif payload.role:
        role_note = f"{rule_type} 不支持岗位限定，岗位设置已忽略"

    leave_note: str | None = None
    if rule_type == "PAID_LEAVE":
        assert employee is not None
        intervals, count, leave_note = _paid_leave_intervals(
            payload, plan, session, employee, rest_hint
        )
    else:
        intervals = [_validate_range(payload, plan)]

    hard = payload.hard
    if hard is None:
        hard = TYPE_DEFAULT_HARD[rule_type]

    note = payload.note
    for extra_note in (leave_note, weekday_note, time_op_note, count_op_note, period_note, role_note):
        if extra_note:
            note = f"{note}；{extra_note}" if note else extra_note

    assert plan.id is not None
    result: list[Rule] = []
    for date_start, date_end in intervals:
        rule_count = count
        if rule_type == "PAID_LEAVE":
            rule_count = len(intersect_dates(date_start, date_end, plan.month))
        result.append(
            Rule(
                plan_id=plan.id,
                employee_id=employee.id if employee else None,
                type=rule_type,
                date_start=date_start,
                date_end=date_end,
                shift_id=shift.id if shift else None,
                start_time=payload_start,
                end_time=(
                    payload.end_time
                    if rule_type in ("SEGMENT_STAFF", "END_STAFF", "SHIFT_LIMIT", "WORK_WINDOW")
                    else None
                ),
                count=rule_count,
                hard=hard,
                raw_text=payload.raw_text,
                source=source,
                valid=True,
                note=note,
                work_hours=work_hours,
                hours_op=hours_op,
                weekdays=weekdays,
                time_op=time_op,
                count_op=count_op,
                period=period,
                role=role,
            )
        )
    return result


def _precheck(
    plan: Plan,
    session: Session,
    candidates: list[Rule],
    exclude_rule_id: int | None = None,
) -> list[dict[str, Any]]:
    """落库前的确定性冲突预检；只返回新规则参与的冲突。"""
    if not candidates:
        return []
    employees = list(
        session.exec(
            select(Employee)
            .where(Employee.plan_id == plan.id)
            .order_by(Employee.sort_order, Employee.id)  # pyright: ignore[reportArgumentType]
        ).all()
    )
    shifts = list(
        session.exec(
            select(Shift)
            .where(Shift.plan_id == plan.id)
            .order_by(Shift.sort_order, Shift.id)  # pyright: ignore[reportArgumentType]
        ).all()
    )
    rules = list(
        session.exec(
            select(Rule).where(Rule.plan_id == plan.id).order_by(Rule.id)  # pyright: ignore[reportArgumentType]
        ).all()
    )
    existing = [rule for rule in rules if rule.id != exclude_rule_id]
    result = rule_checker.check_rule_conflicts(plan, employees, shifts, existing, candidates)
    return [item for item in result["items"] if item.get("new_rule_indexes")]


def _blocking(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if item["severity"] == "error"]


def list_rules(
    plan_id: int,
    rule_type: str | None = None,
    employee_id: int | None = None,
) -> list[Rule]:
    with Session(engine) as session:
        _load_plan(session, plan_id)
        statement = select(Rule).where(Rule.plan_id == plan_id)
        if rule_type:
            statement = statement.where(Rule.type == rule_type)
        if employee_id is not None:
            statement = statement.where(Rule.employee_id == employee_id)
        return list(
            session.exec(
                statement.order_by(Rule.date_start, Rule.id)  # pyright: ignore[reportArgumentType]
            ).all()
        )


def create_rules(
    plan_id: int,
    payloads: list[RuleCreate],
    *,
    source: str = "manual",
    precheck: bool = True,
    force: bool = False,
) -> list[Rule]:
    with Session(engine) as session:
        plan = _load_plan(session, plan_id)
        hints = rest_hints(plan, payloads)
        rules = [
            rule
            for item in payloads
            for rule in build_rule(item, plan, session, source, hints.get(item.employee_id or 0))
        ]
        items = _precheck(plan, session, rules) if precheck else []
        blocking = _blocking(items)
        if blocking and not force:
            raise RuleConflictError("新增约束与现有约束冲突", blocking)
        for rule in rules:
            session.add(rule)
        session.commit()
        for rule in rules:
            session.refresh(rule)
        return rules


def update_rule(
    rule_id: int,
    payload: RuleCreate,
    *,
    precheck: bool = True,
    force: bool = False,
) -> list[Rule]:
    with Session(engine) as session:
        rule = session.get(Rule, rule_id)
        if rule is None:
            raise DomainError("规则不存在")
        plan = _load_plan(session, rule.plan_id)
        updated = build_rule(payload, plan, session, rule.source)
        # 编辑有薪休假时留空日期：保留原日期，避免隐式随机挪动假期
        if (
            payload.type == "PAID_LEAVE"
            and not payload.date_start
            and not payload.date_end
            and rule.date_start
            and rule.date_end
        ):
            updated = [updated[0]]
            updated[0].date_start = rule.date_start
            updated[0].date_end = rule.date_end
            if payload.count is not None and payload.count >= 1:
                updated[0].count = payload.count
        items = _precheck(plan, session, updated, exclude_rule_id=rule_id) if precheck else []
        blocking = _blocking(items)
        if blocking and not force:
            raise RuleConflictError("修改后的约束与现有约束冲突", blocking)
        if len(updated) == 1:
            new_rule = updated[0]
            for field in (
                "employee_id",
                "type",
                "date_start",
                "date_end",
                "shift_id",
                "start_time",
                "end_time",
                "count",
                "hard",
                "raw_text",
                "note",
                "work_hours",
                "hours_op",
                "weekdays",
                "time_op",
                "count_op",
                "period",
                "role",
            ):
                setattr(rule, field, getattr(new_rule, field))
            rule.valid = True
            session.add(rule)
            session.commit()
            session.refresh(rule)
            return [rule]
        session.delete(rule)
        for new_rule in updated:
            session.add(new_rule)
        session.commit()
        for new_rule in updated:
            session.refresh(new_rule)
        return updated


def delete_rules(plan_id: int, rule_ids: list[int]) -> tuple[int, list[int]]:
    """批量删除规则，返回 (删除数, 未找到的 id 列表)。"""
    with Session(engine) as session:
        _load_plan(session, plan_id)
        ids = list(dict.fromkeys(rule_ids))
        if not ids:
            raise DomainError("请至少选择 1 条规则")
        if len(ids) > 200:
            raise DomainError("一次最多删除 200 条规则，请分批操作")
        rules = session.exec(
            select(Rule).where(Rule.plan_id == plan_id, Rule.id.in_(ids))  # pyright: ignore[reportArgumentType]
        ).all()
        found = {rule.id for rule in rules}
        not_found = [rule_id for rule_id in ids if rule_id not in found]
        for rule in rules:
            session.delete(rule)
        session.commit()
        return len(rules), not_found


def count_assignments(plan_id: int) -> int:
    with Session(engine) as session:
        return len(session.exec(select(Assignment).where(Assignment.plan_id == plan_id)).all())


def conflict_error_text(items: list[dict[str, Any]]) -> str:
    return "\n".join(f"· {item.get('message', '')}" for item in items)
