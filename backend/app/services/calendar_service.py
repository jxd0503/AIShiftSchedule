import calendar
from collections.abc import Sequence
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import Shift


WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
# 工时统一以 0.01 小时为整数单位，与入库保留两位小数的工时完全一致
UNIT_PER_HOUR = 100


def month_dates(month: str) -> list[str]:
    year, mon = (int(part) for part in month.split("-"))
    days = calendar.monthrange(year, mon)[1]
    return [date(year, mon, day).isoformat() for day in range(1, days + 1)]


def month_bounds(month: str) -> tuple[str, str]:
    dates = month_dates(month)
    return dates[0], dates[-1]


def parse_hhmm(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def format_hhmm(minutes: int) -> str:
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def normalize_hhmm(value: str) -> str:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"时间格式不正确：{value}")
    hour, minute = int(parts[0]), int(parts[1])
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"时间超出范围：{value}")
    return f"{hour:02d}:{minute:02d}"


def parse_interval(start: str, end: str) -> tuple[int, int]:
    start_minutes = parse_hhmm(start)
    end_minutes = parse_hhmm(end)
    if end_minutes <= start_minutes:
        end_minutes += 1440
    return start_minutes, end_minutes


def window_start_limit(coverage_start: str) -> int:
    """覆盖起点的分钟值，只用于覆盖诊断口径，不参与班次合法性判定。"""
    return parse_hhmm(coverage_start)


def late_end_limit(coverage_end: str, latest_end: str | None = None) -> int:
    """非跨午夜班的下班时间上限（计划级参数，不再写死 19:00）。

    latest_end 为计划配置的「最晚收班时间」；未配置时取覆盖结束时间，
    这样营业到 21:00 的门店可以直接排 13:00-21:00 这类晚班。
    """
    return parse_hhmm(latest_end or coverage_end)


def window_violation(
    soft_start: str,
    soft_end: str,
    start_time: str,
    end_time: str,
    latest_end: str | None = None,
) -> tuple[str, str] | None:
    """判断普通班是否落在软覆盖窗口内，返回 (类型, 说明)。

    软覆盖窗口是班次合法性边界（外边界）：上班不得早于软起点、不得跨午夜、
    下班不得晚于最晚收班时间。硬核心在软窗口内部，由求解器的硬时段规则单独约束。
    类型为 early / overnight / late。
    latest_end 为计划级最晚收班时间，未配置时取软终点。
    """
    start_minutes, end_minutes = parse_interval(start_time, end_time)
    if end_minutes >= 1440:
        return "overnight", f"跨午夜（{start_time}-{end_time}），普通班不允许"
    if start_minutes < parse_hhmm(soft_start):
        return "early", f"上班时间 {start_time} 早于覆盖起点 {soft_start}"
    limit_end = late_end_limit(soft_end, latest_end)
    if end_minutes > limit_end:
        return "late", f"下班时间 {end_time} 超过最晚 {format_hhmm(limit_end)}"
    return None


def time_window_error(
    soft_start: str,
    soft_end: str,
    start_time: str,
    end_time: str,
    latest_end: str | None = None,
) -> str | None:
    violation = window_violation(
        soft_start, soft_end, start_time, end_time, latest_end
    )
    if violation is None:
        return None
    return f"{start_time}-{end_time} {violation[1]}"


def shift_window_error(
    soft_start: str,
    soft_end: str,
    shift: "Shift",
    latest_end: str | None = None,
) -> str | None:
    # 有薪休假 POFF 与休息 OFF 不受覆盖时段窗口约束
    if shift.is_leave or shift.is_off:
        return None
    violation = window_violation(
        soft_start, soft_end, shift.start_time, shift.end_time, latest_end
    )
    if violation is None:
        return None
    return f"班次 {shift.name} {violation[1]}"


def duration_hours(start: str, end: str) -> float:
    start_minutes, end_minutes = parse_interval(start, end)
    return (end_minutes - start_minutes) / 60


def effective_hours(work_hours: float | None, start: str, end: str) -> float:
    if work_hours is not None:
        return float(work_hours)
    return duration_hours(start, end)


def effective_units(work_hours: float | None, start: str, end: str) -> int:
    return int(round(effective_hours(work_hours, start, end) * UNIT_PER_HOUR))


def units_to_hours(units: int) -> float:
    return round(units / UNIT_PER_HOUR, 2)


def shifts_overlap(shift_start: str, shift_end: str, window_start: str, window_end: str) -> bool:
    shift_begin, shift_finish = parse_interval(shift_start, shift_end)
    window_begin, window_finish = parse_interval(window_start, window_end)
    return shift_begin < window_finish and shift_finish > window_begin


def coverage_slices(
    intervals: list[tuple[str, str]], window_start: str, window_end: str
) -> list[tuple[int, int]]:
    """按所有起止时间把窗口切成覆盖人数恒定的最小片段。"""
    window_begin, window_finish = parse_interval(window_start, window_end)
    points = {window_begin, window_finish}
    for start, end in intervals:
        begin, finish = parse_interval(start, end)
        if finish <= window_begin or begin >= window_finish:
            continue
        points.add(max(begin, window_begin))
        points.add(min(finish, window_finish))
    ordered = sorted(points)
    return [(left, right) for left, right in zip(ordered, ordered[1:]) if right > left]


