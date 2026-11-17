# pyright: reportAny=false, reportExplicitAny=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportMissingParameterType=false
"""AI 域应用服务：约束解析、排班微调与冲突修复建议。

模型调用由 GUI 放在子线程执行；本模块负责组装上下文、校验模型输出、
试算后落库，失败统一抛 DomainError。
"""
from __future__ import annotations

import json
from typing import Any, cast

from openai import APITimeoutError
from pydantic import ValidationError
from sqlmodel import Session, select

from ..db import engine
from ..errors import DomainError
from ..models import Assignment, Employee, Plan, Rule, Shift
from ..schemas import ParseResponse, PreviewRule, RuleCreate
from . import advisor_service, llm_service, rules_service, schedule_service, solver

NO_KEY_MESSAGE = "尚未配置模型 API Key，请先到设置页填写"
PARSE_TIMEOUT_MESSAGE = "解析超时：模型未在限定时间内响应，请缩短描述后重试"
ADJUST_TIMEOUT_MESSAGE = "解析超时（已自动重试一次，共约 180 秒）；请缩短描述后重试"
SUGGESTION_TIMEOUT_MESSAGE = "生成方案超时，请稍后重试或减少冲突数量"

# 预览规则无法落库时，按类型给出的改写建议
_REWRITE_HINTS = {
    "REST": "可改写为「张三 11 月 4 日休息」",
    "MUST_WORK": "可改写为「张三 11 月 4 日必须上班」",
    "PAID_LEAVE": "可改写为「张三 11 月休 2 天有薪假」",
    "FIXED_SHIFT": "可改写为「张三 11 月 4 日固定上 2GR 班次」",
    "FIXED_TIME": "可改写为「张三 11 月 4 日 09:00 上班」",
    "ALL_START_AT": "可改写为「全员 09:00 上班」",
    "START_STAFF": "可改写为「11 月 4 日 09:00 前至少 2 人到岗」",
    "END_STAFF": "可改写为「11 月 4 日 20:00 下班至少 3 人」",
    "SHIFT_LIMIT": "可改写为「张三上班日只能上早班」",
    "SINGLE_ROLE_SHIFT": "可改写为「主管当天只有 1 人上班时，该主管必须上 A2I」",
    "BAN_SHIFT": "可改写为「不要给张三排 2GR 班次」",
    "DAY_STAFF": "可改写为「11 月 3 号至少 2 人上班」",
    "SEGMENT_STAFF": "可改写为「11 月 3 号 08:00-10:00 至少 2 人」",
    "SHIFT_STAFF": "可改写为「11 月 3 号 2GR 班次至少 1 人」",
    "SHIFT_HOURS": "可改写为「11 月每个工作日排 8 小时的班次」",
    "MAX_REST_DAYS": "可改写为「每人连休不得超过 2 天」",
}


def _context(
    session: Session, plan_id: int
) -> tuple[Plan, list[Employee], list[Shift], list[Rule]]:
    plan = session.get(Plan, plan_id)
    if plan is None:
        raise DomainError("计划不存在")
    employees = list(
        session.exec(
            select(Employee)
            .where(Employee.plan_id == plan_id)
            .order_by(Employee.sort_order, Employee.id)  # pyright: ignore[reportArgumentType]
        ).all()
    )
    shifts = list(
        session.exec(
            select(Shift)
            .where(Shift.plan_id == plan_id)
            .order_by(Shift.sort_order, Shift.id)  # pyright: ignore[reportArgumentType]
        ).all()
    )
    rules = list(session.exec(select(Rule).where(Rule.plan_id == plan_id)).all())
    return plan, employees, shifts, rules


def _settings(session: Session) -> dict[str, Any]:
    settings = llm_service.get_settings(session)
    if not settings["llm_api_key"].strip():
        raise DomainError(NO_KEY_MESSAGE)
    return settings


def _stored_report(plan: Plan) -> dict[str, Any]:
    if not plan.solve_report:
        return {}
    try:
        return json.loads(plan.solve_report)
    except json.JSONDecodeError:
        return {}


def _preview_to_payload(preview: PreviewRule) -> RuleCreate:
    """预览规则 → 写入入参，与手工新增使用同一份字段口径。"""
    return RuleCreate(
        type=cast(Any, preview.type),
        employee_id=preview.employee_id,
        date_start=preview.date_start,
        date_end=preview.date_end,
        shift_id=preview.shift_id,
        start_time=preview.start_time,
        end_time=preview.end_time,
        count=preview.count,
        hard=preview.hard,
        raw_text=preview.raw_text,
        work_hours=preview.work_hours,
        hours_op=cast(Any, preview.hours_op),
        weekdays=preview.weekdays,
        time_op=cast(Any, preview.time_op),
        count_op=cast(Any, preview.count_op),
        period=cast(Any, preview.period),
    )


