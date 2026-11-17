"""规则字段的公共校验。

写入链路（routers/rules.build_rule）与 AI 修改链路（llm_service.normalize_adjust_fields）
共用同一套口径，避免两处规则漂移；口径不一致时以更严格的为准。
"""

import re  # pyright: ignore[reportUnusedImport]

from collections.abc import Sequence

from ..models import Employee, Rule, Shift
from .calendar_service import intersect_dates

TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"

# 允许按星期限定的规则类型：人数类（含人数上限）、休息类、工时条件与连休上限
# MAX_STAFF 的 weekdays 由 solver._expand 统一展开，写入侧不能丢弃，否则
# 「周三最多 2 人」会退化成「每天最多 2 人」
WEEKDAY_TYPES = {
    "DAY_STAFF",
    "SEGMENT_STAFF",
    "START_STAFF",
    "END_STAFF",
    "MAX_STAFF",
    "REST",
    "MUST_WORK",
    "FIXED_SHIFT",
    "FIXED_TIME",
    "SHIFT_HOURS",
    "MAX_REST_DAYS",
    "SHIFT_LIMIT",
    "SINGLE_ROLE_SHIFT",
    # 工作时段窗（WORK_WINDOW）同样支持按星期限定
    "WORK_WINDOW",
}
# 人数类规则：count 表示人数
COUNT_TYPES = {
    "DAY_STAFF",
    "SEGMENT_STAFF",
    "START_STAFF",
    "END_STAFF",
    "SHIFT_STAFF",
    "MAX_STAFF",
}
# 可按岗位/分组限定统计范围的规则类型（人数类 + 工作时段窗）
ROLE_TYPES = set(COUNT_TYPES) | {"WORK_WINDOW"}
# 岗位条件班次：role 必填、shift_id 必填，count 表示「该岗位当天上班人数 ≤ N 时生效」的阈值
SINGLE_ROLE_TYPES = {"SINGLE_ROLE_SHIFT"}
# 周期天数类规则：count 表示天数（每周/每月至少休 N 天、最多上 N 天）
DAY_COUNT_TYPES = {"MIN_REST_DAYS", "MAX_WORK_DAYS"}
# count 的取值区间（人数类 / 人数上限 / 连休上限天数 / 周期天数）
COUNT_BOUNDS: dict[str, tuple[int, int]] = {
    "MIN": (0, 99),
    # 人数上限允许 0：0 表示当天不排任何人（停业/全员休息）
    "MAX_STAFF": (0, 99),
    "MAX_REST_DAYS": (1, 15),
    # 周期天数：每周/每月至少休 N 天、最多上 N 天
    "DAYS": (1, 31),
    # 月度总工时：1~744 小时（31 天 × 24 小时）
    "MONTHLY_HOURS": (1, 744),
}
# 必须引用班次目录的规则类型
SHIFT_REF_TYPES = {
    "FIXED_SHIFT",
    "SHIFT_STAFF",
    "START_STAFF",
    "BAN_SHIFT",
    "SINGLE_ROLE_SHIFT",
}
# 班次工时条件
HOURS_TYPES = {"SHIFT_HOURS"}
HOURS_OPS = ("eq", "ge", "le")
# 上班时刻比较方向（仅 START_STAFF）
TIME_OPS = ("eq", "le", "ge")
# 人数方向（仅 START_STAFF）：ge=至少，le=至多
COUNT_OPS = ("ge", "le")
# 统计周期（MIN_REST_DAYS / MAX_WORK_DAYS）
PERIODS = ("week", "month")


def count_bounds(rule_type: str) -> tuple[int, int]:
    """按规则类型返回 count 的合法区间。"""
    if rule_type == "MONTHLY_HOURS":
        return COUNT_BOUNDS["MONTHLY_HOURS"]
    if rule_type == "MAX_REST_DAYS":
        return COUNT_BOUNDS["MAX_REST_DAYS"]
    if rule_type in DAY_COUNT_TYPES or rule_type == "MAX_CONSECUTIVE_WORK":
        return COUNT_BOUNDS["DAYS"]
    if rule_type == "MAX_STAFF":
        return COUNT_BOUNDS["MAX_STAFF"]
    return COUNT_BOUNDS["MIN"]


