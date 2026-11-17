# pyright: reportExplicitAny=false, reportImplicitStringConcatenation=false
# 说明：冲突预检结果是给前端直接消费的 JSON 结构，dict 值类型天然异构，无法用单一 TypedDict 精确描述；
# 刻意换行拼接的提示文案属于业务可读性写法，这里只关闭由此产生的类型提示，真实错误仍然保留。
"""规则冲突预检：落库前做纯本地、确定性的规则对矛盾判定。

不调用模型、不跑求解，只覆盖能确定判定的组合；判定不确定时宁可不报，
避免误报打断用户正常的规则录入。新规则与既有规则、同一批次的新规则之间
都会互相检查（批量导入自相矛盾的场景）。

severity 说明：
- error：确定矛盾，接口层据此返回 409 并阻止落库；
- warning：涉及软约束，求解时可以放宽，只提示不阻塞。
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..models import Employee, Plan, Rule, Shift
from .calendar_service import (
    effective_hours,
    month_dates,
    parse_interval,
    period_buckets,
    week_days_in_month,
    weekly_quota,
)
from .solver import expand_rule_dates

# 冲突码对应的业务标签
CONFLICT_LABELS = {
    "RC01": "休息与上班要求同时生效",
    "RC02": "休息与指定班次/时刻同时生效",
    "RC03": "有薪休假与必须上班同时生效",
    "RC04": "指定班次与指定上班时间不一致",
    "RC05": "人数需求超过可用员工",
    "RC06": "人数上限低于现有需求",
    "RC07": "固定班次工时不满足工时条件",
    "RC08": "连续休息天数超过上限",
    "RC09": "星期限定的工时条件与固定班次矛盾",
    "RC10": "最少休息天数与强制上班天数矛盾",
    "RC11": "最多上班天数低于强制上班天数",
    "RC12": "禁止班次与固定班次/班次人数需求矛盾",
}

RULE_LABELS = {
    "REST": "休息",
    "MUST_WORK": "必须上班",
    "PAID_LEAVE": "有薪休假",
    "FIXED_SHIFT": "指定班次",
    "FIXED_TIME": "指定上班时间",
    "ALL_START_AT": "全员统一上班时间",
    "DAY_STAFF": "当天人数",
    "SEGMENT_STAFF": "时段覆盖",
    "START_STAFF": "上班时刻人数",
    "END_STAFF": "下班时刻人数",
    "SHIFT_STAFF": "盘点班次人数",
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
}

MIN_COUNT_TYPES = {"DAY_STAFF", "SEGMENT_STAFF", "START_STAFF", "END_STAFF", "SHIFT_STAFF"}
# 消息里的天数占位符，聚合后统一替换成「2026-11-03」或「2026-11-03、11-04 等 5 天」
DAYS_TOKEN = "__DAYS__"


@dataclass
class _Entry:
    """参与检查的一条规则；new_index 非空表示这批待写入的新规则下标。"""

    rule: Rule
    new_index: int | None = None


@dataclass
class _Hit:
    """同一对规则的逐日命中，最后聚合成一条可读冲突。"""

    code: str
    message: str
    severity: str
    hint: str
    rule_ids: list[int]
    new_indexes: list[int]
    labels: list[str]
    days: list[str] = field(default_factory=list)


class _Collector:
    """聚合冲突：同一对规则的多天命中合并成一条，避免逐日刷屏。"""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, tuple[int, ...], tuple[int, ...]], _Hit] = {}

    def add(
        self,
        code: str,
        message: str,
        severity: str,
        hint: str,
        day: str,
        entries: list[_Entry],
        labels: list[str],
    ) -> None:
        rule_ids = sorted(entry.rule.id for entry in entries if entry.rule.id is not None)
        new_indexes = sorted(
            entry.new_index for entry in entries if entry.new_index is not None
        )
        key = (code, tuple(rule_ids), tuple(new_indexes))
        hit = self._hits.get(key)
        if hit is None:
            hit = _Hit(
                code=code,
                message=message,
                severity=severity,
                hint=hint,
                rule_ids=rule_ids,
                new_indexes=new_indexes,
                labels=labels,
            )
            self._hits[key] = hit
        if day not in hit.days:
            hit.days.append(day)
        # 任一次命中是确定冲突，整体按确定冲突处理
        if severity == "error":
            hit.severity = "error"

    def items(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for hit in self._hits.values():
            hit.days.sort()
            result.append(
                {
                    "code": hit.code,
                    "message": hit.message.replace(DAYS_TOKEN, _days_text(hit.days)),
                    "severity": hit.severity,
                    "hint": hit.hint,
                    "rule_ids": hit.rule_ids,
                    "new_rule_indexes": hit.new_indexes,
                    "labels": hit.labels,
                }
            )
        return result


def _days_text(days: list[str]) -> str:
    if len(days) == 1:
        return days[0]
    preview = "、".join(days[:3])
    return f"{preview} 等 {len(days)} 天"


def _describe(rule: Rule, employee_map: dict[int | None, Employee], shift_map: dict[int, Shift]) -> str:
    """规则 → 一句业务可读短描述。"""
    owner = employee_map[rule.employee_id].name if rule.employee_id in employee_map else "全员"
    period = (
        rule.date_start
        if rule.date_start == rule.date_end
        else f"{rule.date_start}~{rule.date_end}"
    )
    kind = RULE_LABELS.get(rule.type, rule.type)
    detail = ""
    shift = shift_map.get(rule.shift_id) if rule.shift_id is not None else None
    if rule.type in ("FIXED_SHIFT", "SHIFT_STAFF", "SHIFT_LIMIT", "SINGLE_ROLE_SHIFT") and shift is not None:
        detail = f" {shift.name}（{shift.start_time}-{shift.end_time}）"
    elif rule.start_time:
        detail = f" {rule.start_time}"
        if rule.end_time:
            detail += f"-{rule.end_time}"
    if rule.type == "SHIFT_HOURS" and rule.work_hours is not None:
        op = {"ge": "≥", "le": "≤"}.get(rule.hours_op or "eq", "=")
        detail = f" 工时{op}{rule.work_hours:g} 小时"
        if rule.weekdays:
            detail += f"（星期 {rule.weekdays}）"
    elif rule.type == "MAX_REST_DAYS":
        detail = f" 最多连休 {rule.count or 0} 天"
    elif rule.type == "MAX_CONSECUTIVE_WORK":
        detail = f" 最多连上 {rule.count or 0} 天"
    elif rule.type in ("MIN_REST_DAYS", "MAX_WORK_DAYS"):
        scope = "每周" if (rule.period or "month") == "week" else "每月"
        verb = "至少休" if rule.type == "MIN_REST_DAYS" else "最多上"
        detail = f" {scope}{verb} {rule.count or 0} 天"
    elif rule.type == "BAN_SHIFT" and shift is not None:
        detail = f" {shift.name}（{shift.start_time}-{shift.end_time}）"
    elif rule.type == "SINGLE_ROLE_SHIFT" and rule.count is not None:
        detail += f"（岗位「{rule.role or ''}」上班 ≤{rule.count} 人时）"
    elif rule.count is not None and rule.type in MIN_COUNT_TYPES | {"MAX_STAFF"}:
        detail += f" {rule.count} 人"
        if rule.role:
            detail += f"（岗位 {rule.role}）"
    prefix = f"#{rule.id} " if rule.id is not None else ""
    return f"{prefix}{owner} {period} {kind}{detail}"


def _severity(*rules: Rule) -> str:
    """统一的冲突定级：所有参与判定的一方都是硬约束才算确定性冲突。

    - 单条规则场景（如单条人数需求超员）：该规则硬 → error，软 → warning；
    - 规则对场景（如上限 vs 需求、互斥时段求和）：双方都硬 → error，任一为软 → warning。
    """
    return "error" if all(rule.hard for rule in rules) else "warning"


def _hours_match(hours: float, target: float, op: str) -> bool:
    if op == "ge":
        return hours >= target - 1e-6
    if op == "le":
        return hours <= target + 1e-6
    return abs(hours - target) <= 0.01


def _shift_hours(shift: Shift) -> float:
    return effective_hours(shift.work_hours, shift.start_time, shift.end_time)


def _window(rule: Rule, shift_map: dict[int, Shift]) -> tuple[int, int] | None:
    """人数需求对应的时段；无法确定时段（当天人数/上班时刻）时返回 None。"""
    if rule.type == "SEGMENT_STAFF" and rule.start_time and rule.end_time:
        return parse_interval(rule.start_time, rule.end_time)
    if rule.type == "SHIFT_STAFF" and rule.shift_id is not None:
        shift = shift_map.get(rule.shift_id)
        if shift is not None:
            return parse_interval(shift.start_time, shift.end_time)
    return None


def _is_work_shift(rule: Rule, shift_map: dict[int, Shift]) -> bool:
    """FIXED_SHIFT 指向 OFF/POFF 时本质是休息，不参与「休息 vs 班次」冲突判定。"""
    shift = shift_map.get(rule.shift_id) if rule.shift_id is not None else None
    return shift is not None and not shift.is_off and not shift.is_leave


def _pair_conflicts(
    entries: list[_Entry],
    plan: Plan,
    employee_map: dict[int | None, Employee],
    shift_map: dict[int, Shift],
    collector: _Collector,
) -> None:
    dates_cache: dict[int, set[str]] = {
        id(entry.rule): set(expand_rule_dates(plan, entry.rule)) for entry in entries
    }

    def shared_days(first: _Entry, second: _Entry) -> list[str]:
        return sorted(dates_cache[id(first.rule)] & dates_cache[id(second.rule)])

    rest_entries = [e for e in entries if e.rule.type == "REST"]
    must_work = [e for e in entries if e.rule.type == "MUST_WORK"]
    leave_entries = [
        e for e in entries if e.rule.type == "PAID_LEAVE" and e.rule.employee_id is not None
    ]
    fixed_shifts = [
        e
        for e in entries
        if e.rule.type == "FIXED_SHIFT"
        and e.rule.employee_id is not None
        and _is_work_shift(e.rule, shift_map)
    ]
    fixed_times = [
        e for e in entries if e.rule.type == "FIXED_TIME" and e.rule.employee_id is not None
    ]
    hours_rules = [e for e in entries if e.rule.type == "SHIFT_HOURS"]

    def name_of(employee_id: int | None) -> str:
        employee = employee_map.get(employee_id)
        return employee.name if employee else f"#{employee_id}"

    def same_target(first: int | None, second: int | None) -> bool:
        """employee_id=None 表示全员，与任何人员都有交集。"""
        return first is None or second is None or first == second

    def target_name(first: int | None, second: int | None) -> str:
        employee_id = first if first is not None else second
        return name_of(employee_id) if employee_id is not None else "全员"

    def labels_of(*items: _Entry) -> list[str]:
        return [_describe(entry.rule, employee_map, shift_map) for entry in items]

    # RC01：休息 ↔ 必须上班
    for rest in rest_entries:
        for work in must_work:
            if not same_target(rest.rule.employee_id, work.rule.employee_id):
                continue
            for day in shared_days(rest, work):
                who = target_name(rest.rule.employee_id, work.rule.employee_id)
                collector.add(
                    "RC01",
                    f"{who} 在 {DAYS_TOKEN} 同时被要求休息和必须上班",
                    _severity(rest.rule, work.rule),
                    f"删除 {day} {who} 的休息规则，或把它改成可违反并取消当天必须上班",
                    day,
                    [rest, work],
                    labels_of(rest, work),
                )

    # RC02：休息 ↔ 指定班次 / 指定上班时间
    for rest in rest_entries:
        for other in fixed_shifts + fixed_times:
            if not same_target(rest.rule.employee_id, other.rule.employee_id):
                continue
            requirement = "指定班次" if other.rule.type == "FIXED_SHIFT" else "指定上班时间"
            for day in shared_days(rest, other):
                who = target_name(rest.rule.employee_id, other.rule.employee_id)
                collector.add(
                    "RC02",
                    f"{who} 在 {DAYS_TOKEN} 同时被要求休息和{requirement}",
                    _severity(rest.rule, other.rule),
                    f"删除 {day} {who} 的休息规则，或取消当天的{requirement}",
                    day,
                    [rest, other],
                    labels_of(rest, other),
                )

    # RC03：有薪休假 ↔ 必须上班
    for leave in leave_entries:
        for work in must_work:
            if not same_target(leave.rule.employee_id, work.rule.employee_id):
                continue
            for day in shared_days(leave, work):
                who = target_name(leave.rule.employee_id, work.rule.employee_id)
                collector.add(
                    "RC03",
                    f"{who} 在 {DAYS_TOKEN} 同时被要求有薪休假和必须上班",
                    _severity(leave.rule, work.rule),
                    f"删除 {day} {who} 的有薪休假，或取消当天必须上班",
                    day,
                    [leave, work],
                    labels_of(leave, work),
                )

    # RC04：指定班次 ↔ 指定上班时间（上班时刻不一致）
    for fixed in fixed_shifts:
        assert fixed.rule.shift_id is not None
        shift = shift_map[fixed.rule.shift_id]
        for timing in fixed_times:
            if fixed.rule.employee_id != timing.rule.employee_id:
                continue
            if shift.start_time == timing.rule.start_time:
                continue
            for day in shared_days(fixed, timing):
                who = name_of(fixed.rule.employee_id)
                collector.add(
                    "RC04",
                    f"{who} 在 {DAYS_TOKEN} 的指定班次 {shift.name}（{shift.start_time} 上班）"
                    f"与指定 {timing.rule.start_time} 上班冲突",
                    _severity(fixed.rule, timing.rule),
                    f"{day} {who} 只能二选一：改成同一个上班时刻，或删除其中一条规则",
                    day,
                    [fixed, timing],
                    labels_of(fixed, timing),
                )

    # RC07 / RC09：指定班次的工时 ↔ 班次工时条件
    for fixed in fixed_shifts:
        assert fixed.rule.shift_id is not None
        shift = shift_map[fixed.rule.shift_id]
        hours = _shift_hours(shift)
        for condition in hours_rules:
            employee_id = condition.rule.employee_id
            if employee_id is not None and employee_id != fixed.rule.employee_id:
                continue
            target = condition.rule.work_hours
            if target is None:
                continue
            op = condition.rule.hours_op or "eq"
            if _hours_match(hours, target, op):
                continue
            op_text = {
                "ge": f"不少于 {target:g}",
                "le": f"不超过 {target:g}",
            }.get(op, f"等于 {target:g}")
            code = "RC09" if condition.rule.weekdays else "RC07"
            for day in shared_days(fixed, condition):
                collector.add(
                    code,
                    f"{DAYS_TOKEN} 被固定的班次 {shift.name} 为 {hours:g} 小时，"
                    f"不满足「工时{op_text} 小时」的条件",
                    _severity(fixed.rule, condition.rule),
                    f"调整工时条件或更换班次：{shift.name} 只有 {hours:g} 小时",
                    day,
                    [fixed, condition],
                    labels_of(fixed, condition),
                )


def _headcount_conflicts(
    entries: list[_Entry],
    plan: Plan,
    employee_count: int,
    shift_map: dict[int, Shift],
    collector: _Collector,
    employee_map: dict[int | None, Employee],
) -> None:
    """RC05 / RC06：同日人数需求超过可用员工，或人数上限低于现有需求。"""
    if employee_count <= 0:
        return
    day_min: dict[str, list[_Entry]] = defaultdict(list)
    day_max: dict[str, list[_Entry]] = defaultdict(list)
    for entry in entries:
        rule = entry.rule
        if rule.type in MIN_COUNT_TYPES:
            for day in expand_rule_dates(plan, rule):
                day_min[day].append(entry)
        elif rule.type == "MAX_STAFF":
            for day in expand_rule_dates(plan, rule):
                day_max[day].append(entry)

    def describe(entry: _Entry) -> str:
        return _describe(entry.rule, employee_map, shift_map)

    # RC05：单条需求本身超过可用员工数
    for day, items in day_min.items():
        for entry in items:
            count = entry.rule.count or 0
            if count <= employee_count:
                continue
            collector.add(
                "RC05",
                f"{DAYS_TOKEN} 的人数需求 {count} 人超过可用员工 {employee_count} 人",
                _severity(entry.rule),
                "降低人数需求，或先补充人员",
                day,
                [entry],
                [describe(entry)],
            )

    # RC05：时段互不重叠的两个需求之和超过可用员工数（同一人无法同时满足）
    for day, items in day_min.items():
        for index, first in enumerate(items):
            first_window = _window(first.rule, shift_map)
            if first_window is None:
                continue
            for second in items[index + 1 :]:
                second_window = _window(second.rule, shift_map)
                if second_window is None:
                    continue
                disjoint = (
                    first_window[1] <= second_window[0] or second_window[1] <= first_window[0]
                )
                if not disjoint:
                    continue
                total = (first.rule.count or 0) + (second.rule.count or 0)
                if total <= employee_count:
                    continue
                collector.add(
                    "RC05",
                    f"{DAYS_TOKEN} 两个互不重叠的时段分别需要 {first.rule.count or 0} 人与 "
                    f"{second.rule.count or 0} 人，合计 {total} 人，超过可用员工 {employee_count} 人",
                    _severity(first.rule, second.rule),
                    "降低其中一个时段的人数需求，或补充人员",
                    day,
                    [first, second],
                    [describe(first), describe(second)],
                )

    # RC06：人数上限低于同日已有的最少人数需求。
    # 定级必须同时看上限规则与这一条需求规则：只有双方都是硬约束才是确定冲突，
    # 任一为软约束时求解器可以按既有惩罚机制放宽（例如 MAX_STAFF 0 与软性时段覆盖）。
    for day, limits in day_max.items():
        for limit_entry in limits:
            limit = limit_entry.rule.count or 0
            for demand in day_min.get(day, []):
                count = demand.rule.count or 0
                if count <= limit:
                    continue
                collector.add(
                    "RC06",
                    f"{DAYS_TOKEN} 的人数上限为 {limit} 人，但同一天还要求至少 {count} 人上班",
                    _severity(limit_entry.rule, demand.rule),
                    f"把人数上限提高到 {count} 人，或降低这一天的人数需求",
                    day,
                    [limit_entry, demand],
                    [describe(limit_entry), describe(demand)],
                )


def _max_rest_conflicts(
    entries: list[_Entry],
    plan: Plan,
    employees: list[Employee],
    shift_map: dict[int, Shift],
    employee_map: dict[int | None, Employee],
) -> list[dict[str, Any]]:
    """RC08：硬性连休上限 vs 该员工被强制休息的连续天数（每段单独一条）。"""
    rest_forced: dict[int, set[str]] = defaultdict(set)
    rest_sources: dict[int, list[_Entry]] = defaultdict(list)
    for entry in entries:
        rule = entry.rule
        forced = False
        if rule.type == "REST" and rule.hard:
            forced = True
        elif rule.type == "PAID_LEAVE":
            forced = True
        elif rule.type == "FIXED_SHIFT" and rule.shift_id is not None:
            shift = shift_map.get(rule.shift_id)
            forced = shift is not None and (shift.is_off or shift.is_leave)
        if not forced:
            continue
        if rule.employee_id is not None:
            target_ids = [rule.employee_id]
        elif rule.type == "REST":
            # 全员休息：对每个员工都算作强制休息
            target_ids = [employee.id for employee in employees if employee.id is not None]
        else:
            target_ids = []
        for employee_id in target_ids:
            rest_forced[employee_id].update(expand_rule_dates(plan, rule))
            rest_sources[employee_id].append(entry)

    limits = [e for e in entries if e.rule.type == "MAX_REST_DAYS"]
    if not limits:
        return []

    items: list[dict[str, Any]] = []
    month_days = month_dates(plan.month)
    for limit_entry in limits:
        limit = limit_entry.rule.count or 0
        targets = (
            [limit_entry.rule.employee_id]
            if limit_entry.rule.employee_id is not None
            else [employee.id for employee in employees]
        )
        scope = set(expand_rule_dates(plan, limit_entry.rule))
        for employee_id in targets:
            assert employee_id is not None
            forced_days = rest_forced.get(employee_id, set()) & scope
            if not forced_days:
                continue
            runs: list[list[str]] = []
            current: list[str] = []
            for day in month_days:
                if day in scope and day in forced_days:
                    current.append(day)
                    continue
                if current:
                    runs.append(current)
                    current = []
            if current:
                runs.append(current)
            for run in runs:
                if len(run) <= limit:
                    continue
                employee = employee_map.get(employee_id)
                who = employee.name if employee else f"#{employee_id}"
                related = [
                    entry
                    for entry in rest_sources.get(employee_id, [])
                    if any(entry.rule.date_start <= day <= entry.rule.date_end for day in run)
                ]
                involved = [limit_entry] + related
                labels = [
                    _describe(entry.rule, employee_map, shift_map) for entry in involved
                ]
                items.append(
                    {
                        "code": "RC08",
                        # 连休上限默认是软约束：显式更长休息要求优先，这里只做提示
                        "message": (
                            f"{who} 在 {run[0]} 至 {run[-1]} 连续休息 {len(run)} 天，"
                            f"该连休由显式要求触发，超出默认上限 {limit} 天"
                        ),
                        "severity": "warning",
                        "hint": f"显式休息要求优先，连休上限已自动让路（超出 {len(run) - limit} 天）",
                        "rule_ids": sorted(
                            entry.rule.id
                            for entry in involved
                            if entry.rule.id is not None
                        ),
                        "new_rule_indexes": sorted(
                            entry.new_index
                            for entry in involved
                            if entry.new_index is not None
                        ),
                        "labels": labels,
                    }
                )
    return items


def _forced_work_conflicts(
    entries: list[_Entry],
    plan: Plan,
    employees: list[Employee],
    shift_map: dict[int, Shift],
    employee_map: dict[int | None, Employee],
) -> list[dict[str, Any]]:
    """RC10 / RC11：周期天数类规则 vs 同周期内被强制上班的天数。

    强制上班来源：MUST_WORK、FIXED_SHIFT（指向可上班班次）、FIXED_TIME、ALL_START_AT。
    只做确定性判定：强制上班天数 + 要求休息天数 > 周期天数即为矛盾。
    """
    forced: dict[int, dict[str, list[_Entry]]] = defaultdict(lambda: defaultdict(list))
    all_start_entries: dict[str, list[_Entry]] = defaultdict(list)
    for entry in entries:
        rule = entry.rule
        if rule.type == "ALL_START_AT" and rule.start_time:
            for day in expand_rule_dates(plan, rule):
                all_start_entries[day].append(entry)
            continue
        if rule.employee_id is None:
            continue
        if rule.type in ("MUST_WORK", "FIXED_TIME"):
            is_forced = True
        elif rule.type == "FIXED_SHIFT":
            is_forced = _is_work_shift(rule, shift_map)
        else:
            is_forced = False
        if not is_forced:
            continue
        for day in expand_rule_dates(plan, rule):
            forced[rule.employee_id][day].append(entry)

    def forced_for(employee_id: int, day: str) -> list[_Entry]:
        return forced.get(employee_id, {}).get(day, []) + all_start_entries.get(day, [])

    def scope_text(rule: Rule, bucket: list[str]) -> str:
        if (rule.period or "month") == "week" and bucket:
            return f"{bucket[0]} 至 {bucket[-1]} 这一周"
        return f"{rule.date_start} 至 {rule.date_end}"

    items: list[dict[str, Any]] = []
    for entry in entries:
        rule = entry.rule
        if rule.type not in ("MIN_REST_DAYS", "MAX_WORK_DAYS"):
            continue
        count = rule.count or 0
        targets = (
            [rule.employee_id]
            if rule.employee_id is not None
            else [employee.id for employee in employees]
        )
        for bucket in period_buckets(rule.period, expand_rule_dates(plan, rule)):
            if not bucket:
                continue
            # 首尾残周与求解器/校验器同口径：至少休息 floor 折算，
            # 最多上班按 min(count, 可见天数) 折算
            quota = count
            quota_note = ""
            if (rule.period or "month") == "week":
                quota = weekly_quota(
                    count,
                    plan.month,
                    bucket,
                    is_limit=rule.type == "MAX_WORK_DAYS",
                )
                visible = week_days_in_month(plan.month, bucket[0])
                if visible < 7:
                    quota_note = f"（本周仅 {visible} 天在计划月内，配额折算为 {quota} 天）"
            for employee_id in targets:
                assert employee_id is not None
                forced_entries: list[_Entry] = []
                forced_days: list[str] = []
                for day in bucket:
                    hits = forced_for(employee_id, day)
                    if hits:
                        forced_days.append(day)
                        for hit in hits:
                            if hit not in forced_entries:
                                forced_entries.append(hit)
                if not forced_days:
                    continue
                if rule.type == "MIN_REST_DAYS":
                    # 至少休 quota 天 ⇒ 周期内最多上班 len(bucket) - quota 天
                    allowed = len(bucket) - min(quota, len(bucket))
                    if len(forced_days) <= allowed:
                        continue
                    code = "RC10"
                    message = (
                        f"{_name_of(employee_id, employee_map)} 在 {scope_text(rule, bucket)} "
                        f"已有 {len(forced_days)} 天被强制上班，无法满足「至少休 {count} 天」"
                        f"{quota_note}"
                    )
                    hint = (
                        f"减少 {_name_of(employee_id, employee_map)} 的强制上班天数，"
                        f"或把最少休息天数改到 {allowed} 天以内"
                    )
                else:
                    if len(forced_days) <= quota:
                        continue
                    code = "RC11"
                    message = (
                        f"{_name_of(employee_id, employee_map)} 在 {scope_text(rule, bucket)} "
                        f"已被强制上班 {len(forced_days)} 天，超过「最多上 {count} 天」"
                        f"{quota_note}"
                    )
                    hint = f"把最多上班天数提高到 {len(forced_days)} 天，或减少强制上班日"
                if not forced_entries:
                    continue
                involved = [entry] + forced_entries
                items.append(
                    {
                        "code": code,
                        "message": message,
                        "severity": "error" if rule.hard else "warning",
                        "hint": hint,
                        "rule_ids": sorted(
                            item.rule.id for item in involved if item.rule.id is not None
                        ),
                        "new_rule_indexes": sorted(
                            item.new_index
                            for item in involved
                            if item.new_index is not None
                        ),
                        "labels": [
                            _describe(item.rule, employee_map, shift_map) for item in involved
                        ],
                    }
                )
    return items


def _name_of(employee_id: int | None, employee_map: dict[int | None, Employee]) -> str:
    employee = employee_map.get(employee_id)
    return employee.name if employee else f"#{employee_id}"


def _ban_shift_conflicts(
    entries: list[_Entry],
    plan: Plan,
    employees: list[Employee],
    shift_map: dict[int, Shift],
    employee_map: dict[int | None, Employee],
) -> list[dict[str, Any]]:
    """RC12：禁止班次 vs 同员工同日的固定班次；全员禁止 vs 盘点班次人数需求。"""
    bans = [e for e in entries if e.rule.type == "BAN_SHIFT" and e.rule.shift_id is not None]
    if not bans:
        return []
    fixed_shifts = [
        e for e in entries if e.rule.type == "FIXED_SHIFT" and e.rule.employee_id is not None
    ]
    shift_staff = [
        e for e in entries if e.rule.type == "SHIFT_STAFF" and e.rule.shift_id is not None
    ]
    items: list[dict[str, Any]] = []
    for ban in bans:
        ban_days = set(expand_rule_dates(plan, ban.rule))
        shift = shift_map.get(ban.rule.shift_id) if ban.rule.shift_id is not None else None
        shift_name = shift.name if shift is not None else f"#{ban.rule.shift_id}"
        for fixed in fixed_shifts:
            if fixed.rule.shift_id != ban.rule.shift_id:
                continue
            if ban.rule.employee_id is not None and fixed.rule.employee_id != ban.rule.employee_id:
                continue
            days = sorted(ban_days & set(expand_rule_dates(plan, fixed.rule)))
            if not days:
                continue
            who = _name_of(fixed.rule.employee_id, employee_map)
            involved = [ban, fixed]
            items.append(
                {
                    "code": "RC12",
                    "message": (
                        f"{who} 在 {_days_text(days)} 同时被指定班次 {shift_name} 与禁止排该班次"
                    ),
                    "severity": _severity(ban.rule, fixed.rule),
                    "hint": f"取消 {who} 的 {shift_name} 固定班次，或删除该禁止班次规则",
                    "rule_ids": sorted(
                        item.rule.id for item in involved if item.rule.id is not None
                    ),
                    "new_rule_indexes": sorted(
                        item.new_index for item in involved if item.new_index is not None
                    ),
                    "labels": [
                        _describe(item.rule, employee_map, shift_map) for item in involved
                    ],
                }
            )
        # 全员禁止 + 「该班次至少 N 人」：N ≥ 可用员工数时必然矛盾
        if ban.rule.employee_id is not None:
            continue
        for demand in shift_staff:
            if demand.rule.shift_id != ban.rule.shift_id:
                continue
            required = demand.rule.count or 0
            if required < len(employees):
                continue
            days = sorted(ban_days & set(expand_rule_dates(plan, demand.rule)))
            if not days:
                continue
            involved = [ban, demand]
            items.append(
                {
                    "code": "RC12",
                    "message": (
                        f"{_days_text(days)} 要求班次 {shift_name} 至少 {required} 人，"
                        f"但又禁止全员排该班次"
                    ),
                    "severity": _severity(ban.rule, demand.rule),
                    "hint": f"取消全员禁止 {shift_name}，或降低该班次的人数需求",
                    "rule_ids": sorted(
                        item.rule.id for item in involved if item.rule.id is not None
                    ),
                    "new_rule_indexes": sorted(
                        item.new_index for item in involved if item.new_index is not None
                    ),
                    "labels": [
                        _describe(item.rule, employee_map, shift_map) for item in involved
                    ],
                }
            )
    return items


def check_rule_conflicts(
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    existing_rules: list[Rule],
    new_rules: list[Rule],
) -> dict[str, Any]:
    """检查待写入规则与既有规则、同批次规则之间的确定性冲突。

    返回 {"has_conflict": bool, "items": [{code, message, severity, hint,
    rule_ids, new_rule_indexes, labels}]}：
    - rule_ids：已落库规则编号，供前端直接定位要修改/删除的旧规则；
    - new_rule_indexes：本次提交中触发冲突的新规则下标；
    - severity=error 表示确定矛盾（接口层据此 409 阻止落库），warning 只提示。
    """
    employee_map = {employee.id: employee for employee in employees}
    shift_map = {shift.id: shift for shift in shifts if shift.id is not None}
    entries = [_Entry(rule) for rule in existing_rules if rule.valid and rule.id is not None]
    entries += [_Entry(rule, index) for index, rule in enumerate(new_rules) if rule.valid]

    collector = _Collector()
    _pair_conflicts(entries, plan, employee_map, shift_map, collector)
    _headcount_conflicts(entries, plan, len(employees), shift_map, collector, employee_map)
    items = collector.items()
    items.extend(
        _max_rest_conflicts(entries, plan, employees, shift_map, employee_map)
    )
    items.extend(
        _forced_work_conflicts(entries, plan, employees, shift_map, employee_map)
    )
    items.extend(
        _ban_shift_conflicts(entries, plan, employees, shift_map, employee_map)
    )
    return {
        "has_conflict": any(item["severity"] == "error" for item in items),
        "items": items,
    }


def describe_rule(rule: Rule, employees: list[Employee], shifts: list[Shift]) -> str:
    """把规则渲染成一句业务可读的短描述（供接口返回与前端展示）。"""
    employee_map = {employee.id: employee for employee in employees}
    shift_map = {shift.id: shift for shift in shifts if shift.id is not None}
    return _describe(rule, employee_map, shift_map)