def _validate_previews(
    plan: Plan, session: Session, parsed: ParseResponse, text: str
) -> ParseResponse:
    """用写入侧 build_rule 干跑每条预览，避免「解析成功但落库失败」。"""
    unique: list[PreviewRule] = []
    seen: set[tuple[Any, ...]] = set()
    duplicates = 0
    for preview in parsed.rules:
        key = (
            preview.type,
            preview.employee_id,
            preview.date_start,
            preview.date_end,
            preview.shift_id,
            preview.start_time,
            preview.end_time,
            preview.count,
            preview.hard,
            preview.work_hours,
            preview.hours_op,
            preview.weekdays,
            preview.time_op,
            preview.count_op,
            preview.period,
        )
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique.append(preview)
    payloads = [
        _preview_to_payload(preview) if preview.valid else None
        for preview in unique
    ]
    hints = rules_service.rest_hints(
        plan, [item for item in payloads if item is not None]
    )
    kept: list[PreviewRule] = []
    warnings = list(parsed.warnings)
    if duplicates:
        warnings.append(f"已合并 {duplicates} 条重复规则")
    rejected: list[str] = []
    for preview, payload in zip(unique, payloads):
        if payload is None:
            kept.append(preview)
            continue
        try:
            rules_service.build_rule(
                payload, plan, session, "llm", hints.get(payload.employee_id or 0)
            )
        except DomainError as error:
            snippet = (preview.raw_text or text or "").strip()
            if len(snippet) > 40:
                snippet = f"{snippet[:40]}…"
            advice = _REWRITE_HINTS.get(preview.type, "可拆成更具体的句子后重试")
            message = f"「{snippet}」无法落库：{error}；{advice}"
            warnings.append(message)
            rejected.append(message)
            continue
        kept.append(preview)
    warnings = list(dict.fromkeys(warnings))
    hint = llm_service.failure_hint(kept, warnings)
    if hint is None and not kept and rejected:
        hint = f"未能解析出可执行规则：{rejected[0]}"
    return parsed.model_copy(update={"rules": kept, "warnings": warnings, "hint": hint})


def conflict_summary(plan_id: int) -> dict[str, Any]:
    """最近一次求解报告里的冲突概况，供 AI 页签展示。"""
    with Session(engine) as session:
        plan = session.get(Plan, plan_id)
        if plan is None:
            raise DomainError("计划不存在")
        report = _stored_report(plan)
        conflicts = report.get("conflicts") or []
        return {
            "status": report.get("status"),
            "count": len(conflicts),
            "conflicts": conflicts,
        }


def parse_constraints(
    plan_id: int, scope: str, employee_id: int | None, text: str
) -> dict[str, Any]:
    """自然语言约束解析：返回预览规则、提示与兜底模式，不写库。"""
    text = text.strip()
    if not text:
        raise DomainError("请输入要解析的约束描述")
    if scope not in ("global", "employee"):
        raise DomainError("解析范围不合法")
    with Session(engine) as session:
        plan, employees, shifts, _rules = _context(session, plan_id)
        employee: Employee | None = None
        if scope == "employee":
            if employee_id is None:
                raise DomainError("个人约束必须指定员工")
            employee = session.get(Employee, employee_id)
            if employee is None or employee.plan_id != plan_id:
                raise DomainError("员工不存在")
        settings = _settings(session)
        try:
            parsed = llm_service.parse_constraints(
                plan,
                employees,
                shifts,
                scope,
                employee,
                text,
                settings["llm_base_url"],
                settings["llm_model"],
                settings["llm_api_key"],
            )
        except APITimeoutError:
            parsed = llm_service.fallback_parse(
                plan, employees, shifts, scope, employee, text
            )
        except (ValidationError, ValueError) as error:
            raise DomainError(f"约束解析失败：{error}") from error
        except Exception as error:  # noqa: BLE001 - 模型调用异常统一转业务错误
            raise DomainError(f"模型调用失败：{error}") from error
        result = _validate_previews(plan, session, parsed, text)
        if parsed.mode == "fallback" and result.hint is None and parsed.hint:
            result = result.model_copy(update={"hint": parsed.hint})
        return result.model_dump()


def apply_previews(plan_id: int, previews: list[dict[str, Any]], *, force: bool = False) -> int:
    """把用户勾选的预览规则写入数据库，返回写入条数。"""
    if not previews:
        raise DomainError("请先选择要写入的约束")
    payloads = [_preview_to_payload(PreviewRule(**item)) for item in previews]
    rules = rules_service.create_rules(plan_id, payloads, source="llm", force=force)
    return len(rules)


