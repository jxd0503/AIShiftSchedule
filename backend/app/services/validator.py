# pyright: reportAny=false, reportExplicitAny=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportImplicitStringConcatenation=false
# 说明：校验报告是字段随冲突码变化的动态字典，统一用 Any 承载；
# 这里只关闭由此产生的类型噪音，真实错误仍然保留。
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from ..models import Assignment, Employee, Plan, Rule, Shift
from .conflict_codes import SOFT_RELAXED
from .calendar_service import (
    coverage_shortfall,
    effective_hours,
    format_hhmm,
    month_dates,
    parse_hhmm,
    parse_interval,
    parse_weekdays,
    period_buckets,
    rule_month_dates,
    weekly_quota,
    window_violation,
)
from .rule_fields import monthly_hours_target


def _rule_dates(rule: Rule, month: str) -> list[str]:
    """规则生效日期：规则区间与计划月的交集，再按 rule.weekdays 过滤。

    校验侧的所有逐日检查都必须走这里，否则「仅周日」规则会在工作日误报，
    与求解器的 _expand（同样按 weekdays 过滤）口径不一致。
    带 weekdays 时以该月 1 号为遍历起点，避免丢掉月首命中的那一天。
    """
    return rule_month_dates(rule.date_start, rule.date_end, rule.weekdays, month)


def _consecutive_runs(worked_dates: set[str], month: str) -> list[tuple[str, str, int]]:
    runs: list[tuple[str, str, int]] = []
    start: str | None = None
    end: str | None = None
    count = 0
    for day in month_dates(month):
        if day in worked_dates:
            if start is None:
                start = day
            end = day
            count += 1
            continue
        if start is not None and end is not None:
            runs.append((start, end, count))
        start = None
        end = None
        count = 0
    if start is not None and end is not None:
        runs.append((start, end, count))
    return runs


def _rule_segments(rule_dates: Sequence[str]) -> list[list[str]]:
    """按日历连续性把规则生效日期切段（星期过滤造成的间隙会切段）。

    与求解器 _rest_limit_windows 的切段口径一致，避免校验把规则日期间的
    非规则日当作连续的休息/上班段。
    """
    segments: list[list[str]] = []
    current: list[str] = []
    previous: date | None = None
    for day in rule_dates:
        parsed = date.fromisoformat(day)
        if previous is not None and (parsed - previous).days > 1:
            if current:
                segments.append(current)
            current = []
        current.append(day)
        previous = parsed
    if current:
        segments.append(current)
    return segments


def _consecutive_runs_on(
    worked_dates: set[str], rule_dates: Sequence[str]
) -> list[tuple[str, str, int]]:
    """规则生效日期上的连续上班段（用于规则级连续上班上限校验）。"""
    runs: list[tuple[str, str, int]] = []
    for segment in _rule_segments(rule_dates):
        run_start: str | None = None
        run_end: str | None = None
        count = 0
        for day in segment:
            if day in worked_dates:
                if run_start is None:
                    run_start = day
                run_end = day
                count += 1
                continue
            if run_start is not None and run_end is not None:
                runs.append((run_start, run_end, count))
            run_start = None
            run_end = None
            count = 0
        if run_start is not None and run_end is not None:
            runs.append((run_start, run_end, count))
    return runs


def _conflict(
    code: str, message: str, rule_ids: Sequence[int | None] | None = None
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "rule_ids": [item for item in rule_ids or [] if item is not None],
    }


# 规则满足情况的中文标签
_RULE_STATUS_LABELS = {
    "REST": "休息",
    "MUST_WORK": "必须上班",
    "PAID_LEAVE": "有薪休假",
    "FIXED_SHIFT": "指定班次",
    "FIXED_TIME": "指定上班时间",
    "ALL_START_AT": "全员统一上班",
    "DAY_STAFF": "每日人数",
    "SEGMENT_STAFF": "时段人数",
    "START_STAFF": "上班时刻人数",
    "END_STAFF": "下班时刻人数",
    "SHIFT_STAFF": "指定班次人数",
    "MAX_STAFF": "人数上限",
    "SHIFT_HOURS": "班次工时",
    "MAX_REST_DAYS": "连续休息上限",
    "MIN_REST_DAYS": "最少休息天数",
    "MAX_WORK_DAYS": "最多上班天数",
    "MAX_CONSECUTIVE_WORK": "连续上班上限",
    "BAN_SHIFT": "禁止班次",
    "SHIFT_LIMIT": "班次限制",
    "SINGLE_ROLE_SHIFT": "岗位单人班次",
    "WORK_WINDOW": "工作时段",
    "MONTHLY_HOURS": "月工时",
}


def _rule_text(
    rule: Rule,
    employee_map: dict[int, Employee] | None = None,
    shift_map: dict[int, Shift] | None = None,
) -> str:
    """规则摘要文案（用于规则满足情况展示）。"""
    label = _RULE_STATUS_LABELS.get(rule.type, rule.type)
    parts = [label]
    if rule.employee_id is not None:
        employee = (employee_map or {}).get(rule.employee_id)
        parts.append(f"（{employee.name}）" if employee is not None else f"（人员 #{rule.employee_id}）")
    if rule.type == "SHIFT_HOURS":
        target = rule.work_hours or 0.0
        op = rule.hours_op or "eq"
        if op == "ge":
            parts.append(f"不少于 {target:g} 小时")
        elif op == "le":
            parts.append(f"不超过 {target:g} 小时")
        else:
            parts.append(f"等于 {target:g} 小时")
    elif rule.type == "MAX_REST_DAYS":
        parts.append(f"最多连休 {rule.count or 0} 天")
    elif rule.type == "MAX_CONSECUTIVE_WORK":
        parts.append(f"最多连续 {rule.count or 0} 天")
    elif rule.type in ("MIN_REST_DAYS", "MAX_WORK_DAYS"):
        scope = "每周" if (rule.period or "month") == "week" else "每月"
        verb = "至少休" if rule.type == "MIN_REST_DAYS" else "最多上"
        parts.append(f"{scope}{verb} {rule.count or 0} 天")
    elif rule.type == "BAN_SHIFT":
        parts.append("禁止排该班次")
    elif rule.type == "SHIFT_LIMIT":
        parts.append("上班日仅限")
    elif rule.type == "SINGLE_ROLE_SHIFT":
        parts.append(f"岗位「{rule.role or ''}」上班 ≤{rule.count or 1} 人时必须上")
    elif rule.type == "WORK_WINDOW":
        # 岗位与时段由下方通用逻辑补齐，避免重复展示
        parts.append("上班时段必须落在")
    elif rule.type == "MONTHLY_HOURS":
        parts.append(f"{rule.count or 0} 小时")
    elif rule.count is not None:
        if rule.type in ("START_STAFF", "END_STAFF") and (rule.count_op or "ge") == "le":
            parts.append(f"至多 {rule.count} 人")
        else:
            parts.append(f"{rule.count} 人")
    if rule.role and rule.type != "SINGLE_ROLE_SHIFT":
        parts.append(f"岗位 {rule.role}")
    if rule.start_time and rule.end_time:
        parts.append(f"{rule.start_time}-{rule.end_time}")
    elif rule.start_time:
        op_text = ""
        if rule.type in ("START_STAFF", "END_STAFF", "SHIFT_LIMIT") and (rule.time_op or "eq") != "eq":
            op_text = "不晚于" if rule.time_op == "le" else "不早于"
        parts.append(f"{op_text}{rule.start_time}")
    if rule.end_time and not rule.start_time:
        op_text = ""
        if rule.type in ("END_STAFF", "SHIFT_LIMIT") and (rule.time_op or "eq") != "eq":
            op_text = "不晚于" if rule.time_op == "le" else "不早于"
        parts.append(f"{op_text}{rule.end_time} 下班")
    if rule.shift_id is not None:
        shift = (shift_map or {}).get(rule.shift_id)
        parts.append(f"班次 {shift.name}" if shift is not None else f"班次 #{rule.shift_id}")
    if rule.weekdays:
        parts.append(f"星期 {rule.weekdays}")
    return " ".join(str(part) for part in parts if part)