def monthly_hours_target(
    employee: Employee, rules: Sequence[Rule], month: str
) -> tuple[float, Rule | None]:
    """月度工时目标取值：规则值 > 员工档案 monthly_hours。

    规则 employee_id 留空表示全员；员工级规则优先于全员规则。
    规则日期区间与计划月零交集时该规则不生效（与其它规则的月边界口径一致）；
    有交集时一律按整月目标，不按天数折算。
    员工档案 <= 0 表示不设月度工时目标。
    """
    candidates = [
        rule
        for rule in rules
        if rule.type == "MONTHLY_HOURS"
        and rule.valid
        and rule.count
        and rule.employee_id in (employee.id, None)
        and bool(intersect_dates(rule.date_start, rule.date_end, month))
    ]
    candidates.sort(key=lambda rule: 0 if rule.employee_id == employee.id else 1)
    rule = candidates[0] if candidates else None
    if rule is not None and rule.count is not None:
        return float(rule.count), rule
    profile = float(employee.monthly_hours or 0)
    return (profile if profile > 0 else 0.0), None


def normalize_weekdays(value: str | None) -> tuple[str | None, str | None]:
    """把星期输入规范为去重升序的 "1,2"，非法值丢弃并返回提示。"""
    if not value:
        return None, None
    raw = [part.strip() for part in str(value).replace("，", ",").split(",") if part.strip()]
    valid = sorted({int(part) for part in raw if part.isdigit() and 1 <= int(part) <= 7})
    dropped = [part for part in raw if not (part.isdigit() and 1 <= int(part) <= 7)]
    note = None
    if dropped:
        note = f"星期设置中的 {'、'.join(dropped)} 不是 1~7 的取值，已忽略"
    if not valid:
        return None, note
    return ",".join(str(item) for item in valid), note


def normalize_work_hours(value: object) -> tuple[float | None, str | None]:
    """工时阈值：0 < 值 <= 24，最多保留 1 位小数。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "工时阈值必须是数字"
    hours = round(float(value), 1)
    if not 0 < hours <= 24:
        return None, "工时阈值必须大于 0 且不超过 24 小时"
    return hours, None


def normalize_hours_op(value: object) -> tuple[str | None, str | None]:
    """工时比较方式只能是 eq / ge / le。"""
    text = value.strip() if isinstance(value, str) else ""
    if text not in HOURS_OPS:
        return None, "工时比较方式只能是 eq（等于）、ge（不少于）、le（不超过）"
    return text, None


def normalize_count(rule_type: str, value: object) -> tuple[int | None, str | None]:
    """按规则类型校验 count 的人数/天数区间。

    修改链路比写入链路更严格：人数类不允许改成 0（取消需求应删除规则），
    但人数上限（MAX_STAFF）例外——0 表示当天不排任何人，属于合法语义；
    连休上限沿用 1~15 天、周期天数沿用 1~31 天的写入区间。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "人数（天数）必须是整数"
    low, high = count_bounds(rule_type)
    if rule_type != "MAX_STAFF":
        low = max(1, low)
    if not low <= value <= high:
        return None, f"人数（天数）需为 {low}~{high} 的整数"
    return value, None


def normalize_time_op(value: object) -> tuple[str | None, str | None]:
    """上班时刻比较方向只能是 eq / le / ge。"""
    text = value.strip().lower() if isinstance(value, str) else ""
    if text not in TIME_OPS:
        return None, "时刻条件只能是 eq（等于）、le（不晚于）、ge（不早于）"
    return text, None


def normalize_count_op(value: object) -> tuple[str | None, str | None]:
    """人数方向只能是 ge（至少）/ le（至多）。"""
    text = value.strip().lower() if isinstance(value, str) else ""
    if text not in COUNT_OPS:
        return None, "人数条件只能是 ge（至少）或 le（至多）"
    return text, None


def normalize_period(value: object) -> tuple[str | None, str | None]:
    """统计周期只能是 week（自然周）/ month（整段区间）。"""
    text = value.strip().lower() if isinstance(value, str) else ""
    if text not in PERIODS:
        return None, "统计周期只能是 week（每周）或 month（每月）"
    return text, None


def normalize_role(value: object) -> tuple[str | None, str | None]:
    """岗位/分组名：去首尾空白，空值表示不限岗位。"""
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return None, None
    if len(text) > 20:
        return text[:20], "岗位名称超过 20 个字符，已截断"
    return text, None


def normalize_shift_id(value: object, shifts: list[Shift]) -> tuple[int | None, str | None]:
    """班次编号必须存在于该计划的班次目录中。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "班次编号必须是整数"
    if all(shift.id != value for shift in shifts):
        return None, f"班次 #{value} 不在本计划的班次目录中"
    return value, None


def rule_shift(rule: Rule, shift_map: dict[int, Shift]) -> Shift | None:
    """取规则引用的班次；未引用或不存在时返回 None。"""
    if rule.shift_id is None:
        return None
    return shift_map.get(rule.shift_id)
