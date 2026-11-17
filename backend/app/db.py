# pyright: reportAny=false, reportUnusedCallResult=false, reportImplicitStringConcatenation=false
# 说明：迁移脚本大量使用 exec_driver_sql 执行原生 SQL，返回游标结果与 Any 元组属于动态 API 噪音，
# 这里只关闭由此产生的类型提示，真实错误仍然保留。
import re

from sqlmodel import Session, SQLModel, create_engine, select

from . import models  # noqa: F401  # 保证模型注册后再建表  # pyright: ignore[reportUnusedImport]
from .config import data_dir

engine = create_engine(
    f"sqlite:///{(data_dir() / 'data.db').as_posix()}",
    connect_args={"check_same_thread": False},
)


def migrate_shift_columns() -> None:
    expected = {
        "work_hours": "FLOAT",
        "break_code": "VARCHAR",
        "break_hours": "FLOAT",
        "base_code": "VARCHAR",
        "variant": "VARCHAR",
        "is_leave": "BOOLEAN DEFAULT 0",
        # 休息（OFF）标记：仅用于展示，不计工时、不计出勤、不参与覆盖
        "is_off": "BOOLEAN DEFAULT 0",
        # 特殊班（盘点）的判定来源与依据：auto=导入自动识别，manual=人工修改
        "special_source": "VARCHAR",
        "special_reason": "VARCHAR",
    }
    with engine.begin() as connection:
        existing = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info('shift')").fetchall()
        }
        for name, column_type in expected.items():
            if name not in existing:
                connection.exec_driver_sql(f"ALTER TABLE shift ADD COLUMN {name} {column_type}")



def migrate_plan_columns() -> None:
    expected = {
        "max_consecutive_days": "INTEGER DEFAULT 5",
        # 两阶段求解的计划级覆盖；NULL 表示沿用全局 Setting
        "solve_first_phase_seconds": "INTEGER",
        "solve_max_seconds": "INTEGER",
        "solve_min_improvement": "FLOAT",
        # 最晚收班时间；NULL 表示取 coverage_end（旧计划即此行为）
        "latest_end_time": "VARCHAR",
        # 跨午夜班次日休息开关；老数据回填 1，保持既有行为
        "overnight_next_rest": "BOOLEAN DEFAULT 1",
        # 软覆盖外边界与硬核心最少人数；老数据回填默认值
        "coverage_soft_start": "VARCHAR DEFAULT '07:00'",
        "coverage_soft_end": "VARCHAR DEFAULT '19:00'",
        "core_min_count": "INTEGER DEFAULT 1",
        # 月工时硬约束开关；老数据回填 0（保持软目标行为）
        "monthly_hours_hard": "BOOLEAN DEFAULT 0",
    }
    with engine.begin() as connection:
        existing = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info('plan')").fetchall()
        }
        if "max_consecutive_days" not in existing:
            connection.exec_driver_sql(
                "ALTER TABLE plan ADD COLUMN max_consecutive_days INTEGER DEFAULT 5"
            )
            connection.exec_driver_sql(
                "UPDATE plan SET max_consecutive_days = 5 WHERE max_consecutive_days IS NULL"
            )
        for name, column_type in expected.items():
            if name not in existing:
                connection.exec_driver_sql(f"ALTER TABLE plan ADD COLUMN {name} {column_type}")
        connection.exec_driver_sql(
            "UPDATE plan SET overnight_next_rest = 1 WHERE overnight_next_rest IS NULL"
        )
        connection.exec_driver_sql(
            "UPDATE plan SET monthly_hours_hard = 0 WHERE monthly_hours_hard IS NULL"
        )
        # 已废弃的旧列：hours_tolerance 在模型中已移除，但老库仍保留
        # NOT NULL 约束，会导致新建计划插入失败，这里直接删除该列
        obsolete = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info('plan')").fetchall()
        }
        if "hours_tolerance" in obsolete:
            connection.exec_driver_sql("ALTER TABLE plan DROP COLUMN hours_tolerance")


def migrate_rule_columns() -> None:
    """补上规则表新增列（幂等，不重建表、不丢数据）。"""
    expected = {
        "work_hours": "FLOAT",
        "hours_op": "VARCHAR",
        "weekdays": "VARCHAR",
        # 上班时刻比较方向（START_STAFF）与统计周期（MIN_REST_DAYS / MAX_WORK_DAYS）
        "time_op": "VARCHAR",
        # 人数方向（START_STAFF）：ge=至少（默认），le=至多
        "count_op": "VARCHAR",
        "period": "VARCHAR",
        # 岗位/分组限定（人数类规则）
        "role": "VARCHAR",
    }
    with engine.begin() as connection:
        existing = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info('rule')").fetchall()
        }
        for name, column_type in expected.items():
            if name not in existing:
                connection.exec_driver_sql(f"ALTER TABLE rule ADD COLUMN {name} {column_type}")
        # 老数据回填：time_op 缺省按 eq，period 缺省按 month，与前端/求解口径一致
        connection.exec_driver_sql(
            "UPDATE rule SET time_op = 'eq' WHERE type = 'START_STAFF' AND "
            "(time_op IS NULL OR time_op = '')"
        )
        connection.exec_driver_sql(
            "UPDATE rule SET count_op = 'ge' WHERE type = 'START_STAFF' AND "
            "(count_op IS NULL OR count_op = '')"
        )
        connection.exec_driver_sql(
            "UPDATE rule SET period = 'month' WHERE type IN ('MIN_REST_DAYS', 'MAX_WORK_DAYS') "
            "AND (period IS NULL OR period = '')"
        )


