"""冲突智能助手：构造上下文、调用模型、校验并规范化方案。

只负责计算与校验，不做数据库提交；写入由路由层在求解验证通过后执行。
"""

# pyright: reportAny=false, reportExplicitAny=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportImplicitStringConcatenation=false, reportUnusedFunction=false, reportUnusedParameter=false, reportRedeclaration=false, reportUnnecessaryIsInstance=false
# 说明：模型返回的 JSON 结构天然是动态的，解析层用 Any 逐字段校验；
# 这里只关闭由此产生的类型噪音，真实错误仍然保留。
import json
import re
from typing import Any, cast

from openai import APITimeoutError, OpenAI
from openai.types.chat import ChatCompletionMessageParam
from sqlmodel import Session

from ..models import Employee, Plan, Rule, Shift
from . import llm_service
from .calendar_service import TIME_PATTERN, month_bounds, parse_interval

# 允许 AI 使用的白名单动作
ACTIONS = {
    "relax_rule",
    "delete_rule",
    "set_rule_count",
    "set_rule_range",
    "update_plan",
    "update_employee",
}
# 路由与异步任务共用的错误文案
NO_KEY_MESSAGE = "尚未配置模型 API Key，请先到设置页填写"
TIMEOUT_MESSAGE = (
    "方案生成超时（模型 180 秒内未返回），可稍后重试，或先精简规则、缩小冲突范围"
)
# 带人数语义的规则类型
COUNT_RULE_TYPES = {
    "DAY_STAFF",
    "SEGMENT_STAFF",
    "START_STAFF",
    "END_STAFF",
    "SHIFT_STAFF",
    "MAX_STAFF",
    "SINGLE_ROLE_SHIFT",
}