def coverage_shortfall(
    intervals: list[tuple[str, str]],
    window_start: str,
    window_end: str,
    required: int = 1,
) -> tuple[int, list[tuple[int, int]]]:
    """计算时段内连续覆盖缺口，返回 (最大缺员数, 未覆盖区间列表)。"""
    if required <= 0:
        return 0, []
    window_begin, window_finish = parse_interval(window_start, window_end)
    spans: list[tuple[int, int]] = []
    for start, end in intervals:
        begin, finish = parse_interval(start, end)
        if finish <= window_begin or begin >= window_finish:
            continue
        spans.append((begin, finish))
    gaps: list[tuple[int, int]] = []
    minimum = required
    for left, right in coverage_slices(intervals, window_start, window_end):
        depth = sum(1 for begin, finish in spans if begin <= left and finish >= right)
        if depth < required:
            gaps.append((left, right))
            minimum = min(minimum, depth)
    merged: list[tuple[int, int]] = []
    for left, right in gaps:
        if merged and merged[-1][1] == left:
            merged[-1] = (merged[-1][0], right)
        else:
            merged.append((left, right))
    return (required - minimum if gaps else 0), merged


def intersect_dates(date_start: str, date_end: str, month: str) -> list[str]:
    return [day for day in month_dates(month) if date_start <= day <= date_end]


def parse_weekdays(value: str | None) -> set[int]:
    """把 "6,7" 解析为 {6, 7}（1=周一 … 7=周日），非法值直接忽略。"""
    if not value:
        return set()
    result: set[int] = set()
    for part in str(value).replace("，", ",").split(","):
        text = part.strip()
        if text.isdigit() and 1 <= int(text) <= 7:
            result.add(int(text))
    return result


def filter_weekdays(dates: list[str], weekdays: set[int]) -> list[str]:
    if not weekdays:
        return dates
    return [day for day in dates if date.fromisoformat(day).isoweekday() in weekdays]


def rule_month_dates(
    date_start: str, date_end: str, weekdays: str | None, month: str
) -> list[str]:
    """规则在计划月份内的生效日期。

    带 weekdays 的规则一律以该月 1 号为遍历起点：命中星期即纳入候选，
    不能因为模型给出的 date_start 偏晚而丢掉月首命中的那一天；
    结束日期仍只作为上界，允许收缩，不允许抬高。
    单日规则（date_start == date_end）例外：weekdays 只用于校验该日，
    不能把生效范围扩到月初，否则单日调整会被放大成整月约束。
    """
    days = month_dates(month)
    weekday_set = parse_weekdays(weekdays)
    if weekday_set:
        if date_start and date_end and date_start == date_end:
            if date_start > days[-1]:
                return []
            matched = date.fromisoformat(date_start).isoweekday() in weekday_set
            return [date_start] if matched else []
        upper = date_end if date_end and date_end <= days[-1] else days[-1]
        return [day for day in days if day <= upper and date.fromisoformat(day).isoweekday() in weekday_set]
    return intersect_dates(date_start, date_end, month)


def _iso_week(day: str) -> tuple[int, int]:
    iso = date.fromisoformat(day).isocalendar()
    return iso.year, iso.week


def week_days_in_month(month: str, day: str) -> int:
    """该日所在自然周（ISO 周一~周日）落在计划月内的日历天数（1~7）。"""
    key = _iso_week(day)
    return sum(1 for item in month_dates(month) if _iso_week(item) == key)


def weekly_quota(
    count: int, month: str, bucket: Sequence[str], *, is_limit: bool = False
) -> int:
    """周期为 week 时的配额折算。

    - 至少休息天数（is_limit=False）：残周按 floor(count×可见天数/7)；
    - 最多上班天数（is_limit=True）：残周按 min(count, 可见天数)。

    上限型指标不能因为残周而收紧：1 天残周里的「每周最多上 5 天」应当仍允许
    上班 1 天，否则等于强制闭店，会让正常计划无解。

    可见天数取「该自然周在计划月内的日历天数」，而不是 bucket 里被 weekdays
    过滤后的天数，否则「仅周日」规则会因桶只剩 1 天而被折算成 0。
    """
    if not bucket:
        return 0
    visible = week_days_in_month(month, bucket[0])
    if visible >= 7:
        return count
    if is_limit:
        return min(count, max(0, visible))
    return (count * max(0, visible)) // 7


def period_buckets(period: str | None, dates: Sequence[str]) -> list[list[str]]:
    """按统计周期切段：week=自然周（周一~周日），month/None=整段区间。

    求解器、校验器、规则预检共用这一份实现，避免三处口径漂移。
    """
    if (period or "month") != "week":
        return [list(dates)] if dates else []
    buckets: dict[tuple[int, int], list[str]] = {}
    order: list[tuple[int, int]] = []
    for day in dates:
        key = _iso_week(day)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(day)
    return [buckets[key] for key in order]


def add_hours(value: str, hours: float) -> str:
    return format_hhmm(parse_hhmm(value) + int(hours * 60))


def subtract_hours(value: str, hours: float) -> str:
    return format_hhmm(parse_hhmm(value) - int(hours * 60))


def weekday_name(date_value: str) -> str:
    return WEEKDAY_NAMES[date.fromisoformat(date_value).weekday()]


def is_weekend(date_value: str) -> bool:
    return date.fromisoformat(date_value).weekday() >= 5


def max_consecutive(dates: set[str], month: str) -> int:
    longest = 0
    current = 0
    for day in month_dates(month):
        if day in dates:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest
