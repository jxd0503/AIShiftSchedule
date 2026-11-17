from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import cast

from openpyxl import load_workbook

from ..config import resource_root
from ..models import Shift
from .calendar_service import normalize_hhmm

SHIFT_FILE = Path("backend/resources/shift_codes.xlsx")
HEADERS = ["班次代码", "工作时长", "日工作计划", "变式", "起始时间", "终止时间", "休息代码", "休息时间"]

# 盘点班识别规则。源文件（班次代码.xlsx）只有纯字母数字编码，没有任何中文标记，
# 因此只有两类可解释的依据：
# 1) 编码/日工作计划/变式里直接带“盘点/盘”字样，或以 INV/STOCK 开头（换用带名称的文件时生效）；
# 2) 历史排班中确认用于盘点的班次代码。
#    4MRZ 来自既有计划的盘点日安排（11-19 盘点 4MRZ），文件中它与其他夜班的唯一差别
#    是业务含义，编码本身无法泛化，故以白名单固定，误判风险最小。
INVENTORY_KEYWORDS = ("盘点", "盤點", "盘", "盤")
INVENTORY_PREFIXES = ("INV", "STOCK")
KNOWN_INVENTORY_CODES = frozenset({"4MRZ"})
COLORS = [
    "#2563eb",
    "#f59e0b",
    "#10b981",
    "#8b5cf6",
    "#ef4444",
    "#14b8a6",
    "#f97316",
    "#6366f1",
    "#ec4899",
    "#84cc16",
]


@dataclass
class ShiftTemplate:
    name: str
    start_time: str
    end_time: str
    work_hours: float
    break_code: str | None
    break_hours: float
    base_code: str | None
    variant: str | None
    sort_order: int


@dataclass(frozen=True)
class SpecialOverride:
    """人工改过的特殊班标记，重新导入时按班次代码原样保留。"""

    is_special: bool
    reason: str | None


def detect_inventory_shift(
    name: str,
    base_code: str | None = None,
    variant: str | None = None,
) -> tuple[bool, str | None]:
    """判断班次是否为盘点等特殊班，返回 (是否特殊, 判定依据)。

    规则按顺序匹配，命中即返回，因此依据始终指向第一条命中的规则：
    1. 编码/日工作计划/变式含“盘点、盘”等关键字；
    2. 编码以 INV/STOCK 开头；
    3. 编码属于历史盘点班白名单。
    """
    label = " ".join(part for part in (name, base_code, variant) if part).strip()
    upper = name.strip().upper()
    for keyword in INVENTORY_KEYWORDS:
        if keyword in label:
            return True, f"编码/名称含“{keyword}”"
    for prefix in INVENTORY_PREFIXES:
        if upper.startswith(prefix):
            return True, f"编码以“{prefix}”开头"
    if upper in KNOWN_INVENTORY_CODES:
        return True, "历史盘点班代码"
    return False, None


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _time_text(value: object, row_number: int, label: str) -> str:
    if isinstance(value, time):
        return f"{value.hour:02d}:{value.minute:02d}"
    text = _text(value)
    if text is None:
        raise ValueError(f"第 {row_number} 行缺少{label}")
    try:
        return normalize_hhmm(text)
    except ValueError as error:
        raise ValueError(f"第 {row_number} 行{label}格式不正确：{text}") from error


def load_shift_templates() -> list[ShiftTemplate]:
    path = resource_root() / SHIFT_FILE
    if not path.exists():
        raise FileNotFoundError(f"班次代码文件不存在：{path}")
    workbook = load_workbook(path, data_only=True)
    sheet = workbook.worksheets[0]
    header = [_text(cell.value) for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
    if header[: len(HEADERS)] != HEADERS:
        raise ValueError("班次代码文件表头与预期不符，请使用原始班次代码文档")

    templates: list[ShiftTemplate] = []
    for row_number, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
        values = list(row) + [None] * 8
        code = _text(values[0])
        if code is None:
            continue
        work_hours = round(float(cast(float, values[1]) or 0), 2)
        if code.upper() == "OFF" or code.upper() == "POFF":
            # OFF/POFF 由 off_shift()/paid_leave_shift() 以固定工时（0/8 小时）统一构造，不从文件读取
            continue
        if work_hours <= 0:
            continue
        break_code = _text(values[6])
        if break_code in ("0", "0.0"):
            break_code = None
        templates.append(
            ShiftTemplate(
                name=code,
                start_time=_time_text(values[4], row_number, "起始时间"),
                end_time=_time_text(values[5], row_number, "终止时间"),
                work_hours=work_hours,
                break_code=break_code,
                break_hours=round(float(cast(float, values[7]) or 0), 2),
                base_code=_text(values[2]),
                variant=_text(values[3]),
                sort_order=len(templates),
            )
        )
    if not templates:
        raise ValueError("班次代码文件中没有可用班次")
    return templates


def build_shifts(
    plan_id: int,
    templates: list[ShiftTemplate],
    overrides: dict[str, SpecialOverride] | None = None,
) -> list[Shift]:
    """按模板构建班次目录。

    overrides：按班次代码记录的人工修改，命中时原样保留，不参与自动识别，
    这样“用户改过的不要被下次导入覆盖”。
    """
    preserved = overrides or {}
    shifts: list[Shift] = []
    for template in templates:
        auto_special, auto_reason = detect_inventory_shift(
            template.name, template.base_code, template.variant
        )
        override = preserved.get(template.name)
        if override is not None:
            is_special = override.is_special
            special_source = "manual"
            special_reason = override.reason or (
                "人工设为盘点班" if override.is_special else "人工设为普通班"
            )
        else:
            is_special = auto_special
            special_source = "auto" if auto_special else None
            special_reason = auto_reason
        shifts.append(
            Shift(
                plan_id=plan_id,
                name=template.name,
                start_time=template.start_time,
                end_time=template.end_time,
                is_special=is_special,
                special_source=special_source,
                special_reason=special_reason,
                is_leave=template.name.strip().upper() == "POFF",
                color=COLORS[template.sort_order % len(COLORS)],
                sort_order=template.sort_order,
                enabled=True,
                work_hours=template.work_hours,
                break_code=template.break_code,
                break_hours=template.break_hours,
                base_code=template.base_code,
                variant=template.variant,
            )
        )
    if not any(shift.is_leave for shift in shifts):
        shifts.append(paid_leave_shift(plan_id))
    if not any(shift.is_off for shift in shifts):
        shifts.append(off_shift(plan_id))
    return shifts


def paid_leave_shift(plan_id: int) -> Shift:
    return Shift(
        plan_id=plan_id,
        name="POFF",
        start_time="00:00",
        end_time="00:00",
        is_special=False,
        is_leave=True,
        color="#6b7280",
        sort_order=999,
        enabled=True,
        work_hours=8.0,
        base_code="POFF",
        variant="有薪休假",
    )


def off_shift(plan_id: int) -> Shift:
    # 休息（OFF）班次：固定 0 工时，仅用于排班表展示，不计工时、不计出勤、不参与覆盖
    return Shift(
        plan_id=plan_id,
        name="OFF",
        start_time="00:00",
        end_time="00:00",
        is_special=False,
        is_leave=False,
        is_off=True,
        color="#9ca3af",
        sort_order=998,
        enabled=True,
        work_hours=0.0,
        base_code="OFF",
        variant="休息",
    )