RULE_TYPE_LABELS = {
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

SUGGESTION_PROMPT = """你是排班冲突诊断专家。你只能通过「修改现有规则参数 / 删除规则 / 软化规则 / 调整计划参数 / 调整员工参数」来解决排班冲突。

约束：
1. 不能新增规则，不能编造不存在的 rule_id 或 employee_id，所有取值必须来自用户上下文。
2. 只输出一个 JSON 对象，不要 Markdown，不要解释 JSON 之外的内容。
3. operations[].action 只能取：relax_rule、delete_rule、set_rule_count、set_rule_range、update_plan、update_employee。
   - relax_rule：把现有硬规则软化为可违反，需要 rule_id，不需要 fields。
   - delete_rule：删除现有规则，需要 rule_id。
   - set_rule_count：修改人数类规则的人数，需要 rule_id，fields 里给 count（1~99 整数；取消需求请用 delete_rule）。
   - set_rule_range：修改规则日期，需要 rule_id，fields 里给 date_start、date_end（YYYY-MM-DD）。
   - update_plan：修改计划参数，fields 里可给 max_consecutive_days（0~14）、coverage_start、coverage_end（HH:MM）。
   - update_employee：修改员工参数，需要 employee_id，fields 里可给 max_daily_hours（大于 0，不超过 12）、monthly_hours（0~240）。
   不得给出超出上述范围的建议；不要以「每日超过 12 小时」「连续上班超过 14 天」作为解决手段。
4. 生成 2 条互不相同的方案（调用方会通过 limit 指定 1 或 2 条，以用户消息末尾的条数为准），覆盖不同代价维度（例如「放宽休息 / 降低当天人数 / 提高每日上限或工时偏差」），按预计成功率从高到低排序。
5. 每条方案必须针对给定冲突：diagnosis 引用冲突码与具体日期或人员；expected 写明预计消除哪个冲突码；risk 写明对工时或其它约束的影响；无法自动执行但确实需要人工处理的（例如必须增加人手）写进 manual_hint。
6. 已在 history 中出现且失败的方案不得重复给出。
7. 每条方案必须优先使用规则级动作（relax_rule / delete_rule / set_rule_count / set_rule_range）；只有当规则级调整确实无法消除冲突时，才允许使用 update_plan / update_employee，并在 risk 中说明原因。
8. 降低人数用 set_rule_count，取值必须 ≥1；若意图是「取消该时段的人数要求」，必须改用 delete_rule 删除对应规则，不得把 count 写成 0。
9. 所有日期写 YYYY-MM-DD，时间写 HH:MM。

输出结构：
{"suggestions":[{"title":"放宽 11-03 的两人同时休息","diagnosis":"当天两名员工都被硬性休息，同时又要求 2 人上班，二者直接矛盾。","operations":[{"action":"relax_rule","rule_id":12,"employee_id":null,"fields":{"hard":false}}],"expected":"预计消除 E012，11-03 可排出 2 人。","risk":"张三当天可能需要上班，月工时增加 8 小时。","manual_hint":null}],"warnings":[]}"""

INTERPRET_PROMPT = """你正在把用户对排班冲突的修改想法解析成可执行改动。

要求：
1. 用户可以描述「改人数、改日期、改硬软、改工时条件、改星期、改班次、删掉某条约束」。
2. 可执行部分只能输出 rules（新增或替代约束，字段与常规约束解析一致）、
   updates（修改已有规则的字段）与 removals（要删除的已有规则 id）。
3. 「把某条规则改成 X」必须用 updates 表达，不要去新增一条 + 删除原规则：
   updates 只写 {"rule_id": 12, "fields": {...}, "reason": "..."}，
   fields 只能包含 date_start、date_end、hard、count、start_time、end_time、
   work_hours、hours_op、weekdays、shift_id、time_op（仅 START_STAFF）、
   period（仅 MIN_REST_DAYS / MAX_WORK_DAYS）。
4. removals 只能引用上下文中列出的真实规则 id，不能编造；只有用户明确「取消 / 删掉」时才用 removals。
   同一条规则不要同时出现在 updates 与 removals 中。
5. 无法表达的要求写进 warnings，不要强行生成规则。
6. 只输出一个 JSON 对象，结构为：
{"rules":[],"updates":[{"rule_id":12,"fields":{"count":1},"reason":"当天只需 1 人"}],"removals":[],"warnings":[]}

示例：
- 「把 11-03 的人数从 3 改成 1」→ updates 命中该条 DAY_STAFF：{"rule_id":12,"fields":{"count":1}}（不要新增+删除）
- 「把 11 月 3 号那条要求挪到 11 月 4 号」→ updates：{"rule_id":15,"fields":{"date_start":"2026-11-04","date_end":"2026-11-04"}}
- 「把这条休息规则改成可违反」→ updates：{"rule_id":8,"fields":{"hard":false}}
- 「把每天 8 小时改成至少 7 小时」→ updates：{"rule_id":9,"fields":{"work_hours":7,"hours_op":"ge"}}
- 「删掉 11-03 的人数要求」→ removals：[{"rule_id":12,"reason":"取消该时段人数要求"}]（不产生 updates）"""

# 预检模式：没有已知冲突时，让模型基于规则集合检查潜在矛盾
PRECHECK_NOTE = (
    "当前没有已检测到的冲突（预检模式）。请仅基于规则集合检查潜在矛盾："
    "例如规则之间互相冲突、人数需求超过人员总数、硬约束无法同时满足。"
    "如果确实没有问题，就返回空的方案列表，不要编造冲突。"
)

# 待添加约束（尚未保存、没有规则 id）：只作为上下文，不能被 update / removals 引用
PENDING_NOTE = (
    "本次还有尚未保存的待添加约束（没有规则 id，不能用 update / removals 引用）。"
    "要调整它们只能在 add 段重述一遍新的内容；解决冲突时优先建议调整已落库的旧规则，"
    "不要默认让用户改掉刚填的那条。"
)


def describe_pending_rules(rules: list[Rule], employees: list[Employee]) -> list[str]:
    """把待添加约束转成短描述，供提示词上下文与前端卡片共用同一口径。"""
    employee_map = {employee.id: employee for employee in employees}
    lines: list[str] = []
    for rule in rules:
        owner = employee_map[rule.employee_id].name if rule.employee_id in employee_map else "全员"
        period = (
            rule.date_start
            if rule.date_start == rule.date_end
            else f"{rule.date_start}~{rule.date_end}"
        )
        kind = RULE_TYPE_LABELS.get(rule.type, rule.type)
        detail = ""
        if rule.type == "SHIFT_HOURS" and rule.work_hours is not None:
            op = {"ge": "≥", "le": "≤"}.get(rule.hours_op or "eq", "=")
            detail = f" 工时{op}{rule.work_hours:g} 小时"
        elif rule.type == "MAX_REST_DAYS":
            detail = f" 最多连休 {rule.count or 0} 天"
        elif rule.type == "MAX_CONSECUTIVE_WORK":
            detail = f" 最多连上 {rule.count or 0} 天"
        elif rule.type in ("MIN_REST_DAYS", "MAX_WORK_DAYS"):
            scope = "每周" if (rule.period or "month") == "week" else "每月"
            verb = "至少休" if rule.type == "MIN_REST_DAYS" else "最多上"
            detail = f" {scope}{verb} {rule.count or 0} 天"
        elif rule.type == "BAN_SHIFT" and rule.shift_id is not None:
            detail = f" 禁止班次 #{rule.shift_id}"
        elif rule.count is not None:
            detail = f" {rule.count} 人"
        elif rule.start_time:
            detail = f" {rule.start_time}"
        lines.append(f"- {owner} {period} {kind}{detail}")
    return lines


def rule_label(rule: Rule, employee_map: dict[int | None, Employee]) -> str:
    """把规则渲染成人能读懂的短描述。"""
    name = employee_map[rule.employee_id].name if rule.employee_id in employee_map else "全员"
    kind = RULE_TYPE_LABELS.get(rule.type, rule.type)
    period = rule.date_start if rule.date_start == rule.date_end else f"{rule.date_start}~{rule.date_end}"
    return f"{name} {period} {kind}"


def _select_rules(rules: list[Rule], conflicts: list[dict[str, Any]]) -> tuple[list[Rule], int]:
    """规则过多时只保留与冲突相关的那部分，避免上下文过长。"""
    if len(rules) <= 120:
        return rules, 0
    hit_ids = {rule_id for item in conflicts for rule_id in item.get("rule_ids") or []}
    hit_rules = [rule for rule in rules if rule.id in hit_ids]
    related_employees = {
        rule.employee_id for rule in hit_rules if rule.employee_id is not None
    }
    kept: list[Rule] = []
    for rule in rules:
        if rule.id in hit_ids:
            kept.append(rule)
            continue
        if rule.employee_id in related_employees and any(
            hit.date_start <= rule.date_end and rule.date_start <= hit.date_end
            for hit in hit_rules
        ):
            kept.append(rule)
    return kept, len(rules) - len(kept)


# 规则逐条展示的上限，超出后聚合
MAX_RULE_LINES = 20
# 上下文软目标，仅用于测试与交付说明
MAX_CONTEXT_CHARS = 8000
# 班次目录超过该行数时改为聚合展示
MAX_SHIFT_LINES = 60
# 模型调用超时：单次（limit=1）/ 首轮（limit=2）/ 降级重试 / 其它调用方默认
SINGLE_TIMEOUT_SECONDS = 180  # 实测 61~125s，留足抖动余量
DUAL_TIMEOUT_SECONDS = 90  # 首轮超时即降级（前端已不再触发多方案）
RETRY_TIMEOUT_SECONDS = 90  # 降级为 1 条后的重试
CHAT_TIMEOUT_SECONDS = 180  # 其它调用方默认值不变
# 无冲突时段信息时最多列出的规则条数
FALLBACK_LIST_LIMIT = 10


def _relevant_windows(plan: Plan, rules: list[Rule], conflicts: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """从计划覆盖时段与冲突规则中收集需要关注的时段。"""
    windows = [(plan.coverage_start, plan.coverage_end)]
    rule_map = {rule.id: rule for rule in rules if rule.id is not None}
    for item in conflicts:
        for rule_id in item.get("rule_ids") or []:
            rule = rule_map.get(rule_id)
            if rule is not None and rule.start_time:
                windows.append((rule.start_time, rule.end_time or plan.coverage_end))
    return windows


def _shift_intersects(shift: Shift, windows: list[tuple[str, str]]) -> bool:
    """班次与任一时段有交集（跨午夜班次按分钟偏移处理）。"""
    start, end = parse_interval(shift.start_time, shift.end_time)
    for left, right in windows:
        window_start, window_end = parse_interval(left, right)
        if start < window_end and window_start < end:
            return True
    return False


def _format_number(value: float) -> str:
    """保留一位小数并去掉末尾的 .0。"""
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _format_hours(hours: list[float]) -> str:
    """工时展示：唯一时直接给值，多值时给区间与种数。"""
    values = sorted(set(hours))
    if not values:
        return "未知"
    if len(values) == 1:
        return f"{_format_number(values[0])}h"
    return (
        f"{_format_number(values[0])}–{_format_number(values[-1])}h"
        f"（共 {len(values)} 种）"
    )


def _format_shift_catalog(shifts: list[Shift], windows: list[tuple[str, str]]) -> str:
    """生成班次目录段落：小目录逐条详列，大目录按起始时刻聚合。"""
    normal_shifts = [
        shift for shift in shifts if shift.enabled and not shift.is_off and not shift.is_leave
    ]
    relevant = [shift for shift in normal_shifts if _shift_intersects(shift, windows)]
    omitted = len(normal_shifts) - len(relevant)
    lines: list[str] = []
    if not relevant:
        lines.append("（无可用班次）")
    elif len(relevant) <= MAX_SHIFT_LINES:
        lines.append("班次目录（名称 / 起止 / 工时 / 特殊 / OFF / POFF）：")
        for shift in relevant:
            hours = shift.work_hours if shift.work_hours is not None else ""
            lines.append(
                f"- {shift.name} / {shift.start_time}-{shift.end_time} / {hours} / "
                f"{'是' if shift.is_special else '否'} / {'是' if shift.is_off else '否'} / "
                f"{'是' if shift.is_leave else '否'}"
            )
    else:
        # 聚合键只保留起始时刻与是否特殊，同一时刻的不同工时合并为区间展示
        groups: dict[tuple[str, bool], list[str]] = {}
        group_hours: dict[tuple[str, bool], list[float]] = {}
        for shift in relevant:
            key = (shift.start_time, shift.is_special)
            groups.setdefault(key, []).append(shift.name)
            if shift.work_hours is not None:
                group_hours.setdefault(key, []).append(shift.work_hours)
        lines.append("班次目录（已按「起始时刻」聚合，同组班次可互换）：")
        for (start_time, is_special), names in sorted(groups.items()):
            unique_names = list(dict.fromkeys(names))
            shown = "、".join(unique_names[:3]) + ("…" if len(unique_names) > 3 else "")
            flag = "，特殊" if is_special else ""
            lines.append(
                f"- {start_time} 起（共 {len(names)} 个班次，"
                f"工时 {_format_hours(group_hours.get((start_time, is_special), []))}{flag}）：{shown}"
            )
    # OFF / POFF 不参与过滤，始终各保留一行摘要
    for shift in shifts:
        if not shift.enabled:
            continue
        if shift.is_off:
            lines.append("- OFF：休息班次（0 工时，仅展示）")
        elif shift.is_leave:
            lines.append("- POFF：有薪休假班次（8 工时，计入月工时）")
    if omitted:
        lines.append(f"（共省略 {omitted} 个与冲突时段无关的班次）")
    return "\n".join(lines)


def _rule_json(rule: Rule) -> str:
    """单条规则的紧凑 JSON；值为空字段省略，语义与 null 相同且更省字符。"""
    data = {
        "id": rule.id,
        "type": rule.type,
        "employee_id": rule.employee_id,
        "date_start": rule.date_start,
        "date_end": rule.date_end,
        "shift_id": rule.shift_id,
        "start_time": rule.start_time,
        "end_time": rule.end_time,
        "count": rule.count,
        "hard": rule.hard,
    }
    return json.dumps(
        {key: value for key, value in data.items() if value is not None},
        ensure_ascii=False,
    )


def _conflict_dates(conflicts: list[dict[str, Any]]) -> list[str]:
    """从冲突文案中提取日期（YYYY-MM-DD），可能多个。"""
    dates: list[str] = []
    for item in conflicts:
        for date in re.findall(r"\d{4}-\d{2}-\d{2}", str(item.get("message") or "")):
            if date not in dates:
                dates.append(date)
    return dates


def _conflict_windows(conflicts: list[dict[str, Any]], rule_map: dict[int, Rule]) -> list[tuple[str, str]]:
    """从冲突引用规则或冲突文案中提取冲突时段。"""
    windows: list[tuple[str, str]] = []
    for item in conflicts:
        for rule_id in item.get("rule_ids") or []:
            rule = rule_map.get(rule_id)
            if rule is not None and rule.start_time and rule.end_time:
                window = (rule.start_time, rule.end_time)
                if window not in windows:
                    windows.append(window)
        for match in re.findall(r"(\d{2}:\d{2})-(\d{2}:\d{2})", str(item.get("message") or "")):
            if match not in windows:
                windows.append(match)
    return windows


def _format_rules(
    rules: list[Rule], conflicts: list[dict[str, Any]], plan: Plan | None = None
) -> str:
    """规则段落：冲突相关规则逐条列出，其余规则按类型聚合，避免提示词过长。"""
    header = "规则（id/type/employee_id/date_start/date_end/shift_id/start_time/end_time/count/hard）："
    if len(rules) <= MAX_RULE_LINES:
        lines = [header]
        for rule in rules:
            lines.append(_rule_json(rule))
        return "\n".join(lines)

    rule_map = {rule.id: rule for rule in rules if rule.id is not None}
    listed: list[Rule] = []
    listed_ids: set[int] = set()
    limited_note: str | None = None

    def take(rule: Rule) -> None:
        assert rule.id is not None
        if len(listed) < MAX_RULE_LINES and rule.id not in listed_ids:
            listed.append(rule)
            listed_ids.add(rule.id)

    # 必列：冲突直接引用的规则，AI 只能引用这些 id
    for item in conflicts:
        for rule_id in item.get("rule_ids") or []:
            rule = rule_map.get(rule_id)
            if rule is not None:
                take(rule)
    if listed:
        # 选列：与必列规则同员工且日期区间有交集，按 id 升序补齐
        for rule in sorted(rules, key=lambda item: item.id or 0):
            if len(listed) >= MAX_RULE_LINES:
                break
            if rule.id in listed_ids or rule.employee_id is None:
                continue
            if any(
                rule.employee_id == hit.employee_id
                and rule.date_start <= hit.date_end
                and hit.date_start <= rule.date_end
                for hit in listed
            ):
                take(rule)
    else:
        # 冲突没有可引用的规则 id 时退化选列，否则模型只能改全局参数
        conflict_days = _conflict_dates(conflicts)
        # 1. 强相关：硬规则且日期覆盖冲突日期
        if conflict_days:
            for rule in sorted(rules, key=lambda item: item.id or 0):
                if rule.hard and any(
                    rule.date_start <= day <= rule.date_end for day in conflict_days
                ):
                    take(rule)
        # 2. 时段相关：只保留起始时刻落在冲突时段内的规则
        windows = _conflict_windows(conflicts, rule_map)
        if windows:
            for rule in sorted(rules, key=lambda item: item.id or 0):
                if len(listed) >= MAX_RULE_LINES:
                    break
                if rule.id in listed_ids:
                    continue
                if rule.start_time and any(
                    left <= rule.start_time <= right for left, right in windows
                ):
                    take(rule)
        else:
            # 没有冲突时段信息时只列前 10 条，避免把全部时段规则倒进来
            picked = 0
            for rule in sorted(rules, key=lambda item: item.id or 0):
                if picked >= FALLBACK_LIST_LIMIT or len(listed) >= MAX_RULE_LINES:
                    break
                if rule.id in listed_ids or not rule.start_time:
                    continue
                take(rule)
                picked += 1
            if picked:
                limited_note = f"（无冲突时段信息，仅列前 {FALLBACK_LIST_LIMIT} 条相关规则）"
        # 3. 兜底：其余硬规则
        for rule in sorted(rules, key=lambda item: item.id or 0):
            if len(listed) >= MAX_RULE_LINES:
                break
            if rule.hard:
                take(rule)

    lines = [header]
    if listed:
        lines.append("以下规则与冲突相关，可引用其 id；未列出的规则不可修改。")
        if limited_note:
            lines.append(limited_note)
    for rule in listed:
        lines.append(_rule_json(rule))
    # 聚合：其余规则不列 id，明确禁止 AI 改动
    groups: dict[tuple[str, str, str], int] = {}
    for rule in rules:
        if rule.id in listed_ids:
            continue
        key = (
            rule.type,
            str(rule.count) if rule.count is not None else "-",
            "硬" if rule.hard else "软",
        )
        groups[key] = groups.get(key, 0) + 1
    if groups:
        lines.append("其他规则（不可引用 id，AI 不得修改）：")
        for (rule_type, count, hardness), total in sorted(
            groups.items(), key=lambda item: -item[1]
        ):
            count_text = "" if count == "-" else f" {count} 人"
            lines.append(f"- {rule_type}{count_text} / {hardness} × {total}")
    lines.append(f"共 {len(rules)} 条规则，其中 {len(rules) - len(listed)} 条已聚合")
    return "\n".join(lines)


def build_conflict_context(
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    rules: list[Rule],
    report: dict[str, Any],
    history: list[dict[str, Any]],
    pending: list[str] | None = None,
) -> str:
    """生成给模型看的中文上下文，紧凑且可读。"""
    conflicts = report.get("conflicts") or []
    lines: list[str] = []
    lines.append(
        f"计划：{plan.month}，覆盖时段 {plan.coverage_start}-{plan.coverage_end}，"
        f"连续上班上限 {plan.max_consecutive_days}，"
        f"求解时长 {plan.solve_seconds} 秒。"
    )
    lines.append("人员（id / 姓名 / 月工时 / 每日上限 / 可排特殊）：")
    for employee in employees:
        lines.append(
            f"- {employee.id} / {employee.name} / {employee.monthly_hours} / "
            f"{employee.max_daily_hours} / {'是' if employee.allow_special else '否'}"
        )
    lines.append(_format_shift_catalog(shifts, _relevant_windows(plan, rules, conflicts)))
    lines.append(_format_rules(rules, conflicts, plan))
    lines.append("当前冲突：")
    for conflict in conflicts:
        lines.append(
            f"- {conflict.get('code')}：{conflict.get('message')}（规则 {conflict.get('rule_ids') or '无'}）"
        )
    coverage = report.get("coverage") or []
    if coverage:
        lines.append("覆盖缺口（前 10 条）：")
        for item in coverage[:10]:
            lines.append(
                f"- {item.get('date')} {item.get('label')} 需要 {item.get('required')} 人，"
                f"实际 {item.get('assigned')} 人，缺口 {item.get('shortfall')} 人"
            )
    hours = [row for row in report.get("hours") or [] if not row.get("matches_target")]
    if hours:
        lines.append("工时未达标：")
        for row in hours:
            lines.append(
                f"- {row.get('name')} 目标 {row.get('target')} 小时，实际 {row.get('actual')} 小时，"
                f"差值 {row.get('delta')} 小时"
            )
    if history:
        lines.append("本轮已试过并失败的方案（不要重复）：")
        for item in history:
            lines.append(f"- {item.get('title')}：{item.get('result')}")
    if pending:
        # 待添加约束没有 id，只作为上下文，模型不能用 update / removals 引用
        lines.append(PENDING_NOTE)
        lines.append("尚未保存的待添加约束（没有规则 id，不能用 update / removals 引用）：")
        lines.extend(pending)
    return "\n".join(lines)


def _chat_json(
    messages: list[ChatCompletionMessageParam],
    settings: dict[str, Any],
    timeout: int = CHAT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """单独封装模型调用，测试通过 monkeypatch 此函数避免真实网络请求。"""
    client = OpenAI(
        base_url=settings["llm_base_url"],
        api_key=settings["llm_api_key"],
        # 超时由调用方按阶段指定：首次 120 秒、降级重试 100 秒
        timeout=timeout,
        max_retries=0,
    )
    response = client.chat.completions.create(
        model=settings["llm_model"],
        messages=messages,
        temperature=0,
    )
    return llm_service.extract_json(response.choices[0].message.content or "")


def request_suggestions(
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    rules: list[Rule],
    report: dict[str, Any],
    history: list[dict[str, Any]],
    settings: dict[str, Any],
    limit: int = 1,
    precheck: bool = False,
    pending: list[str] | None = None,
) -> dict[str, Any]:
    """调用模型生成方案，并逐条校验；limit=2 首轮超时自动降级为 1 条重试。

    precheck=True 表示当前没有已知冲突，提示词改为「基于规则集合检查潜在矛盾」；
    pending 是尚未保存的待添加约束描述，只进上下文，不参与写库定位。
    """
    context = build_conflict_context(plan, employees, shifts, rules, report, history, pending)
    system_prompt = f"{SUGGESTION_PROMPT}\n\n{PRECHECK_NOTE}" if precheck else SUGGESTION_PROMPT
    if pending:
        system_prompt = f"{system_prompt}\n\n{PENDING_NOTE}"

    def build_messages_for(count: int) -> list[ChatCompletionMessageParam]:
        # 条数约束写进 user 消息，降级时必须重新声明
        return [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"{context}\n\n本次只输出 {count} 条方案，不要输出更多，按预计成功率排序。",
            },
        ]

    messages = build_messages_for(limit)

    degraded = False
    if limit >= 2:
        try:
            payload = _chat_json(messages, settings, timeout=DUAL_TIMEOUT_SECONDS)
        except APITimeoutError:
            # 只降级一次，第二次仍超时交由路由返回 504
            payload = _chat_json(
                build_messages_for(1), settings, timeout=RETRY_TIMEOUT_SECONDS
            )
            limit = 1
            degraded = True
    else:
        # 单方案模式不重试，超时直接抛出由路由转 504
        payload = _chat_json(messages, settings, timeout=SINGLE_TIMEOUT_SECONDS)
    suggestions, warnings = validate_suggestions(payload, plan, rules, employees, limit=limit)
    if degraded:
        warnings.append("模型响应较慢，已自动降级为单方案")
    return {"suggestions": suggestions, "warnings": warnings}


def _normalize_operation(
    action: str,
    raw: dict[str, Any],
    plan: Plan,
    rule_map: dict[int, Rule],
    employee_map: dict[int | None, Employee],
    warnings: list[str],
) -> tuple[dict[str, Any] | None, str | None]:
    """校验单个操作，返回规范化后的操作或错误原因。"""
    rule_id = raw.get("rule_id")
    employee_id = raw.get("employee_id")
    raw_fields = raw.get("fields")
    fields = raw_fields if isinstance(raw_fields, dict) else {}
    month_start, month_end = month_bounds(plan.month)

    if action in ("relax_rule", "delete_rule", "set_rule_count", "set_rule_range"):
        if not isinstance(rule_id, (int, str)) or isinstance(rule_id, bool):
            return None, "改动项缺少有效的规则编号"
        try:
            rule_id = int(rule_id)
        except ValueError:
            return None, "改动项缺少有效的规则编号"
        rule = rule_map.get(rule_id)
        if rule is None:
            return None, f"规则 #{rule_id} 不存在"
        if action == "relax_rule":
            if not rule.hard:
                return None, f"规则 #{rule_id} 已是软约束，无需软化"
            return {"action": action, "rule_id": rule_id, "employee_id": None, "fields": {"hard": False}}, None
        if action == "delete_rule":
            return {"action": action, "rule_id": rule_id, "employee_id": None, "fields": {}}, None
        if action == "set_rule_count":
            if rule.type not in COUNT_RULE_TYPES:
                return None, f"规则 #{rule_id} 不是人数类规则，不能改人数"
            count = fields.get("count")
            if isinstance(count, int) and not isinstance(count, bool) and count == 0:
                # 0 等于取消该时段的人数要求，语义上应删除规则
                warnings.append("set_rule_count 取值需在 1~99，取消人数要求请改用 delete_rule")
                return None, "没有可执行的改动"
            if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 99:
                return None, f"规则 #{rule_id} 的人数必须是 1~99 的整数"
            return {"action": action, "rule_id": rule_id, "employee_id": None, "fields": {"count": count}}, None
        date_start = str(fields.get("date_start") or "").strip()
        date_end = str(fields.get("date_end") or "").strip()
        for value in (date_start, date_end):
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
                return None, f"规则 #{rule_id} 的日期必须是 YYYY-MM-DD"
            if not month_start <= value <= month_end:
                return None, f"规则 #{rule_id} 的日期必须落在 {month_start} 至 {month_end} 之间"
        if date_start > date_end:
            return None, f"规则 #{rule_id} 的开始日期不能晚于结束日期"
        return {
            "action": action,
            "rule_id": rule_id,
            "employee_id": None,
            "fields": {"date_start": date_start, "date_end": date_end},
        }, None

    if action == "update_plan":
        normalized: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "hours_tolerance":
                warnings.append("计划参数 hours_tolerance 已移除，月度工时按员工目标硬等值处理")
                continue
            if key == "max_consecutive_days":
                if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 14:
                    warnings.append("计划参数 max_consecutive_days 超出 0~14，已忽略")
                    continue
                normalized[key] = value
            elif key in ("coverage_start", "coverage_end"):
                if not isinstance(value, str) or not re.match(TIME_PATTERN, value):
                    warnings.append(f"计划参数 {key} 不是 HH:MM，已忽略")
                    continue
                normalized[key] = value
            else:
                warnings.append(f"计划字段 {key} 不在白名单内，已忽略")
        if not normalized:
            return None, "没有可执行的计划参数改动"
        start = normalized.get("coverage_start", plan.coverage_start)
        end = normalized.get("coverage_end", plan.coverage_end)
        if start >= end:
            return None, "覆盖时段的开始时间必须早于结束时间"
        return {"action": action, "rule_id": None, "employee_id": None, "fields": normalized}, None

    if action == "update_employee":
        if not isinstance(employee_id, (int, str)) or isinstance(employee_id, bool):
            return None, "改动项缺少有效的员工编号"
        try:
            employee_id = int(employee_id)
        except ValueError:
            return None, "改动项缺少有效的员工编号"
        if employee_id not in employee_map:
            return None, f"员工 #{employee_id} 不属于该计划"
        normalized: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "max_daily_hours":
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not 0 < value <= 12
                ):
                    warnings.append("员工参数 max_daily_hours 超出 0~12，已忽略")
                    continue
                normalized[key] = float(value)
            elif key == "monthly_hours":
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not 0 <= value <= 240
                ):
                    warnings.append("员工参数 monthly_hours 超出 0~240，已忽略")
                    continue
                normalized[key] = float(value)
            else:
                warnings.append(f"员工字段 {key} 不在白名单内，已忽略")
        if not normalized:
            return None, f"员工 #{employee_id} 没有可执行的改动"
        return {"action": action, "rule_id": None, "employee_id": employee_id, "fields": normalized}, None

    return None, f"不支持的操作：{action}"


