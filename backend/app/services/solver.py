# pyright: reportAny=false, reportExplicitAny=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportUnknownLambdaType=false, reportUnusedCallResult=false, reportImplicitStringConcatenation=false
# 说明：OR-Tools 的 CpModel 动态 API 与线性表达式无法完全静态标注，
# 约束项统一用 Any 承载；这里只关闭由此产生的类型噪音，真实错误仍然保留。
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
import time
from typing import Any, Protocol, TypeAlias, cast

from ortools.sat.python import cp_model

from ..models import Assignment, Employee, Plan, Rule, Shift
from .conflict_codes import RULE_INEFFECTIVE
from .rule_fields import monthly_hours_target
from .calendar_service import (
    UNIT_PER_HOUR,
    WEEKDAY_NAMES,
    coverage_slices,
    effective_hours,
    effective_units,
    format_hhmm,
    month_dates,
    parse_hhmm,
    parse_interval,
    period_buckets,
    rule_month_dates,
    shift_window_error,
    time_window_error,
    units_to_hours,
    week_days_in_month,
    weekly_quota,
    window_violation,
)
from .soft_weights import SoftWeights, scheme_name, unit_weight, weight_items
from .validator import evaluate

# 冲突/报告条目：字段随冲突码略有差异，统一用宽松的字典类型承载
Conflict: TypeAlias = dict[str, Any]
# 「规则 + 生效日期」分组，_build_rule_maps 的多数返回值都是这种形态
RuleDates: TypeAlias = list[tuple[Rule, list[str]]]
# 指定班次：员工 → 日期 → [(班次, 规则 id)]
FixedShiftMap: TypeAlias = dict[int, dict[str, list[tuple[int, int | None]]]]
# 指定上班时刻：[(员工, 日期), (时刻, 规则 id)]
FixedTimeMap: TypeAlias = dict[tuple[int, str], tuple[str, int | None]]
# 全员统一上班时刻：日期 → (时刻, 规则 id)
AllStartMap: TypeAlias = dict[str, tuple[str, int | None]]
# _build_rule_maps 的 23 元组（顺序与两处解包保持一致）
RuleMaps: TypeAlias = tuple[
    dict[int, set[str]],  # rest_dates
    dict[int, set[str]],  # soft_rest
    dict[int, set[str]],  # must_work
    dict[int, set[str]],  # leave_dates
    dict[int, set[str]],  # off_dates
    FixedShiftMap,
    FixedTimeMap,
    AllStartMap,
    RuleDates,  # day_rules
    RuleDates,  # segment_rules
    RuleDates,  # start_rules
    RuleDates,  # end_rules
    RuleDates,  # shift_rules
    RuleDates,  # max_rules
    RuleDates,  # hours_rules
    RuleDates,  # rest_limit_rules
    RuleDates,  # min_rest_rules
    RuleDates,  # max_work_rules
    RuleDates,  # ban_shift_rules
    RuleDates,  # max_work_streak_rules
    RuleDates,  # shift_limit_rules
    RuleDates,  # role_shift_rules
    RuleDates,  # window_rules
]


@dataclass
class SolveOutcome:
    status: str
    message: str
    assignments: list[Assignment]
    report: dict[str, Any]


@dataclass
class SolutionSnapshot:
    """求解过程中每一次改进解的轻量快照，供两阶段优化收集。"""

    objective: float
    elapsed_seconds: float
    assignments: list[Assignment]
    # CP-SAT 当前证明的 best bound；阶段 A（只求可行）没有目标，恒为 0
    best_bound: float | None = None
    # 该快照目标值的分项归因（与 objective 相加严格相等）；阶段 A 为 None
    attribution: dict[str, Any] | None = None


@dataclass
class _ObjectiveEntry:
    """目标函数中的一项，同时保留归因所需的信息。

    分项贡献 = 系数 × 变量取值；归因时反推违约量（贡献 / 单位权重），
    保证「数量 × 单位权重 = 贡献」且所有分项相加等于 objective。
    """

    category: str
    coefficient: int
    quantity: float = 0.0  # 预设违约量（按班次/小时等）；松弛变量项为 0
    activation: Any | None = None  # 触发项取值 0/1
    quantity_var: Any | None = None  # 本身就是违约量（松弛变量）
    employee_id: int | None = None
    date: str | None = None
    shift_id: int | None = None
    note: str = ""


class _SnapshotCallback(cp_model.CpSolverSolutionCallback):
    """把 CP-SAT 的改进解节流成 SolutionSnapshot，并支持外部停止搜索。

    每次改进都遍历全部变量取值成本很高（大计划一次约 0.3 秒），会拖长
    实际求解时长；因此默认每 2 秒最多产出一次快照，最终解仍由求解结束后的
    solver.Value 精确构建。sink 返回 False 时立即 StopSearch。
    """

    def __init__(
        self,
        build: Callable[[Any], list[Any]],
        sink: Callable[[SolutionSnapshot], bool | None],
        min_interval: float = 2.0,
        attribute: Callable[[Callable[[Any], int]], dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self._build = build
        self._sink = sink
        self._min_interval = min_interval
        self._attribute = attribute
        self._last_emit = -1.0e9

    def on_solution_callback(self) -> None:  # pragma: no cover - CP-SAT 回调
        now = float(self.WallTime())
        if now - self._last_emit < self._min_interval:
            return
        self._last_emit = now
        try:
            best_bound: float | None = float(self.BestObjectiveBound())
        except Exception:
            best_bound = None
        snapshot = SolutionSnapshot(
            objective=float(self.ObjectiveValue()),
            elapsed_seconds=now,
            assignments=self._build(self.Value),
            best_bound=best_bound,
            attribution=self._attribute(self.Value) if self._attribute is not None else None,
        )
        if self._sink(snapshot) is False:
            self.StopSearch()


class _CpModel(Protocol):
    """`cp_model.CpModel` 的类型化外观。

    OR-Tools 的 Add / NewBoolVar / NewIntVar / AddAssumption / AddHint / Minimize
    是导入期动态挂到类上的，静态分析看不到；这里只声明本项目实际使用的子集，
    调用处用 cast 收窄，运行期仍是原生 CpModel。
    """

    def Add(self, ct: Any) -> cp_model.Constraint: ...

    def NewBoolVar(self, name: str) -> cp_model.IntVar: ...

    def NewIntVar(self, lb: int, ub: int, name: str) -> cp_model.IntVar: ...

    def AddAssumption(self, literal: cp_model.IntVar) -> None: ...

    def AddHint(self, var: cp_model.IntVar, value: int) -> None: ...

    def Minimize(self, obj: Any) -> None: ...


def _eid(employee: Employee) -> int:
    """员工主键：进入求解前所有员工都已落库，id 不应为空。"""
    assert employee.id is not None
    return employee.id


def _pid(plan: Plan) -> int:
    """计划主键：同上，求解前计划必然已落库。"""
    assert plan.id is not None
    return plan.id


def _window_ok(plan: Plan, shift: Shift) -> bool:
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


def _eligible(
    plan: Plan,
    employee: Employee,
    shift: Shift,
) -> bool:
    if not shift.enabled or shift.is_leave:
        return False
    if shift.is_special:
        if not employee.allow_special:
            return False
    elif not _window_ok(plan, shift):
        return False
    return (
        effective_hours(shift.work_hours, shift.start_time, shift.end_time)
        <= employee.max_daily_hours + 1e-6
    )


def _covers(start_time: str, end_time: str, left: int, right: int) -> bool:
    begin, finish = parse_interval(start_time, end_time)
    return begin <= left and finish >= right


def _employee_covers(
    employee: Employee,
    day: str,
    left: int,
    right: int,
    allowed: dict[tuple[int, str], list[Shift]],
    rest_dates: dict[int, set[str]],
    leave_days: dict[int, set[str]],
    locked_extra: dict[tuple[int, str], Assignment],
) -> bool:
    if day in rest_dates.get(_eid(employee), set()) or day in leave_days.get(_eid(employee), set()):
        return False
    if any(
        _covers(shift.start_time, shift.end_time, left, right)
        for shift in allowed.get((_eid(employee), day), [])
    ):
        return True
    extra = locked_extra.get((_eid(employee), day))
    return extra is not None and _covers(extra.start_time, extra.end_time, left, right)


def _coverage_diagnostic(
    plan: Plan,
    employees: list[Employee],
    dates: list[str],
    allowed: dict[tuple[int, str], list[Shift]],
    rest_dates: dict[int, set[str]],
    leave_days: dict[int, set[str]],
    locked_extra: dict[tuple[int, str], Assignment],
    segment_rules: RuleDates,
) -> Conflict | None:
    """求解不可行时按天定位覆盖缺口，不参与模型求解。"""
    # 没有任何硬性时段规则时不产出 E017，软规则缺口只走覆盖报表
    if not any(rule.hard for rule, _ in segment_rules):
        return None
    interval_pairs: set[tuple[str, str]] = {
        (shift.start_time, shift.end_time)
        for candidates in allowed.values()
        for shift in candidates
    }
    interval_pairs.update(
        (rule.start_time, rule.end_time)
        for rule, _ in segment_rules
        if rule.hard and rule.start_time and rule.end_time
    )
    interval_pairs.update(
        (item.start_time, item.end_time) for item in locked_extra.values()
    )
    intervals = list(interval_pairs)
    slices = coverage_slices(intervals, plan.coverage_start, plan.coverage_end)
    if not slices:
        return None

    best: tuple[tuple[int, int], str, list[Conflict], int] | None = None
    for day in dates:
        # 只诊断硬性时段规则，软规则造成的缺口交给覆盖报表
        active = [
            rule
            for rule, rule_dates in segment_rules
            if day in rule_dates and rule.hard and rule.start_time and rule.end_time
        ]
        day_gaps: list[Conflict] = []
        # 当天没有生效的硬性时段规则时不参与诊断
        if not active:
            continue
        for left, right in slices:
            demand = 0
            gap_rule_ids: list[int | None] = []
            for rule in active:
                if not rule.start_time or not rule.end_time:
                    continue
                begin, finish = parse_interval(rule.start_time, rule.end_time)
                if begin <= left and finish >= right:
                    gap_rule_ids.append(rule.id)
                    demand = max(demand, rule.count or 0)
            if demand <= 0:
                continue
            coverers = [
                employee
                for employee in employees
                if _employee_covers(
                    employee, day, left, right, allowed, rest_dates, leave_days, locked_extra
                )
            ]
            shortfall = demand - len(coverers)
            if shortfall <= 0:
                continue
            if (
                day_gaps
                and day_gaps[-1]["right"] == left
                and day_gaps[-1]["demand"] == demand
            ):
                day_gaps[-1]["right"] = right
                day_gaps[-1]["shortfall"] = max(day_gaps[-1]["shortfall"], shortfall)
                day_gaps[-1]["rule_ids"].extend(
                    rule_id
                    for rule_id in gap_rule_ids
                    if rule_id not in day_gaps[-1]["rule_ids"]
                )
            else:
                day_gaps.append(
                    {
                        "left": left,
                        "right": right,
                        "demand": demand,
                        "shortfall": shortfall,
                        "rule_ids": list(gap_rule_ids),
                    }
                )
        if not day_gaps:
            continue

        groups: dict[int, list[Conflict]] = defaultdict(list)
        for gap in day_gaps:
            groups[gap["demand"]].append(gap)
        demand, group = max(
            groups.items(),
            key=lambda item: (
                max(gap["shortfall"] for gap in item[1]),
                sum(gap["right"] - gap["left"] for gap in item[1]),
            ),
        )
        score = (
            max(gap["shortfall"] for gap in day_gaps),
            sum(gap["right"] - gap["left"] for gap in day_gaps),
        )
        if best is None or score > best[0]:
            best = (score, day, group, demand)
    if best is None:
        return None

    _, day, group, demand = best

    def covers_all(employee: Employee) -> bool:
        return all(
            _employee_covers(
                employee,
                day,
                gap["left"],
                gap["right"],
                allowed,
                rest_dates,
                leave_days,
                locked_extra,
            )
            for gap in group
        )

    coverers = [employee for employee in employees if covers_all(employee)]
    reasons: list[str] = []
    for employee in employees:
        if day in rest_dates.get(_eid(employee), set()):
            reasons.append(f"{employee.name}休息")
        elif day in leave_days.get(_eid(employee), set()):
            reasons.append(f"{employee.name}休假")
        elif covers_all(employee):
            continue
        elif not allowed.get((_eid(employee), day)) and (_eid(employee), day) not in locked_extra:
            reasons.append(f"{employee.name}无可用班次")
        else:
            reasons.append(f"{employee.name}无覆盖该时段的班次")
    reason_text = "、".join(reasons) if reasons else "无人持有可用班次"
    intervals_text = "、".join(
        f"{format_hhmm(gap['left'])}-{format_hhmm(gap['right'])}" for gap in group
    )
    rule_ids: list[int] = []
    for gap in group:
        for rule_id in gap["rule_ids"]:
            if rule_id not in rule_ids:
                rule_ids.append(rule_id)
    return _conflict(
        "E017",
        f"{day} {intervals_text} 需求 {demand} 人，"
        f"可用 {len(coverers)} 人，原因：{reason_text}",
        rule_ids,
    )


def _conflict(
    code: str, message: str, rule_ids: Sequence[int | None] | None = None
) -> Conflict:
    # 规则 id 允许为 None（尚未落库的临时规则），输出时统一过滤
    return {
        "code": code,
        "message": message,
        "rule_ids": [item for item in rule_ids or [] if item is not None],
    }


def _rule_is_soft(rule_id: int | None, rule_by_id: dict[int, Rule]) -> bool:
    """判断规则是否为软约束；未落库或查不到时按硬规则处理，保持既有行为。"""
    if rule_id is None:
        return False
    rule = rule_by_id.get(rule_id)
    return rule is not None and not rule.hard


_RULE_TYPE_LABELS = {
    "REST": "休息",
    "MUST_WORK": "必须上班",
    "PAID_LEAVE": "带薪休假",
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


def _weekday_text(weekdays: str | None) -> str:
    """把 "1,3" 这类星期编码转成「周一、周三」，便于冲突面板直接展示。"""
    if not weekdays:
        return ""
    names: list[str] = []
    for token in weekdays.split(","):
        token = token.strip()
        if not token:
            continue
        if token.isdigit() and 1 <= int(token) <= 7:
            names.append(WEEKDAY_NAMES[int(token) - 1])
        else:
            names.append(token)
    return "、".join(names)


def _rule_human_text(
    rule: Rule, employee_name: str, shift_names: dict[int, str]
) -> str:
    """把规则还原成一句可读中文，供 E012 冲突面板展示原文语义。"""
    label = _RULE_TYPE_LABELS.get(rule.type, rule.type)
    date_text = ""
    if rule.date_start:
        date_text = rule.date_start
        if rule.date_end and rule.date_end != rule.date_start:
            date_text = f"{rule.date_start} 至 {rule.date_end}"
    weekday_text = _weekday_text(rule.weekdays)
    time_text = ""
    if rule.start_time and rule.end_time:
        time_text = f"{rule.start_time}-{rule.end_time}"
    elif rule.start_time:
        time_text = rule.start_time
    shift_text = shift_names.get(rule.shift_id, f"班次 #{rule.shift_id}") if rule.shift_id else ""
    head = " ".join(part for part in (employee_name, date_text, weekday_text) if part)
    if rule.role:
        head = f"{head} 岗位「{rule.role}」".strip()
    count = rule.count if rule.count is not None else 0
    if rule.type in ("REST", "PAID_LEAVE", "MUST_WORK"):
        return f"{head} {label}"
    if rule.type in ("FIXED_SHIFT", "BAN_SHIFT"):
        return f"{head} {label}「{shift_text}」"
    if rule.type == "FIXED_TIME":
        return f"{head} {rule.start_time or ''} 到店"
    if rule.type == "ALL_START_AT":
        return f"{head} {rule.start_time or ''} 到岗"
    if rule.type in ("DAY_STAFF", "MAX_STAFF"):
        op_text = "至多" if (rule.count_op or "ge") == "le" else "至少"
        return f"{head} {op_text} {count} 人"
    if rule.type == "SEGMENT_STAFF":
        op_text = "至多" if (rule.count_op or "ge") == "le" else "至少"
        return f"{head} {time_text} {op_text} {count} 人"
    if rule.type == "START_STAFF":
        op_text = "至多" if (rule.count_op or "ge") == "le" else "至少"
        return f"{head} {rule.start_time or ''} 在岗 {op_text} {count} 人"
    if rule.type == "END_STAFF":
        op_text = "至多" if (rule.count_op or "ge") == "le" else "至少"
        return f"{head} {rule.end_time or ''} 下班 {op_text} {count} 人"
    if rule.type == "SHIFT_LIMIT":
        parts: list[str] = []
        if rule.shift_id:
            parts.append(f"班次「{shift_text}」")
        if rule.start_time:
            parts.append(f"{rule.start_time} 上班")
        if rule.end_time:
            parts.append(f"{rule.end_time} 下班")
        return f"{head} 上班日仅限 {'、'.join(parts)}"
    if rule.type == "SINGLE_ROLE_SHIFT":
        return (
            f"{date_text} 岗位「{rule.role or ''}」上班人数 ≤{count} 人时"
            f"必须上班次「{shift_text}」"
        ).strip()
    if rule.type == "WORK_WINDOW":
        scope = f"岗位「{rule.role}」" if rule.role else "员工"
        return f"{date_text} {scope}上班时段必须在 {time_text} 内".strip()
    if rule.type == "SHIFT_STAFF":
        return f"{head} 班次「{shift_text}」至少 {count} 人"
    if rule.type == "SHIFT_HOURS":
        op_text = {"ge": "不少于", "le": "不超过"}.get(rule.hours_op or "eq", "等于")
        return f"{head} 班次工时{op_text} {rule.work_hours or 0:g} 小时"
    if rule.type == "MAX_REST_DAYS":
        return f"{employee_name} 最多连休 {count} 天"
    if rule.type in ("MIN_REST_DAYS", "MAX_WORK_DAYS"):
        scope = "每周" if (rule.period or "month") == "week" else "每月"
        verb = "至少休" if rule.type == "MIN_REST_DAYS" else "最多上"
        return f"{employee_name} {scope}{verb} {count} 天"
    if rule.type == "MAX_CONSECUTIVE_WORK":
        return f"{employee_name} 连续上班不超过 {count} 天"
    if rule.type == "MONTHLY_HOURS":
        return f"{employee_name} 当月工时 {count} 小时"
    return f"{head} {label}"


def _rule_detail(
    rule: Rule, employee_map: dict[int, Employee], shift_names: dict[int, str]
) -> dict[str, Any]:
    """E012 明细：让每条规则都能对上「谁、哪天、什么要求、硬还是软」。"""
    employee = employee_map.get(rule.employee_id) if rule.employee_id is not None else None
    employee_name = employee.name if employee is not None else "全员"
    return {
        "id": rule.id,
        "type": rule.type,
        "type_label": _RULE_TYPE_LABELS.get(rule.type, rule.type),
        "raw_text": rule.raw_text,
        "employee_id": rule.employee_id,
        "employee_name": employee_name,
        "date_start": rule.date_start,
        "date_end": rule.date_end,
        "weekdays": rule.weekdays,
        "weekdays_label": _weekday_text(rule.weekdays),
        "count": rule.count,
        "hard": rule.hard,
        "summary": _rule_human_text(rule, employee_name, shift_names),
    }


def _plan_params(plan: Plan) -> dict[str, Any]:
    """求解说明里展示的计划级口径，避免「看不见的默认」再次出现。"""
    return {
        "coverage_start": plan.coverage_start,
        "coverage_end": plan.coverage_end,
        "latest_end_time": plan.latest_end_time,
        "latest_end_time_effective": plan.latest_end_time_effective,
        "overnight_next_rest": plan.overnight_next_rest,
        "max_consecutive_days": plan.max_consecutive_days,
    }


def _empty_report(
    status: str, message: str, conflicts: list[Conflict], plan: Plan
) -> dict[str, Any]:
    return {
        "status": status,
        "message": message,
        "objective": 0,
        "params": _plan_params(plan),
        "coverage": [],
        "hours": [],
        "conflicts": conflicts,
        "rest_summary": [],
        "limits": [],
        "rule_status": [],
    }


def _expand(month: str, rule: Rule) -> list[str]:
    if not rule.valid:
        return []
    # 星期过滤统一在这里做一次：带 weekdays 时以月首日为遍历起点
    return rule_month_dates(rule.date_start, rule.date_end, rule.weekdays, month)


def expand_rule_dates(plan: Plan, rule: Rule) -> list[str]:
    """规则在计划月份内的生效日期（含星期过滤），与求解建模口径一致。"""
    return _expand(plan.month, rule)


def _rest_limit_windows(
    rule_dates: list[str], leave_dates: set[str], limit: int
) -> list[list[str]]:
    """MAX_REST_DAYS 的滑动窗口。

    先按规则日期的日历连续性切段（星期过滤造成的间隙会切段），
    再从段内扣除带薪假：带薪假是中性日，不打断连续性，也不占用连休额度。
    rule_dates 为空或有效天数不足 limit+1 时返回空列表（无约束）。
    """
    if limit <= 0:
        return []
    segments: list[list[str]] = []
    current: list[str] = []
    previous: date | None = None
    for day in rule_dates:
        parsed = date.fromisoformat(day)
        if previous is not None and (parsed - previous).days > 1:
            segments.append(current)
            current = []
        if day not in leave_dates:
            current.append(day)
        previous = parsed
    segments.append(current)
    return [
        segment[index : index + limit + 1]
        for segment in segments
        for index in range(len(segment) - limit)
    ]


def _shift_hours(shift: Shift) -> float:
    return effective_hours(shift.work_hours, shift.start_time, shift.end_time)


def _hours_match(hours: float, target: float, op: str) -> bool:
    if op == "ge":
        return hours >= target - 1e-6
    if op == "le":
        return hours <= target + 1e-6
    return abs(hours - target) <= 0.01


def _hours_text(target: float, op: str) -> str:
    if op == "ge":
        return f"工时不少于 {target:g} 小时"
    if op == "le":
        return f"工时不超过 {target:g} 小时"
    return f"工时等于 {target:g} 小时"


def _time_matches(value: str | None, target: str | None, op: str | None) -> bool:
    """时刻比较：eq=等于，le=不晚于，ge=不早于（缺省 eq）。"""
    if value is None or target is None:
        return False
    mode = op or "eq"
    current, limit = parse_hhmm(value), parse_hhmm(target)
    if mode == "le":
        return current <= limit
    if mode == "ge":
        return current >= limit
    return current == limit


def _start_matches(start_time: str | None, target: str | None, op: str | None) -> bool:
    """START_STAFF 的上班时刻比较：eq=等于，le=不晚于，ge=不早于（缺省 eq）。"""
    return _time_matches(start_time, target, op)


def _role_match(employee: Employee | None, role: str | None) -> bool:
    """岗位限定：规则未指定岗位时全员通过；否则要求员工岗位完全一致。"""
    if not role:
        return True
    return employee is not None and (employee.role or "").strip() == role


def _shift_limit_ok(shift: Shift, rule: Rule) -> bool:
    """SHIFT_LIMIT：班次必须同时满足规则给出的班次、上班时刻、下班时刻条件。"""
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


def _shift_within_window(shift: Shift, rule: Rule) -> bool:
    """WORK_WINDOW：班次起止必须完全落在规则的 [start_time, end_time] 内。

    跨午夜班次（结束时刻早于开始时刻）必然越过窗口，直接判为不满足。
    """
    if not (rule.start_time and rule.end_time):
        return True
    begin, finish = parse_interval(shift.start_time, shift.end_time)
    return begin >= parse_hhmm(rule.start_time) and finish <= parse_hhmm(rule.end_time)


def _achievable_mask(extra: int, day_units: dict[str, set[int]]) -> int:
    """按天枚举可选工时，用位图记录所有可达的月度工时总量。"""
    mask = 1 << extra
    for units in day_units.values():
        options = [value for value in units if value > 0]
        if not options:
            continue
        combined = mask
        for value in options:
            combined |= mask << value
        mask = combined
    return mask


def _nearest_total(mask: int, target: int) -> int:
    """返回最接近目标的可达工时总量，等距时取下限。"""
    if (mask >> target) & 1:
        return target
    limit = mask.bit_length()
    for distance in range(1, limit + 1):
        lower = target - distance
        if lower >= 0 and (mask >> lower) & 1:
            return lower
        upper = target + distance
        if upper < limit and (mask >> upper) & 1:
            return upper
    return 0


def _build_rule_maps(
    plan: Plan,
    rules: list[Rule],
    shift_map: dict[int, Shift],
    employees: list[Employee],
) -> RuleMaps:
    rest_dates: dict[int, set[str]] = defaultdict(set)
    soft_rest: dict[int, set[str]] = defaultdict(set)
    must_work: dict[int, set[str]] = defaultdict(set)
    leave_dates: dict[int, set[str]] = defaultdict(set)
    # 休息（OFF）日期：FIXED_SHIFT 指向 OFF 班次时记入，结果层自动补 OFF 展示
    off_dates: dict[int, set[str]] = defaultdict(set)
    fixed_shifts: FixedShiftMap = defaultdict(lambda: defaultdict(list))
    fixed_times: FixedTimeMap = {}
    all_start: AllStartMap = {}
    day_rules: RuleDates = []
    segment_rules: RuleDates = []
    start_rules: RuleDates = []
    end_rules: RuleDates = []
    shift_rules: RuleDates = []
    # 人数上限规则（MAX_STAFF）：当天普通上班人数不得超过 count
    max_rules: list[tuple[Rule, list[str]]] = []
    # 班次工时条件（SHIFT_HOURS）与连续休息上限（MAX_REST_DAYS）
    hours_rules: list[tuple[Rule, list[str]]] = []
    rest_limit_rules: list[tuple[Rule, list[str]]] = []
    # 周期天数类：每周/每月至少休 N 天（MIN_REST_DAYS）、最多上 N 天（MAX_WORK_DAYS）
    min_rest_rules: list[tuple[Rule, list[str]]] = []
    max_work_rules: list[tuple[Rule, list[str]]] = []
    # 禁止排某班次（BAN_SHIFT）
    ban_shift_rules: list[tuple[Rule, list[str]]] = []
    # 员工/日期细分的连续上班上限（MAX_CONSECUTIVE_WORK），与计划级设置叠加
    max_work_streak_rules: list[tuple[Rule, list[str]]] = []
    # 班次限制（SHIFT_LIMIT）：只限制上班日可排的班次，不强制上班
    shift_limit_rules: list[tuple[Rule, list[str]]] = []
    # 岗位单人班次（SINGLE_ROLE_SHIFT）：该岗位当天上班人数 ≤ count 时，这些人都必须上指定班次
    role_shift_rules: list[tuple[Rule, list[str]]] = []
    # 工作时段窗（WORK_WINDOW）：员工的上班班次必须整体落在窗口内
    window_rules: list[tuple[Rule, list[str]]] = []
    leave_ids = {shift.id for shift in shift_map.values() if shift.is_leave}
    off_ids = {shift.id for shift in shift_map.values() if shift.is_off}

    for rule in rules:
        dates = _expand(plan.month, rule)
        if not dates:
            continue
        if rule.type == "REST":
            # employee_id=None 表示全员休息
            targets = (
                [rule.employee_id]
                if rule.employee_id is not None
                else [_eid(employee) for employee in employees]
            )
            for employee_id in targets:
                if rule.hard:
                    rest_dates[employee_id].update(dates)
                else:
                    soft_rest[employee_id].update(dates)
        elif rule.type == "MUST_WORK":
            targets = (
                [rule.employee_id]
                if rule.employee_id is not None
                else [_eid(employee) for employee in employees]
            )
            for employee_id in targets:
                must_work[employee_id].update(dates)
        elif rule.type == "PAID_LEAVE" and rule.employee_id is not None:
            # 只给天数、不给日期的有薪假：由求解器在区间内选 N 天（配额变量），
            # 不能把区间内每一天都当成固定休假，否则整月都无法上班。
            if rule.count is not None and 0 < rule.count < len(dates):
                pass
            else:
                leave_dates[rule.employee_id].update(dates)
        elif rule.type == "FIXED_SHIFT" and rule.employee_id is not None and rule.shift_id is not None:
            if rule.shift_id in leave_ids:
                leave_dates[rule.employee_id].update(dates)
            elif rule.shift_id in off_ids:
                # FIXED_SHIFT 指向 OFF 班次：指定日期给某人排休息（OFF）
                off_dates[rule.employee_id].update(dates)
            else:
                for day in dates:
                    fixed_shifts[rule.employee_id][day].append((rule.shift_id, rule.id))
        elif rule.type == "FIXED_TIME" and rule.employee_id is not None and rule.start_time:
            for day in dates:
                fixed_times.setdefault((rule.employee_id, day), (rule.start_time, rule.id))
        elif rule.type == "ALL_START_AT" and rule.start_time:
            for day in dates:
                all_start.setdefault(day, (rule.start_time, rule.id))
        elif rule.type == "DAY_STAFF":
            day_rules.append((rule, dates))
        elif rule.type == "SEGMENT_STAFF" and rule.start_time and rule.end_time:
            segment_rules.append((rule, dates))
        elif rule.type == "START_STAFF" and rule.start_time:
            start_rules.append((rule, dates))
        elif rule.type == "END_STAFF" and rule.end_time:
            end_rules.append((rule, dates))
        elif rule.type == "SHIFT_STAFF" and rule.shift_id is not None:
            shift_rules.append((rule, dates))
        elif rule.type == "MAX_STAFF":
            max_rules.append((rule, dates))
        elif rule.type == "SHIFT_HOURS":
            hours_rules.append((rule, dates))
        elif rule.type == "MAX_REST_DAYS":
            rest_limit_rules.append((rule, dates))
        elif rule.type == "MIN_REST_DAYS":
            min_rest_rules.append((rule, dates))
        elif rule.type == "MAX_WORK_DAYS":
            max_work_rules.append((rule, dates))
        elif rule.type == "BAN_SHIFT" and rule.shift_id is not None:
            ban_shift_rules.append((rule, dates))
        elif rule.type == "MAX_CONSECUTIVE_WORK":
            max_work_streak_rules.append((rule, dates))
        elif rule.type == "SHIFT_LIMIT" and (
            rule.shift_id is not None or rule.start_time or rule.end_time
        ):
            shift_limit_rules.append((rule, dates))
        elif rule.type == "SINGLE_ROLE_SHIFT" and rule.role and rule.shift_id is not None:
            role_shift_rules.append((rule, dates))
        elif rule.type == "WORK_WINDOW" and rule.start_time and rule.end_time:
            window_rules.append((rule, dates))

    return (
        rest_dates,
        soft_rest,
        must_work,
        leave_dates,
        off_dates,
        fixed_shifts,
        fixed_times,
        all_start,
        day_rules,
        segment_rules,
        start_rules,
        end_rules,
        shift_rules,
        max_rules,
        hours_rules,
        rest_limit_rules,
        min_rest_rules,
        max_work_rules,
        ban_shift_rules,
        max_work_streak_rules,
        shift_limit_rules,
        role_shift_rules,
        window_rules,
    )


def _feasible_message(seconds: int, slack_units: int = 0) -> str:
    """达到求解时长上限时的说明文案，附带可行解质量指标（软约束放宽总量）。"""
    base = (
        f"已在 {seconds} 秒内找到可行排班，但未证明最优；"
        f"可提高单次求解时长后重试"
    )
    if slack_units > 0:
        base += f"（软约束放宽总量 {slack_units} 个违例当量）"
    return base


def _segment_coverage_conflicts(
    employees: list[Employee],
    shifts: list[Shift],
    rules: list[Rule],
    assignments: list[Assignment],
    coverage: list[dict[str, Any]],
    skip_rule_ids: set[int],
) -> list[Conflict]:
    """把时段人数缺口合并成 P003 提示，避免按「日期 × 员工」逐条刷屏。

    - 合并维度：同一规则的同一时段，逐日一条，文案列出需求人数、实际人数与缺勤人员；
    - 汇总模式：同一时段被超过 3 天违反时只留一条汇总，明细仍保留在报告的覆盖缺口列表；
    - 判定逻辑不变：缺口人数直接取自 validator 的覆盖统计结果。
    """
    rule_map = {rule.id: rule for rule in rules}
    shift_map = {shift.id: shift for shift in shifts}
    groups: dict[tuple[int | None, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in coverage:
        rule = rule_map.get(row.get("rule_id"))
        if rule is None or rule.type != "SEGMENT_STAFF":
            continue
        # 预检查已按整窗口报过 P002/E018 或结构不可覆盖 P003/E004 的规则不再重复提示
        if rule.id in skip_rule_ids:
            continue
        groups[(rule.id, rule.start_time or "", rule.end_time or "")].append(row)
    if not groups:
        return []

    # 当天有正常出勤（非休息、非休假、非特殊班次）的人员，用于区分「缺勤」与「未覆盖」
    worked_by_day: dict[str, set[int]] = defaultdict(set)
    for item in assignments:
        shift = shift_map.get(item.shift_id) if item.shift_id is not None else None
        if shift is not None and (shift.is_off or shift.is_leave or shift.is_special):
            continue
        worked_by_day[item.date].add(item.employee_id)

    conflicts: list[Conflict] = []
    for (rule_id, start_time, end_time), rows in sorted(groups.items(), key=lambda item: item[0]):
        rows.sort(key=lambda row: row["date"])
        required = max(row.get("required", 0) for row in rows)
        window = f"{start_time}-{end_time}"
        if len(rows) > 3:
            conflicts.append(
                _conflict(
                    "P003",
                    f"{rows[0]['date']} 至 {rows[-1]['date']} 共 {len(rows)} 天 "
                    f"{window} 时段人数不足（每天需 {required} 人），已按软约束放宽",
                    [rule_id],
                )
            )
            continue
        for row in rows:
            absent = [
                employee.name
                for employee in employees
                if employee.id not in worked_by_day.get(row["date"], set())
            ]
            gap_text = "、".join(
                f"{gap['start']}-{gap['end']}" for gap in row.get("gaps") or []
            )
            if absent and len(absent) == len(employees):
                detail = f"（{'、'.join(absent)} 缺勤）"
            elif gap_text:
                detail = f"（缺口 {gap_text} 无人覆盖）"
            elif absent:
                detail = f"（{'、'.join(absent)} 缺勤）"
            else:
                detail = ""
            conflicts.append(
                _conflict(
                    "P003",
                    f"{row['date']} {window} 需 {required} 人，"
                    f"实际 {row.get('assigned', 0)} 人{detail}，已按软约束放宽",
                    [rule_id],
                )
            )
    return conflicts


def _min_window_shifts(
    intervals: list[tuple[int, int]],
    window_begin: int,
    window_finish: int,
    required: int,
) -> int | None:
    """铺满窗口所需的最少「班次段」数（每时段至少 required 段同时覆盖）。

    只作为可行性的下界判定：每段班次至少占用一名员工，同一人同一天只能上一段，
    因此所需段数超过可用人数时必然无解。返回 None 表示任一时段的可覆盖段数
    不足 required。
    """
    if required <= 0:
        return 0
    points = {window_begin, window_finish}
    for begin, finish in intervals:
        if finish <= window_begin or begin >= window_finish:
            continue
        points.add(max(begin, window_begin))
        points.add(min(finish, window_finish))
    ordered = sorted(points)
    chosen: list[tuple[int, int]] = []
    for left, right in zip(ordered, ordered[1:]):
        if right <= left:
            continue
        covering = sum(1 for begin, finish in chosen if begin <= left and finish >= right)
        if covering >= required:
            continue
        need = required - covering
        available = sorted(
            (
                (begin, finish)
                for begin, finish in intervals
                if begin <= left and finish >= right
            ),
            key=lambda item: (-item[1], item[0]),
        )
        if len(available) < need:
            return None
        chosen.extend(available[:need])
    return len(chosen)


def _apply_fixed_precedence(
    rules: list[Rule],
    fixed_shifts: FixedShiftMap,
) -> list[Conflict]:
    """同日多个固定班：带日期的显式规则优先，按星期展开的通例让位并留 P013。"""
    rule_by_id = {rule.id: rule for rule in rules if rule.id is not None}
    warnings: list[Conflict] = []
    for days in fixed_shifts.values():
        for day, entries in list(days.items()):
            if len({shift_id for shift_id, _ in entries}) <= 1:
                continue
            explicit: list[tuple[int, int | None]] = []
            general: list[tuple[int, int | None]] = []
            for shift_id, rule_id in entries:
                rule = rule_by_id.get(rule_id) if rule_id is not None else None
                if rule is None:
                    explicit.append((shift_id, rule_id))
                elif rule.weekdays is None and rule.date_start == rule.date_end:
                    explicit.append((shift_id, rule_id))
                else:
                    general.append((shift_id, rule_id))
            if not explicit:
                continue
            keep = explicit
            for shift_id, rule_id in general:
                kept = keep[0]
                kept_rule = rule_by_id.get(kept[1]) if kept[1] is not None else None
                dropped_rule = rule_by_id.get(rule_id) if rule_id is not None else None
                warnings.append(
                    _conflict(
                        "P013",
                        f"同日固定班让位：{dropped_rule.raw_text if dropped_rule else '通例固定班'}"
                        f" 被 {kept_rule.raw_text if kept_rule else '显式固定班'} 覆盖（{day}）",
                        [rule_id, kept[1]],
                    )
                )
            days[day] = keep
    return warnings


def _apply_rest_precedence(
    plan: Plan,
    rules: list[Rule],
    fixed_shifts: FixedShiftMap,
    employees: list[Employee],
) -> list[Conflict]:
    """单日硬休息优先于按星期/区间展开的通例固定班。

    跨午夜班次日休息（自动生成）等「单日显式休息」应与「每周五上某班」这类
    通例共存：单日休息更具体，覆盖当日的通例班次，只留提示不阻塞求解。
    """
    rule_by_id = {rule.id: rule for rule in rules if rule.id is not None}
    warnings: list[Conflict] = []
    for rule in rules:
        if (
            rule.type != "REST"
            or not rule.hard
            or rule.weekdays is not None
            or rule.date_start != rule.date_end
        ):
            continue
        # employee_id=None 表示全员休息：对每个员工的固定班次都让位
        target_ids = (
            [rule.employee_id]
            if rule.employee_id is not None
            else [_eid(employee) for employee in employees]
        )
        for employee_id in target_ids:
            for day in _expand(plan.month, rule):
                entries = fixed_shifts.get(employee_id, {}).get(day)
                if not entries:
                    continue
                kept: list[tuple[int, int | None]] = []
                for shift_id, rule_id in entries:
                    fixed_rule = rule_by_id.get(rule_id) if rule_id is not None else None
                    is_general = (
                        fixed_rule is None
                        or fixed_rule.weekdays is not None
                        or fixed_rule.date_start != fixed_rule.date_end
                    )
                    if is_general:
                        warnings.append(
                            _conflict(
                                "P013",
                                f"单日休息让位：{fixed_rule.raw_text if fixed_rule else '通例固定班'}"
                                f" 被 {rule.raw_text or '单日休息'} 覆盖（{day}）",
                                [rule_id, rule.id],
                            )
                        )
                    else:
                        kept.append((shift_id, rule_id))
                if kept:
                    fixed_shifts[employee_id][day] = kept
                else:
                    fixed_shifts[employee_id].pop(day, None)
    return warnings


def _precheck(
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    rules: list[Rule],
    locked: list[Assignment],
    maps: RuleMaps,
) -> tuple[list[Conflict], list[Conflict]]:
    rest_dates, _soft_rest, must_work, leave_dates, off_dates, fixed_shifts, fixed_times, all_start, day_rules, segment_rules, start_rules, _end_rules, shift_rules, _max_rules, hours_rules, rest_limit_rules, min_rest_rules, max_work_rules, _ban_shift_rules, max_work_streak_rules, shift_limit_rules, role_shift_rules, window_rules = maps
    shift_map: dict[int | None, Shift] = {
        shift.id: shift for shift in shifts
    }
    employee_map = {employee.id: employee for employee in employees}
    rule_by_id = {rule.id: rule for rule in rules if rule.id is not None}
    work_shifts = [shift for shift in shifts if shift.enabled and not shift.is_leave]
    normal = [shift for shift in work_shifts if not shift.is_special]
    leave_shift = next((shift for shift in shifts if shift.enabled and shift.is_leave), None)
    conflicts: list[Conflict] = []
    # 软约束被放宽后的提示：不阻塞求解，合并进最终报告
    warnings: list[Conflict] = []
    # 同日固定班冲突先按「显式日期优先」裁决，再进入后续硬冲突检查
    warnings.extend(_apply_fixed_precedence(rules, fixed_shifts))
    # 单日硬休息（含跨午夜班次日休息）覆盖通例固定班，避免误报为硬冲突
    warnings.extend(_apply_rest_precedence(plan, rules, fixed_shifts, employees))

    if not employees:
        conflicts.append(_conflict("E000", "尚未录入人员"))
    if not any(shift.enabled for shift in shifts):
        conflicts.append(_conflict("E000", "尚未配置可用班次"))
    if leave_dates and leave_shift is None:
        conflicts.append(
            _conflict("E014", "班次目录中没有可用的有薪休假班次 POFF，请重新导入班次代码")
        )

    # 工作时段窗（WORK_WINDOW）：窗口合法、能匹配到员工，且窗口内确实有可排班次；
    # 同时检查与该员工的硬性固定班次/固定上班时刻是否互斥，避免进入求解后静默不可行。
    for rule, rule_dates in window_rules:
        assert rule.start_time and rule.end_time
        label = rule.raw_text or "工作时段"
        begin, finish = parse_hhmm(rule.start_time), parse_hhmm(rule.end_time)
        if begin >= finish:
            conflicts.append(
                _conflict(
                    "E028",
                    f"规则「{label}」的工作时段 {rule.start_time}-{rule.end_time} 不合法："
                    f"结束时间必须晚于开始时间",
                    [rule.id],
                )
            )
            continue
        targets = [
            employee
            for employee in employees
            if (rule.employee_id is None or employee.id == rule.employee_id)
            and _role_match(employee, rule.role)
        ]
        if not targets:
            conflicts.append(
                _conflict("E028", f"规则「{label}」没有匹配到任何员工", [rule.id])
            )
            continue
        if not rule.hard:
            continue
        scope = f"岗位「{rule.role}」" if rule.role else "相关员工"
        if not any(
            _eligible(plan, employee, shift) and _shift_within_window(shift, rule)
            for employee in targets
            for shift in normal
        ):
            conflicts.append(
                _conflict(
                    "E028",
                    f"规则「{label}」：{scope}在 {rule.start_time}-{rule.end_time} 内没有可排班次，"
                    f"请检查班次目录或放宽该时段",
                    [rule.id],
                )
            )
        active_days = set(rule_dates)
        for employee in targets:
            for day, entries in fixed_shifts.get(_eid(employee), {}).items():
                if day not in active_days:
                    continue
                for shift_id, _ in entries:
                    shift = shift_map.get(shift_id)
                    if shift is not None and not _shift_within_window(shift, rule):
                        conflicts.append(
                            _conflict(
                                "E028",
                                f"规则「{label}」：{employee.name} 在 {day} 的固定班次 {shift.name}"
                                f"（{shift.start_time}-{shift.end_time}）超出 {rule.start_time}-{rule.end_time}",
                                [rule.id],
                            )
                        )
            for (employee_id, day), (time_value, _) in fixed_times.items():
                if employee_id != _eid(employee) or day not in active_days:
                    continue
                if not any(
                    shift.start_time == time_value and _shift_within_window(shift, rule)
                    for shift in normal
                ):
                    conflicts.append(
                        _conflict(
                            "E028",
                            f"规则「{label}」：{employee.name} 在 {day} 被固定为 {time_value} 上班，"
                            f"但该时刻没有落在 {rule.start_time}-{rule.end_time} 内的班次",
                            [rule.id],
                        )
                    )

    window_begin, window_finish = parse_interval(plan.coverage_start, plan.coverage_end)
    window_hours = (window_finish - window_begin) / 60
    soft_begin, soft_finish = parse_interval(
        plan.coverage_soft_start_effective, plan.coverage_soft_end_effective
    )
    # 时段规则必须落在软覆盖窗内：建模按规则自身时段切段，软窗口之外的
    # 区间根本无法排班，必须提前报错，而不是静默进入求解得到 INFEASIBLE
    outside_window_rule_ids: set[int] = set()
    for rule, _rule_dates in segment_rules:
        if not (rule.start_time and rule.end_time):
            continue
        begin, finish = parse_interval(rule.start_time, rule.end_time)
        if finish >= 1440:
            # 跨午夜规则永远无法被普通班覆盖（window_violation 明确排除跨午夜普通班），
            # 不能提示「扩展软覆盖窗」，否则用户会沿着错误方向调整
            if rule.id is not None:
                outside_window_rule_ids.add(rule.id)
            conflicts.append(
                _conflict(
                    "E024",
                    f"规则「{rule.raw_text or rule.type}」要求 {rule.start_time}-{rule.end_time} 跨午夜；"
                    "普通班不允许跨午夜，请改用特殊班（盘点班）承接，"
                    "或把该规则结束时间调整到 23:59 之前",
                    [rule.id],
                )
            )
            continue
        beyond: list[str] = []
        if begin < soft_begin:
            beyond.append(f"{format_hhmm(begin)}-{plan.coverage_soft_start_effective}")
        if finish > soft_finish:
            beyond.append(f"{plan.coverage_soft_end_effective}-{format_hhmm(finish)}")
        if not beyond:
            continue
        if rule.id is not None:
            outside_window_rule_ids.add(rule.id)
        conflicts.append(
            _conflict(
                "E024",
                f"规则「{rule.raw_text or rule.type}」要求 {rule.start_time}-{rule.end_time}，"
                f"超出软覆盖窗 {plan.coverage_soft_start_effective}-{plan.coverage_soft_end_effective}"
                f"（超出 {'、'.join(beyond)}）；请扩展覆盖窗或收窄该规则时段",
                [rule.id],
            )
        )
    full_window_rules = [
        rule
        for rule, _ in segment_rules
        if rule.start_time
        and rule.end_time
        and parse_hhmm(rule.start_time) <= window_begin
        and parse_interval(rule.start_time, rule.end_time)[1] >= window_finish
        # 只有规则窗口完全落在计划窗口内才走整窗口分支
        and rule.id not in outside_window_rule_ids
    ]
    # 已按整窗口规则提示过的规则编号，后续 E004 不再重复提示
    window_rule_ids: set[int] = set()
    if employees and full_window_rules:
        required = max((rule.count or 0) for rule in full_window_rules)
        # count=0 的整窗口规则不产生人数要求，直接跳过
        if required > 0:
            # 与建模同口径：只用建模真正会建变量的班次
            # （普通班次、启用、非 OFF/POFF/盘点班、满足计划最晚收班，且对该员工可排），
            # 覆盖判定按 coverage_slices 切片，允许任意段数接力。
            pairs: list[tuple[float, float, int, int, Shift, float]] = []
            # (工时, 与窗口重叠分钟, 上班分钟, 下班分钟, 班次, 该员工每日上限)
            for employee in employees:
                for shift in shifts:
                    if not shift.enabled or shift.is_off or shift.is_leave or shift.is_special:
                        continue
                    if not _eligible(plan, employee, shift):
                        continue
                    begin, finish = parse_interval(shift.start_time, shift.end_time)
                    overlap = max(0, min(finish, window_finish) - max(begin, window_begin))
                    if overlap <= 0:
                        continue
                    pairs.append(
                        (
                            effective_hours(shift.work_hours, shift.start_time, shift.end_time),
                            float(overlap),
                            begin,
                            finish,
                            shift,
                            employee.max_daily_hours,
                        )
                    )
            intervals = [(begin, finish) for _, _, begin, finish, _, _ in pairs]
            slices = coverage_slices(
                [(shift.start_time, shift.end_time) for _, _, _, _, shift, _ in pairs],
                plan.coverage_start,
                plan.coverage_end,
            )
            gaps = [
                (left, right)
                for left, right in slices
                if not any(begin <= left and finish >= right for begin, finish in intervals)
            ]
            needed = _min_window_shifts(intervals, window_begin, window_finish, required)
            # 同一时段可覆盖段数不足 required：结构上就排不满
            too_few_shifts = needed is None and not gaps
            # 接力段数超过当日人数：同一人不能同时在两个时段上班
            people_short = needed is not None and needed > len(employees)
            if gaps or too_few_shifts or people_short:
                if pairs:
                    pairs.sort(key=lambda item: (-item[0], -item[1], item[2], -item[3]))
                    best_hours, best_shift, best_limit = (
                        pairs[0][0],
                        pairs[0][4],
                        pairs[0][5],
                    )
                    longest = f"最长可用班次 {best_shift.name} {best_hours:g} 小时"
                else:
                    best_limit = max(
                        (employee.max_daily_hours for employee in employees), default=0
                    )
                    longest = "最长可用班次 无 0 小时"
                if gaps:
                    gap_text = "、".join(
                        f"{format_hhmm(left)}-{format_hhmm(right)}" for left, right in gaps
                    )
                    gap_reason = (
                        f"缺口 {gap_text} 无班次覆盖：现有班次与窗口重叠不足，"
                        f"单班无法铺满且班次间无法接力拼接"
                    )
                elif too_few_shifts:
                    depth = max(
                        (
                            sum(
                                1
                                for begin, finish in intervals
                                if begin <= left and finish >= right
                            )
                            for left, right in slices
                        ),
                        default=0,
                    )
                    gap_reason = (
                        f"窗口内每个时段至少需要 {required} 段班次同时覆盖，"
                        f"当前班次池最多只能同时覆盖 {depth} 段"
                    )
                else:
                    gap_reason = (
                        f"排满窗口至少要 {needed} 段班次接力，当日仅有 {len(employees)} 人；"
                        f"同一人不能同时在两个时段上班，无法覆盖全部时段"
                    )
                advice = (
                    f"请提高每日上限、补充覆盖 {plan.coverage_start} 开始或 "
                    f"{plan.coverage_end} 结束的班次"
                )
                if people_short:
                    advice = f"请增加人员、{advice}"
                advice += "、或放宽该时段规则"
                message = (
                    f"窗口 {plan.coverage_start}-{plan.coverage_end} 需 {window_hours:g} 小时"
                    f"连续覆盖，当前每日上限 {best_limit:g} 小时，{longest}；"
                    f"{gap_reason}。{advice}"
                )
                rule_ids = [rule.id for rule in full_window_rules if rule.id is not None]
                # 整窗口规则全为软约束时降级为 P002 警告，交给求解器用 slack 放宽
                if any(rule.hard for rule in full_window_rules):
                    conflicts.append(_conflict("E018", message, rule_ids))
                else:
                    warnings.append(_conflict("P002", message, rule_ids))
                window_rule_ids = set(rule_ids)

    # 只给天数的有薪假：区间内可用天数不足 count 时才报冲突（不指定日期）
    for rule in rules:
        if rule.type != "PAID_LEAVE" or rule.employee_id is None or not rule.count:
            continue
        rule_dates = _expand(plan.month, rule)
        if rule.count >= len(rule_dates):
            continue
        employee = employee_map.get(rule.employee_id)
        if employee is None:
            continue
        blocked = set(must_work.get(rule.employee_id, set()))
        blocked.update(fixed_shifts.get(rule.employee_id, {}).keys())
        blocked.update(day for (eid, day) in fixed_times if eid == rule.employee_id)
        available = [day for day in rule_dates if day not in blocked]
        if len(available) < rule.count:
            conflicts.append(
                _conflict(
                    "E014",
                    f"{employee.name} 需要 {rule.count} 天有薪假，但区间内只有"
                    f" {len(available)} 天可安排（与必须上班/固定班次冲突）",
                    [rule.id],
                )
            )

    for employee_id, days in leave_dates.items():
        employee = employee_map.get(employee_id)
        if employee is None:
            continue
        for day in days:
            if day in must_work.get(employee_id, set()):
                conflicts.append(
                    _conflict(
                        "E014",
                        f"{employee.name} 在 {day} 同时被要求必须上班与有薪休假",
                        [
                            rule.id
                            for rule in rules
                            if rule.employee_id == employee_id
                            and rule.type in ("MUST_WORK", "PAID_LEAVE")
                        ],
                    )
                )
            if any(
                not _rule_is_soft(rule_id, rule_by_id)
                for _, rule_id in fixed_shifts.get(employee_id, {}).get(day, [])
            ):
                conflicts.append(
                    _conflict("E014", f"{employee.name} 在 {day} 同时被要求有薪休假与指定班次")
                )
            if (employee_id, day) in fixed_times:
                conflicts.append(
                    _conflict("E014", f"{employee.name} 在 {day} 同时被要求有薪休假与指定上班时间")
                )

    # 休息（OFF）日期的互斥校验：与必须上班/指定班次/指定上班时间/全员到岗冲突
    for employee_id, days in off_dates.items():
        employee = employee_map.get(employee_id)
        if employee is None:
            continue
        for day in days:
            if day in must_work.get(employee_id, set()):
                conflicts.append(
                    _conflict(
                        "E001",
                        f"{employee.name} 在 {day} 同时被要求休息 OFF 与必须上班",
                    )
                )
            if any(
                not _rule_is_soft(rule_id, rule_by_id)
                for _, rule_id in fixed_shifts.get(employee_id, {}).get(day, [])
            ):
                conflicts.append(
                    _conflict("E001", f"{employee.name} 在 {day} 同时被要求休息 OFF 与指定班次")
                )
            if (employee_id, day) in fixed_times:
                conflicts.append(
                    _conflict("E001", f"{employee.name} 在 {day} 同时被要求休息 OFF 与指定上班时间")
                )
            if day in all_start:
                conflicts.append(
                    _conflict(
                        "E016",
                        f"{day} 全员 {all_start[day][0]} 到岗与 {employee.name} 休息 OFF 冲突",
                    )
                )

    for employee_id, days in fixed_shifts.items():
        employee = employee_map.get(employee_id)
        if employee is None:
            continue
        for day, entries in days.items():
            # 软固定班次不参与硬冲突判定：未排上时由建模罚分，而不是阻塞求解
            hard_entries = [
                (shift_id, rule_id)
                for shift_id, rule_id in entries
                if not _rule_is_soft(rule_id, rule_by_id)
            ]
            hard_rule_ids = [rule_id for _, rule_id in hard_entries]
            unique_ids = {shift_id for shift_id, _ in hard_entries}
            if len(unique_ids) > 1:
                conflicts.append(
                    _conflict(
                        "E007",
                        f"{employee.name} 在 {day} 被指定了多个不同班次",
                        hard_rule_ids,
                    )
                )
            for shift_id, rule_id in hard_entries:
                shift = shift_map.get(shift_id)
                if shift is None or not shift.enabled:
                    conflicts.append(
                        _conflict("E009", f"{employee.name} 在 {day} 的指定班次不存在或已停用", hard_rule_ids)
                    )
                    continue
                if shift.is_special and not employee.allow_special:
                    conflicts.append(
                        _conflict("E006", f"{employee.name} 未开启盘点班次资格，不能排入 {shift.name}", hard_rule_ids)
                    )
                if effective_hours(shift.work_hours, shift.start_time, shift.end_time) > employee.max_daily_hours + 1e-6:
                    conflicts.append(
                        _conflict(
                            "E002",
                            f"{shift.name} 时长超过 {employee.name} 的每日上限 {employee.max_daily_hours} 小时",
                            hard_rule_ids,
                        )
                    )
                error = shift_window_error(
                    plan.coverage_soft_start_effective, plan.coverage_soft_end_effective, shift, plan.latest_end_time
                )
                if error and not shift.is_special:
                    conflicts.append(_conflict("E015", f"{employee.name} 在 {day} 的{error}", hard_rule_ids))
            if hard_entries and day in rest_dates.get(employee_id, set()):
                conflicts.append(
                    _conflict("E001", f"{employee.name} 在 {day} 同时被要求休息与指定班次", hard_rule_ids)
                )

    for (employee_id, day), (time_value, rule_id) in fixed_times.items():
        employee = employee_map.get(employee_id)
        if employee is None:
            continue
        matching = [shift for shift in normal if shift.start_time == time_value and _window_ok(plan, shift)]
        if not matching:
            conflicts.append(
                _conflict(
                    "E003",
                    f"{employee.name} 要求 {time_value} 上班，班次目录中没有该开始时间且符合时段的普通班次",
                    [rule_id],
                )
            )
        if day in rest_dates.get(employee_id, set()):
            conflicts.append(
                _conflict("E001", f"{employee.name} 在 {day} 同时被要求休息与指定上班时间", [rule_id])
            )

    for employee_id, days in must_work.items():
        employee = employee_map.get(employee_id)
        if employee is None:
            continue
        missing = []
        for day in days:
            fixed = fixed_shifts.get(employee_id, {}).get(day)
            if fixed:
                ok = any(
                    shift_id in shift_map
                    and _eligible(plan, employee, shift_map[shift_id])
                    for shift_id, _ in fixed
                )
            else:
                time_rule = fixed_times.get((employee_id, day))
                if time_rule:
                    ok = any(
                        shift.start_time == time_rule[0]
                        and _eligible(plan, employee, shift)
                        for shift in normal
                    )
                else:
                    ok = any(_eligible(plan, employee, shift) for shift in shifts)
            if not ok:
                missing.append(day)
        if not missing:
            continue
        rule_ids = [
            rule.id
            for rule in rules
            if rule.type == "MUST_WORK"
            and rule.employee_id in (None, employee_id)
        ]
        conflicts.append(
            _conflict(
                "E013",
                f"{employee.name} 没有可用班次（受时段、每日时长上限或盘点资格限制），必须上班的要求无法满足",
                rule_ids,
            )
        )

    # 强制上班规则（必须上班 / 指定班次 / 指定上班时间）叠加「连续上班上限」时，
    # 模型层只能回报 E012 这类非最小集合，用户无法定位；这里按人逐段提前判出冲突区间
    forced_days: dict[int, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    for rule in rules:
        if not rule.hard or rule.id is None:
            continue
        if rule.type not in ("MUST_WORK", "FIXED_SHIFT", "FIXED_TIME"):
            continue
        if rule.employee_id is None:
            # 只有 MUST_WORK 支持全员；固定班次/固定时刻仍要求指定人员
            if rule.type != "MUST_WORK":
                continue
            targets = [_eid(employee) for employee in employees]
        else:
            targets = [rule.employee_id]
        for employee_id in targets:
            for day in _expand(plan.month, rule):
                forced_days[employee_id][day].add(int(rule.id))
    for employee_id, forced_by_day in forced_days.items():
        employee = employee_map.get(employee_id)
        if employee is None:
            continue
        limit = plan.max_consecutive_days or 0
        for streak_rule, _streak_dates in max_work_streak_rules:
            if not streak_rule.hard or not streak_rule.count:
                continue
            if streak_rule.employee_id not in (None, employee_id):
                continue
            limit = min(limit, streak_rule.count) if limit > 0 else streak_rule.count
        if limit <= 0 or len(forced_by_day) <= limit:
            continue
        runs: list[list[str]] = []
        current_run: list[str] = []
        previous: date | None = None
        for day in sorted(forced_by_day):
            current = date.fromisoformat(day)
            if previous is not None and (current - previous).days == 1:
                current_run.append(day)
            else:
                if current_run:
                    runs.append(current_run)
                current_run = [day]
            previous = current
        if current_run:
            runs.append(current_run)
        # 只报完整区间（避免同一段连续被报成 6 天、7 天两次），最多 3 条
        for run in [item for item in runs if len(item) > limit][:3]:
            conflicts.append(
                _conflict(
                    "E025",
                    f"{employee.name} 在 {run[0]} 至 {run[-1]} 被规则强制上班 {len(run)} 天，"
                    f"超过连续上班上限 {limit} 天；请放宽该区间的休息要求或强制上班规则",
                    sorted({rule_id for item in run for rule_id in forced_by_day[item]}),
                )
            )

    # 班次限制（SHIFT_LIMIT）：限定条件下班次目录里没有可用班次时，该员工上班日无法排班
    for limit_rule, limit_dates in shift_limit_rules:
        if not limit_rule.hard:
            continue
        limit_day_set = set(limit_dates)
        targets = (
            [employee_map.get(limit_rule.employee_id)]
            if limit_rule.employee_id is not None
            else list(employees)
        )
        for employee in targets:
            if employee is None:
                continue
            candidates = [
                shift
                for shift in work_shifts
                if not shift.is_off
                and _eligible(plan, employee, shift)
                and _shift_limit_ok(shift, limit_rule)
            ]
            if not candidates:
                conflicts.append(
                    _conflict(
                        "E026",
                        f"{employee.name} 的班次限制（{_shift_limit_text(limit_rule, shift_map)}）"
                        f"在班次目录中没有符合条件的可用班次",
                        [limit_rule.id],
                    )
                )
                continue
            # 与硬性「指定班次 / 指定上班时间」互斥时提前报出，避免这些强制项被静默丢弃
            for day, entries in fixed_shifts.get(_eid(employee), {}).items():
                if day not in limit_day_set:
                    continue
                for shift_id, fixed_rule_id in entries:
                    shift = shift_map.get(shift_id)
                    if shift is None or _shift_limit_ok(shift, limit_rule):
                        continue
                    if not _rule_is_soft(fixed_rule_id, rule_by_id):
                        conflicts.append(
                            _conflict(
                                "E026",
                                f"{employee.name} 在 {day} 被指定班次 {shift.name}，"
                                f"与班次限制（{_shift_limit_text(limit_rule, shift_map)}）冲突",
                                [limit_rule.id, fixed_rule_id],
                            )
                        )
            for (fixed_employee_id, fixed_day), (
                time_value,
                time_rule_id,
            ) in fixed_times.items():
                if fixed_employee_id != _eid(employee) or fixed_day not in limit_day_set:
                    continue
                matching = [
                    shift
                    for shift in work_shifts
                    if not shift.is_off
                    and _eligible(plan, employee, shift)
                    and shift.start_time == time_value
                    and _shift_limit_ok(shift, limit_rule)
                ]
                if matching:
                    continue
                conflicts.append(
                    _conflict(
                        "E026",
                        f"{employee.name} 在 {fixed_day} 被要求 {time_value} 上班，"
                        f"但班次限制（{_shift_limit_text(limit_rule, shift_map)}）下没有符合条件的班次",
                        [limit_rule.id, time_rule_id],
                    )
                )

    # 岗位单人班次（SINGLE_ROLE_SHIFT）：岗位/班次必须可用，且该岗位至少一人能上该班次
    for rule, _rule_dates in role_shift_rules:
        role_name = (rule.role or "").strip()
        role_employees = [
            employee
            for employee in employees
            if role_name and (employee.role or "").strip() == role_name
        ]
        if not role_employees:
            conflicts.append(
                _conflict(
                    "E027",
                    f"岗位「{role_name or '未填写'}」下没有人员，无法要求该岗位单人上指定班次",
                    [rule.id],
                )
            )
            continue
        shift = shift_map.get(rule.shift_id) if rule.shift_id is not None else None
        if shift is None or not shift.enabled:
            conflicts.append(
                _conflict("E027", f"岗位「{role_name}」的指定班次不存在或已停用", [rule.id])
            )
            continue
        if not any(_eligible(plan, employee, shift) for employee in role_employees):
            conflicts.append(
                _conflict(
                    "E027",
                    f"岗位「{role_name}」没有人员能排入班次 {shift.name}（时长或时段不满足）",
                    [rule.id],
                )
            )

    for day, (time_value, rule_id) in all_start.items():
        if not any(shift.start_time == time_value and _window_ok(plan, shift) for shift in normal):
            conflicts.append(
                _conflict(
                    "E003",
                    f"{day} 要求全员 {time_value} 上班，班次目录中没有该开始时间且符合时段的普通班次",
                    [rule_id],
                )
            )
        # 全员到岗时取最严上限：任何一个人达不到，全体上同一条班次就不可行
        caps = [(employee.name, employee.max_daily_hours) for employee in employees]
        min_cap = min((cap for _, cap in caps), default=0)
        # 以该时刻起班、且符合计划覆盖窗的普通班次（不含停用/请假/盘点班）
        starting_shifts = [
            shift
            for shift in shifts
            if shift.enabled
            and not shift.is_leave
            and not shift.is_special
            and shift.start_time == time_value
            and _window_ok(plan, shift)
        ]
        reaching_shifts = [
            shift
            for shift in starting_shifts
            if parse_interval(shift.start_time, shift.end_time)[1] >= window_finish
        ]
        if reaching_shifts:
            needed = min(
                effective_hours(shift.work_hours, shift.start_time, shift.end_time)
                for shift in reaching_shifts
            )
        else:
            needed = ((window_finish - parse_hhmm(time_value)) % 1440) / 60 or 24
        cap_short = needed > min_cap + 1e-6
        pool_short = not reaching_shifts
        if cap_short or pool_short:
            reasons: list[str] = []
            if cap_short:
                gap = needed - min_cap
                short = [
                    f"{name}（上限 {cap:g}h）"
                    for name, cap in caps
                    if cap + 1e-6 < needed
                ]
                names = f"涉及人员：{'、'.join(short)}；" if short else ""
                reasons.append(
                    f"原因 A：{time_value} 到岗要到 {plan.coverage_end} 需要 {needed:g} 小时，"
                    f"当前每日上限 {min_cap:g} 小时，差 {gap:g} 小时；{names}"
                )
            if pool_short:
                if starting_shifts:
                    longest = max(
                        starting_shifts,
                        key=lambda item: effective_hours(
                            item.work_hours, item.start_time, item.end_time
                        ),
                    )
                    longest_hours = effective_hours(
                        longest.work_hours, longest.start_time, longest.end_time
                    )
                    latest_end = format_hhmm(
                        max(
                            parse_interval(shift.start_time, shift.end_time)[1]
                            for shift in starting_shifts
                        )
                    )
                    pool_text = (
                        f"班次池中 {time_value} 起班的可用班次最长是 {longest.name}"
                        f"（{longest.start_time}-{longest.end_time}，{longest_hours:g} 小时），"
                        f"最晚收班 {latest_end}"
                    )
                else:
                    pool_text = f"班次池中没有 {time_value} 起班的普通班次"
                cross = [
                    shift
                    for shift in shifts
                    if shift.enabled
                    and not shift.is_leave
                    and parse_interval(shift.start_time, shift.end_time)[0]
                    > parse_interval(shift.start_time, shift.end_time)[1]
                    and _window_ok(plan, shift)
                ]
                if cross:
                    cross_text = "跨午夜班 " + "、".join(
                        f"{shift.name}（{shift.start_time}-{shift.end_time}）" for shift in cross
                    ) + f" 能覆盖到 {plan.coverage_end}，但起点不是 {time_value}，无法满足全员 {time_value} 到岗"
                else:
                    cross_text = "也没有跨午夜班能覆盖该区间"
                reasons.append(
                    f"原因 B：{pool_text}；没有任何 {time_value} 起班且收班不早于 "
                    f"{plan.coverage_end} 的班次；{cross_text}"
                )
            if pool_short:
                conclusion = (
                    f"结论：仅提高每日上限无效，必须补充能从 {time_value} 起班并覆盖到 "
                    f"{plan.coverage_end} 的班次，或调整到岗时刻/覆盖结束时间"
                )
            else:
                conclusion = f"结论：把每日上限提高到 {needed:g} 小时（还差 {needed - min_cap:g} 小时）即可解出覆盖"
            conflicts.append(
                _conflict(
                    "E019",
                    f"{day} 全员 {time_value} 到岗无法覆盖到 {plan.coverage_end}。"
                    + "；".join(reasons)
                    + f"；{conclusion}",
                    [rule_id],
                )
            )
        inventory_conflicts: list[tuple[Shift, int | None]] = []
        for days in fixed_shifts.values():
            for shift_id, fixed_rule_id in days.get(day, []):
                if _rule_is_soft(fixed_rule_id, rule_by_id):
                    continue
                shift = shift_map.get(shift_id)
                if shift is not None and shift.is_special:
                    inventory_conflicts.append((shift, fixed_rule_id))
        for shift_rule, shift_rule_dates in shift_rules:
            if day not in shift_rule_dates:
                continue
            shift = shift_map.get(shift_rule.shift_id)
            if shift is not None and shift.is_special:
                inventory_conflicts.append((shift, shift_rule.id))
        for shift, inventory_rule_id in inventory_conflicts:
            conflicts.append(
                _conflict(
                    "E021",
                    f"{day} 全员 {time_value} 到岗与盘点班次 {shift.name} 冲突",
                    [rule_id, inventory_rule_id],
                )
            )
        for employee in employees:
            if day not in rest_dates.get(_eid(employee), set()):
                continue
            rest_rule_ids = [
                rule.id
                for rule in rules
                if rule.valid
                and rule.employee_id in (None, employee.id)
                and rule.type == "REST"
                and rule.hard
                and rule.date_start <= day <= rule.date_end
            ]
            conflicts.append(
                _conflict(
                    "E016",
                    f"{day} 全员 {time_value} 到岗与 {employee.name} 休息冲突",
                    [rule_id, *rest_rule_ids],
                )
            )

    for rule, _dates in day_rules:
        if (rule.count or 0) > len(employees):
            message = f"{rule.date_start} 起当天需求人数超过人员总数"
            # 软需求允许求解器用 slack 放弃该规则，仅提示
            if rule.hard:
                conflicts.append(_conflict("E005", message, [rule.id]))
            else:
                warnings.append(
                    _conflict("P004", f"{message}，已按软约束放宽", [rule.id])
                )
            break

    for rule, _dates in start_rules:
        # count_op=le（至多 N 人）恒可满足：即使没有匹配班次或 N 超过人员总数也不报错
        if (rule.count_op or "ge") == "le":
            continue
        matched = [
            shift
            for shift in normal
            if _start_matches(shift.start_time, rule.start_time, rule.time_op)
            and not shift.is_off
            and _window_ok(plan, shift)
        ]
        if not matched:
            if (rule.time_op or "eq") == "eq":
                detail = "班次目录中没有该时刻开始且符合覆盖时段的普通班次"
            elif rule.time_op == "le":
                detail = f"班次目录中没有 {rule.start_time} 之前开始且符合覆盖时段的普通班次"
            else:
                detail = f"班次目录中没有 {rule.start_time} 之后开始且符合覆盖时段的普通班次"
            message = f"{rule.start_time} 上班人数需求无法满足：{detail}"
            # 上班时刻人数规则沿用 soft 降级提示 P005
            if rule.hard:
                conflicts.append(_conflict("E021", message, [rule.id]))
            else:
                warnings.append(_conflict("P005", message, [rule.id]))
            continue
        required = rule.count or 0
        if required > len(employees):
            message = f"{rule.start_time} 上班人数需求 {required} 超过人员总数 {len(employees)}"
            if rule.hard:
                conflicts.append(_conflict("E005", message, [rule.id]))
            else:
                warnings.append(_conflict("P004", f"{message}，已按软约束放宽", [rule.id]))

    # 跨午夜普通班按产品设计不可排，但它确实存在时容易被误读成「目录里没有班次」，
    # 在 E004/P003 文案里点名说明被排除的原因
    overnight_names = [
        shift.name
        for shift in normal
        if not shift.is_off
        and parse_interval(shift.start_time, shift.end_time)[1] >= 1440
    ]
    overnight_note = (
        f"（班次 {'、'.join(overnight_names)} 因普通班不允许跨午夜被排除）"
        if overnight_names
        else ""
    )
    for rule, _dates in segment_rules:
        if not rule.start_time or not rule.end_time:
            continue
        if (rule.count or 0) > len(employees):
            message = f"{rule.date_start} 起时段需求人数超过人员总数"
            # 软需求允许求解器用 slack 放弃该规则，仅提示
            if rule.hard:
                conflicts.append(_conflict("E005", message, [rule.id]))
            else:
                warnings.append(
                    _conflict("P004", f"{message}，已按软约束放宽", [rule.id])
                )
        eligible = [
            shift
            for shift in normal
            if _window_ok(plan, shift)
        ]
        slices = coverage_slices(
            [(shift.start_time, shift.end_time) for shift in eligible],
            rule.start_time,
            rule.end_time,
        )
        coverable = bool(slices) and all(
            any(
                _covers(shift.start_time, shift.end_time, left, right)
                for shift in eligible
            )
            for left, right in slices
        )
        # 已按整窗口规则报过 E018/P002 或跨午夜 E024 的规则不再重复报覆盖提示
        if (
            (rule.count or 0) > 0
            and not coverable
            and rule.id not in window_rule_ids
            and rule.id not in outside_window_rule_ids
        ):
            message = (
                f"{rule.date_start} 起 {rule.start_time}-{rule.end_time} 时段没有任何普通班次可以覆盖"
                f"{overnight_note}"
            )
            if rule.hard:
                conflicts.append(_conflict("E004", message, [rule.id]))
            else:
                warnings.append(
                    _conflict("P003", f"{message}，已按软约束放宽", [rule.id])
                )

    for rule, _dates in shift_rules:
        shift = shift_map.get(rule.shift_id)
        if shift is None or not shift.enabled:
            conflicts.append(_conflict("E009", "盘点班次规则引用的班次不存在或已停用", [rule.id]))
            continue
        required = rule.count or 0
        error = shift_window_error(
            plan.coverage_soft_start_effective, plan.coverage_soft_end_effective, shift, plan.latest_end_time
        )
        if error and not shift.is_special:
            conflicts.append(_conflict("E015", error, [rule.id]))
        if shift.is_special:
            eligible = sum(
                1
                for employee in employees
                if employee.allow_special and _role_match(employee, rule.role)
            )
            if required > eligible:
                conflicts.append(
                    _conflict(
                        "E006",
                        f"{shift.name} 需要 {required} 人，但只有 {eligible} 人具备盘点班次资格",
                        [rule.id],
                    )
                )
        else:
            pool = sum(1 for employee in employees if _role_match(employee, rule.role))
            if required > pool:
                conflicts.append(
                    _conflict("E005", f"{shift.name} 需求人数超过人员总数", [rule.id])
                )

    # 班次工时条件（SHIFT_HOURS）：班次目录与锁定班次里是否存在满足工时的班次
    for rule, rule_dates in hours_rules:
        target = rule.work_hours or 0.0
        op = rule.hours_op or "eq"
        candidates = [
            shift
            for shift in work_shifts
            if not shift.is_off and _hours_match(_shift_hours(shift), target, op)
        ]
        if not candidates:
            message = (
                f"{rule.date_start} 需排{_hours_text(target, op)}的班次，"
                f"但班次目录中没有符合条件的班次"
            )
            if rule.hard:
                conflicts.append(_conflict("E022", message, [rule.id]))
            else:
                warnings.append(_conflict("P007", message, [rule.id]))
        allowed_ids = {shift.id for shift in candidates}
        for item in locked:
            if rule.employee_id is not None and item.employee_id != rule.employee_id:
                continue
            if item.date not in rule_dates:
                continue
            shift = shift_map.get(item.shift_id) if item.shift_id is not None else None
            if shift is not None and (shift.is_off or shift.is_leave):
                continue
            if item.shift_id is not None:
                if item.shift_id in allowed_ids:
                    continue
                hours = _shift_hours(shift) if shift is not None else 0.0
            else:
                hours = effective_hours(None, item.start_time, item.end_time)
                if _hours_match(hours, target, op):
                    continue
            employee = employee_map.get(item.employee_id)
            name = employee.name if employee else f"#{item.employee_id}"
            message = (
                f"{name} 在 {item.date} 锁定的班次为 {hours:g} 小时，"
                f"不满足{_hours_text(target, op)}"
            )
            if rule.hard:
                conflicts.append(_conflict("E022", message, [rule.id]))
            else:
                warnings.append(_conflict("P007", message, [rule.id]))

    # 连续休息上限（MAX_REST_DAYS）：窗口比计划月份还长时无法生效。
    # 带薪假是中性日，只会放宽该约束（不会新增不可行），因此预检口径保持不变。
    total_days = len(month_dates(plan.month))
    for rule, _ in rest_limit_rules:
        limit = rule.count or 0
        if limit + 1 <= total_days:
            continue
        message = f"连续休息上限 {limit} 天超过计划天数，无法生效"
        if rule.hard:
            conflicts.append(_conflict("E023", message, [rule.id]))
        else:
            warnings.append(_conflict("P008", message, [rule.id]))

    # 连续上班上限（MAX_CONSECUTIVE_WORK）：上限覆盖整个规则区间时规则不会生效。
    # 这是配置冗余而非不可行，统一给建议级提示，避免误阻塞求解。
    for rule, rule_dates in max_work_streak_rules:
        limit = rule.count or 0
        if limit <= 0 or limit < len(rule_dates):
            continue
        warnings.append(
            _conflict(
                RULE_INEFFECTIVE,
                f"连续上班上限 {limit} 天覆盖了规则区间 {len(rule_dates)} 天的全部日期，"
                "规则不会生效；可缩短日期范围或调小上限",
                [rule.id],
            )
        )

    # 周期残周折算：计划月首/末的自然周不足 7 天时按残周口径折算。
    # 至少休息天数用 floor（残周不提出更高要求），最多上班天数用 min(count, 可见天数)
    # （上限型指标不能因残周被收紧，否则 1 天残周会变成强制闭店）。
    # 折算会真实改变约束强度，必须显式说明，不能静默。
    # 同一周同一口径（类型 + 人数 + 折算结果）只提示一次，并把相关规则合并展示，
    # 否则「全员每周至少休 N 天」会按员工数 × 残周数刷出成倍重复提示。
    residual_groups: dict[tuple[str, int, str, str, int, int], list[int]] = {}
    for rule, rule_dates in min_rest_rules + max_work_rules:
        if (rule.period or "month") != "week":
            continue
        count = rule.count or 0
        for bucket in period_buckets(rule.period, rule_dates):
            visible = week_days_in_month(plan.month, bucket[0])
            if visible >= 7:
                continue
            quota = weekly_quota(
                count, plan.month, bucket, is_limit=rule.type == "MAX_WORK_DAYS"
            )
            key = (rule.type, count, bucket[0], bucket[-1], visible, quota)
            group = residual_groups.setdefault(key, [])
            if rule.id is not None:
                group.append(rule.id)
    for (rule_type, count, first, last, visible, quota), rule_ids in residual_groups.items():
        desc = (
            f"每周至少休 {count} 天"
            if rule_type == "MIN_REST_DAYS"
            else f"每周最多上 {count} 天"
        )
        formula = (
            f"配额按 floor({count}×{visible}/7) 折算为 {quota} 天"
            if rule_type == "MIN_REST_DAYS"
            else f"上限按本周可见 {visible} 天折算为 {quota} 天"
        )
        warnings.append(
            _conflict(
                "P015",
                f"{desc}：{first} 至 {last} 这一周只有 {visible} 天落在计划月内，{formula}",
                sorted(set(rule_ids)),
            )
        )

    # 跨月连续休息/连续上班：规则区间超出计划月时只判断本月交集部分，
    # 跨月段无法延伸取证，必须提示覆盖不全，避免用户误以为已全量校验。
    month_days = month_dates(plan.month)
    month_start, month_end = month_days[0], month_days[-1]
    for rule, rule_dates in rest_limit_rules + max_work_streak_rules:
        if rule.date_start >= month_start and rule.date_end <= month_end:
            continue
        label = (
            "连续休息上限"
            if rule.type == "MAX_REST_DAYS"
            else "连续上班上限"
        )
        warnings.append(
            _conflict(
                "P016",
                f"{label}规则区间 {rule.date_start} 至 {rule.date_end} 超出计划月 "
                f"{plan.month}，本月仅覆盖 {len(rule_dates)} 天，跨月部分未纳入判断",
                [rule.id],
            )
        )

    for item in locked:
        employee = employee_map.get(item.employee_id)
        if employee is None:
            conflicts.append(_conflict("E011", f"锁定单元格引用了不存在的人员（{item.date}）"))
            continue
        if item.date not in set(month_dates(plan.month)):
            conflicts.append(_conflict("E011", f"{employee.name} 的锁定日期 {item.date} 不在计划月份内"))
        locked_shift = shift_map.get(item.shift_id) if item.shift_id is not None else None
        if locked_shift is not None and locked_shift.is_leave:
            if not locked_shift.enabled:
                conflicts.append(_conflict("E011", f"{employee.name} 在 {item.date} 锁定的有薪休假班次已停用"))
            continue
        if item.date in leave_dates.get(_eid(employee), set()):
            conflicts.append(
                _conflict("E014", f"{employee.name} 在 {item.date} 已有有薪休假，无法锁定上班班次")
            )
            continue
        if item.date in rest_dates.get(_eid(employee), set()):
            conflicts.append(_conflict("E011", f"{employee.name} 在 {item.date} 已被要求休息，无法锁定排班"))
        if item.shift_id is not None:
            shift = shift_map.get(item.shift_id)
            if shift is None or not shift.enabled:
                conflicts.append(_conflict("E011", f"{employee.name} 在 {item.date} 锁定的班次不存在或已停用"))
                continue
            if shift.is_special and not employee.allow_special:
                conflicts.append(_conflict("E011", f"{employee.name} 无盘点班次资格，锁定班次无法保留"))
            if effective_hours(shift.work_hours, shift.start_time, shift.end_time) > employee.max_daily_hours + 1e-6:
                conflicts.append(_conflict("E011", f"{employee.name} 在 {item.date} 的锁定班次超过每日时长上限"))
            error = shift_window_error(
                plan.coverage_soft_start_effective, plan.coverage_soft_end_effective, shift, plan.latest_end_time
            )
            if error and not shift.is_special:
                conflicts.append(_conflict("E015", f"{employee.name} 在 {item.date} 锁定的{error}"))
            fixed = [
                (shift_id, rule_id)
                for shift_id, rule_id in fixed_shifts.get(_eid(employee), {}).get(item.date, [])
                if not _rule_is_soft(rule_id, rule_by_id)
            ]
            if fixed and all(shift_id != item.shift_id for shift_id, _ in fixed):
                conflicts.append(_conflict("E011", f"{employee.name} 在 {item.date} 的锁定班次与指定班次冲突"))
            fixed_time = fixed_times.get((_eid(employee), item.date))
            if fixed_time and shift.start_time != fixed_time[0]:
                conflicts.append(_conflict("E011", f"{employee.name} 在 {item.date} 的锁定班次与指定上班时间冲突"))
        else:
            if effective_hours(None, item.start_time, item.end_time) > employee.max_daily_hours + 1e-6:
                conflicts.append(_conflict("E011", f"{employee.name} 在 {item.date} 的锁定自定义时长超过每日上限"))
            error = time_window_error(
                plan.coverage_soft_start_effective,
                plan.coverage_soft_end_effective,
                item.start_time,
                item.end_time,
                plan.latest_end_time,
            )
            if error:
                conflicts.append(_conflict("E015", f"{employee.name} 在 {item.date} 锁定的自定义班次 {error}"))
            if any(
                not _rule_is_soft(rule_id, rule_by_id)
                for _, rule_id in fixed_shifts.get(_eid(employee), {}).get(item.date, [])
            ):
                conflicts.append(_conflict("E011", f"{employee.name} 在 {item.date} 的锁定自定义班次与指定班次冲突"))
            if fixed_times.get((_eid(employee), item.date)):
                conflicts.append(_conflict("E011", f"{employee.name} 在 {item.date} 的锁定自定义班次与指定上班时间冲突"))
        all_rule = all_start.get(item.date)
        if all_rule and item.shift_id is not None:
            shift = shift_map.get(item.shift_id)
            if shift and not shift.is_special and shift.start_time != all_rule[0]:
                conflicts.append(_conflict("E011", f"{employee.name} 在 {item.date} 的锁定班次与全员上班时间冲突"))

    day_set = set(month_dates(plan.month))

    def next_day_of(day: str) -> str:
        return (date.fromisoformat(day) + timedelta(days=1)).isoformat()

    def next_day_conflict(employee: Employee, day: str):
        following = next_day_of(day)
        if following not in day_set:
            return None
        if following in leave_dates.get(_eid(employee), set()):
            return None
        for item in locked:
            if item.employee_id != employee.id or item.date != following:
                continue
            locked_shift = shift_map.get(item.shift_id) if item.shift_id is not None else None
            if locked_shift is not None and locked_shift.is_leave:
                return None
        fixed = fixed_shifts.get(_eid(employee), {}).get(following)
        if fixed:
            hard = [
                (shift_id, rule_id)
                for shift_id, rule_id in fixed
                if not _rule_is_soft(rule_id, rule_by_id)
            ]
            if not hard:
                return None
            shift = shift_map.get(hard[0][0])
            name = shift.name if shift else "指定班次"
            return f"指定班次 {name}", [rule_id for _, rule_id in hard]
        if following in must_work.get(_eid(employee), set()):
            rule_ids = [
                rule.id
                for rule in rules
                if rule.valid
                and rule.employee_id in (None, employee.id)
                and rule.type == "MUST_WORK"
                and rule.date_start <= following <= rule.date_end
            ]
            return "必须上班", rule_ids
        if any(
            item.employee_id == employee.id and item.date == following for item in locked
        ):
            return "锁定排班", []
        all_rule = all_start.get(following)
        if all_rule:
            return f"全员 {all_rule[0]} 到岗", [all_rule[1]]
        return None

    # 计划关闭「跨午夜班次日休息」时，不再做跨夜次日的冲突预检
    if not plan.overnight_next_rest:
        return conflicts, warnings
    seen_cross: set[tuple[int, str, int | None]] = set()
    for employee_id, days in fixed_shifts.items():
        employee = employee_map.get(employee_id)
        if employee is None:
            continue
        for day, entries in days.items():
            if next_day_of(day) not in day_set:
                continue
            for shift_id, rule_id in entries:
                if _rule_is_soft(rule_id, rule_by_id):
                    continue
                shift = shift_map.get(shift_id)
                if shift is None:
                    continue
                _, end_minutes = parse_interval(shift.start_time, shift.end_time)
                if end_minutes < 1440:
                    continue
                key = (employee_id, day, shift_id)
                if key in seen_cross:
                    continue
                seen_cross.add(key)
                conflict = next_day_conflict(employee, day)
                if conflict is None:
                    continue
                description, next_rule_ids = conflict
                conflicts.append(
                    _conflict(
                        "E016",
                        f"{employee.name} {day} 的 {shift.name} 到 {next_day_of(day)} "
                        f"{shift.end_time} 结束，与次日 {description} 冲突",
                        [rule_id, *next_rule_ids],
                    )
                )

    for item in locked:
        employee = employee_map.get(item.employee_id)
        if employee is None:
            continue
        if item.shift_id is not None:
            shift = shift_map.get(item.shift_id)
            if shift is None or shift.is_leave:
                continue
            start_time = shift.start_time
            end_time = shift.end_time
            name = shift.name
            shift_key = item.shift_id
        else:
            start_time = item.start_time
            end_time = item.end_time
            name = "自定义班次"
            shift_key = None
        _, end_minutes = parse_interval(start_time, end_time)
        if end_minutes < 1440:
            continue
        key = (_eid(employee), item.date, shift_key)
        if key in seen_cross:
            continue
        seen_cross.add(key)
        conflict = next_day_conflict(employee, item.date)
        if conflict is None:
            continue
        description, next_rule_ids = conflict
        conflicts.append(
            _conflict(
                "E016",
                f"{employee.name} {item.date} 的 {name} 到 {next_day_of(item.date)} "
                f"{end_time} 结束，与次日 {description} 冲突",
                next_rule_ids,
            )
        )

    return conflicts, warnings


def solve(
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    rules: list[Rule],
    locked: list[Assignment],
    *,
    time_limit: float | None = None,
    solution_callback: Callable[[SolutionSnapshot], bool | None] | None = None,
    random_seed: int = 42,
    num_search_workers: int = 8,
    objective_mode: str = "full",
    hint_assignments: list[Assignment] | None = None,
    use_auto_hint: bool = True,
    fix_hint: bool = False,
    allow_fix_hint: bool = True,
    objective_upper_bound: float | None = None,
    solver_hook: Callable[[Any], None] | None = None,
    soft_weights: SoftWeights | None = None,
) -> SolveOutcome:
    dates = month_dates(plan.month)
    shift_map: dict[int, Shift] = {
        shift.id: shift for shift in shifts if shift.id is not None
    }
    employee_by_id = {
        employee.id: employee for employee in employees if employee.id is not None
    }
    enabled_shifts = [shift for shift in shifts if shift.enabled]
    work_shifts = [
        shift
        for shift in enabled_shifts
        if not shift.is_leave and not shift.is_off and _window_ok(plan, shift)
    ]
    normal_shifts = [shift for shift in work_shifts if not shift.is_special]
    regular_shifts = list(normal_shifts)
    special_shifts = [
        shift
        for shift in enabled_shifts
        if not shift.is_leave and not shift.is_off and shift.is_special
    ]
    leave_shift = next((shift for shift in enabled_shifts if shift.is_leave), None)

    maps = _build_rule_maps(plan, rules, shift_map, employees)
    rest_dates, soft_rest, must_work, leave_dates, off_dates, fixed_shifts, fixed_times, all_start, day_rules, segment_rules, start_rules, end_rules, shift_rules, max_rules, hours_rules, rest_limit_rules, min_rest_rules, max_work_rules, ban_shift_rules, max_work_streak_rules, shift_limit_rules, role_shift_rules, window_rules = maps
    rule_by_id = {rule.id: rule for rule in rules if rule.id is not None}
    for employee_id, days in must_work.items():
        rest_dates[employee_id].difference_update(days)
        soft_rest[employee_id].difference_update(days)

    conflicts, precheck_warnings = _precheck(plan, employees, shifts, rules, locked, maps)
    if conflicts:
        # 为每条硬约束冲突补上「谁、哪天、什么要求、硬还是软」的规则明细，
        # 让前端冲突报告能展开并定位到具体约束配置项。
        employee_map = {_eid(employee): employee for employee in employees if employee.id is not None}
        shift_names = {int(shift.id): shift.name for shift in shifts if shift.id is not None}
        rule_map = {int(rule.id): rule for rule in rules if rule.id is not None}
        for item in conflicts:
            item["rules"] = [
                _rule_detail(rule_map[rule_id], employee_map, shift_names)
                for rule_id in item.get("rule_ids", [])
                if rule_id in rule_map
            ]
        return SolveOutcome(
            status="PRECHECK_FAILED",
            message="存在无法满足的硬性条件，请先修正",
            assignments=[],
            report=_empty_report("PRECHECK_FAILED", "存在无法满足的硬性条件，请先修正", conflicts, plan),
        )

    locked_catalog: dict[tuple[int, str], Assignment] = {}
    locked_extra: dict[tuple[int, str], Assignment] = {}
    locked_leave: dict[tuple[int, str], Assignment] = {}
    leave_ids = {shift.id for shift in enabled_shifts if shift.is_leave}
    for item in locked:
        key = (item.employee_id, item.date)
        if item.shift_id is None:
            locked_extra[key] = item
        elif item.shift_id in leave_ids:
            locked_leave[key] = item
        else:
            locked_catalog[key] = item

    leave_days: dict[int, set[str]] = defaultdict(set)
    for employee_id, days in leave_dates.items():
        leave_days[employee_id].update(days)
    for employee_id, day in locked_leave:
        leave_days[employee_id].add(day)

    # 休息（OFF）日期：FIXED_SHIFT 指定 + 锁定 OFF，与休假一样不进模型，结果层补 OFF 展示
    off_ids = {shift.id for shift in enabled_shifts if shift.is_off}
    off_days: dict[int, set[str]] = defaultdict(set)
    for employee_id, days in off_dates.items():
        off_days[employee_id].update(days)
    for item in locked:
        if item.shift_id is not None and item.shift_id in off_ids:
            off_days[item.employee_id].add(item.date)

    allowed: dict[tuple[int, str], list[Shift]] = {}
    for employee in employees:
        for day in dates:
            key = (_eid(employee), day)
            if key in locked_extra:
                allowed[key] = []
                continue
            if key in locked_catalog:
                catalog_shift_id = locked_catalog[key].shift_id
                shift = (
                    shift_map.get(catalog_shift_id)
                    if catalog_shift_id is not None
                    else None
                )
                allowed[key] = [shift] if shift and shift.enabled else []
                continue
            if day in rest_dates.get(_eid(employee), set()) or day in leave_days.get(_eid(employee), set()) or day in off_days.get(_eid(employee), set()):
                allowed[key] = []
                continue
            fixed = fixed_shifts.get(_eid(employee), {}).get(day)
            if fixed:
                fixed_ids = {shift_id for shift_id, _ in fixed}
                candidates = [shift_map[shift_id] for shift_id in fixed_ids if shift_id in shift_map]
            else:
                time_rule = fixed_times.get((_eid(employee), day))
                if time_rule:
                    candidates = [shift for shift in normal_shifts if shift.start_time == time_rule[0]]
                else:
                    candidates = list(regular_shifts)
                    if employee.allow_special:
                        existing_ids = {shift.id for shift in candidates}
                        candidates.extend(
                            shift for shift in special_shifts if shift.id not in existing_ids
                        )
            if all_start.get(day) and not fixed and not fixed_times.get((_eid(employee), day)):
                start_time = all_start[day][0]
                candidates = [
                    shift for shift in normal_shifts if shift.start_time == start_time
                ]
            candidates = [
                shift for shift in candidates if _eligible(plan, employee, shift)
            ]
            # 班次限制（SHIFT_LIMIT）：只收窄可选班次，不要求当天上班；
            # 硬规则直接过滤候选，休息日/休假日本来就没有候选，自然不受影响。
            for limit_rule, limit_dates in shift_limit_rules:
                if not limit_rule.hard:
                    continue
                if day not in limit_dates:
                    continue
                if limit_rule.employee_id is not None and limit_rule.employee_id != _eid(employee):
                    continue
                candidates = [
                    shift for shift in candidates if _shift_limit_ok(shift, limit_rule)
                ]
            # 工作时段窗（WORK_WINDOW）：硬规则直接裁掉窗口外的候选班次；
            # 软规则不参与建模，仅在结果校验时提示。
            for window_rule, window_dates in window_rules:
                if not window_rule.hard or day not in window_dates:
                    continue
                if window_rule.employee_id is not None and window_rule.employee_id != _eid(employee):
                    continue
                if not _role_match(employee, window_rule.role):
                    continue
                candidates = [
                    shift for shift in candidates if _shift_within_window(shift, window_rule)
                ]
            allowed[key] = candidates

    model: _CpModel = cast(_CpModel, cast(object, cp_model.CpModel()))
    work: dict[tuple[int, str, int], cp_model.IntVar] = {}
    work_day: dict[tuple[int, str], cp_model.IntVar] = {}
    for (employee_id, day), candidates in allowed.items():
        variables = []
        for shift in candidates:
            if shift.id is None:
                continue
            variable = model.NewBoolVar(f"work_{employee_id}_{day}_{shift.id}")
            work[(employee_id, day, shift.id)] = variable
            variables.append(variable)
        if variables:
            day_var = model.NewBoolVar(f"workday_{employee_id}_{day}")
            model.Add(day_var == sum(variables))
            work_day[(employee_id, day)] = day_var
            model.Add(sum(variables) <= 1)

    def _matches_role(employee_id: int, role: str | None) -> bool:
        """人数类规则的岗位口径：未限定岗位时全员计入。"""
        return _role_match(employee_by_id.get(employee_id), role)

    # 连续上班上限：任意 limit + 1 天窗口内最多上班 limit 天
    consecutive_limit = plan.max_consecutive_days
    if consecutive_limit > 0:
        for employee in employees:
            for index in range(len(dates) - consecutive_limit):
                window = dates[index : index + consecutive_limit + 1]
                terms = [
                    work_day[(_eid(employee), day)]
                    for day in window
                    if (_eid(employee), day) in work_day
                ]
                if terms:
                    model.Add(sum(terms) <= consecutive_limit)

    # 全员统一上班时间：当天未休假且未硬性休息的员工必须上班
    for day in all_start:
        for employee in employees:
            key = (_eid(employee), day)
            if key in locked_extra:
                continue
            if day in leave_days.get(_eid(employee), set()):
                continue
            if day in rest_dates.get(_eid(employee), set()):
                continue
            day_var = work_day.get(key)
            if day_var is not None:
                model.Add(day_var == 1)

    # 目标函数统一入口：每加一项都登记归因信息，保证分项之和 = objective。
    weights = soft_weights or SoftWeights()
    objective_terms: list[Any] = []
    objective_entries: list[_ObjectiveEntry] = []

    def add_scaled(
        category: str,
        variable: Any,
        quantity: float,
        *,
        employee_id: int | None = None,
        date: str | None = None,
        shift_id: int | None = None,
        note: str = "",
    ) -> None:
        """按「违约量 × 单位权重」加入目标；变量是 0/1 触发项。"""
        coefficient = int(round(quantity * unit_weight(weights, category)))
        if coefficient == 0:
            return
        objective_terms.append(variable * coefficient)
        objective_entries.append(
            _ObjectiveEntry(
                category=category,
                coefficient=coefficient,
                quantity=quantity,
                activation=variable,
                employee_id=employee_id,
                date=date,
                shift_id=shift_id,
                note=note,
            )
        )

    def add_quantity(
        category: str,
        variable: Any,
        *,
        scale: float = 1.0,
        employee_id: int | None = None,
        date: str | None = None,
        rule_id: int | None = None,
        note: str = "",
    ) -> None:
        """松弛变量本身就是违约量，按「单位权重 × 归一化倍数」计入目标。

        scale 用于把人头缺口折算成人·小时（覆盖缺口），其它类别保持 1。
        """
        coefficient = int(round(unit_weight(weights, category) * scale))
        if coefficient == 0:
            return
        objective_terms.append(variable * coefficient)
        objective_entries.append(
            _ObjectiveEntry(
                category=category,
                coefficient=coefficient,
                quantity_var=variable,
                employee_id=employee_id,
                date=date,
                note=note if rule_id is None else f"{note}（规则 #{rule_id}）".strip(),
            )
        )

    # 软休息日为偏好项：尽量安排休息，但工时目标与覆盖需求优先
    for employee_id, days in soft_rest.items():
        for day in days:
            day_var = work_day.get((employee_id, day))
            if day_var is not None:
                add_scaled("soft_rest", day_var, 1.0, employee_id=employee_id, date=day)

    # 跨午夜班次次日不排班（计划开关关闭时不加这条硬约束）
    if plan.overnight_next_rest:
        for (employee_id, day, shift_id), variable in work.items():
            shift = shift_map[shift_id]
            _, end_minutes = parse_interval(shift.start_time, shift.end_time)
            if end_minutes < 1440:
                continue
            next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
            next_var = work_day.get((employee_id, next_day))
            if next_var is not None:
                model.Add(next_var == 0).OnlyEnforceIf(variable)
        for (employee_id, day), item in locked_extra.items():
            _, end_minutes = parse_interval(item.start_time, item.end_time)
            if end_minutes < 1440:
                continue
            next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
            next_var = work_day.get((employee_id, next_day))
            if next_var is not None:
                model.Add(next_var == 0)

    # 贴合实际排班：日工时接近 8 小时、避免过短班次、上班时间接近计划覆盖起点的常规班优先
    coverage_start_minutes = parse_hhmm(plan.coverage_start)
    coverage_begin, coverage_finish = parse_interval(plan.coverage_start, plan.coverage_end)
    # 一个人整天缺席折算的缺口小时：标准 8 小时，覆盖窗口更短时按窗口长度
    person_day_hours = min(8.0, max(1.0, (coverage_finish - coverage_begin) / 60.0))
    for (employee_id, day, shift_id), variable in work.items():
        shift = shift_map[shift_id]
        hours = effective_hours(shift.work_hours, shift.start_time, shift.end_time)
        # 「偏好 8 小时 / 避免过短班次」只对能上到 8 小时的员工成立：
        # 非全日制（日上限 4 小时）若仍按 8 小时口径计罚，排 4 小时班会被
        # 短班惩罚压过工时目标收益，导致求解器干脆不给他排班（工时严重不足）
        employee = employee_by_id.get(employee_id)
        daily_cap = min(8.0, float(employee.max_daily_hours)) if employee else 8.0
        short_cap = min(5.0, daily_cap)
        add_scaled(
            "hours_deviation",
            variable,
            abs(hours - daily_cap),
            employee_id=employee_id,
            date=day,
            shift_id=shift_id,
        )
        add_scaled(
            "short_shift",
            variable,
            max(0.0, short_cap - hours),
            employee_id=employee_id,
            date=day,
            shift_id=shift_id,
        )
        add_scaled(
            "long_shift",
            variable,
            1.0 if hours > 8 + 1e-6 else 0.0,
            employee_id=employee_id,
            date=day,
            shift_id=shift_id,
        )
        add_scaled(
            "start_deviation",
            variable,
            max(0, parse_hhmm(shift.start_time) - coverage_start_minutes) // 30,
            employee_id=employee_id,
            date=day,
            shift_id=shift_id,
        )

    for key, item in locked_catalog.items():
        if item.shift_id is None:
            continue
        variable = work.get((key[0], key[1], item.shift_id))
        if variable is not None:
            model.Add(variable == 1)

    for employee_id, days in fixed_shifts.items():
        for day, entries in days.items():
            for shift_id, rule_id in entries:
                variable = work.get((employee_id, day, shift_id))
                if variable is None:
                    continue
                if _rule_is_soft(rule_id, rule_by_id):
                    # 软固定班次：未排上该班次时计入目标罚分，不构成硬约束
                    missed = model.NewBoolVar(
                        f"soft_fixed_miss_{employee_id}_{day}_{shift_id}"
                    )
                    model.Add(missed + variable == 1)
                    add_scaled(
                        "soft_rest",
                        missed,
                        1.0,
                        employee_id=employee_id,
                        date=day,
                        shift_id=shift_id,
                        note=f"软固定班次未排上（规则 #{rule_id}）",
                    )
                else:
                    model.Add(variable == 1)

    for employee_id, day in fixed_times:
        variables = [
            variable
            for (work_employee, work_date, _), variable in work.items()
            if work_employee == employee_id and work_date == day
        ]
        if variables:
            model.Add(sum(variables) == 1)

    for employee_id, days in must_work.items():
        for day in days:
            day_var = work_day.get((employee_id, day))
            if day_var is not None:
                model.Add(day_var == 1)

    # 班次池中存在 8 小时班时优先选用，降低「每天尽量 8 小时」的偏差
    for (employee_id, day, shift_id), variable in work.items():
        shift = shift_map.get(shift_id)
        if shift is None or shift.is_off or shift.is_leave:
            continue
        if abs(effective_hours(shift.work_hours, shift.start_time, shift.end_time) - 8) <= 0.01:
            add_scaled(
                "eight_hour_bonus",
                variable,
                -1.0,
                employee_id=employee_id,
                date=day,
                shift_id=shift_id,
            )

    # 所有软约束放宽变量：求解后汇总为可行解质量指标
    soft_slack_vars: list[Any] = []
    rule_literals: dict[int, cp_model.IntVar] = {}
    work_by_day: dict[str, list[tuple[int, int, cp_model.IntVar]]] = defaultdict(list)
    for (employee_id, day, shift_id), variable in work.items():
        work_by_day[day].append((employee_id, shift_id, variable))

    def rule_literal(rule_id: int | None) -> cp_model.IntVar:
        # 求解前所有规则都已落库，id 不应为空；此处收窄类型并做运行期兜底
        assert rule_id is not None, "规则必须已落库后才能参与求解"
        literal = rule_literals.get(rule_id)
        if literal is None:
            literal = model.NewBoolVar(f"hard_rule_{rule_id}")
            rule_literals[rule_id] = literal
            model.AddAssumption(literal)
        return literal

    # 只给天数的有薪假：在区间内选 N 天（配额变量）
    leave_quota_vars: list[tuple[int, list[tuple[str, cp_model.IntVar]]]] = []
    for rule in rules:
        if rule.type != "PAID_LEAVE" or rule.employee_id is None or not rule.count:
            continue
        quota_dates = _expand(plan.month, rule)
        if int(rule.count) >= len(quota_dates):
            continue
        blocked = set(must_work.get(rule.employee_id, set()))
        blocked.update(fixed_shifts.get(rule.employee_id, {}).keys())
        blocked.update(day for (eid, day) in fixed_times if eid == rule.employee_id)
        blocked.update(leave_days.get(rule.employee_id, set()))
        quota_vars: list[tuple[str, cp_model.IntVar]] = []
        for day in quota_dates:
            if day in blocked:
                continue
            leave_var = model.NewBoolVar(f"leave_{rule.id}_{rule.employee_id}_{day}")
            day_var = work_day.get((rule.employee_id, day))
            if day_var is not None:
                model.Add(day_var + leave_var <= 1)
            quota_vars.append((day, leave_var))
        if not quota_vars:
            continue
        total_leave = sum(var for _, var in quota_vars)
        if rule.hard:
            model.Add(total_leave == int(rule.count)).OnlyEnforceIf(rule_literal(rule.id))
        else:
            slack = model.NewIntVar(0, int(rule.count), f"leave_slack_{rule.id}")
            model.Add(total_leave + slack >= int(rule.count))
            add_quantity("other_soft", slack, rule_id=rule.id)
            soft_slack_vars.append(slack)
        leave_quota_vars.append((rule.employee_id, quota_vars))

    def add_requirement(
        rule: Rule, day: str, terms: list[Any], offset: int, required: int, hours: float
    ) -> None:
        if required <= 0:
            return
        if rule.hard:
            model.Add(sum(terms) + offset >= required).OnlyEnforceIf(rule_literal(rule.id))
        else:
            slack = model.NewIntVar(0, required, f"slack_{rule.id}_{day}")
            model.Add(sum(terms) + offset + slack >= required)
            # 覆盖缺口按「缺口人数 × 缺口小时」累计（人·小时）
            add_quantity("coverage", slack, scale=hours, date=day.split("_")[0], rule_id=rule.id)
            soft_slack_vars.append(slack)

    for rule, rule_dates in day_rules:
        for day in rule_dates:
            terms = [
                variable
                for (_employee_id, work_date, shift_id), variable in work.items()
                if work_date == day
                and not shift_map[shift_id].is_special
                and _matches_role(_employee_id, rule.role)
            ]
            offset = sum(
                1
                for (_employee_id, work_date), _item in locked_extra.items()
                if work_date == day and _matches_role(_employee_id, rule.role)
            )
            add_requirement(rule, day, terms, offset, rule.count or 0, person_day_hours)

    # 人数上限：普通班次 + 普通锁定班次 ≤ count；OFF / POFF / 特殊班次不计入
    for rule, rule_dates in max_rules:
        limit = rule.count or 0
        for day in rule_dates:
            terms = [
                variable
                for (_employee_id, work_date, shift_id), variable in work.items()
                if work_date == day
                and not shift_map[shift_id].is_special
                and _matches_role(_employee_id, rule.role)
            ]
            offset = sum(
                1
                for (_employee_id, work_date), _item in locked_extra.items()
                if work_date == day and _matches_role(_employee_id, rule.role)
            )
            if rule.hard:
                model.Add(sum(terms) + offset <= limit).OnlyEnforceIf(rule_literal(rule.id))
            else:
                # 软上限：超出部分作为惩罚项进入目标函数，不阻塞求解
                over = model.NewIntVar(0, len(employees), f"max_over_{rule.id}_{day}")
                model.Add(sum(terms) + offset <= limit + over)
                add_quantity("overstaff", over, date=day, rule_id=rule.id)
                soft_slack_vars.append(over)

    # 上班/下班时刻人数：按 time_op 圈定「到岗/离岗班次集合」，再按 count_op 约束人数
    # count_op=ge：该集合人数 ≥ count（现状）；count_op=le：该集合人数 ≤ count（至多 N 人）
    for rule, rule_dates in list(start_rules) + list(end_rules):
        by_end = rule.type == "END_STAFF"
        target_time = rule.end_time if by_end else rule.start_time
        for day in rule_dates:
            terms = [
                variable
                for (_employee_id, work_date, shift_id), variable in work.items()
                if work_date == day
                and not shift_map[shift_id].is_special
                and _matches_role(_employee_id, rule.role)
                and _time_matches(
                    shift_map[shift_id].end_time if by_end else shift_map[shift_id].start_time,
                    target_time,
                    rule.time_op,
                )
            ]
            offset = sum(
                1
                for (_employee_id, work_date), item in locked_extra.items()
                if work_date == day
                and _matches_role(_employee_id, rule.role)
                and _time_matches(
                    item.end_time if by_end else item.start_time,
                    target_time,
                    rule.time_op,
                )
            )
            if (rule.count_op or "ge") == "le":
                limit = rule.count or 0
                if rule.hard:
                    model.Add(sum(terms) + offset <= limit).OnlyEnforceIf(rule_literal(rule.id))
                else:
                    over = model.NewIntVar(0, len(employees), f"start_max_over_{rule.id}_{day}")
                    model.Add(sum(terms) + offset <= limit + over)
                    add_quantity("overstaff", over, date=day, rule_id=rule.id)
                    soft_slack_vars.append(over)
            else:
                add_requirement(rule, day, terms, offset, rule.count or 0, person_day_hours)

    for rule, rule_dates in segment_rules:
        if not rule.start_time or not rule.end_time:
            continue
        intervals = [
            (shift.start_time, shift.end_time)
            for shift in work_shifts
            if not shift.is_special
        ]
        slices = coverage_slices(intervals, rule.start_time, rule.end_time)
        for day in rule_dates:
            for left, right in slices:
                terms = [
                    variable
                    for employee_id, shift_id, variable in work_by_day.get(day, [])
                    if not shift_map[shift_id].is_special
                    and _matches_role(employee_id, rule.role)
                    and _covers(
                        shift_map[shift_id].start_time,
                        shift_map[shift_id].end_time,
                        left,
                        right,
                    )
                ]
                offset = sum(
                    1
                    for (employee_id, work_date), item in locked_extra.items()
                    if work_date == day
                    and _matches_role(employee_id, rule.role)
                    and _covers(item.start_time, item.end_time, left, right)
                )
                add_requirement(
                    rule, f"{day}_{left}", terms, offset, rule.count or 0, (right - left) / 60.0
                )

    for rule, rule_dates in shift_rules:
        for day in rule_dates:
            terms = [
                variable
                for (_employee_id, work_date, shift_id), variable in work.items()
                if work_date == day
                and shift_id == rule.shift_id
                and _matches_role(_employee_id, rule.role)
            ]
            offset = sum(
                1
                for (employee_id, work_date), item in locked_extra.items()
                if work_date == day
                and item.shift_id == rule.shift_id
                and _matches_role(employee_id, rule.role)
            )
            add_requirement(rule, day, terms, offset, rule.count or 0, person_day_hours)

    # 岗位单人班次（SINGLE_ROLE_SHIFT）：该岗位当天上班人数 ≤ count 时，这些人都必须上指定班次。
    # above = 「当天该岗位上班人数 > count」；above=0 时要求该岗位上的人全部排入指定班次。
    role_members: dict[str, list[int]] = defaultdict(list)
    for employee in employees:
        role_name = (employee.role or "").strip()
        if role_name:
            role_members[role_name].append(_eid(employee))

    for rule, rule_dates in role_shift_rules:
        if rule.shift_id is None:
            continue
        member_ids = role_members.get((rule.role or "").strip(), [])
        if not member_ids:
            continue
        threshold = rule.count or 1
        for day in rule_dates:
            total_terms = [
                work_day[(member_id, day)]
                for member_id in member_ids
                if (member_id, day) in work_day
            ]
            if not total_terms:
                continue
            total = sum(total_terms)
            target_terms = [
                work[(member_id, day, rule.shift_id)]
                for member_id in member_ids
                if (member_id, day, rule.shift_id) in work
            ]
            target_sum = sum(target_terms) if target_terms else 0
            above = model.NewBoolVar(f"role_shift_above_{rule.id}_{day}")
            model.Add(total >= (threshold + 1) * above)
            span = len(member_ids)
            if rule.hard:
                model.Add(target_sum >= total - span * above).OnlyEnforceIf(
                    rule_literal(rule.id)
                )
            else:
                slack = model.NewIntVar(0, span, f"role_shift_slack_{rule.id}_{day}")
                model.Add(target_sum + slack >= total - span * above)
                add_quantity("other_soft", slack, date=day, rule_id=rule.id)
                soft_slack_vars.append(slack)

    # 班次工时条件（SHIFT_HOURS）：不满足工时的班次禁止排入（硬）或计入惩罚（软）
    hours_by_shift = {
        shift_id: _shift_hours(shift) for shift_id, shift in shift_map.items()
    }
    work_by_employee_day: dict[tuple[int, str], list[tuple[int, cp_model.IntVar]]] = defaultdict(list)
    for (employee_id, work_date, shift_id), variable in work.items():
        work_by_employee_day[(employee_id, work_date)].append((shift_id, variable))

    for rule, rule_dates in hours_rules:
        target = rule.work_hours or 0.0
        op = rule.hours_op or "eq"
        for day in rule_dates:
            for employee in employees:
                if rule.employee_id is not None and rule.employee_id != employee.id:
                    continue
                entries = work_by_employee_day.get((_eid(employee), day), [])
                if not entries:
                    continue
                allowed_ids = {
                    shift_id
                    for shift_id, _ in entries
                    if _hours_match(hours_by_shift[shift_id], target, op)
                }
                if rule.hard:
                    literal = rule_literal(rule.id)
                    for shift_id, variable in entries:
                        if shift_id not in allowed_ids:
                            model.Add(variable == 0).OnlyEnforceIf(literal)
                    continue
                total_expr = sum(variable for _, variable in entries)
                allowed_expr = sum(
                    variable for shift_id, variable in entries if shift_id in allowed_ids
                )
                slack = model.NewIntVar(0, 1, f"hours_slack_{rule.id}_{employee.id}_{day}")
                model.Add(slack >= total_expr - allowed_expr)
                add_quantity(
                    "other_soft",
                    slack,
                    employee_id=employee.id,
                    date=day,
                    rule_id=rule.id,
                )
                soft_slack_vars.append(slack)

    # 连续休息上限（MAX_REST_DAYS）：任意 count+1 天窗口内至少上 1 天班。
    # 带薪假（PAID_LEAVE）视为中性：既不要求上班，也不消耗连休额度。
    # 判断窗口时把假期从日期序列中扣除，假期两侧的休息日在有效序列里相邻。
    # 没有带薪假时窗口与原来完全一致，求解结果不变。
    for rule, rule_dates in rest_limit_rules:
        limit = rule.count or 0
        if limit <= 0:
            continue
        for employee in employees:
            if rule.employee_id is not None and rule.employee_id != employee.id:
                continue
            leave_set = leave_days.get(_eid(employee), set())
            for window_index, window in enumerate(
                _rest_limit_windows(rule_dates, leave_set, limit)
            ):
                terms = [
                    work_day[(_eid(employee), day)]
                    for day in window
                    if (_eid(employee), day) in work_day
                ]
                if not terms:
                    continue
                if rule.hard:
                    model.Add(sum(terms) >= 1).OnlyEnforceIf(rule_literal(rule.id))
                else:
                    slack = model.NewIntVar(
                        0,
                        1,
                        f"rest_slack_{rule.id}_{employee.id}_{window_index}",
                    )
                    model.Add(sum(terms) + slack >= 1)
                    add_quantity(
                        "other_soft",
                        slack,
                        employee_id=employee.id,
                        rule_id=rule.id,
                        note=f"连休窗口 {window_index}",
                    )
                    soft_slack_vars.append(slack)

    # 员工/日期细分的连续上班上限（MAX_CONSECUTIVE_WORK）：
    # 任意 count+1 天窗口内上班天数不超过 count。
    # 与计划级 max_consecutive_days 是两条独立约束、同时生效，等价于取更严格值。
    for rule, rule_dates in max_work_streak_rules:
        limit = rule.count or 0
        if limit <= 0:
            continue
        for employee in employees:
            if rule.employee_id is not None and rule.employee_id != employee.id:
                continue
            for window_index, window in enumerate(
                _rest_limit_windows(rule_dates, set(), limit)
            ):
                terms = [
                    work_day[(_eid(employee), day)]
                    for day in window
                    if (_eid(employee), day) in work_day
                ]
                if not terms:
                    continue
                if rule.hard:
                    model.Add(sum(terms) <= limit).OnlyEnforceIf(rule_literal(rule.id))
                else:
                    over = model.NewIntVar(
                        0,
                        len(window),
                        f"work_streak_over_{rule.id}_{employee.id}_{window_index}",
                    )
                    model.Add(sum(terms) <= limit + over)
                    add_quantity(
                        "other_soft",
                        over,
                        employee_id=employee.id,
                        rule_id=rule.id,
                        note=f"连续上班窗口 {window_index}",
                    )
                    soft_slack_vars.append(over)

    # 周期天数类（MIN_REST_DAYS / MAX_WORK_DAYS）：按「员工 × 周期」统计上班天数。
    # 周期为 month 时整段区间一个桶，week 时按自然周（周一~周日）切段。
    # 首尾不完整的自然周按残周口径折算：至少休息用 floor，最多上班用
    # min(count, 可见天数)；完整 7 天桶结果不变，口径与 validator / rule_checker 共用。
    for rule, rule_dates in min_rest_rules + max_work_rules:
        buckets = period_buckets(rule.period, rule_dates)
        if not buckets:
            continue
        for employee in employees:
            if rule.employee_id is not None and rule.employee_id != employee.id:
                continue
            for index, bucket in enumerate(buckets):
                terms = [
                    work_day[(_eid(employee), day)]
                    for day in bucket
                    if (_eid(employee), day) in work_day
                ]
                if not terms:
                    continue
                bucket_days = len(bucket)
                quota = rule.count or 0
                if (rule.period or "month") == "week":
                    quota = weekly_quota(
                        quota,
                        plan.month,
                        bucket,
                        is_limit=rule.type == "MAX_WORK_DAYS",
                    )
                if rule.type == "MIN_REST_DAYS":
                    # 上班天数 ≤ 周期天数 - 最少休息天数；不足时该周放宽为全休
                    limit = max(0, bucket_days - quota)
                else:
                    limit = quota
                if limit >= bucket_days:
                    continue
                if rule.hard:
                    model.Add(sum(terms) <= limit).OnlyEnforceIf(rule_literal(rule.id))
                else:
                    over = model.NewIntVar(
                        0, bucket_days, f"days_over_{rule.id}_{employee.id}_{index}"
                    )
                    model.Add(sum(terms) <= limit + over)
                    add_quantity(
                        "other_soft",
                        over,
                        employee_id=employee.id,
                        rule_id=rule.id,
                        note=f"周期 {index}",
                    )
                    soft_slack_vars.append(over)

    # 禁止班次（BAN_SHIFT）：对应员工（或全员）在区间内不得被排入该班次
    for rule, rule_dates in ban_shift_rules:
        if rule.shift_id is None:
            continue
        for day in rule_dates:
            for employee in employees:
                if rule.employee_id is not None and rule.employee_id != employee.id:
                    continue
                variable = work.get((_eid(employee), day, rule.shift_id))
                if variable is None:
                    continue
                if rule.hard:
                    model.Add(variable == 0).OnlyEnforceIf(rule_literal(rule.id))
                else:
                    slack = model.NewIntVar(
                        0, 1, f"ban_slack_{rule.id}_{employee.id}_{day}"
                    )
                    model.Add(slack >= variable)
                    add_quantity(
                        "other_soft",
                        slack,
                        employee_id=employee.id,
                        date=day,
                        rule_id=rule.id,
                    )
                    soft_slack_vars.append(slack)

    # 班次限制（SHIFT_LIMIT）软规则：排入不符合限制的班次时计入惩罚
    for rule, rule_dates in shift_limit_rules:
        if rule.hard:
            continue
        for day in rule_dates:
            for employee in employees:
                if rule.employee_id is not None and rule.employee_id != employee.id:
                    continue
                entries = work_by_employee_day.get((_eid(employee), day), [])
                violating = [
                    variable
                    for shift_id, variable in entries
                    if not _shift_limit_ok(shift_map[shift_id], rule)
                ]
                if not violating:
                    continue
                slack = model.NewIntVar(
                    0, 1, f"shift_limit_slack_{rule.id}_{employee.id}_{day}"
                )
                model.Add(slack >= sum(violating))
                add_quantity(
                    "other_soft",
                    slack,
                    employee_id=employee.id,
                    date=day,
                    rule_id=rule.id,
                )
                soft_slack_vars.append(slack)

    extra_units: dict[int, int] = defaultdict(int)
    for (employee_id, day), item in locked_extra.items():
        extra_units[employee_id] += effective_units(None, item.start_time, item.end_time)

    if leave_shift is not None:
        leave_units = effective_units(
            leave_shift.work_hours, leave_shift.start_time, leave_shift.end_time
        )
        for employee_id, days in leave_days.items():
            extra_units[employee_id] += leave_units * len(days)

    warnings: list[Conflict] = []
    hours_conflicts: list[Conflict] = []
    for employee in employees:
        target_hours, monthly_rule = monthly_hours_target(employee, rules, plan.month)
        if target_hours <= 0:
            continue
        if monthly_rule is not None and float(employee.monthly_hours or 0) != target_hours:
            warnings.append(
                _conflict(
                    "P014",
                    f"{employee.name}：MONTHLY_HOURS 规则目标 {target_hours:g} 小时优先于"
                    f"员工档案 {float(employee.monthly_hours or 0):g} 小时，"
                    f"以规则 {target_hours:g} 小时为准（员工档案为 "
                    f"{float(employee.monthly_hours or 0):g} 小时）",
                    [monthly_rule.id],
                )
            )
        employee_vars = [
            (variable, shift_map[shift_id])
            for (employee_id, _day, shift_id), variable in work.items()
            if employee_id == employee.id
        ]
        day_units: dict[str, set[int]] = defaultdict(set)
        for (employee_id, day), candidates in allowed.items():
            if employee_id != employee.id:
                continue
            day_units[day] = {
                effective_units(shift.work_hours, shift.start_time, shift.end_time)
                for shift in candidates
            }
        target = int(round(target_hours * UNIT_PER_HOUR))
        if plan.max_consecutive_days > 0 and day_units:
            limit = plan.max_consecutive_days
            max_workdays = len(dates) - len(dates) // (limit + 1)
            ranked_days = sorted(
                day_units.items(),
                key=lambda item: (max(item[1]) if item[1] else -1, item[0]),
                reverse=True,
            )
            day_units = dict(ranked_days[:max_workdays])
        mask = _achievable_mask(extra_units[_eid(employee)], day_units)
        best = _nearest_total(mask, target)
        quota_units = 0
        if leave_shift is not None and leave_quota_vars:
            quota_leave_units = effective_units(
                leave_shift.work_hours, leave_shift.start_time, leave_shift.end_time
            )
            quota_units = quota_leave_units * sum(
                variable
                for quota_employee, quota_vars in leave_quota_vars
                if quota_employee == employee.id
                for _, variable in quota_vars
            )
        total = extra_units[_eid(employee)] + quota_units + sum(
            variable * effective_units(shift.work_hours, shift.start_time, shift.end_time)
            for variable, shift in employee_vars
        )

        # 显式 MONTHLY_HOURS 硬规则、或计划开启「月工时硬约束」时，按精确等值建模；
        # 否则员工档案月工时为软目标，低于/超出目标都按小时计入目标函数，不阻塞求解。
        if (monthly_rule is not None and monthly_rule.hard) or plan.monthly_hours_hard:
            cap_text = (
                f"、连续上班上限 {plan.max_consecutive_days} 天"
                if plan.max_consecutive_days > 0
                else ""
            )
            if best != target:
                if best < target:
                    message = (
                        f"{employee.name} 按现有休息与每日上限{cap_text}可达 "
                        f"{units_to_hours(best):.2f} 小时，目标 {units_to_hours(target):.2f} 小时，"
                        f"缺口 {units_to_hours(target - best):.2f} 小时"
                    )
                else:
                    message = (
                        f"{employee.name} 按现有休息与每日上限{cap_text}可达 "
                        f"{units_to_hours(best):.2f} 小时，目标 {units_to_hours(target):.2f} 小时，"
                        f"超出 {units_to_hours(best - target):.2f} 小时"
                    )
                hours_conflicts.append(_conflict("E020", message))
                continue
            model.Add(total == target)
        else:
            # 软月工时（员工档案或软 MONTHLY_HOURS 规则）：低于/超出目标都按小时惩罚。
            # 只惩罚缺口会诱导无上限地多排 8h 班来赚取 8 小时班奖励；
            # 双向偏差让排班围绕目标分布，同时允许为休息偏好/覆盖需求适度让步。
            max_total = extra_units[_eid(employee)] + sum(
                effective_units(shift.work_hours, shift.start_time, shift.end_time)
                for _variable, shift in employee_vars
            )
            deviation_ub = (
                max(max_total, target) + UNIT_PER_HOUR - 1
            ) // UNIT_PER_HOUR
            deviation = model.NewIntVar(
                0, deviation_ub, f"monthly_deviation_{employee.id}"
            )
            model.Add(deviation * UNIT_PER_HOUR >= total - target)
            model.Add(deviation * UNIT_PER_HOUR >= target - total)
            add_quantity(
                "monthly_deviation",
                deviation,
                employee_id=_eid(employee),
                note="月工时偏离目标",
            )

    if hours_conflicts:
        return SolveOutcome(
            status="INFEASIBLE",
            message="未找到满足硬约束的排班",
            assignments=[],
            report=_empty_report("INFEASIBLE", "未找到满足硬约束的排班", hours_conflicts, plan),
        )

    for employee in employees:
        for index in range(len(dates) - 1):
            first = work_day.get((_eid(employee), dates[index]))
            second = work_day.get((_eid(employee), dates[index + 1]))
            if first is None or second is None:
                continue
            pair = model.NewBoolVar(f"pair_{employee.id}_{index}")
            model.Add(pair <= first)
            model.Add(pair <= second)
            model.Add(pair >= first + second - 1)
            add_scaled(
                "consecutive_pair",
                pair,
                1.0,
                employee_id=employee.id,
                date=dates[index],
            )

    # 可行解热启动。
    # 段 B 优先使用段 A 的解做完整目标下的初始解；否则用「休息优先 + 常规班次」
    # 构造提示解，引导搜索快速进入可行域。提示不完全满足硬约束也没关系
    # （CP-SAT 会自动修复），目标是缩短找可行解的时间。
    hint_map: dict[tuple[int, str], int | None] = {}
    if hint_assignments:
        for item in hint_assignments:
            hint_map[(item.employee_id, item.date)] = item.shift_id

    objective_expr = sum(objective_terms)
    # 段 A（只求可行）：不加任何软目标，CP-SAT 找到首个硬约束可行解即停止。
    # 软约束在建模时都带松弛变量，因此不最小化它们不会改变可行性。
    feasibility_only = objective_mode == "feasibility"
    # 修复模式：以提示解（通常是软工时可行解）为基准，只最小化「改动量」，
    # 先把硬月工时等强约束对齐，质量优化留给后续阶段。改动量按员工-日期计，
    # 提示当天休息/休假而实际排了班同样计为一次改动。
    repair_only = objective_mode == "repair"
    if repair_only:
        repair_terms: list[Any] = []
        variables_by_day: dict[tuple[int, str], list[tuple[int, Any]]] = defaultdict(list)
        for (employee_id, day, shift_id), variable in work.items():
            variables_by_day[(employee_id, day)].append((shift_id, variable))
        for (employee_id, day), hinted_shift in hint_map.items():
            variables = variables_by_day.get((employee_id, day))
            if not variables:
                continue
            hinted_var = next(
                (variable for shift_id, variable in variables if shift_id == hinted_shift),
                None,
            )
            if hinted_var is not None:
                repair_terms.append(1 - hinted_var)
            else:
                repair_terms.append(sum(variable for _, variable in variables))
        if repair_terms:
            model.Minimize(sum(repair_terms))
    elif not feasibility_only:
        model.Minimize(objective_expr)
        # 段 B 从「已验证可行的初始解」出发时，把它的真实目标值作为上界：
        # 已知存在目标 ≤ 上界的解，因此这条约束不会让模型变得不可行，
        # 却能让 CP-SAT 只搜比初始解更优的区域（提示解引导下先找到更差解）
        if objective_upper_bound is not None:
            model.Add(objective_expr <= int(round(float(objective_upper_bound))))

    if hint_map:
        for (employee_id, day, shift_id), variable in work.items():
            model.AddHint(variable, 1 if hint_map.get((employee_id, day)) == shift_id else 0)
    elif use_auto_hint:
        hinted: set[tuple[int, str]] = set()
        for (employee_id, day, shift_id), variable in work.items():
            shift = shift_map[shift_id]
            rest_day = (
                day in rest_dates.get(employee_id, set())
                or day in leave_days.get(employee_id, set())
                or day in off_days.get(employee_id, set())
            )
            hint_value = 0
            if not rest_day and not shift.is_special:
                if (employee_id, day) not in hinted:
                    hint_value = 1
                    hinted.add((employee_id, day))
            model.AddHint(variable, hint_value)
        if not feasibility_only:
            for variable in soft_slack_vars:
                model.AddHint(variable, 0)

    def build_assignments(value_of: Callable[[Any], int]) -> list[Assignment]:
        """把当前解的变量取值转成 Assignment 列表（回调与最终结果共用）。"""
        assignments: list[Assignment] = []
        for (employee_id, day, shift_id), variable in work.items():
            if value_of(variable) != 1:
                continue
            shift = shift_map[shift_id]
            locked_item = locked_catalog.get((employee_id, day))
            assignments.append(
                Assignment(
                    plan_id=_pid(plan),
                    employee_id=employee_id,
                    date=day,
                    shift_id=shift_id,
                    start_time=shift.start_time,
                    end_time=shift.end_time,
                    hours=units_to_hours(
                        effective_units(shift.work_hours, shift.start_time, shift.end_time)
                    ),
                    locked=locked_item is not None,
                    source=locked_item.source if locked_item else "solver",
                )
            )
        for (employee_id, day), item in locked_extra.items():
            assignments.append(
                Assignment(
                    plan_id=_pid(plan),
                    employee_id=employee_id,
                    date=day,
                    shift_id=None,
                    start_time=item.start_time,
                    end_time=item.end_time,
                    hours=units_to_hours(effective_units(None, item.start_time, item.end_time)),
                    locked=True,
                    source=item.source,
                )
            )
        if leave_shift is not None:
            leave_units = effective_units(
                leave_shift.work_hours, leave_shift.start_time, leave_shift.end_time
            )
            leave_hours = units_to_hours(leave_units)
            for employee_id, days in leave_days.items():
                for day in sorted(days):
                    locked_item = locked_leave.get((employee_id, day))
                    assignments.append(
                        Assignment(
                            plan_id=_pid(plan),
                            employee_id=employee_id,
                            date=day,
                            shift_id=leave_shift.id,
                            start_time=leave_shift.start_time,
                            end_time=leave_shift.end_time,
                            hours=leave_hours,
                            locked=locked_item is not None,
                            source=locked_item.source if locked_item else "solver",
                        )
                    )
            # 只给天数的有薪假：按求解器选中的日期补 POFF
            for employee_id, quota_vars in leave_quota_vars:
                for day, leave_variable in quota_vars:
                    if value_of(leave_variable) != 1:
                        continue
                    assignments.append(
                        Assignment(
                            plan_id=_pid(plan),
                            employee_id=employee_id,
                            date=day,
                            shift_id=leave_shift.id,
                            start_time=leave_shift.start_time,
                            end_time=leave_shift.end_time,
                            hours=leave_hours,
                            locked=False,
                            source="solver",
                        )
                    )
        # 休息（OFF）展示补全：当天无出勤（含规则指定休息、硬休息、未排上班）的日期，结果层补 OFF（0 工时）
        off_shift = next((shift for shift in enabled_shifts if shift.is_off), None)
        if off_shift is not None:
            assigned_days: dict[int, set[str]] = defaultdict(set)
            for item in assignments:
                assigned_days[item.employee_id].add(item.date)
            for employee in employees:
                for day in dates:
                    key = (_eid(employee), day)
                    if key in locked_extra or key in locked_catalog:
                        continue
                    if day in leave_days.get(_eid(employee), set()):
                        continue
                    if day in assigned_days.get(_eid(employee), set()):
                        continue
                    assignments.append(
                        Assignment(
                            plan_id=_pid(plan),
                            employee_id=_eid(employee),
                            date=day,
                            shift_id=off_shift.id,
                            start_time=off_shift.start_time,
                            end_time=off_shift.end_time,
                            hours=0.0,
                            locked=False,
                            source="solver",
                        )
                    )
        order = {employee.id: index for index, employee in enumerate(employees)}
        assignments.sort(key=lambda item: (order.get(item.employee_id, 0), item.date))
        return assignments

    employee_names = {_eid(employee): employee.name for employee in employees}
    shift_names = {
        int(shift.id): shift.name for shift in shifts if shift.id is not None
    }
    weight_meta = {item["field"]: item for item in weight_items(weights)}

    def build_attribution(value_of: Callable[[Any], int]) -> dict[str, Any]:
        """按求解器目标项逐项归因，分项贡献之和严格等于 objective。

        违约量 = 贡献 / 单位权重，因此「数量 × 单位权重 = 贡献」在不做二次
        取整的情况下成立（目标系数为整数，小时数会按四舍五入折算）。
        """
        rows: dict[str, dict[str, Any]] = {}
        for entry in objective_entries:
            if entry.quantity_var is not None:
                contribution = float(value_of(entry.quantity_var)) * entry.coefficient
            elif entry.activation is not None:
                contribution = float(value_of(entry.activation)) * entry.coefficient
            else:
                continue
            if contribution == 0:
                continue
            meta = weight_meta[entry.category]
            unit = float(meta["value"])
            row = rows.setdefault(
                entry.category,
                {
                    "field": entry.category,
                    "key": meta["key"],
                    "label": meta["label"],
                    "unit": meta["unit"],
                    "priority": meta["priority"],
                    "unit_weight": unit,
                    "quantity": 0.0,
                    "contribution": 0.0,
                    "details": [],
                },
            )
            quantity = contribution / unit if unit else 0.0
            row["quantity"] += quantity
            row["contribution"] += contribution
            row["details"].append(
                {
                    "employee_id": entry.employee_id,
                    "employee_name": employee_names.get(
                        entry.employee_id, f"#{entry.employee_id}"
                    )
                    if entry.employee_id is not None
                    else None,
                    "date": entry.date,
                    "shift_id": entry.shift_id,
                    "shift_name": shift_names.get(entry.shift_id)
                    if entry.shift_id is not None
                    else None,
                    "quantity": quantity,
                    "contribution": contribution,
                    "note": entry.note,
                }
            )

        items: list[dict[str, Any]] = []
        for row in rows.values():
            details = row["details"]
            details.sort(
                key=lambda item: (
                    item["date"] or "",
                    item["employee_id"] or 0,
                    item["shift_id"] or 0,
                )
            )
            items.append(row)
        items.sort(key=lambda row: (row["priority"], -row["contribution"]))
        total = sum(row["contribution"] for row in items)
        objective_value = int(round(total))
        for row in items:
            row["share"] = (
                round(row["contribution"] / objective_value, 4)
                if objective_value
                else 0.0
            )
        return {
            "scheme": scheme_name(weights),
            "weight_scheme": weight_items(weights),
            "items": items,
            "objective": objective_value,
            "total": objective_value,
            "difference": 0,
            "note": "分项贡献由求解器目标项逐项累加，合计等于 objective；"
            "违约量 = 贡献 / 单位权重（目标系数取整会造成不足 1 分的显示误差）。",
            "hard_constraint_note": "硬约束不进入目标函数，权重视为无穷大，"
            "即使软权重全部降为 0 也不会产生硬违反。",
        }

    last_snapshot: SolutionSnapshot | None = None

    def _sink(snapshot: SolutionSnapshot) -> bool | None:
        nonlocal last_snapshot
        last_snapshot = snapshot
        if solution_callback is None:
            return None
        return solution_callback(snapshot)

    solver = cp_model.CpSolver()
    if solver_hook is not None:
        # 暴露求解器实例，让取消操作能立刻 StopSearch，而不是等下一次解回报
        solver_hook(solver)
    effective_limit = (
        float(plan.solve_seconds) if time_limit is None else float(time_limit)
    )
    solver.parameters.max_time_in_seconds = effective_limit
    solver.parameters.num_search_workers = num_search_workers
    solver.parameters.random_seed = random_seed
    # 段 A 复用上一轮可行解时，把提示的排班变量固定在提示值上：
    # 只要这些硬约束仍然成立，presolve 会立刻证明可行，不必重新搜索。
    # allow_fix_hint=False 用于「固定失败后」的重试：同一份提示解只当搜索方向
    # （AddHint 不裁剪可行域），避免过期的旧排班把搜索锁死。
    if hint_map and (fix_hint or (objective_mode == "feasibility" and allow_fix_hint)):
        solver.parameters.fix_variables_to_their_hinted_value = True
    solved_status = solver.Solve(
        cast(cp_model.CpModel, cast(object, model)),
        _SnapshotCallback(
            build_assignments,
            _sink,
            attribute=None if feasibility_only else build_attribution,
        ),
    )
    # 当前 OR-Tools 的 StatusName() 依赖旧 proto 的 status() 方法，运行期会报错；
    # Solve() 本身返回 CpSolverStatus 枚举，直接用 .name 取 "OPTIMAL"/"FEASIBLE" 等。
    status = solved_status.name

    if status in ("OPTIMAL", "FEASIBLE"):
        # 快照被节流后可能落后于真实最优解；用 solver.Value 精确重建最终赋值
        try:
            assignments = build_assignments(solver.Value)
        except Exception:  # pragma: no cover - 防御性兜底（StopSearch 后取值失败）
            assignments = last_snapshot.assignments if last_snapshot is not None else []
        report = evaluate(plan, employees, shifts, rules, assignments)
        # 预检查已按整窗口/结构问题提示过的规则，覆盖缺口不再重复报 P003
        reported_rule_ids = {
            rule_id
            for item in precheck_warnings + warnings
            if item["code"] in ("P002", "E018", "P003", "E004")
            for rule_id in item.get("rule_ids", [])
        }
        coverage_warnings = _segment_coverage_conflicts(
            employees,
            shifts,
            rules,
            assignments,
            report.get("coverage", []),
            reported_rule_ids,
        )
        # 软约束放宽总量作为可行解质量指标，供 FEASIBLE 消息展示
        slack_total = sum(solver.Value(variable) for variable in soft_slack_vars)
        report["params"] = _plan_params(plan)
        report["conflicts"] = report["conflicts"] + precheck_warnings + warnings + coverage_warnings
        if feasibility_only:
            # 段 A 只保证硬约束成立，软目标一律置 0；不能把 0 当成真实目标值
            report["status"] = "FEASIBLE"
            report["objective"] = None
            report["feasibility_only"] = True
            report["quality_optimized"] = False
            message = "已找到满足全部硬约束的可用方案，尚未进行质量优化"
            report["message"] = message
            return SolveOutcome(
                status="FEASIBLE",
                message=message,
                assignments=assignments,
                report=report,
            )
        report["status"] = "OPTIMAL" if status == "OPTIMAL" else "FEASIBLE"
        report["feasibility_only"] = False
        report["quality_optimized"] = True
        # 达到时长上限的可行解要提示「未证明最优」，否则界面无法区分两种状态
        message = "" if status == "OPTIMAL" else _feasible_message(int(round(effective_limit)), slack_total)
        report["message"] = message
        report["objective"] = int(round(solver.ObjectiveValue()))
        # 违约定向归因：分项贡献之和与 objective 对齐，供报告展示
        attribution = build_attribution(solver.Value)
        objective_value = int(report["objective"])
        attribution["objective"] = objective_value
        attribution["difference"] = objective_value - int(attribution["total"])
        report["attribution"] = attribution
        return SolveOutcome(
            status=report["status"],
            message=message,
            assignments=assignments,
            report=report,
        )

    if status == "INFEASIBLE":
        assumption_indices = solver.SufficientAssumptionsForInfeasibility()
        rule_ids = sorted(
            {
                rule_id
                for rule_id, literal in rule_literals.items()
                if literal.Index() in assumption_indices
            }
        )
        conflict_list: list[Conflict] = []
        employee_map = {
            _eid(employee): employee for employee in employees if employee.id is not None
        }
        shift_names = {
            int(shift.id): shift.name for shift in shifts if shift.id is not None
        }
        rule_map = {int(rule.id): rule for rule in rules if rule.id is not None}
        streak_rule_ids = {
            rule.id
            for rule in rules
            if rule.type == "MAX_CONSECUTIVE_WORK" and rule.id is not None
        }
        streak_hit = bool(streak_rule_ids.intersection(rule_ids))
        # 连续上班上限无法满足时给出可操作建议，而不是只报「无解」
        streak_hint = (
            "；可尝试放宽「连续上班」上限、调整请假/休息日期，或缩短规则日期范围"
            if streak_hit
            else ""
        )
        # 充分假设集只包含挂了假设的规则；计划级连续上班上限、固定班次、休息、
        # 请假等不挂假设的硬性安排也会参与冲突。文案必须说明这一点，否则用户
        # 删掉点名的规则后仍可能无解，误以为 E012 归因错误。
        plan_context: list[str] = []
        if plan.max_consecutive_days > 0:
            plan_context.append(f"连续上班上限 {plan.max_consecutive_days} 天")
        fixed_types = {
            "FIXED_SHIFT",
            "FIXED_TIME",
            "ALL_START_AT",
            "REST",
            "MUST_WORK",
            "PAID_LEAVE",
        }
        fixed_count = sum(
            1 for rule in rules if rule.valid and rule.hard and rule.type in fixed_types
        )
        if fixed_count:
            plan_context.append(f"{fixed_count} 条固定班次/休息/请假安排")
        context_note = (
            f"；同时请检查计划级设置（{'、'.join(plan_context)}）"
            if plan_context
            else ""
        )
        if rule_ids:
            # 充分假设集常常包含几十条规则，按「规则类型 + 人员」聚合后给出可读摘要，
            # 让用户先缩小排查范围，再逐条放开验证
            type_counts: dict[str, int] = defaultdict(int)
            employee_counts: dict[str, int] = defaultdict(int)
            for rule_id in rule_ids:
                rule_item = rule_map.get(rule_id)
                if rule_item is None:
                    continue
                type_counts[_RULE_TYPE_LABELS.get(rule_item.type, rule_item.type)] += 1
                owner = (
                    employee_map.get(rule_item.employee_id)
                    if rule_item.employee_id is not None
                    else None
                )
                employee_counts[owner.name if owner is not None else "全员"] += 1
            summary = "；".join(
                f"{name} {count} 条"
                for name, count in sorted(type_counts.items(), key=lambda kv: -kv[1])
            )
            item = _conflict(
                "E012",
                "下列硬约束与计划级硬性设置叠加后无法同时满足，请逐条放宽或删除后重试"
                + context_note
                + streak_hint,
                rule_ids,
            )
            item["rules"] = [
                _rule_detail(rule_map[rule_id], employee_map, shift_names)
                for rule_id in rule_ids
                if rule_id in rule_map
            ]
            item["group_summary"] = summary
            item["employee_summary"] = "、".join(
                f"{name} {count} 条"
                for name, count in sorted(employee_counts.items(), key=lambda kv: -kv[1])
            )
            item["minimal"] = True
            item["scope_note"] = (
                "以下规则来自 CP-SAT 充分假设集（足以判定不可行，未做逐条最小化验证）；"
                f"按类型：{summary}。若删除后仍无解，说明还叠加了计划级设置或固定安排，需一并调整"
            )
            conflict_list.append(item)
        if not conflict_list:
            related_ids = sorted(
                int(rule.id)
                for rule in rules
                if rule.valid and rule.hard and rule.id is not None
            )
            item = _conflict(
                "E012",
                "当前规则组合无法同时满足，请放宽约束后重试" + streak_hint
                + "；以下为相关规则集合，非最小集",
                related_ids,
            )
            item["rules"] = [
                _rule_detail(rule_map[rule_id], employee_map, shift_names)
                for rule_id in related_ids
                if rule_id in rule_map
            ]
            item["minimal"] = False
            item["scope_note"] = "未取得 CP-SAT 充分假设集，以下为相关硬规则集合，非最小集"
            conflict_list.append(item)
        diagnostic = _coverage_diagnostic(
            plan,
            employees,
            dates,
            allowed,
            rest_dates,
            leave_days,
            locked_extra,
            segment_rules,
        )
        if diagnostic is not None:
            conflict_list.append(diagnostic)
        return SolveOutcome(
            status="INFEASIBLE",
            message="未找到满足硬约束的排班",
            assignments=[],
            report=_empty_report(
                "INFEASIBLE",
                "未找到满足硬约束的排班",
                # 无解时也要保留预检查的可解释提示（如残周配额折算），
                # 否则用户只看到 E012 却不知道为什么会无解
                conflict_list + precheck_warnings + warnings,
                plan,
            ),
        )

    message = "求解超时，未得到可行解，可提高求解时长后重试"
    return SolveOutcome(
        status="UNKNOWN",
        message=message,
        assignments=[],
        report=_empty_report(
            "UNKNOWN", message, precheck_warnings + warnings, plan
        ),
    )