def parse_adjustment(plan_id: int, text: str) -> dict[str, Any]:
    """把「对排班不满意」的描述解析成规则新增/修改/删除，不写库。"""
    text = text.strip()
    if not text:
        raise DomainError("请输入要调整的内容")
    with Session(engine) as session:
        plan, employees, shifts, rules = _context(session, plan_id)
        settings = _settings(session)
        try:
            result = llm_service.parse_schedule_adjustment(
                plan, employees, shifts, rules, text, settings
            )
        except APITimeoutError as error:
            raise DomainError(ADJUST_TIMEOUT_MESSAGE) from error
        except (ValidationError, ValueError) as error:
            raise DomainError(f"约束解析失败：{error}") from error
        except Exception as error:  # noqa: BLE001
            raise DomainError(f"模型调用失败：{error}") from error
        if not (result["add"] or result["update"] or result["remove"]):
            result["warnings"].append("没有解析出可执行的调整")
        rule_map = {rule.id: rule for rule in rules if rule.id is not None}
        employee_map = {employee.id: employee for employee in employees}
        for item in result["update"]:
            rule = rule_map.get(item["rule_id"])
            item["label"] = (
                advisor_service.rule_label(rule, employee_map)
                if rule is not None
                else f"规则 #{item['rule_id']}"
            )
            item["summary"] = "、".join(
                f"{key}={value}" for key, value in item["fields"].items()
            )
        for item in result["remove"]:
            rule = rule_map.get(item["rule_id"])
            item["label"] = (
                advisor_service.rule_label(rule, employee_map)
                if rule is not None
                else f"规则 #{item['rule_id']}"
            )
        return result


def _apply_and_solve(
    session: Session,
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    rules: list[Rule],
    applied: list[str],
) -> dict[str, Any]:
    """规则改动已在会话内生效：重新求解，可行才提交，否则整体回滚。"""
    locked = list(
        session.exec(
            select(Assignment).where(
                Assignment.plan_id == plan.id, Assignment.locked == True  # pyright: ignore[reportArgumentType]
            )
        ).all()
    )
    outcome = solver.solve(plan, employees, shifts, rules, locked)
    conflicts = outcome.report.get("conflicts", [])
    if outcome.status in ("OPTIMAL", "FEASIBLE"):
        schedule_service.persist_solution(plan, session, outcome)
        session.commit()
        return {
            "solved": True,
            "rolled_back": False,
            "applied": applied,
            "status": outcome.status,
            "message": outcome.message,
            "report": outcome.report,
            "conflicts": conflicts,
        }
    session.rollback()
    return {
        "solved": False,
        "rolled_back": True,
        "applied": applied,
        "status": outcome.status,
        "message": outcome.message,
        "report": outcome.report,
        "conflicts": conflicts,
    }


def apply_adjustment(
    plan_id: int,
    add: list[dict[str, Any]],
    update: list[dict[str, Any]],
    remove: list[int],
) -> dict[str, Any]:
    """应用规则增删改并试算：可行才落库，否则整体回滚。"""
    if not (add or update or remove):
        raise DomainError("没有可应用的调整")
    with Session(engine) as session:
        plan, employees, shifts, rules = _context(session, plan_id)
        employee_map = {employee.id: employee for employee in employees}
        rule_map = {rule.id: rule for rule in rules if rule.id is not None}
        updates: list[tuple[Rule, dict[str, Any]]] = []
        for item in update:
            rule = rule_map.get(item.get("rule_id"))
            if rule is None:
                raise DomainError(f"规则 #{item.get('rule_id')} 不存在")
            fields, warnings = llm_service.normalize_adjust_fields(
                rule, dict(item.get("fields") or {}), plan, shifts
            )
            if warnings:
                raise DomainError("；".join(warnings))
            if not fields:
                raise DomainError(f"规则 #{rule.id} 没有可应用的字段")
            updates.append((rule, fields))
        for rule_id in remove:
            if rule_id not in rule_map:
                raise DomainError(f"规则 #{rule_id} 不存在")

        applied: list[str] = []
        try:
            for rule, fields in updates:
                for key, value in fields.items():
                    setattr(rule, key, value)
                session.add(rule)
                change = "、".join(f"{key}={value}" for key, value in fields.items())
                applied.append(
                    f"修改规则 #{rule.id}（{advisor_service.rule_label(rule, employee_map)}）：{change}"
                )
            for rule_id in remove:
                rule = rule_map[rule_id]
                applied.append(
                    f"删除规则 #{rule_id}（{advisor_service.rule_label(rule, employee_map)}）"
                )
                session.delete(rule)
                rule_map.pop(rule_id, None)
            for item in add:
                payload = _preview_to_payload(PreviewRule(**item))
                for rule in rules_service.build_rule(payload, plan, session, "llm"):
                    session.add(rule)
                    applied.append(
                        f"新增规则：{advisor_service.rule_label(rule, employee_map)}"
                    )
            session.flush()
        except DomainError:
            session.rollback()
            raise
        except ValueError as error:
            session.rollback()
            raise DomainError(str(error)) from error

        current_rules = list(
            session.exec(select(Rule).where(Rule.plan_id == plan_id)).all()
        )
        return _apply_and_solve(session, plan, employees, shifts, current_rules, applied)


