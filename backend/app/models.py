from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

TYPE_DEFAULT_HARD = {
    "REST": True,
    "MUST_WORK": True,
    "PAID_LEAVE": True,
    "FIXED_SHIFT": True,
    "FIXED_TIME": True,
    "ALL_START_AT": True,
    "DAY_STAFF": True,
    "SEGMENT_STAFF": False,
    "START_STAFF": False,
    # 下班时刻人数属于偏好，默认软性
    "END_STAFF": False,
    "SHIFT_STAFF": True,
    # 人数上限属于「不能超」的约束，默认硬性
    "MAX_STAFF": True,
    # 班次工时条件属于偏好，默认软性
    "SHIFT_HOURS": False,
    # 连休上限默认是软约束：遇到显式更长的休息要求时自动让路
    "MAX_REST_DAYS": False,
    # 「每周/每月至少休 N 天」属于硬限制
    "MIN_REST_DAYS": True,
    # 「每周/每月最多上 N 天」属于硬限制
    "MAX_WORK_DAYS": True,
    # 「禁止排某班次」属于硬限制
    "BAN_SHIFT": True,
    # 「上班日只能排某类班次」属于硬限制，但不强制上班（休息日自动让路）
    "SHIFT_LIMIT": True,
    # 「员工上班时段必须落在指定窗口内」属于硬限制
    "WORK_WINDOW": True,
    # 「岗位当天只有 N 人及以下上班时，这些人都必须上指定班次」属于硬限制
    "SINGLE_ROLE_SHIFT": True,
    # 「连续上班不得超过」属于硬限制，与计划级 max_consecutive_days 叠加时取更严格值
    "MAX_CONSECUTIVE_WORK": True,
    # 月度总工时：硬等值，count 表示目标小时数
    "MONTHLY_HOURS": True,
}


class Plan(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    month: str
    coverage_start: str = "07:30"
    coverage_end: str = "17:00"
    # 软覆盖外边界：早于/晚于硬核心的边缘时段，可排班但非必须
    coverage_soft_start: str = "07:00"
    coverage_soft_end: str = "19:00"
    # 硬核心时段内最少在岗人数
    core_min_count: int = 1
    # 非跨午夜班的最晚收班时间；留空表示取软覆盖结束时间（旧计划即此行为）
    latest_end_time: str | None = None
    # 跨午夜班是否自动要求次日休息（默认开启，保持既有行为）
    overnight_next_rest: bool = True
    max_consecutive_days: int = 5
    # 月工时是否作为硬约束：开启后人员档案月工时必须精确达成（等价于每人一条 MONTHLY_HOURS 硬规则）
    monthly_hours_hard: bool = False
    # 兼容旧字段：已废弃，不再参与两阶段预算（保留列以免破坏旧数据/API）
    solve_seconds: int = 30
    # 计划级覆盖全局 Setting；留空则读全局默认
    solve_first_phase_seconds: int | None = None
    solve_max_seconds: int | None = None
    solve_min_improvement: float | None = None
    status: str = "draft"
    solve_report: str | None = None
    last_solve_at: str | None = None
    created_at: str

    @property
    def coverage_soft_start_effective(self) -> str:
        """生效的软覆盖起点：软/硬谁更早就用谁，保证软窗口始终包裹硬核心。"""
        return min(self.coverage_soft_start, self.coverage_start)

    @property
    def coverage_soft_end_effective(self) -> str:
        """生效的软覆盖终点：软/硬谁更晚就用谁，保证软窗口始终包裹硬核心。"""
        return max(self.coverage_soft_end, self.coverage_end)

    @property
    def latest_end_time_effective(self) -> str:
        """当前生效的最晚收班时间：未配置自定义值时跟随软覆盖结束时间。"""
        return self.latest_end_time or self.coverage_soft_end_effective


class Employee(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    plan_id: int = Field(foreign_key="plan.id", index=True)
    name: str
    monthly_hours: float = 160
    max_daily_hours: float = 10
    allow_special: bool = False
    # 岗位/分组（如「主管」「一般员工」）：人数类规则可只统计该岗位的人
    role: str | None = None
    sort_order: int = 0


class Shift(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    plan_id: int = Field(foreign_key="plan.id", index=True)
    name: str
    start_time: str
    end_time: str
    is_special: bool = False
    # 特殊班（盘点）判定来源：auto=导入规则自动识别，manual=人工修改；None=未标记
    special_source: str | None = None
    # 自动识别的判定依据，用于向用户解释“为什么这条是盘点班”
    special_reason: str | None = None
    is_leave: bool = False
    # 休息（OFF）标记：仅用于展示，不计工时、不计出勤、不参与覆盖
    is_off: bool = False
    color: str = "#2563eb"
    sort_order: int = 0
    enabled: bool = True
    work_hours: float | None = None
    break_code: str | None = None
    break_hours: float | None = None
    base_code: str | None = None
    variant: str | None = None


class Rule(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    plan_id: int = Field(foreign_key="plan.id", index=True)
    employee_id: int | None = Field(default=None, foreign_key="employee.id", index=True)
    type: str
    date_start: str
    date_end: str
    shift_id: int | None = Field(default=None, foreign_key="shift.id")
    start_time: str | None = None
    end_time: str | None = None
    count: int | None = None
    hard: bool = True
    raw_text: str | None = None
    source: str = "manual"
    valid: bool = True
    note: str | None = None
    # 班次工时条件（SHIFT_HOURS）：工时阈值与比较方式 eq/ge/le
    work_hours: float | None = None
    hours_op: str | None = None
    # 按星期限定（1=周一 … 7=周日），如 "6,7" 表示周六周日
    weekdays: str | None = None
    # 上班时刻的比较方向（仅 START_STAFF）：eq=等于，le=不晚于，ge=不早于
    time_op: str | None = None
    # 人数方向（仅 START_STAFF）：ge=至少（默认），le=至多
    count_op: str | None = None
    # 统计周期（MIN_REST_DAYS / MAX_WORK_DAYS）：week=自然周，month=区间整段
    period: str | None = None
    # 岗位/分组限定（仅人数类规则）：只统计该岗位的员工；留空表示全员
    role: str | None = None


class Assignment(SQLModel, table=True):
    __table_args__: tuple[UniqueConstraint, ...] = (
        UniqueConstraint("plan_id", "employee_id", "date", name="uq_assignment_person_date"),
    )
    id: int | None = Field(default=None, primary_key=True)
    plan_id: int = Field(foreign_key="plan.id", index=True)
    employee_id: int = Field(foreign_key="employee.id", index=True)
    date: str = Field(index=True)
    shift_id: int | None = Field(default=None, foreign_key="shift.id")
    start_time: str
    end_time: str
    hours: float
    locked: bool = False
    source: str = "solver"


class PlanCandidate(SQLModel, table=True):
    """后台优化找到的更优解快照，不写 assignments，替换时才落库。"""

    __tablename__ = "plan_candidates"  # pyright: ignore[reportAssignmentType]
    id: int | None = Field(default=None, primary_key=True)
    plan_id: int = Field(foreign_key="plan.id", index=True)
    objective: float = 0
    breakdown_json: str = "{}"
    assignments_json: str = "[]"
    validate_report_json: str = "{}"
    # pending | accepted | discarded | stale
    status: str = Field(default="pending", index=True)
    created_at: str
    elapsed_seconds: float = 0


class AdjustmentLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    plan_id: int = Field(foreign_key="plan.id", index=True)
    employee_id: int = Field(foreign_key="employee.id", index=True)
    date: str = Field(index=True)
    shift_id: int | None = Field(default=None, foreign_key="shift.id")
    hours: float
    action: str = "补足工时"
    detail: str = ""
    created_at: str


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = ""