def normalize_operations(
    operations: list[dict[str, Any]], plan: Plan, rules: list[Rule], employees: list[Employee]
) -> tuple[list[dict[str, Any]], list[str], str | None]:
    """逐个校验原始操作，返回 (规范化操作, warnings, 第一个错误原因)。"""
    warnings: list[str] = []
    rule_map = {rule.id: rule for rule in rules if rule.id is not None}
    employee_map = {employee.id: employee for employee in employees}
    normalized: list[dict[str, Any]] = []
    for raw in operations:
        if not isinstance(raw, dict):
            return normalized, warnings, "改动项格式不正确"
        action = str(raw.get("action") or "").strip()
        if action not in ACTIONS:
            return normalized, warnings, f"不支持的操作：{action}"
        operation, error = _normalize_operation(
            action, raw, plan, rule_map, employee_map, warnings
        )
        if error:
            return normalized, warnings, error
        assert operation is not None
        normalized.append(operation)
    return normalized, warnings, None


def validate_suggestions(
    payload: dict[str, Any],
    plan: Plan,
    rules: list[Rule],
    employees: list[Employee] | None = None,
    limit: int = 2,
) -> tuple[list[dict[str, Any]], list[str]]:
    """逐条校验方案，单条不合法只标记该条；超过 limit 的截断并告警。"""
    raw_list = payload.get("suggestions") if isinstance(payload, dict) else None
    if not isinstance(raw_list, list):
        raise ValueError("模型返回的 suggestions 不是数组")
    warnings: list[str] = []
    rule_map = {rule.id: rule for rule in rules if rule.id is not None}
    employee_map = {employee.id: employee for employee in employees or []}
    suggestions: list[dict[str, Any]] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            warnings.append("方案格式不正确，已跳过")
            continue
        title = str(raw.get("title") or "").strip()
        operations_raw = raw.get("operations")
        if not title or not isinstance(operations_raw, list) or not operations_raw:
            warnings.append(f"方案「{title or '未命名'}」缺少标题或改动，已丢弃")
            continue
        operations: list[dict[str, Any]] = []
        reason: str | None = None
        for item in operations_raw:
            if not isinstance(item, dict):
                reason = "改动项格式不正确"
                break
            action = str(item.get("action") or "").strip()
            if action not in ACTIONS:
                reason = f"不支持的操作：{action}"
                break
            normalized, error = _normalize_operation(
                action, item, plan, rule_map, employee_map, warnings
            )
            if error:
                reason = error
                break
            assert normalized is not None
            operations.append(normalized)
        suggestions.append(
            {
                "id": f"S{len(suggestions) + 1}",
                "title": title,
                "diagnosis": str(raw.get("diagnosis") or "").strip(),
                "operations": operations,
                "expected": str(raw.get("expected") or "").strip(),
                "risk": raw.get("risk") if isinstance(raw.get("risk"), str) else None,
                "manual_hint": raw.get("manual_hint") if isinstance(raw.get("manual_hint"), str) else None,
                "invalid": reason is not None,
                "reason": reason,
            }
        )
    if len(suggestions) > limit:
        warnings.append(f"模型返回超过 {limit} 条方案，已截断")
        suggestions = suggestions[:limit]
    return suggestions, warnings


