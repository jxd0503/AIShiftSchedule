# pyright: reportAny=false, reportExplicitAny=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportAttributeAccessIssue=false, reportUnusedCallResult=false
# 说明：openpyxl 的单元格/工作表 API 类型标注为 Any/可选（active、cell.value、column_dimensions、row_dimensions 等），
# 运行时均为有效对象；这里只关闭由此产生的类型噪音，真实错误仍然保留。
from datetime import date
from io import BytesIO
from typing import Any, cast

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from ..models import Assignment, Employee, Plan, Shift
from .calendar_service import month_dates, weekday_name

THIN = Side(style="thin", color="D1D5DB")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEADER_FILL = PatternFill("solid", fgColor="E5E7EB")
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _style_header(cell) -> None:
    cell.font = Font(bold=True)
    cell.fill = HEADER_FILL
    cell.alignment = CENTER
    cell.border = BORDER


def _display_width(text: str) -> int:
    """Excel 列宽近似：ASCII 字符按 1 计，中文等宽字符按 2 计。"""
    return sum(2 if not ch.isascii() else 1 for ch in text)


def build_workbook(
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    assignments: list[Assignment],
    report: dict[str, Any],
) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "排班表"
    shift_map = {shift.id: shift for shift in shifts}
    assignment_map = {(item.employee_id, item.date): item for item in assignments}
    days = month_dates(plan.month)

    sheet.cell(row=1, column=1, value="员工")
    _style_header(sheet.cell(row=1, column=1))
    for column, day in enumerate(days, start=2):
        cell = sheet.cell(
            row=1, column=column, value=f"{day[5:]}\n{weekday_name(day)}"
        )
        _style_header(cell)
        if date.fromisoformat(day).weekday() >= 5:
            cell.fill = PatternFill("solid", fgColor="D9E2F3")

    for row, employee in enumerate(employees, start=2):
        name_cell = sheet.cell(row=row, column=1, value=employee.name)
        name_cell.alignment = CENTER
        name_cell.border = BORDER
        for column, day in enumerate(days, start=2):
            cell = sheet.cell(row=row, column=column)
            cell.border = BORDER
            cell.alignment = CENTER
            item = assignment_map.get((cast(int, employee.id), day))
            if item is None:
                cell.value = "休"
                continue
            shift = shift_map.get(item.shift_id)
            if shift is not None and shift.is_off:
                cell.value = shift.name or "OFF"
                continue
            if shift is not None and shift.is_leave:
                cell.value = shift.name or "POFF"
                continue
            cell.value = shift.name if shift else "自定义"

    sheet.freeze_panes = "B2"
    uniform = max(
        [_display_width(employee.name) for employee in employees]
        + [_display_width(day[5:]) for day in days]
        + [_display_width(weekday_name(day)) for day in days]
        + [_display_width(shift.name or "") for shift in shifts]
        + [_display_width("休"), _display_width("自定义"), _display_width("OFF"), _display_width("POFF")]
    )
    sheet.column_dimensions["A"].width = uniform + 2
    for column in range(2, len(days) + 2):
        sheet.column_dimensions[get_column_letter(column)].width = uniform + 2
    for row in range(2, len(employees) + 2):
        sheet.row_dimensions[row].height = 34

    summary = workbook.create_sheet("工时统计")
    headers = [
        "姓名",
        "目标工时",
        "实际工时",
        "差值",
        "出勤天数",
        "最长连续上班",
        "休息天数",
        "有薪休假天数",
    ]
    for column, header in enumerate(headers, start=1):
        cell = summary.cell(row=1, column=column, value=header)
        _style_header(cell)
    for row, item in enumerate(report.get("hours", []), start=2):
        rest = next(
            (
                entry
                for entry in report.get("rest_summary", [])
                if entry["employee_id"] == item["employee_id"]
            ),
            {"work_days": 0, "max_consecutive": 0, "rest_days": 0, "paid_leave_days": 0},
        )
        values = [
            item["name"],
            item["target"],
            item["actual"],
            item["delta"],
            rest["work_days"],
            rest["max_consecutive"],
            rest["rest_days"],
            rest["paid_leave_days"],
        ]
        for column, value in enumerate(values, start=1):
            cell = summary.cell(row=row, column=column, value=value)
            cell.border = BORDER
            cell.alignment = CENTER
    summary.column_dimensions["A"].width = 14
    for column in range(2, len(headers) + 1):
        summary.column_dimensions[get_column_letter(column)].width = 14

    gaps = workbook.create_sheet("未满足需求")
    gap_headers = ["类型", "日期", "说明", "需求", "实际", "缺口"]
    for column, header in enumerate(gap_headers, start=1):
        cell = gaps.cell(row=1, column=column, value=header)
        _style_header(cell)
    row = 2
    for item in report.get("coverage", []):
        values = ["覆盖缺口", item["date"], item["label"], item["required"], item["assigned"], item["shortfall"]]
        for column, value in enumerate(values, start=1):
            cell = gaps.cell(row=row, column=column, value=value)
            cell.border = BORDER
        row += 1
    for item in report.get("conflicts", []):
        values = ["冲突", "", item["message"], "", "", ""]
        for column, value in enumerate(values, start=1):
            cell = gaps.cell(row=row, column=column, value=value)
            cell.border = BORDER
        row += 1
    gaps.column_dimensions["A"].width = 12
    gaps.column_dimensions["B"].width = 12
    gaps.column_dimensions["C"].width = 60
    for column in ("D", "E", "F"):
        gaps.column_dimensions[column].width = 8

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