def migrate_employee_columns() -> None:
    """补上人员表新增列（幂等，不重建表、不丢数据）。"""
    expected = {
        # 岗位/分组：人数类规则可按岗位统计
        "role": "VARCHAR",
    }
    with engine.begin() as connection:
        existing = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info('employee')").fetchall()
        }
        for name, column_type in expected.items():
            if name not in existing:
                connection.exec_driver_sql(
                    f"ALTER TABLE employee ADD COLUMN {name} {column_type}"
                )


def _shift_referenced(session: Session, shift_id: int) -> bool:
    from .models import Assignment, Rule

    if session.exec(select(Assignment).where(Assignment.shift_id == shift_id)).first():
        return True
    return session.exec(select(Rule).where(Rule.shift_id == shift_id)).first() is not None


def ensure_paid_leave_shifts() -> None:
    from .models import Plan, Shift
    from .services.shift_import import paid_leave_shift

    with Session(engine) as session:
        for plan in session.exec(select(Plan)).all():
            assert plan.id is not None
            shifts = session.exec(
                select(Shift)
                .where(Shift.plan_id == plan.id)
                .order_by(Shift.sort_order, Shift.id)  # pyright: ignore[reportArgumentType]
            ).all()
            poffs = [shift for shift in shifts if shift.name.strip().upper() == "POFF"]
            if poffs:
                target = poffs[0]
                target.is_leave = True
                session.add(target)
            else:
                session.add(paid_leave_shift(plan.id))
                continue
            for extra in poffs[1:]:
                if extra.id is not None and not _shift_referenced(session, extra.id):
                    session.delete(extra)
        session.commit()


def ensure_off_shifts() -> None:
    from .models import Plan, Shift
    from .services.shift_import import off_shift

    with Session(engine) as session:
        for plan in session.exec(select(Plan)).all():
            assert plan.id is not None
            shifts = session.exec(
                select(Shift)
                .where(Shift.plan_id == plan.id)
                .order_by(Shift.sort_order, Shift.id)  # pyright: ignore[reportArgumentType]
            ).all()
            offs = [shift for shift in shifts if shift.name.strip().upper() == "OFF"]
            if offs:
                target = offs[0]
                target.is_off = True
                session.add(target)
            else:
                session.add(off_shift(plan.id))
                continue
            for extra in offs[1:]:
                if extra.id is not None and not _shift_referenced(session, extra.id):
                    session.delete(extra)
        session.commit()


def backfill_special_flags() -> None:
    """老数据回填：special_source 为空的班次重新跑盘点识别。

    白名单（如 4MRZ）扩充前创建的班次不会随导入更新，这里在启动时补齐，
    只补未标记的；人工改过（manual）的保持原样。
    """
    from .models import Shift
    from .services.shift_import import detect_inventory_shift

    with Session(engine) as session:
        shifts = session.exec(
            select(Shift).where(Shift.special_source.is_(None))  # pyright: ignore[reportAttributeAccessIssue, reportOptionalMemberAccess, reportUnknownMemberType, reportUnknownArgumentType]
        ).all()
        changed = False
        for shift in shifts:
            is_special, reason = detect_inventory_shift(
                shift.name, shift.base_code, shift.variant
            )
            if is_special:
                if not shift.is_special:
                    shift.is_special = True
                    changed = True
                if not shift.special_source:
                    shift.special_source = "auto"
                    shift.special_reason = reason
                    session.add(shift)
                    changed = True
        if changed:
            session.commit()


def backfill_rule_classification() -> None:
    """修复历史规则的硬/软口径与类型误判（幂等）。

    三类已知问题：
    1. 周末「N 小时及以上」被误转成 SEGMENT_STAFF（覆盖时段口径）→ 还原为 SHIFT_HOURS；
    2. 含软词「可以/排不开」的 DAY_STAFF 被标成硬约束 → 降为软约束；
    3. 含「必须/务必」的 FIXED_SHIFT 被标成软约束 → 升为硬约束。
    """
    from .models import Rule

    with Session(engine) as session:
        rules = session.exec(select(Rule)).all()
        changed = False
        for rule in rules:
            raw = (rule.raw_text or "").strip()
            if not raw:
                continue
            if rule.type == "SEGMENT_STAFF" and "小时" in raw:
                m = re.search(r"(\d+(?:\.\d+)?)\s*小时", raw)
                rule.type = "SHIFT_HOURS"
                rule.work_hours = float(m.group(1)) if m else 8.0
                rule.hours_op = "ge" if ("以上" in raw or "及以上" in raw) else "eq"
                rule.hard = False
                rule.start_time = None
                rule.end_time = None
                rule.count = None
                changed = True
            if rule.type == "DAY_STAFF" and rule.hard and ("可以" in raw or "排不开" in raw):
                rule.hard = False
                changed = True
            if rule.type == "FIXED_SHIFT" and not rule.hard and ("必须" in raw or "务必" in raw):
                rule.hard = True
                changed = True
        if changed:
            session.commit()


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    migrate_plan_columns()
    migrate_shift_columns()
    migrate_rule_columns()
    migrate_employee_columns()
    backfill_special_flags()
    backfill_rule_classification()
    ensure_paid_leave_shifts()
    ensure_off_shifts()


def get_session():
    with Session(engine) as session:
        yield session