def apply_operations(
    operations: list[dict[str, Any]],
    plan: Plan,
    employees: list[Employee],
    rules: list[Rule],
    session: Session | None = None,
) -> list[str]:
    """在内存中的 ORM 对象上执行改动，返回人类可读的改动描述。"""
    rule_map = {rule.id: rule for rule in rules if rule.id is not None}
    employee_map = {employee.id: employee for employee in employees}
    applied: list[str] = []
    for operation in operations:
        action = operation.get("action")
        raw_fields = operation.get("fields")
        fields = raw_fields if isinstance(raw_fields, dict) else {}
        # 动作白名单前置校验，未知动作直接拒绝，不进入字段处理
        if action not in ACTIONS:
            raise ValueError(f"不支持的操作：{action}")
        if action == "update_plan":
            for key, value in fields.items():
                setattr(plan, key, value)
            applied.append(
                "调整计划参数：" + "、".join(f"{key}={value}" for key, value in fields.items())
            )
            continue
        if action == "update_employee":
            employee = employee_map.get(operation.get("employee_id"))
            if employee is None:
                raise ValueError("员工不存在或不属于该计划")
            for key, value in fields.items():
                setattr(employee, key, value)
            applied.append(
                f"调整员工 {employee.name} 参数："
                + "、".join(f"{key}={value}" for key, value in fields.items())
            )
            continue
        rule_id = operation.get("rule_id")
        if not isinstance(rule_id, int) or isinstance(rule_id, bool):
            raise ValueError("规则编号无效")
        rule = rule_map.get(rule_id)
        if rule is None:
            raise ValueError(f"规则 #{rule_id} 不存在")
        label = rule_label(rule, employee_map)
        if action == "relax_rule":
            rule.hard = False
            applied.append(f"将规则 #{rule.id}（{label}）改为软约束")
        elif action == "delete_rule":
            if session is None:
                raise ValueError("删除规则需要数据库会话")
            session.delete(rule)
            assert rule.id is not None
            rule_map.pop(rule.id, None)  # pyright: ignore[reportUnusedCallResult]
            applied.append(f"删除规则 #{rule.id}（{label}）")
        elif action == "set_rule_count":
            rule.count = fields.get("count")
            applied.append(f"将规则 #{rule.id}（{label}）人数改为 {rule.count} 人")
        elif action == "set_rule_range":
            rule.date_start = cast(str, fields.get("date_start"))
            rule.date_end = cast(str, fields.get("date_end"))
            applied.append(
                f"将规则 #{rule.id}（{label}）日期改为 {rule.date_start}~{rule.date_end}"
            )
        else:
            raise ValueError(f"不支持的操作：{action}")
    return applied


def build_interpret_context(
    plan: Plan,
    rules: list[Rule],
    employees: list[Employee],
    conflicts: list[dict[str, Any]],
    pending: list[str] | None = None,
) -> str:
    """把冲突与现有规则追加到解析请求里，供模型输出可执行的改动。"""
    employee_map = {employee.id: employee for employee in employees}
    lines = [INTERPRET_PROMPT, ""]
    if conflicts:
        lines.append("当前冲突：")
        for conflict in conflicts:
            lines.append(
                f"- {conflict.get('code')}：{conflict.get('message')}（规则 {conflict.get('rule_ids') or '无'}）"
            )
    else:
        lines.append(PRECHECK_NOTE)
    lines.append("可引用的现有规则（id / 描述）：")
    for rule in rules:
        lines.append(f"- {rule.id} / {rule_label(rule, employee_map)}")
    lines.append(f"计划月份：{plan.month}，覆盖时段：{plan.coverage_start}-{plan.coverage_end}。")
    if pending:
        # 待添加约束没有 id：只能通过 add 段重述调整，update / removals 不可引用
        lines.append(PENDING_NOTE)
        lines.append("尚未保存的待添加约束（没有规则 id，不能用 update / removals 引用）：")
        lines.extend(pending)
    return "\n".join(lines)