def _build_rule_status(
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    rules: list[Rule],
    assignments: list[Assignment],
    conflicts: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """逐条规则生成满足情况：状态 满足/放宽/违反/部分满足 + 量化达成信息。

    - 软约束被放宽（P0xx）或硬约束被违反（V0xx/E0xx）时，按 conflicts 中的
      rule_ids 归属到对应规则；
    - coverage 中的缺口与 rule_status 共用同一份评估结果，缺口规则不得报「满足」；
    - 软性 REST 只部分命中时返回「部分满足」并附命中/未命中日期。
    """
    shift_map = {shift.id: shift for shift in shifts}
    violated: dict[int | None, list[dict[str, Any]]] = defaultdict(list)
    for item in conflicts:
        for rule_id in item.get("rule_ids", []):
            violated[rule_id].append(item)

    # 量化数据预处理
    rest_limits = {
        rule.id: rule.count or 0
        for rule in rules
        if rule.valid and rule.type == "MAX_REST_DAYS"
    }
    # 带薪假中性日：既不算上班也不算休息，不计入连休天数
    leave_days_by_employee: dict[int | None, set[str]] = defaultdict(set)
    for item in assignments:
        leave_shift = shift_map.get(item.shift_id) if item.shift_id is not None else None
        if leave_shift is not None and leave_shift.is_leave:
            leave_days_by_employee[item.employee_id].add(item.date)
    for rule in rules:
        if rule.valid and rule.type == "PAID_LEAVE" and rule.employee_id is not None:
            leave_days_by_employee[rule.employee_id].update(_rule_dates(rule, plan.month))

    longest_rest_by_employee: dict[int | None, int] = {}
    if rest_limits:
        for employee in employees:
            worked = {
                item.date
                for item in assignments
                if item.employee_id == employee.id
                and not (
                    shift_map.get(item.shift_id)
                    and (
                        shift_map[item.shift_id].is_leave
                        or shift_map[item.shift_id].is_off
                    )
                )
            }
            runs = _consecutive_runs_rest(
                worked,
                plan.month,
                leave_days_by_employee.get(employee.id, set()),
            )
            longest_rest_by_employee[employee.id] = max(runs) if runs else 0

    status_rows: list[dict[str, Any]] = []
    coverage_by_rule: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for entry in coverage:
        rule_id = entry.get("rule_id")
        if isinstance(rule_id, int):
            coverage_by_rule[rule_id].append(entry)
    # 员工实际出勤日（OFF/POFF 不算上班），用于 REST 规则的命中/未命中统计
    worked_by_employee: dict[int | None, set[str]] = defaultdict(set)
    for item in assignments:
        shift = shift_map.get(item.shift_id) if item.shift_id is not None else None
        if shift is not None and (shift.is_off or shift.is_leave):
            continue
        worked_by_employee[item.employee_id].add(item.date)
    for rule in rules:
        if not rule.valid:
            continue
        related = violated.get(rule.id, [])
        soft_relaxed = [
            item for item in related if item["code"].startswith("P")
        ]
        hard_broken = [
            item for item in related if not item["code"].startswith("P")
        ]
        gaps = coverage_by_rule.get(rule.id) if rule.id is not None else None
        if hard_broken:
            status = "违反"
            note = hard_broken[0]["message"]
        elif soft_relaxed:
            status = "放宽"
            note = soft_relaxed[0]["message"]
        elif gaps:
            # 与 coverage 共用同一份评估：有缺口就不能报「满足」
            status = "违反" if rule.hard else "部分满足"
            entries = [
                f"{entry.get('date')} 需要 {entry.get('required')} 人，"
                f"实际 {entry.get('assigned')} 人"
                for entry in gaps
            ]
            note = "；".join(entries)
        elif rule.type == "REST":
            dates = _rule_dates(rule, plan.month)
            target_ids = (
                [rule.employee_id]
                if rule.employee_id is not None
                else [employee.id for employee in employees if employee.id is not None]
            )
            missed = sorted(
                {
                    day
                    for day in dates
                    for employee_id in target_ids
                    if day in worked_by_employee.get(employee_id, set())
                }
            )
            hits = [day for day in dates if day not in missed]
            if missed:
                status = "违反" if rule.hard else "部分满足"
                note = (
                    f"命中 {len(hits)}/{len(dates)} 天；"
                    f"未命中：{'、'.join(missed)}"
                )
            else:
                status = "满足"
                note = "已满足"
        else:
            status = "满足"
            note = "已满足"
            if rule.type == "MAX_REST_DAYS":
                limit = rule.count or 0
                longest = max(longest_rest_by_employee.values(), default=0)
                note = f"最长连休 {longest} 天 ≤ {limit} 天"
            elif rule.type == "SHIFT_HOURS":
                target = rule.work_hours or 0.0
                op = rule.hours_op or "eq"
                rule_dates = set(_rule_dates(rule, plan.month))
                covered = 0
                total = 0
                for item in assignments:
                    shift = shift_map.get(item.shift_id) if item.shift_id is not None else None
                    if shift is not None and (shift.is_off or shift.is_leave):
                        continue
                    if rule.employee_id is not None and item.employee_id != rule.employee_id:
                        continue
                    if item.date not in rule_dates:
                        continue
                    total += 1
                    hours = (
                        effective_hours(shift.work_hours, shift.start_time, shift.end_time)
                        if shift is not None
                        else effective_hours(None, item.start_time, item.end_time)
                    )
                    if op == "ge":
                        ok = hours >= target - 1e-6
                    elif op == "le":
                        ok = hours <= target + 1e-6
                    else:
                        ok = abs(hours - target) <= 0.01
                    if ok:
                        covered += 1
                note = f"{covered}/{total} 个班次满足条件"
            elif rule.type in (
                "DAY_STAFF",
                "SEGMENT_STAFF",
                "START_STAFF",
                "END_STAFF",
                "SHIFT_STAFF",
            ):
                note = "全程满足人数需求"
        status_rows.append(
            {
                "rule_id": rule.id,
                "type": rule.type,
                "label": _rule_text(
                    rule,
                    {employee.id: employee for employee in employees if employee.id is not None},
                    shift_map,
                ),
                "hard": rule.hard,
                "status": status,
                "note": note,
            }
        )
    return status_rows


def _consecutive_runs_rest(
    worked_dates: set[str], month: str, neutral_dates: set[str] | None = None
) -> list[int]:
    """返回所有连续休息段的长度（休息 = 未上班，含 OFF/POFF/未排班）。

    带薪假是中性日：既不算上班也不算休息，从连休统计中跳过，且不打断两侧休息段。
    """
    neutral = neutral_dates or set()
    lengths: list[int] = []
    run = 0
    for day in month_dates(month):
        if day in neutral:
            continue
        if day in worked_dates:
            if run:
                lengths.append(run)
            run = 0
        else:
            run += 1
    if run:
        lengths.append(run)
    return lengths


def _counts_as_normal(item: Assignment, shift_map: dict[int | None, Shift]) -> bool:
    if item.shift_id is None:
        return True
    shift = shift_map.get(item.shift_id)
    if shift is None:
        return False
    # 休息 OFF 与有薪休假 POFF 均不参与覆盖人数统计
    return not shift.is_special and not shift.is_leave and not shift.is_off


def _role_matches(
    employee_map: dict[int | None, Employee], employee_id: int, role: str | None
) -> bool:
    """人数类规则的岗位口径：规则未限定岗位时全员计入。"""
    if not role:
        return True
    employee = employee_map.get(employee_id)
    return employee is not None and (employee.role or "").strip() == role


def _time_matches(value: str, target: str, op: str | None) -> bool:
    """时刻比较：eq=等于，le=不晚于，ge=不早于（缺省 eq）。"""
    mode = op or "eq"
    current, limit = parse_hhmm(value), parse_hhmm(target)
    if mode == "le":
        return current <= limit
    if mode == "ge":
        return current >= limit
    return current == limit


def _start_matches(start_time: str, target: str, op: str | None) -> bool:
    """START_STAFF 的上班时刻比较：eq=等于，le=不晚于，ge=不早于（缺省 eq）。"""
    return _time_matches(start_time, target, op)


def _shift_limit_ok(shift: Shift, rule: Rule) -> bool:
    """SHIFT_LIMIT：班次必须同时满足规则给出的班次、上班/下班时刻条件。"""
    if rule.shift_id is not None and shift.id != rule.shift_id:
        return False
    if rule.start_time and not _time_matches(shift.start_time, rule.start_time, rule.time_op):
        return False
    if rule.end_time and not _time_matches(shift.end_time, rule.end_time, rule.time_op):
        return False
    return rule.shift_id is not None or bool(rule.start_time) or bool(rule.end_time)


def _shift_limit_text(rule: Rule, shift_map: dict[int | None, Shift]) -> str:
    """SHIFT_LIMIT 的可读条件描述。"""
    parts: list[str] = []
    if rule.shift_id is not None:
        shift = shift_map.get(rule.shift_id)
        parts.append(f"班次 {shift.name if shift else f'#{rule.shift_id}'}")
    op_text = {"eq": "", "le": "不晚于", "ge": "不早于"}.get(rule.time_op or "eq", "")
    if rule.start_time:
        parts.append(f"{op_text}{rule.start_time} 上班")
    if rule.end_time:
        parts.append(f"{op_text}{rule.end_time} 下班")
    return "、".join(parts)


def compute_staff_limits(
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    assignments: list[Assignment],
    rules: list[Rule],
) -> list[dict[str, Any]]:
    """统计人数上限（MAX_STAFF）被突破的日期，只返回 over > 0 的行。"""
    shift_map = {shift.id: shift for shift in shifts}
    employee_map = {employee.id: employee for employee in employees}
    rows: list[dict[str, Any]] = []
    for rule in rules:
        if not rule.valid or rule.type != "MAX_STAFF":
            continue
        limit = rule.count or 0
        for day in _rule_dates(rule, plan.month):
            assigned = sum(
                1
                for item in assignments
                if item.date == day
                and _counts_as_normal(item, shift_map)
                and _role_matches(employee_map, item.employee_id, rule.role)
            )
            over = max(0, assigned - limit)
            if over <= 0:
                continue
            rows.append(
                {
                    "rule_id": rule.id,
                    "date": day,
                    "label": f"当天最多 {limit} 人",
                    "limit": limit,
                    "assigned": assigned,
                    "over": over,
                }
            )
    return rows


def evaluate(
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    rules: list[Rule],
    assignments: list[Assignment],
) -> dict[str, Any]:
    employee_map = {employee.id: employee for employee in employees}
    shift_map = {shift.id: shift for shift in shifts}

    coverage: list[dict[str, Any]] = []
    for rule in rules:
        if not rule.valid or rule.type not in (
            "DAY_STAFF",
            "SEGMENT_STAFF",
            "START_STAFF",
            "END_STAFF",
            "SHIFT_STAFF",
        ):
            continue
        role = rule.role
        for day in _rule_dates(rule, plan.month):
            required = rule.count or 0
            if rule.type == "DAY_STAFF":
                assigned = sum(
                    1
                    for item in assignments
                    if item.date == day
                    and _counts_as_normal(item, shift_map)
                    and _role_matches(employee_map, item.employee_id, role)
                )
                label = f"当天至少 {required} 人"
            elif rule.type == "SEGMENT_STAFF":
                window_start = rule.start_time or "00:00"
                window_end = rule.end_time or "00:00"
                intervals = [
                    (
                        shift_map[item.shift_id].start_time
                        if item.shift_id
                        else item.start_time,
                        shift_map[item.shift_id].end_time if item.shift_id else item.end_time,
                    )
                    for item in assignments
                    if item.date == day
                    and _counts_as_normal(item, shift_map)
                    and _role_matches(employee_map, item.employee_id, role)
                ]
                shortfall, gaps = coverage_shortfall(
                    intervals, window_start, window_end, required
                )
                if shortfall > 0:
                    gap_text = "、".join(
                        f"{format_hhmm(left)}-{format_hhmm(right)}" for left, right in gaps
                    )
                    coverage.append(
                        {
                            "rule_id": rule.id,
                            "date": day,
                            "label": f"{window_start}-{window_end} 至少 {required} 人；未覆盖 {gap_text}",
                            "required": required,
                            "assigned": required - shortfall,
                            "shortfall": shortfall,
                            "gaps": [
                                {"start": format_hhmm(left), "end": format_hhmm(right)}
                                for left, right in gaps
                            ],
                        }
                    )
                continue
            elif rule.type in ("START_STAFF", "END_STAFF"):
                # 上班/下班时刻人数：按 time_op 统计该时刻/之前/之后开始（结束）的普通班次
                by_end = rule.type == "END_STAFF"
                target = (rule.end_time if by_end else rule.start_time) or "00:00"

                def _moment_time(item: Assignment) -> str:
                    shift = shift_map.get(item.shift_id) if item.shift_id is not None else None
                    if shift is None:
                        return item.end_time if by_end else item.start_time
                    return shift.end_time if by_end else shift.start_time

                assigned = sum(
                    1
                    for item in assignments
                    if item.date == day
                    and _counts_as_normal(item, shift_map)
                    and _role_matches(employee_map, item.employee_id, role)
                    and _time_matches(_moment_time(item), target, rule.time_op)
                )
                moment = "下班" if by_end else "上班"
                if (rule.time_op or "eq") == "eq":
                    label = f"{target} {moment}至少 {required} 人"
                elif rule.time_op == "le":
                    label = f"{target} 之前{moment}至少 {required} 人"
                else:
                    label = f"{target} 之后{moment}至少 {required} 人"
                # 人数方向为「至多」时改述为上限语义
                if (rule.count_op or "ge") == "le":
                    label = label.replace("至少", "至多")
            else:
                shift = shift_map.get(rule.shift_id)
                assigned = sum(
                    1
                    for item in assignments
                    if item.date == day
                    and item.shift_id == rule.shift_id
                    and _role_matches(employee_map, item.employee_id, role)
                )
                label = f"{shift.name if shift else '指定班次'} 至少 {required} 人"
            if rule.type in ("START_STAFF", "END_STAFF") and (rule.count_op or "ge") == "le":
                if assigned > required:
                    coverage.append(
                        {
                            "rule_id": rule.id,
                            "date": day,
                            "label": label,
                            "required": required,
                            "assigned": assigned,
                            "over": assigned - required,
                        }
                    )
            elif assigned < required:
                coverage.append(
                    {
                        "rule_id": rule.id,
                        "date": day,
                        "label": label,
                        "required": required,
                        "assigned": assigned,
                        "shortfall": required - assigned,
                    }
                )

    hours: list[dict[str, Any]] = []
    rest_summary: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    # V017：班次库工时固定校验——OFF 班次工时必须为 0，POFF 班次工时必须为 8 小时
    for shift in shifts:
        hours_value = effective_hours(shift.work_hours, shift.start_time, shift.end_time)
        if shift.is_off and abs(hours_value) > 1e-6:
            conflicts.append(
                _conflict(
                    "V017",
                    f"休息班次 {shift.name} 工时必须为 0 小时，当前为 {hours_value} 小时",
                )
            )
        if shift.is_leave and abs(hours_value - 8.0) > 1e-6:
            conflicts.append(
                _conflict(
                    "V017",
                    f"有薪休假班次 {shift.name} 工时必须为 8 小时，当前为 {hours_value} 小时",
                )
            )

    # 同一员工同一天的排班明细（用于 V018 重叠检测）
    items_by_key: dict[tuple[int, str], list[Assignment]] = defaultdict(list)
    for item in assignments:
        items_by_key[(item.employee_id, item.date)].append(item)
    # V018：OFF/POFF 与同日其他班次重叠报错（同一员工同一天只能有一个 OFF 或 POFF 或普通班次）
    for (employee_id, day), day_items in items_by_key.items():
        employee = employee_map.get(employee_id)
        who = employee.name if employee else f"#{employee_id}"
        special_items = [
            item
            for item in day_items
            if shift_map.get(item.shift_id) is not None
            and (shift_map[item.shift_id].is_off or shift_map[item.shift_id].is_leave)
        ]
        if not special_items:
            continue
        # 存在多个明细（OFF/POFF 重复，或 OFF/POFF 与普通班次并存）即视为重叠
        if len(day_items) > 1:
            names = [
                shift_map[item.shift_id].name
                if shift_map.get(item.shift_id) is not None
                else "未命名班次"
                for item in day_items
            ]
            kind = "OFF/POFF"
            conflicts.append(
                _conflict(
                    "V018",
                    f"{who} 在 {day} 的 {kind} 与同日其他班次重叠：{'、'.join(names)}",
                )
            )
    employee_worked_dates: dict[int | None, set[str]] = {}
    for employee in employees:
        own = [item for item in assignments if item.employee_id == employee.id]
        actual = round(sum(item.hours for item in own), 2)
        # 有薪休假（POFF）计入 8 小时带薪工时，休息（OFF）计入 0 小时
        paid_leave_hours = round(
            sum(
                item.hours
                for item in own
                if shift_map.get(item.shift_id) is not None and shift_map[item.shift_id].is_leave
            ),
            2,
        )
        off_hours = round(
            sum(
                item.hours
                for item in own
                if shift_map.get(item.shift_id) is not None and shift_map[item.shift_id].is_off
            ),
            2,
        )
        target_hours, monthly_rule = monthly_hours_target(employee, rules, plan.month)
        target = round(target_hours, 2)
        delta = round(actual - target, 2)
        # 只有显式 MONTHLY_HOURS 硬规则或计划级「月工时硬约束」才是硬指标，
        # 其余（档案月工时）按软目标展示
        soft_target = not (
            (monthly_rule is not None and monthly_rule.hard) or plan.monthly_hours_hard
        )
        if target <= 0:
            within = True
        elif (monthly_rule is not None and monthly_rule.hard) or plan.monthly_hours_hard:
            # 硬指标：必须精确等于目标值（0.01 小时仅用于浮点容错）
            within = abs(delta) <= 0.01
        else:
            # 员工档案月工时为软目标：与目标相差一个标准班次（8 小时）以内视为达标
            within = abs(delta) <= 8.0
        # 软目标允许让步（整班粒度、覆盖需求），但必须显式标注放宽了多少，
        # 避免用户把「目标 76 / 实际 80」误读为计算错误
        target_note: str | None = None
        if target > 0 and soft_target and abs(delta) > 0.01:
            if delta > 0:
                target_note = (
                    f"软目标已放宽：超出目标 {abs(delta):g} 小时（按整班时长排班）"
                )
            else:
                target_note = f"软目标未达成：低于目标 {abs(delta):g} 小时"
        hours.append(
            {
                "employee_id": employee.id,
                "name": employee.name,
                "target": target,
                "actual": actual,
                "delta": delta,
                "matches_target": within,
                "soft_target": soft_target,
                "target_note": target_note,
                # 报表区分展示：有薪休假（8h）与休息（0h）合计
                "paid_leave_hours": paid_leave_hours,
                "off_hours": off_hours,
            }
        )
        worked_dates = {
            item.date
            for item in own
            if not (
                shift_map.get(item.shift_id)
                and (shift_map[item.shift_id].is_leave or shift_map[item.shift_id].is_off)
            )
        }
        employee_worked_dates[employee.id] = worked_dates
        # 休息天数：OFF 计入；有薪休假天数：POFF 单列，不计入休息
        rest_days = sum(
            1
            for item in own
            if shift_map.get(item.shift_id) is not None and shift_map[item.shift_id].is_off
        )
        paid_leave_days = sum(
            1
            for item in own
            if shift_map.get(item.shift_id) is not None and shift_map[item.shift_id].is_leave
        )
        runs = _consecutive_runs(worked_dates, plan.month)
        longest_run = max(runs, key=lambda run: run[2]) if runs else None
        longest = longest_run[2] if longest_run else 0
        rest_summary.append(
            {
                "employee_id": employee.id,
                "name": employee.name,
                "work_days": len(worked_dates),
                "max_consecutive": longest,
                "limit": plan.max_consecutive_days,
                "rest_days": rest_days,
                "paid_leave_days": paid_leave_days,
            }
        )
        if (
            plan.max_consecutive_days > 0
            and longest_run is not None
            and longest > plan.max_consecutive_days
        ):
            conflicts.append(
                _conflict(
                    "V016",
                    f"{employee.name} {longest_run[0]} 至 {longest_run[1]} 连续上班 "
                    f"{longest} 天，超过上限 {plan.max_consecutive_days} 天",
                )
            )
        if not within:
            # POFF 计入个人工时合计后，与 172 工时硬指标不符
            code = "V019" if paid_leave_hours > 0 else "V008"
            label = "（含有薪休假 8 小时/天）" if paid_leave_hours > 0 else ""
            if soft_target:
                label += "（人员档案月工时为软目标，可偏离）"
            conflicts.append(
                _conflict(
                    code,
                    f"{employee.name} 月度工时 {actual} 小时，目标 {target} 小时，"
                    f"差值 {delta:+g} 小时{label}",
                )
            )

    rules_by_employee: dict[int, list[Rule]] = defaultdict(list)
    date_rules: dict[str, list[Rule]] = defaultdict(list)
    must_work: dict[int, set[str]] = defaultdict(set)
    must_work_rules: list[tuple[Rule, list[str]]] = []
    leave_rules: list[tuple[Rule, list[str]]] = []
    rest_limit_rules: list[tuple[Rule, list[str]]] = []
    hours_rules: list[tuple[Rule, list[str]]] = []
    min_rest_rules: list[tuple[Rule, list[str]]] = []
    max_work_rules: list[tuple[Rule, list[str]]] = []
    max_work_streak_rules: list[tuple[Rule, list[str]]] = []
    ban_shift_rules: list[tuple[Rule, list[str]]] = []
    # 同日固定班让位（与 solver._apply_fixed_precedence 同口径）：
    # 带日期的显式固定班优先，通例固定班在当天让位，不再进入逐日校验，避免 V003 误报
    explicit_fixed: set[tuple[int, str]] = set()
    single_rest: set[tuple[int, str]] = set()
    for rule in rules:
        if (
            rule.valid
            and rule.type == "FIXED_SHIFT"
            and rule.employee_id is not None
            and rule.weekdays is None
            and rule.date_start == rule.date_end
        ):
            explicit_fixed.add((rule.employee_id, rule.date_start or ""))
        elif (
            rule.valid
            and rule.type == "REST"
            and rule.hard
            and rule.employee_id is not None
            and rule.weekdays is None
            and rule.date_start == rule.date_end
        ):
            # 单日硬休息优先于通例固定班（与 solver._apply_rest_precedence 同口径）
            single_rest.add((rule.employee_id, rule.date_start or ""))
    for rule in rules:
        if not rule.valid:
            continue
        dates = _rule_dates(rule, plan.month)
        if rule.employee_id is not None:
            rules_by_employee[rule.employee_id].append(rule)
        if rule.type == "MUST_WORK":
            # employee_id=None 表示全员必须上班
            target_ids = (
                [rule.employee_id]
                if rule.employee_id is not None
                else [employee.id for employee in employees if employee.id is not None]
            )
            for employee_id in target_ids:
                must_work[employee_id].update(dates)
            must_work_rules.append((rule, dates))
        if rule.type == "PAID_LEAVE" and rule.employee_id is not None:
            leave_rules.append((rule, dates))
        if rule.type == "MAX_REST_DAYS":
            rest_limit_rules.append((rule, dates))
        if rule.type == "SHIFT_HOURS":
            hours_rules.append((rule, dates))
        if rule.type == "MIN_REST_DAYS":
            min_rest_rules.append((rule, dates))
        if rule.type == "MAX_WORK_DAYS":
            max_work_rules.append((rule, dates))
        if rule.type == "MAX_CONSECUTIVE_WORK":
            max_work_streak_rules.append((rule, dates))
        if rule.type == "BAN_SHIFT":
            ban_shift_rules.append((rule, dates))
        is_general_fixed = not (
            rule.weekdays is None and rule.date_start == rule.date_end
        )
        for day in dates:
            if (
                rule.type == "FIXED_SHIFT"
                and rule.employee_id is not None
                and is_general_fixed
                and (
                    (rule.employee_id, day) in explicit_fixed
                    or (rule.employee_id, day) in single_rest
                )
            ):
                continue
            date_rules[day].append(rule)

    seen: set[tuple[Any, ...]] = set()

    def add_conflict(
        code: str,
        message: str,
        rule_ids: Sequence[int | None],
        key: tuple[Any, ...],
    ) -> None:
        if key in seen:
            return
        seen.add(key)
        conflicts.append(_conflict(code, message, rule_ids))

    month_days = set(month_dates(plan.month))
    # V020：有薪休假规则声明的天数与日期实际天数不一致（拦截历史脏数据与直接写接口的规则）
    for leave_rule, leave_days in leave_rules:
        if leave_rule.count is None or leave_rule.count == len(leave_days):
            continue
        leave_employee = employee_map.get(leave_rule.employee_id)
        leave_name = leave_employee.name if leave_employee else "该员工"
        add_conflict(
            "V020",
            f"{leave_name} 有薪休假规则声明 {leave_rule.count} 天，实际日期为 {len(leave_days)} 天",
            [leave_rule.id] if leave_rule.id is not None else [],
            ("V020", leave_rule.id, leave_rule.date_start),
        )

    # V022 / P008：连续休息上限（MAX_REST_DAYS）。休息包含 OFF、未排班；
    # PAID_LEAVE 是中性日，既不算上班也不算休息，不消耗连休额度。
    # 只扫描规则生效日期（已按 weekdays 过滤），并按日历连续性切段：
    # 星期过滤造成的间隙会切段，避免把规则日期间的非规则日也算进连休。
    # 软规则文案统一以「，已按软约束放宽」结尾。
    def _max_rest_runs(
        worked: set[str], rule_dates: Sequence[str], neutral: set[str] | None = None
    ) -> list[tuple[str, str, int]]:
        neutral_days = neutral or set()
        runs: list[tuple[str, str, int]] = []
        for segment in _rule_segments(rule_dates):
            run_start: str | None = None
            run_end: str | None = None
            count = 0
            for day in segment:
                # 带薪假中性日：跳过且不打断两侧休息段
                if day in neutral_days:
                    continue
                if day in worked:
                    if count and run_start is not None and run_end is not None:
                        runs.append((run_start, run_end, count))
                    run_start = run_end = None
                    count = 0
                    continue
                if run_start is None:
                    run_start = day
                run_end = day
                count += 1
            if count and run_start is not None and run_end is not None:
                runs.append((run_start, run_end, count))
        return runs

    # 带薪假中性日：从连休统计中跳过，不消耗连休额度
    leave_dates_by_employee: dict[int, set[str]] = defaultdict(set)
    for leave_rule, leave_days in leave_rules:
        if leave_rule.employee_id is not None:
            leave_dates_by_employee[leave_rule.employee_id].update(leave_days)
    for item in assignments:
        shift = shift_map.get(item.shift_id) if item.shift_id is not None else None
        if shift is not None and shift.is_leave and item.employee_id is not None:
            leave_dates_by_employee[item.employee_id].add(item.date)

    # 显式休息要求的日期：连休超限若由显式 REST 触发，只作提示，不再报「放宽」
    explicit_rest_days: dict[int | None, set[str]] = defaultdict(set)
    for rest_only_rule in rules:
        if rest_only_rule.valid and rest_only_rule.type == "REST":
            rest_days = _rule_dates(rest_only_rule, plan.month)
            target_ids = (
                [rest_only_rule.employee_id]
                if rest_only_rule.employee_id is not None
                else [employee.id for employee in employees if employee.id is not None]
            )
            for employee_id in target_ids:
                explicit_rest_days[employee_id].update(rest_days)

    for rest_rule, rest_dates in rest_limit_rules:
        limit = rest_rule.count or 0
        targets = (
            [employee_map.get(rest_rule.employee_id)]
            if rest_rule.employee_id is not None
            else list(employees)
        )
        if not rest_dates:
            continue
        for target in targets:
            if target is None:
                continue
            worked = employee_worked_dates.get(target.id, set())
            neutral = leave_dates_by_employee.get(target.id, set())
            for run_start, run_end, run_len in _max_rest_runs(worked, rest_dates, neutral):
                if run_len <= limit:
                    continue
                code = "V022" if rest_rule.hard else "P008"
                message = (
                    f"{target.name} {run_start} 至 {run_end} 连续休息 {run_len} 天，"
                    f"超过上限 {limit} 天"
                )
                if code == "P008":
                    run_days = {
                        (date.fromisoformat(run_start) + timedelta(days=offset)).isoformat()
                        for offset in range(
                            (date.fromisoformat(run_end) - date.fromisoformat(run_start)).days + 1
                        )
                    }
                    triggered = bool(
                        run_days & explicit_rest_days.get(target.id, set())
                    ) or bool(run_days & explicit_rest_days.get(None, set()))
                    if triggered:
                        message = (
                            f"{target.name} {run_start} 至 {run_end} 连续休息 {run_len} 天，"
                            f"该连休由显式要求触发，超出默认上限 {limit} 天"
                        )
                    else:
                        message = f"{message}，已按软约束放宽"
                add_conflict(
                    code,
                    message,
                    [rest_rule.id] if rest_rule.id is not None else [],
                    (code, rest_rule.id, target.id, run_start),
                )

    # V025 / P011：连续上班上限（MAX_CONSECUTIVE_WORK），按规则生效日期切段扫描
    for work_rule, work_dates in max_work_streak_rules:
        limit = work_rule.count or 0
        targets = (
            [employee_map.get(work_rule.employee_id)]
            if work_rule.employee_id is not None
            else list(employees)
        )
        if not work_dates:
            continue
        for target in targets:
            if target is None:
                continue
            worked = employee_worked_dates.get(target.id, set())
            for run_start, run_end, run_len in _consecutive_runs_on(worked, work_dates):
                if run_len <= limit:
                    continue
                code = "V025" if work_rule.hard else SOFT_RELAXED
                message = (
                    f"{target.name} {run_start} 至 {run_end} 连续上班 {run_len} 天，"
                    f"超过上限 {limit} 天"
                )
                if code == SOFT_RELAXED:
                    message = f"{message}，已按软约束放宽"
                add_conflict(
                    code,
                    message,
                    [work_rule.id] if work_rule.id is not None else [],
                    (code, work_rule.id, target.id, run_start),
                )

    # V023 / P009、V024 / P010：周期天数类（MIN_REST_DAYS / MAX_WORK_DAYS）。
    # 休息包含 OFF、POFF、未排班；week 按自然周（周一~周日）切段，month 按整段区间。
    # 首尾残周与求解器同口径：至少休息 floor 折算，最多上班按 min(count, 可见天数)。
    def _bucket_scope(rule: Rule, bucket: list[str]) -> str:
        if (rule.period or "month") == "week" and bucket:
            return f"{bucket[0]} 至 {bucket[-1]} 这一周"
        return f"{rule.date_start} 至 {rule.date_end}"

    for rule, rule_dates in min_rest_rules:
        required = rule.count or 0
        targets = (
            [employee_map.get(rule.employee_id)]
            if rule.employee_id is not None
            else list(employees)
        )
        for index, bucket in enumerate(period_buckets(rule.period, rule_dates)):
            if not bucket:
                continue
            quota = required
            if (rule.period or "month") == "week":
                quota = weekly_quota(required, plan.month, bucket)
            need = min(quota, len(bucket))
            scope = _bucket_scope(rule, bucket)
            for target in targets:
                if target is None:
                    continue
                worked = employee_worked_dates.get(target.id, set())
                rest_days = sum(1 for day in bucket if day not in worked)
                if rest_days >= need:
                    continue
                code = "V023" if rule.hard else "P009"
                message = (
                    f"{target.name} 在 {scope} 休息 {rest_days} 天，"
                    f"少于要求的 {need} 天"
                )
                if code == "P009":
                    message = f"{message}，已按软约束放宽"
                add_conflict(
                    code,
                    message,
                    [rule.id] if rule.id is not None else [],
                    (code, rule.id, target.id, index),
                )

    for rule, rule_dates in max_work_rules:
        targets = (
            [employee_map.get(rule.employee_id)]
            if rule.employee_id is not None
            else list(employees)
        )
        for index, bucket in enumerate(period_buckets(rule.period, rule_dates)):
            if not bucket:
                continue
            limit = rule.count or 0
            if (rule.period or "month") == "week":
                limit = weekly_quota(limit, plan.month, bucket, is_limit=True)
            scope = _bucket_scope(rule, bucket)
            for target in targets:
                if target is None:
                    continue
                worked = employee_worked_dates.get(target.id, set())
                work_days = sum(1 for day in bucket if day in worked)
                if work_days <= limit:
                    continue
                code = "V024" if rule.hard else "P010"
                message = (
                    f"{target.name} 在 {scope} 上班 {work_days} 天，"
                    f"超过上限 {limit} 天"
                )
                if code == "P010":
                    message = f"{message}，已按软约束放宽"
                add_conflict(
                    code,
                    message,
                    [rule.id] if rule.id is not None else [],
                    (code, rule.id, target.id, index),
                )

    # V025 / P011：禁止班次（BAN_SHIFT）实际被排入时报告
    for rule, rule_dates in ban_shift_rules:
        shift = shift_map.get(rule.shift_id) if rule.shift_id is not None else None
        shift_name = shift.name if shift is not None else f"#{rule.shift_id}"
        rule_days = set(rule_dates)
        for item in assignments:
            if item.date not in rule_days or item.shift_id != rule.shift_id:
                continue
            if rule.employee_id is not None and item.employee_id != rule.employee_id:
                continue
            employee = employee_map.get(item.employee_id)
            who = employee.name if employee else f"#{item.employee_id}"
            code = "V025" if rule.hard else SOFT_RELAXED
            message = f"{who} 在 {item.date} 被排入禁止班次 {shift_name}"
            if code == SOFT_RELAXED:
                message = f"{message}，已按软约束放宽"
            add_conflict(
                code,
                message,
                [rule.id] if rule.id is not None else [],
                (code, rule.id, item.employee_id, item.date),
            )

    # V026 / P011：班次限制（SHIFT_LIMIT）在上班日被违反时报告；休息日不算违反
    for rule in rules:
        if not rule.valid or rule.type != "SHIFT_LIMIT":
            continue
        rule_days = set(_rule_dates(rule, plan.month))
        if not rule_days:
            continue
        requirement = _shift_limit_text(rule, shift_map)
        for item in assignments:
            if item.date not in rule_days:
                continue
            if rule.employee_id is not None and item.employee_id != rule.employee_id:
                continue
            shift = shift_map.get(item.shift_id) if item.shift_id is not None else None
            if shift is None or shift.is_off or shift.is_leave:
                continue
            if _shift_limit_ok(shift, rule):
                continue
            employee = employee_map.get(item.employee_id)
            who = employee.name if employee else f"#{item.employee_id}"
            code = "V026" if rule.hard else SOFT_RELAXED
            message = f"{who} 在 {item.date} 排入 {shift.name}，不满足班次限制（{requirement}）"
            if code == SOFT_RELAXED:
                message = f"{message}，已按软约束放宽"
            add_conflict(
                code,
                message,
                [rule.id] if rule.id is not None else [],
                (code, rule.id, item.employee_id, item.date),
            )

    # V027 / P011：岗位单人班次（SINGLE_ROLE_SHIFT）触发条件下未排指定班次
    for rule in rules:
        if not rule.valid or rule.type != "SINGLE_ROLE_SHIFT":
            continue
        role_name = (rule.role or "").strip()
        shift = shift_map.get(rule.shift_id) if rule.shift_id is not None else None
        if not role_name or shift is None:
            continue
        threshold = rule.count or 1
        for day in _rule_dates(rule, plan.month):
            working = [
                item
                for item in assignments
                if item.date == day
                and _counts_as_normal(item, shift_map)
                and _role_matches(employee_map, item.employee_id, role_name)
            ]
            if len(working) > threshold:
                continue
            for item in working:
                if item.shift_id == rule.shift_id:
                    continue
                employee = employee_map.get(item.employee_id)
                who = employee.name if employee else f"#{item.employee_id}"
                code = "V027" if rule.hard else SOFT_RELAXED
                message = (
                    f"{who} 在 {day} 属于岗位「{role_name}」当天上班人员"
                    f"（共 {len(working)} 人，阈值 {threshold}），却没有排入班次 {shift.name}"
                )
                if code == SOFT_RELAXED:
                    message = f"{message}，已按软约束放宽"
                add_conflict(
                    code,
                    message,
                    [rule.id] if rule.id is not None else [],
                    (code, rule.id, item.employee_id, day),
                )

    # V028 / P011：工作时段窗（WORK_WINDOW）——上班班次必须整体落在窗口内
    for rule in rules:
        if not rule.valid or rule.type != "WORK_WINDOW":
            continue
        if not (rule.start_time and rule.end_time):
            continue
        rule_days = set(_rule_dates(rule, plan.month))
        if not rule_days:
            continue
        begin, finish = parse_hhmm(rule.start_time), parse_hhmm(rule.end_time)
        for item in assignments:
            if item.date not in rule_days:
                continue
            if rule.employee_id is not None and item.employee_id != rule.employee_id:
                continue
            if rule.role and not _role_matches(employee_map, item.employee_id, rule.role):
                continue
            shift = shift_map.get(item.shift_id) if item.shift_id is not None else None
            if shift is None or shift.is_off or shift.is_leave:
                continue
            shift_begin, shift_finish = parse_interval(shift.start_time, shift.end_time)
            if shift_begin >= begin and shift_finish <= finish:
                continue
            employee = employee_map.get(item.employee_id)
            who = employee.name if employee else f"#{item.employee_id}"
            code = "V028" if rule.hard else SOFT_RELAXED
            message = (
                f"{who} 在 {item.date} 排入 {shift.name}"
                f"（{shift.start_time}-{shift.end_time}），超出工作时段 "
                f"{rule.start_time}-{rule.end_time}"
            )
            if code == SOFT_RELAXED:
                message = f"{message}，已按软约束放宽"
            add_conflict(
                code,
                message,
                [rule.id] if rule.id is not None else [],
                (code, rule.id, item.employee_id, item.date),
            )

    # P007：软性班次工时条件（SHIFT_HOURS hard=false）实际被违反时给出提示，不阻塞。
    # 硬规则由求解器保证，无需重复报错；休息日（OFF/POFF/未排班）不算违反。
    # 降噪：同一「员工+日期+班次工时」的多条规则违例合并为一条；同一规则跨人跨日的
    # 违例合并为一条汇总条目，保证 conflicts 列表可控且信息不丢失。
    def _hours_match_value(hours: float, target: float, op: str) -> bool:
        if op == "ge":
            return hours >= target - 1e-6
        if op == "le":
            return hours <= target + 1e-6
        return abs(hours - target) <= 0.01

    def _hours_text_value(target: float, op: str) -> str:
        if op == "ge":
            return f"工时不少于 {target:g} 小时"
        if op == "le":
            return f"工时不超过 {target:g} 小时"
        return f"工时等于 {target:g} 小时"

    # 违例明细：(员工, 日期, 班次工时) → 被违反条件集合 + 规则编号
    p007_details: dict[tuple[int | None, str, float], dict[str, Any]] = {}
    # 规则汇总：规则 → [(姓名, 日期, 班次工时)]
    p007_by_rule: dict[int, dict[str, Any]] = {}
    for hours_rule, _hours_dates in hours_rules:
        if hours_rule.hard:
            continue
        target = hours_rule.work_hours or 0.0
        op = hours_rule.hours_op or "eq"
        rule_days = set(_hours_dates)
        rule_weekdays = parse_weekdays(hours_rule.weekdays)
        rule_id = hours_rule.id if hours_rule.id is not None else 0
        bucket = p007_by_rule.setdefault(
            rule_id, {"rule": hours_rule, "items": []}
        )
        for employee in employees:
            if hours_rule.employee_id is not None and hours_rule.employee_id != employee.id:
                continue
            for item in assignments:
                if item.employee_id != employee.id or item.date not in rule_days:
                    continue
                if rule_weekdays and date.fromisoformat(item.date).isoweekday() not in rule_weekdays:
                    continue
                shift = shift_map.get(item.shift_id) if item.shift_id is not None else None
                if shift is not None and (shift.is_off or shift.is_leave):
                    continue
                # 注意：这里用 shift_hours，避免覆盖外层报告的 hours 列表
                shift_hours = (
                    effective_hours(shift.work_hours, shift.start_time, shift.end_time)
                    if shift is not None
                    else effective_hours(None, item.start_time, item.end_time)
                )
                if _hours_match_value(shift_hours, target, op):
                    continue
                key = (employee.id, item.date, round(shift_hours, 2))
                detail = p007_details.setdefault(
                    key,
                    {
                        "name": employee.name,
                        "date": item.date,
                        "hours": shift_hours,
                        "texts": [],
                        "rule_ids": [],
                    },
                )
                text = _hours_text_value(target, op)
                if text not in detail["texts"]:
                    detail["texts"].append(text)
                if hours_rule.id is not None and hours_rule.id not in detail["rule_ids"]:
                    detail["rule_ids"].append(hours_rule.id)
                bucket["items"].append((employee.name, item.date, shift_hours))

    # 明细条目：同一「员工+日期+班次工时」合并列出所有被违反条件；
    # 违例组合超过 3 个时，同一规则跨人跨日的违例合并为一条汇总条目（避免逐条刷屏）。
    if len(p007_details) > 3:
        for rule_id in sorted(p007_by_rule):
            bucket = p007_by_rule[rule_id]
            items = bucket["items"]
            if not items:
                continue
            rule = bucket["rule"]
            target = rule.work_hours or 0.0
            op = rule.hours_op or "eq"
            names: list[str] = []
            for name, _date, _hours in items:
                if name not in names:
                    names.append(name)
            hours_values = sorted({round(hours, 2) for _n, _d, hours in items})
            hours_text = "、".join(f"{value:g} 小时" for value in hours_values)
            dates_sorted = sorted({d for _n, d, _h in items})
            date_text = (
                dates_sorted[0]
                if len(dates_sorted) == 1
                else f"{dates_sorted[0]} 至 {dates_sorted[-1]}"
            )
            add_conflict(
                "P007",
                f"{'、'.join(names)} 在 {date_text} 共 {len(items)} 个班次为 {hours_text}，"
                f"不满足{_hours_text_value(target, op)}，已按软约束放宽",
                [rule_id] if rule_id else [],
                ("P007", "summary", rule_id),
            )
    else:
        for key in sorted(p007_details):
            detail = p007_details[key]
            add_conflict(
                "P007",
                f"{detail['name']} 在 {detail['date']} 的班次为 {detail['hours']:g} 小时，"
                f"不满足{' / '.join(detail['texts'])}，已按软约束放宽",
                detail["rule_ids"],
                ("P007", key[0], key[1], key[2]),
            )

    for item in assignments:
        employee = employee_map.get(item.employee_id)
        shift = shift_map.get(item.shift_id)
        if employee is None:
            add_conflict("V009", "排班引用了不存在的人员", [], ("V009", item.employee_id, item.date))
            continue
        if item.date not in month_days:
            add_conflict(
                "V010",
                f"{employee.name} 的排班日期 {item.date} 不在计划月份内",
                [],
                ("V010", item.employee_id, item.date),
            )
        # 班次引用的排班缺失时下面的分支不会再赋值，这里先给默认值，避免后续使用未绑定变量
        is_off = False
        if item.shift_id is not None and shift is None:
            add_conflict(
                "V009",
                f"{employee.name} 在 {item.date} 的排班引用了不存在的班次",
                [],
                ("V009", item.employee_id, item.date),
            )
        else:
            start_time = shift.start_time if shift else item.start_time
            end_time = shift.end_time if shift else item.end_time
            work_hours = shift.work_hours if shift else None
            is_off = shift is not None and shift.is_off
            if shift is None or not (shift.is_leave or shift.is_off):
                if effective_hours(work_hours, start_time, end_time) > employee.max_daily_hours + 1e-6:
                    add_conflict(
                        "V006",
                        f"{employee.name} 在 {item.date} 的班次时长超过每日上限 {employee.max_daily_hours} 小时",
                        [],
                        ("V006", item.employee_id, item.date),
                    )
                if shift is not None and shift.is_special and not employee.allow_special:
                    add_conflict(
                        "V007",
                        f"{employee.name} 未开启盘点班次资格，却被排入 {shift.name}",
                        [],
                        ("V007", item.employee_id, item.date),
                    )
                violation = window_violation(
                    plan.coverage_soft_start_effective,
                    plan.coverage_soft_end_effective,
                    start_time,
                    end_time,
                    plan.latest_end_time,
                )
                if violation is not None and not (shift is not None and shift.is_special):
                    message = violation[1]
                    add_conflict(
                        "V014",
                        f"{employee.name} 在 {item.date} 的排班不符合时段：{message}",
                        [],
                        ("V014", item.employee_id, item.date),
                    )
                # OFF/POFF 的 00:00-00:00 为占位时段（parse_interval 视为跨全天），不参与跨午夜检查
                # 计划关闭「跨午夜班次日休息」时，同样不再报 V015
                _, end_minutes = parse_interval(start_time, end_time)
                if (
                    plan.overnight_next_rest
                    and end_minutes >= 1440
                    and not (start_time == "00:00" and end_time == "00:00")
                ):
                    next_day = (date.fromisoformat(item.date) + timedelta(days=1)).isoformat()
                    if any(
                        other.employee_id == item.employee_id
                        and other.date == next_day
                        and not (
                            shift_map.get(other.shift_id)
                            and (shift_map[other.shift_id].is_off or shift_map[other.shift_id].is_leave)
                        )
                        for other in assignments
                    ):
                        add_conflict(
                            "V015",
                            f"{employee.name} 在 {item.date} 上跨午夜班次，次日 {next_day} 仍被排班",
                            [],
                            ("V015", item.employee_id, item.date),
                        )
        is_leave = shift is not None and shift.is_leave

        active = date_rules.get(item.date, [])
        for rule in active:
            # REST 支持全员（employee_id=None），其余类型仍按人员精确匹配
            if rule.employee_id is not None and rule.employee_id != item.employee_id:
                continue
            if rule.type == "REST":
                if not rule.hard:
                    continue
                # 休假（POFF）豁免休息规则；休息（OFF）本身就是休息展示，不算被排班
                if is_leave or is_off or item.date in must_work.get(item.employee_id, set()):
                    continue
                add_conflict(
                    "V002",
                    f"{employee.name} 在 {item.date} 应休息，但仍被排班",
                    [rule.id],
                    ("V002", item.employee_id, item.date, rule.id),
                )
            elif rule.type == "FIXED_SHIFT" and item.shift_id != rule.shift_id:
                add_conflict(
                    "V003",
                    f"{employee.name} 在 {item.date} 应上指定班次，实际班次不符",
                    [rule.id],
                    ("V003", item.employee_id, item.date, rule.id),
                )
            elif rule.type == "FIXED_TIME":
                # 休息（OFF）展示不算上班，不参与到岗时间校验
                if is_off:
                    continue
                actual_start = shift.start_time if shift else item.start_time
                is_special = shift.is_special if shift else False
                if is_special or actual_start != rule.start_time:
                    add_conflict(
                        "V004",
                        f"{employee.name} 在 {item.date} 应 {rule.start_time} 上班，实际开始时间不符",
                        [rule.id],
                        ("V004", item.employee_id, item.date, rule.id),
                    )
        for rule in active:
            if rule.type == "ALL_START_AT":
                # 休假（POFF）与休息（OFF）均不要求全员到岗
                if is_leave or is_off:
                    continue
                actual_start = shift.start_time if shift else item.start_time
                is_special = shift.is_special if shift else False
                if is_special or actual_start != rule.start_time:
                    add_conflict(
                        "V005",
                        f"{employee.name} 在 {item.date} 应统一 {rule.start_time} 上班，实际开始时间不符",
                        [rule.id],
                        ("V005", item.employee_id, item.date, rule.id),
                    )

    leave_dates_by_employee: dict[int | None, set[str]] = defaultdict(set)
    for rule, dates in leave_rules:
        if rule.employee_id is not None:
            leave_dates_by_employee[rule.employee_id].update(dates)
    for rule in rules:
        if not rule.valid or rule.type != "ALL_START_AT" or not rule.start_time:
            continue
        for day in _rule_dates(rule, plan.month):
            for employee in employees:
                own = [
                    item
                    for item in assignments
                    if item.employee_id == employee.id and item.date == day
                ]
                if day in leave_dates_by_employee.get(employee.id, set()):
                    continue
                if any(
                    shift_map.get(item.shift_id) is not None
                    and shift_map[item.shift_id].is_leave
                    for item in own
                ):
                    continue
                # 休息（OFF）为展示补全，不算出勤：全员到岗日出现 OFF 视为缺勤
                worked_own = [
                    item
                    for item in own
                    if not (
                        shift_map.get(item.shift_id) is not None
                        and shift_map[item.shift_id].is_off
                    )
                ]
                if worked_own:
                    continue
                add_conflict(
                    "V005",
                    f"{employee.name} 在 {day} 应全员 {rule.start_time} 上班，当天没有排班",
                    [rule.id],
                    ("V005", employee.id, rule.id, day),
                )

    for rule, dates in must_work_rules:
        target_ids = (
            [rule.employee_id]
            if rule.employee_id is not None
            else [employee.id for employee in employees if employee.id is not None]
        )
        for employee_id in target_ids:
            employee = employee_map.get(employee_id)
            worked_dates = {
                item.date
                for item in assignments
                if item.employee_id == employee_id
                and not (
                    shift_map.get(item.shift_id)
                    and (shift_map[item.shift_id].is_leave or shift_map[item.shift_id].is_off)
                )
            }
            missed = [day for day in dates if day not in worked_dates]
            if not missed:
                continue
            who = employee.name if employee else f"#{employee_id}"
            add_conflict(
                "V011",
                f"{who} 在 {missed[0]} 应必须上班，但当天没有排班",
                [rule.id],
                ("V011", employee_id, rule.id, missed[0]),
            )

    for rule, dates in leave_rules:
        employee = employee_map.get(rule.employee_id)
        own = [item for item in assignments if item.employee_id == rule.employee_id]
        # 只给天数的有薪假：只校验区间内的有薪假天数，不要求每一天都是 POFF
        quota = rule.count if rule.count is not None else None
        if quota is not None and 0 < quota < len(dates):
            leave_days = {
                item.date
                for item in own
                if shift_map.get(item.shift_id) is not None
                and shift_map[item.shift_id].is_leave
            }
            actual = len(leave_days & set(dates))
            if actual < quota:
                who = employee.name if employee else f"#{rule.employee_id}"
                add_conflict(
                    "V012",
                    f"{who} 应有 {quota} 天有薪假，实际只有 {actual} 天",
                    [rule.id],
                    ("V012", rule.employee_id, rule.id, None),
                )
            continue
        for day in dates:
            day_items = [item for item in own if item.date == day]
            if any(
                shift_map.get(item.shift_id) is not None
                and shift_map[item.shift_id].is_leave
                for item in day_items
            ):
                continue
            who = employee.name if employee else f"#{rule.employee_id}"
            # 休息（OFF）展示不算被排班，只是未记录 POFF
            worked_day_items = [
                item
                for item in day_items
                if not (
                    shift_map.get(item.shift_id) is not None
                    and shift_map[item.shift_id].is_off
                )
            ]
            message = (
                f"{who} 在 {day} 应有薪休假，却被排班"
                if worked_day_items
                else f"{who} 在 {day} 应有薪休假，但未记录 POFF"
            )
            add_conflict(
                "V012",
                message,
                [rule.id],
                ("V012", rule.employee_id, rule.id, day),
            )

    for employee_id, employee_rules in rules_by_employee.items():
        if employee_id not in employee_map:
            for rule in employee_rules:
                add_conflict(
                    "V009",
                    "规则引用了不存在的人员",
                    [rule.id],
                    ("V009", employee_id, rule.id),
                )

    # V021 / P006：人数上限被突破（硬约束报错，软约束只提示）
    limits = compute_staff_limits(plan, employees, shifts, assignments, rules)
    rule_map = {rule.id: rule for rule in rules}
    for row in limits:
        rule = rule_map.get(row["rule_id"])
        message = (
            f"{row['date']} {row['label']}，实际 {row['assigned']} 人，超出 {row['over']} 人"
        )
        if rule is not None and rule.hard:
            conflicts.append(_conflict("V021", message, [row["rule_id"]]))
        else:
            conflicts.append(_conflict("P006", f"{message}，已按软约束放宽", [row["rule_id"]]))

    # 规则满足情况：逐条规则标注 满足/放宽 并给出量化信息，软约束被放宽或硬约束被
    # 违反的规则在 conflicts 中都有对应条目（P0xx / V0xx / E0xx）。
    rule_status = _build_rule_status(
        plan, employees, shifts, rules, assignments, conflicts, coverage
    )

    # P8：不设超额班次上限，但报告统计超过 8 小时的班次数量（休息/休假班次不计）
    long_shift_count = 0
    for item in assignments:
        shift = shift_map.get(item.shift_id) if item.shift_id is not None else None
        if shift is not None and (shift.is_off or shift.is_leave):
            continue
        value = (
            effective_hours(shift.work_hours, shift.start_time, shift.end_time)
            if shift is not None
            else effective_hours(None, item.start_time, item.end_time)
        )
        if value > 8 + 1e-6:
            long_shift_count += 1

    # 两阶段优化的统一指标口径（横幅、候选、报告共用同一份计算）
    breakdown = build_breakdown(plan, employees, shifts, rules, assignments, coverage)

    return {
        "status": "CHECKED",
        "message": "",
        "objective": 0,
        "coverage": coverage,
        "hours": hours,
        "conflicts": conflicts,
        "rest_summary": rest_summary,
        "limits": limits,
        "rule_status": rule_status,
        "long_shift_count": long_shift_count,
        "breakdown": breakdown,
    }


def _gap_minutes(start: str, end: str) -> int:
    try:
        start_h, start_m = (int(part) for part in start.split(":"))
        end_h, end_m = (int(part) for part in end.split(":"))
    except (ValueError, AttributeError):
        return 0
    return max(0, (end_h * 60 + end_m) - (start_h * 60 + start_m))


def build_breakdown(
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    rules: list[Rule],
    assignments: list[Assignment],
    coverage: list[dict[str, Any]],
) -> dict[str, Any]:
    """两阶段优化的分项指标，全部基于 validator 已有的同一批数据计算。

    - long_shifts：超过 8 小时的班次数
    - non_8h_shifts：非 8 小时的普通班次数（不含特殊/休息/休假）
    - soft_rest_missed_days：软休规则未命中天数（MUST_WORK 当天豁免）
    - coverage_gap_minutes：覆盖缺口累计分钟
    - rest_over_limit：连休超过上限的当量天数
    """
    shift_map: dict[int | None, Shift] = {
        shift.id: shift for shift in shifts if shift.id is not None
    }
    off_keys = {
        (item.employee_id, item.date)
        for item in assignments
        if (shift := shift_map.get(item.shift_id)) is not None
        and (shift.is_off or shift.is_leave)
    }
    long_shifts = 0
    non_8h_shifts = 0
    for item in assignments:
        shift = shift_map.get(item.shift_id)
        if shift is not None and (shift.is_off or shift.is_leave):
            continue
        hours = (
            effective_hours(shift.work_hours, shift.start_time, shift.end_time)
            if shift is not None
            else effective_hours(None, item.start_time, item.end_time)
        )
        if hours > 8 + 1e-6:
            long_shifts += 1
        if shift is None or not shift.is_special:
            if abs(hours - 8) > 1e-6:
                non_8h_shifts += 1

    must_work_keys: set[tuple[int, str]] = set()
    for rule in rules:
        if rule.type != "MUST_WORK" or not rule.valid:
            continue
        rule_days = _rule_dates(rule, plan.month)
        target_ids = [rule.employee_id] if rule.employee_id is not None else [
            employee.id for employee in employees if employee.id is not None
        ]
        for employee_id in target_ids:
            for day in rule_days:
                must_work_keys.add((employee_id, day))

    soft_rest_missed = 0
    for rule in rules:
        if rule.type != "REST" or rule.hard or not rule.valid:
            continue
        rule_days = _rule_dates(rule, plan.month)
        target_ids = [rule.employee_id] if rule.employee_id is not None else [
            employee.id for employee in employees if employee.id is not None
        ]
        for employee_id in target_ids:
            for day in rule_days:
                if (employee_id, day) in must_work_keys:
                    continue
                if (employee_id, day) not in off_keys:
                    soft_rest_missed += 1

    coverage_gap_minutes = 0
    for entry in coverage:
        for gap in entry.get("gaps", []) or []:
            coverage_gap_minutes += _gap_minutes(gap.get("start", ""), gap.get("end", ""))

    # 连休超限当量：复用规则满足情况里的同一套连续休息段计算（带薪假为中性日）
    leave_days_by_employee: dict[int | None, set[str]] = defaultdict(set)
    for item in assignments:
        shift = shift_map.get(item.shift_id)
        if shift is not None and shift.is_leave:
            leave_days_by_employee[item.employee_id].add(item.date)
    for rule in rules:
        if rule.valid and rule.type == "PAID_LEAVE" and rule.employee_id is not None:
            leave_days_by_employee[rule.employee_id].update(_rule_dates(rule, plan.month))
    rest_limit_rules = [
        rule
        for rule in rules
        if rule.valid and rule.type == "MAX_REST_DAYS" and (rule.count or 0) > 0
    ]
    rest_over_limit = 0
    if rest_limit_rules:
        for employee in employees:
            if employee.id is None:
                continue
            worked = {
                item.date
                for item in assignments
                if item.employee_id == employee.id
                and not (
                    shift_map.get(item.shift_id)
                    and (
                        shift_map[item.shift_id].is_leave
                        or shift_map[item.shift_id].is_off
                    )
                )
            }
            runs = _consecutive_runs_rest(
                worked, plan.month, leave_days_by_employee.get(employee.id, set())
            )
            longest = max(runs) if runs else 0
            limits = [
                rule.count or 0
                for rule in rest_limit_rules
                if rule.employee_id in (None, employee.id)
            ]
            if limits:
                rest_over_limit += max(0, longest - min(limits))

    return {
        "long_shifts": long_shifts,
        "non_8h_shifts": non_8h_shifts,
        "soft_rest_missed_days": soft_rest_missed,
        "coverage_gap_minutes": coverage_gap_minutes,
        "rest_over_limit": rest_over_limit,
    }