def apply_operations(
    plan_id: int, operations: list[dict[str, Any]]
) -> dict[str, Any]:
    """应用 AI 方案里的操作并试算：可行才落库，否则整体回滚。"""
    if not operations:
        raise DomainError("没有可应用的改动")
    with Session(engine) as session:
        plan, employees, shifts, rules = _context(session, plan_id)
        normalized, _warnings, error = advisor_service.normalize_operations(
            operations, plan, rules, employees
        )
        if error:
            raise DomainError(error)
        if not normalized:
            raise DomainError("没有可执行的改动")
        try:
            applied = advisor_service.apply_operations(
                normalized, plan, employees, rules, session
            )
            session.flush()
        except DomainError:
            session.rollback()
            raise
        except ValueError as error:
            session.rollback()
            raise DomainError(str(error)) from error
        current_rules = list(
            session.exec(select(Rule).where(Rule.plan_id == plan_id)).all()
        )
        return _apply_and_solve(session, plan, employees, shifts, current_rules, applied)


def _operation_text(
    operation: dict[str, Any],
    rule_map: dict[int, Rule],
    employee_map: dict[int, Employee],
) -> str:
    action = operation.get("action")
    fields = operation.get("fields") or {}
    if action == "update_plan":
        return "调整计划参数：" + "、".join(f"{key}={value}" for key, value in fields.items())
    if action == "update_employee":
        employee = employee_map.get(operation.get("employee_id"))
        name = employee.name if employee is not None else f"员工 #{operation.get('employee_id')}"
        return f"调整 {name} 参数：" + "、".join(
            f"{key}={value}" for key, value in fields.items()
        )
    rule_id = operation.get("rule_id")
    rule = rule_map.get(rule_id) if isinstance(rule_id, int) else None
    label = (
        advisor_service.rule_label(rule, employee_map)
        if rule is not None
        else f"规则 #{rule_id}"
    )
    if action == "relax_rule":
        return f"把「{label}」改为软约束"
    if action == "delete_rule":
        return f"删除「{label}」"
    if action == "set_rule_count":
        return f"把「{label}」人数改为 {fields.get('count')} 人"
    if action == "set_rule_range":
        return f"把「{label}」日期改为 {fields.get('date_start')}~{fields.get('date_end')}"
    return f"{action}：{label}"


def suggest_conflicts(plan_id: int, limit: int = 1) -> dict[str, Any]:
    """基于当前冲突生成修复方案；没有冲突时做规则体检。"""
    limit = 2 if limit >= 2 else 1
    with Session(engine) as session:
        plan, employees, shifts, rules = _context(session, plan_id)
        settings = _settings(session)
        report = _stored_report(plan)
        conflicts = report.get("conflicts") or []
        if not report:
            report = {"conflicts": conflicts}
        try:
            result = advisor_service.request_suggestions(
                plan,
                employees,
                shifts,
                rules,
                report,
                [],
                settings,
                limit=limit,
                precheck=not conflicts,
            )
        except APITimeoutError as error:
            raise DomainError(SUGGESTION_TIMEOUT_MESSAGE) from error
        except (ValidationError, ValueError) as error:
            raise DomainError(f"方案解析失败：{error}") from error
        except Exception as error:  # noqa: BLE001
            raise DomainError(f"模型调用失败：{error}") from error
        warnings = list(result.get("warnings") or [])
        if not conflicts and not result.get("suggestions"):
            warnings.append("当前没有已检测到的冲突，规则体检未发现需要调整的地方")
        rule_map = {rule.id: rule for rule in rules if rule.id is not None}
        employee_map = {employee.id: employee for employee in employees}
        for suggestion in result.get("suggestions") or []:
            suggestion["summary"] = [
                _operation_text(operation, rule_map, employee_map)
                for operation in suggestion.get("operations") or []
            ]
        result["warnings"] = warnings
        result["mode"] = "precheck" if not conflicts else "normal"
        result["conflict_count"] = len(conflicts)
        return result
