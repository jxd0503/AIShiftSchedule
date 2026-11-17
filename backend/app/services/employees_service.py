# pyright: reportAny=false, reportUnknownMemberType=false
"""人员域应用服务：GUI 直接调用的纯函数，不依赖 HTTP 层。"""
from sqlmodel import Session, select

from ..db import engine
from ..errors import DomainError
from ..models import Assignment, Employee, Plan, Rule, Setting

DEFAULT_MAX_DAILY_HOURS = 10.0


def _load_plan(session: Session, plan_id: int) -> Plan:
    plan = session.get(Plan, plan_id)
    if plan is None:
        raise DomainError("计划不存在")
    return plan


def global_max_daily_hours(session: Session) -> float:
    """全局默认日工时上限，可通过 Setting.default_max_daily_hours 配置。"""
    row = session.get(Setting, "default_max_daily_hours")
    if row is None or not row.value:
        return DEFAULT_MAX_DAILY_HOURS
    try:
        value = float(row.value)
    except ValueError:
        return DEFAULT_MAX_DAILY_HOURS
    return value if value > 0 else DEFAULT_MAX_DAILY_HOURS


def _ensure_unique_name(
    session: Session, plan_id: int, name: str, exclude_id: int | None = None
) -> None:
    rows = session.exec(
        select(Employee).where(Employee.plan_id == plan_id, Employee.name == name)
    ).all()
    for employee in rows:
        if employee.id != exclude_id:
            raise DomainError(f"人员姓名重复：{name}")


def _validate_profile(name: str, monthly_hours: float, max_daily_hours: float) -> None:
    """服务层兜底校验：与 EmployeeCreate/EmployeeUpdate 的字段口径保持一致。"""
    if not name.strip():
        raise DomainError("人员姓名不能为空")
    if monthly_hours < 0:
        raise DomainError("月度工时目标不能为负数")
    if not 0 < max_daily_hours <= 24:
        raise DomainError("每日工时上限必须大于 0 且不超过 24 小时")


def list_employees(plan_id: int) -> list[Employee]:
    with Session(engine) as session:
        _load_plan(session, plan_id)
        return list(
            session.exec(
                select(Employee)
                .where(Employee.plan_id == plan_id)
                .order_by(Employee.sort_order, Employee.id)  # pyright: ignore[reportArgumentType]
            ).all()
        )


def create_employee(
    plan_id: int,
    *,
    name: str,
    monthly_hours: float,
    max_daily_hours: float,
    allow_special: bool,
    role: str | None = None,
) -> Employee:
    with Session(engine) as session:
        _load_plan(session, plan_id)
        _validate_profile(name, monthly_hours, max_daily_hours)
        _ensure_unique_name(session, plan_id, name)
        count = len(session.exec(select(Employee).where(Employee.plan_id == plan_id)).all())
        employee = Employee(
            plan_id=plan_id,
            name=name,
            monthly_hours=monthly_hours,
            max_daily_hours=max_daily_hours,
            allow_special=allow_special,
            role=role,
            sort_order=count,
        )
        session.add(employee)
        session.commit()
        session.refresh(employee)
        return employee


def update_employee(
    employee_id: int,
    *,
    name: str,
    monthly_hours: float,
    max_daily_hours: float,
    allow_special: bool,
    role: str | None = None,
) -> Employee:
    with Session(engine) as session:
        employee = session.get(Employee, employee_id)
        if employee is None:
            raise DomainError("人员不存在")
        _validate_profile(name, monthly_hours, max_daily_hours)
        _ensure_unique_name(session, employee.plan_id, name, exclude_id=employee_id)
        employee.name = name
        employee.monthly_hours = monthly_hours
        employee.max_daily_hours = max_daily_hours
        employee.allow_special = allow_special
        employee.role = role
        session.add(employee)
        session.commit()
        session.refresh(employee)
        return employee


def delete_employee(employee_id: int) -> None:
    with Session(engine) as session:
        employee = session.get(Employee, employee_id)
        if employee is None:
            raise DomainError("人员不存在")
        for rule in session.exec(select(Rule).where(Rule.employee_id == employee_id)).all():
            session.delete(rule)
        for assignment in session.exec(
            select(Assignment).where(Assignment.employee_id == employee_id)
        ).all():
            session.delete(assignment)
        session.delete(employee)
        session.commit()
