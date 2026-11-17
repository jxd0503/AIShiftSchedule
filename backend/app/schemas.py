# pyright: reportExplicitAny=false
# 说明：AI 方案/冲突/试算接口的请求与响应字段是模型返回的 JSON 结构，天然用 dict[str, Any] 表达，
# 这里只关闭由此产生的 Any 提示，真实错误仍然保留。
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from .services.calendar_service import TIME_PATTERN


RuleType = Literal[
    "REST",
    "MUST_WORK",
    "PAID_LEAVE",
    "FIXED_SHIFT",
    "FIXED_TIME",
    "ALL_START_AT",
    "DAY_STAFF",
    "SEGMENT_STAFF",
    "START_STAFF",
    "END_STAFF",
    "SHIFT_STAFF",
    "MAX_STAFF",
    "SHIFT_HOURS",
    "MAX_REST_DAYS",
    "MIN_REST_DAYS",
    "MAX_WORK_DAYS",
    "MAX_CONSECUTIVE_WORK",
    "MONTHLY_HOURS",
    "BAN_SHIFT",
    "SHIFT_LIMIT",
    "SINGLE_ROLE_SHIFT",
    "WORK_WINDOW",
]


class PlanCreate(BaseModel):
    name: str | None = None
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    coverage_start: str = Field(default="07:30", pattern=TIME_PATTERN)
    coverage_end: str = Field(default="17:00", pattern=TIME_PATTERN)
    coverage_soft_start: str = Field(default="07:00", pattern=TIME_PATTERN)
    coverage_soft_end: str = Field(default="19:00", pattern=TIME_PATTERN)
    core_min_count: int = Field(default=1, ge=1)
    max_consecutive_days: int = Field(default=5, ge=0, le=30)
    # 月工时是否作为硬约束（精确达成）
    monthly_hours_hard: bool = False
    # 非跨午夜班的最晚收班时间；留空（或空串）表示跟随 coverage_end
    latest_end_time: str | None = Field(
        default=None, pattern=r"^$|^([01]\d|2[0-3]):[0-5]\d$"
    )
    # 跨午夜班是否自动要求次日休息（默认开启，保持既有行为）
    overnight_next_rest: bool = True
    # 两阶段求解的计划级覆盖；留空沿用全局 Setting
    solve_first_phase_seconds: int | None = Field(default=None, ge=5, le=600)
    solve_max_seconds: int | None = Field(default=None, ge=5, le=900)
    solve_min_improvement: float | None = Field(default=None, ge=0, le=1000)


class PlanUpdate(BaseModel):
    name: str | None = None
    month: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    coverage_start: str | None = Field(default=None, pattern=TIME_PATTERN)
    coverage_end: str | None = Field(default=None, pattern=TIME_PATTERN)
    coverage_soft_start: str | None = Field(default=None, pattern=TIME_PATTERN)
    coverage_soft_end: str | None = Field(default=None, pattern=TIME_PATTERN)
    core_min_count: int | None = Field(default=None, ge=1)
    max_consecutive_days: int | None = Field(default=None, ge=0, le=30)
    monthly_hours_hard: bool | None = None
    # 传空串或 null 表示清除自定义值、跟随 coverage_end
    latest_end_time: str | None = Field(
        default=None, pattern=r"^$|^([01]\d|2[0-3]):[0-5]\d$"
    )
    overnight_next_rest: bool | None = None
    solve_seconds: int | None = Field(default=None, ge=5, le=900)
    solve_first_phase_seconds: int | None = Field(default=None, ge=5, le=600)
    solve_max_seconds: int | None = Field(default=None, ge=5, le=900)
    solve_min_improvement: float | None = Field(default=None, ge=0, le=1000)


class PlanRead(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: int
    name: str
    month: str
    coverage_start: str
    coverage_end: str
    coverage_soft_start: str
    coverage_soft_end: str
    core_min_count: int
    max_consecutive_days: int
    monthly_hours_hard: bool = False
    # 当前生效的最晚收班时间（未配置时由 coverage_end 补位，见 latest_end_time_effective）
    latest_end_time: str | None = None
    latest_end_time_effective: str | None = None
    overnight_next_rest: bool = True
    solve_seconds: int
    solve_first_phase_seconds: int | None = None
    solve_max_seconds: int | None = None
    solve_min_improvement: float | None = None
    status: str
    solve_report: str | None
    last_solve_at: str | None
    created_at: str


class PlanListItem(PlanRead):
    employee_count: int
    shift_count: int
    rule_count: int


class EmployeeCreate(BaseModel):
    name: str = Field(min_length=1)
    monthly_hours: float = Field(default=160, ge=0)
    # 留空表示使用全局默认（Setting.default_max_daily_hours，缺省 10 小时）
    max_daily_hours: float | None = Field(default=None, gt=0, le=24)
    allow_special: bool = False
    # 岗位/分组：人数类规则可按岗位统计
    role: str | None = None
    sort_order: int = 0


class EmployeeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    monthly_hours: float | None = Field(default=None, ge=0)
    max_daily_hours: float | None = Field(default=None, gt=0, le=24)
    allow_special: bool | None = None
    role: str | None = None
    sort_order: int | None = None


class EmployeeRead(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: int
    plan_id: int
    name: str
    monthly_hours: float
    max_daily_hours: float
    allow_special: bool
    role: str | None = None
    sort_order: int


class ShiftCreate(BaseModel):
    name: str = Field(min_length=1)
    start_time: str = Field(pattern=TIME_PATTERN)
    end_time: str = Field(pattern=TIME_PATTERN)
    is_special: bool = False
    is_leave: bool = False
    is_off: bool = False
    color: str = "#2563eb"
    sort_order: int = 0
    enabled: bool = True
    work_hours: float | None = Field(default=None, ge=0)
    break_code: str | None = None
    break_hours: float | None = Field(default=None, ge=0)
    base_code: str | None = None
    variant: str | None = None


class ShiftUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    start_time: str | None = Field(default=None, pattern=TIME_PATTERN)
    end_time: str | None = Field(default=None, pattern=TIME_PATTERN)
    is_special: bool | None = None
    is_leave: bool | None = None
    is_off: bool | None = None
    color: str | None = None
    sort_order: int | None = None
    enabled: bool | None = None
    work_hours: float | None = Field(default=None, ge=0)
    break_code: str | None = None
    break_hours: float | None = Field(default=None, ge=0)
    base_code: str | None = None
    variant: str | None = None


class ShiftRead(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: int
    plan_id: int
    name: str
    start_time: str
    end_time: str
    is_special: bool
    special_source: str | None
    special_reason: str | None
    is_leave: bool
    is_off: bool
    color: str
    sort_order: int
    enabled: bool
    work_hours: float | None
    break_code: str | None
    break_hours: float | None
    base_code: str | None
    variant: str | None


class RuleCreate(BaseModel):
    type: RuleType
    employee_id: int | None = None
    date_start: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    date_end: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    shift_id: int | None = None
    start_time: str | None = None
    end_time: str | None = None
    count: int | None = Field(default=None, ge=0)
    hard: bool | None = None
    raw_text: str | None = None
    note: str | None = None
    # 工时阈值：SHIFT_HOURS 的合法上限仍为 24 小时（由 rules_service 校验）；
    # 这里放宽到 744 是为了不让月度工时等场景在字段层被 24 小时误拦截
    work_hours: float | None = Field(default=None, ge=0.5, le=744)
    hours_op: Literal["eq", "ge", "le"] | None = None
    weekdays: str | None = None
    # 上班时刻比较方向（仅 START_STAFF）：eq=等于，le=不晚于，ge=不早于
    time_op: Literal["eq", "le", "ge"] | None = None
    # 人数方向（仅 START_STAFF）：ge=至少（默认），le=至多
    count_op: Literal["ge", "le"] | None = None
    # 统计周期（MIN_REST_DAYS / MAX_WORK_DAYS）：week=自然周，month=区间整段
    period: Literal["week", "month"] | None = None
    # 岗位/分组限定（仅人数类规则）：留空表示全员
    role: str | None = None


class RuleRead(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: int
    plan_id: int
    employee_id: int | None
    type: str
    date_start: str
    date_end: str
    shift_id: int | None
    start_time: str | None
    end_time: str | None
    count: int | None
    hard: bool
    raw_text: str | None
    source: str
    valid: bool
    note: str | None
    work_hours: float | None = None
    hours_op: str | None = None
    weekdays: str | None = None
    time_op: str | None = None
    count_op: str | None = None
    period: str | None = None
    role: str | None = None


class ParseRequest(BaseModel):
    scope: Literal["employee", "global"]
    employee_id: int | None = None
    text: str = Field(min_length=1)


class PreviewRule(BaseModel):
    index: int
    employee_id: int | None = None
    employee_name: str | None = None
    type: str
    date_start: str | None = None
    date_end: str | None = None
    shift_id: int | None = None
    shift_name: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    count: int | None = None
    hard: bool
    raw_text: str | None = None
    valid: bool = True
    note: str | None = None
    work_hours: float | None = None
    hours_op: str | None = None
    weekdays: str | None = None
    time_op: str | None = None
    count_op: str | None = None
    period: str | None = None
    role: str | None = None
    # 人数规则硬/软判定的依据词（例如「至少 → 硬」），供预览页展示
    hard_basis: str | None = None
    # 规则来源：llm=模型解析，fallback=模型超时后的本地关键词兜底
    origin: str = "llm"


class ParseResponse(BaseModel):
    rules: list[PreviewRule]
    warnings: list[str]
    # 全部规则都未通过校验时的可操作提示（含改写建议），供前端直接展示
    hint: str | None = None
    # llm=模型解析；fallback=模型超时后的本地关键词兜底
    mode: str = "llm"
    # 兜底时未能确定性提取的原文条款，供用户手动补充
    missing: list[str] = Field(default_factory=list)


class RuleImportRequest(BaseModel):
    rules: list[RuleCreate]


class RuleBatchDeleteRequest(BaseModel):
    """批量删除入参：一次提交一组规则 id，由后端在单个事务内删完。"""

    ids: list[int] = Field(default_factory=list)


class RuleBatchDeleteResult(BaseModel):
    """批量删除结果：不存在的 id 不整体失败，单列返回供前端提示。"""

    deleted: int
    not_found: list[int] = Field(default_factory=list)
    # 该计划下保留的排班记录数：删规则不级联删除已生成的排班
    assignments_kept: int = 0


class RuleConflictItem(BaseModel):
    """规则冲突预检条目：同时给出新规则下标与已落库规则 id，便于前端分别改删。"""

    code: str
    message: str
    severity: str = "error"
    hint: str = ""
    rule_ids: list[int] = Field(default_factory=list)
    new_rule_indexes: list[int] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


class RuleConflictResult(BaseModel):
    has_conflict: bool
    items: list[RuleConflictItem] = Field(default_factory=list)


class AssignmentCreate(BaseModel):
    employee_id: int
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    shift_id: int
    locked: bool = False


class AssignmentRead(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: int
    plan_id: int
    employee_id: int
    date: str
    shift_id: int | None
    start_time: str
    end_time: str
    hours: float
    locked: bool
    source: str


class AssignmentUpdate(BaseModel):
    shift_id: int | None = None
    start_time: str | None = Field(default=None, pattern=TIME_PATTERN)
    end_time: str | None = Field(default=None, pattern=TIME_PATTERN)
    locked: bool | None = None


class SolveResponse(BaseModel):
    status: str
    message: str
    report: dict[str, Any]
    assignments: list[AssignmentRead]
    # 两阶段求解：solved_first_solution | no_solution_yet | precheck_failed
    phase: str | None = None
    task_id: str | None = None
    elapsed_seconds: float | None = None
    # 第一阶段结束后剩余的可用优化时长（秒）
    remaining_seconds: float | None = None


class RestCandidate(BaseModel):
    date: str
    shift_id: int | None
    shift_name: str
    start_time: str
    end_time: str
    hours: float


class HoursGapEmployee(BaseModel):
    employee_id: int
    name: str
    standard_hours: float
    actual_hours: float
    gap_hours: float
    candidates: list[RestCandidate]


class HoursGapResponse(BaseModel):
    month: str
    standard_hours: float
    actual_hours: float
    gap_hours: float
    filled: bool
    employees: list[HoursGapEmployee]


class HoursFillItem(BaseModel):
    employee_id: int
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class HoursFillRequest(BaseModel):
    items: list[HoursFillItem] = Field(min_length=1)
    note: str | None = None


class AdjustmentLogRead(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: int
    plan_id: int
    employee_id: int
    date: str
    shift_id: int | None
    hours: float
    action: str
    detail: str
    created_at: str


class HoursFillResponse(BaseModel):
    created: list[AssignmentRead]
    logs: list[AdjustmentLogRead]
    gap: HoursGapResponse


class CalendarDay(BaseModel):
    date: str
    weekday: str
    is_weekend: bool
    rule_count: int
    staff_total: int
    segment_total: int
    special_total: int
    # 按类别统计当天生效的规则条数（与 special_total 同口径，仅统计对应规则类型）
    rest_total: int = 0
    must_work_total: int = 0
    paid_leave_total: int = 0
    fixed_shift_total: int = 0
    fixed_time_total: int = 0
    all_start_total: int = 0
    start_staff_total: int = 0


class SoftWeightUpdate(BaseModel):
    key: str
    value: float


class SoftWeightItem(BaseModel):
    key: str
    field: str
    label: str
    unit: str
    priority: int
    value: float
    default: float
    rationale: str
    changed: bool


class SettingsPayload(BaseModel):
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    # 软约束权重：只提交要改的项，未提交的保持当前值
    soft_weights: list[SoftWeightUpdate] | None = None


class SettingsRead(BaseModel):
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    soft_weights: list[SoftWeightItem] = Field(default_factory=list)


class ConflictItem(BaseModel):
    code: str
    message: str = ""
    rule_ids: list[int] = Field(default_factory=list)


class HistoryItem(BaseModel):
    title: str
    result: str = ""


class SuggestionOperation(BaseModel):
    action: str
    rule_id: int | None = None
    employee_id: int | None = None
    fields: dict[str, Any] = Field(default_factory=dict)


class ConflictSuggestion(BaseModel):
    id: str
    title: str
    diagnosis: str = ""
    operations: list[SuggestionOperation] = Field(default_factory=list)
    expected: str = ""
    risk: str | None = None
    manual_hint: str | None = None
    invalid: bool = False
    reason: str | None = None


class ConflictSuggestionResponse(BaseModel):
    suggestions: list[ConflictSuggestion]
    warnings: list[str] = Field(default_factory=list)
    # normal=针对已知冲突出方案；precheck=没有冲突时做规则体检
    mode: str = "normal"


class SuggestionRequest(BaseModel):
    conflicts: list[ConflictItem] | None = None
    history: list[HistoryItem] = Field(default_factory=list)
    # 生成方案条数，默认 1；2 表示用户主动选择多方案
    limit: int = Field(default=1, ge=1, le=2)
    # 尚未保存的待添加约束：只进提示词上下文，不参与写库定位
    pending_rules: list[RuleCreate] = Field(default_factory=list)


class SuggestionTaskCreated(BaseModel):
    """异步任务创建响应。"""

    task_id: str
    status: str


class SuggestionTaskResult(BaseModel):
    """异步任务轮询响应：进行中只带状态，成功后带方案，失败带原因。"""

    task_id: str
    status: str
    suggestions: list[ConflictSuggestion] | None = None
    warnings: list[str] | None = None
    error: str | None = None
    error_status: int | None = None
    # normal=针对已知冲突出方案；precheck=没有冲突时的规则体检
    mode: str | None = None


class ConflictInterpretRequest(BaseModel):
    text: str = Field(min_length=1)
    conflicts: list[ConflictItem] | None = None
    # 尚未保存的待添加约束：只进提示词上下文，不参与写库定位
    pending_rules: list[RuleCreate] = Field(default_factory=list)


class RemovalItem(BaseModel):
    rule_id: int
    reason: str | None = None


class RuleUpdateInput(BaseModel):
    """修改现有规则：fields 只能是白名单字段（与排班调整共用校验）。"""

    rule_id: int
    fields: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None


class ConflictUpdatePreview(BaseModel):
    """冲突助手解析出的修改项：原值 → 新值，供前端展示 diff。"""

    rule_id: int
    label: str
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None


class ConflictInterpretResponse(BaseModel):
    rules: list[PreviewRule]
    removals: list[RemovalItem]
    warnings: list[str] = Field(default_factory=list)
    updates: list[ConflictUpdatePreview] = Field(default_factory=list)
    # conflict=有已知冲突；precheck=无冲突时的规则体检
    mode: str = "conflict"


class ConflictApplyRequest(BaseModel):
    operations: list[SuggestionOperation] = Field(default_factory=list)
    rules: list[RuleCreate] = Field(default_factory=list)
    removals: list[int] = Field(default_factory=list)
    # 修改现有规则（冲突助手 updates 段）
    updates: list[RuleUpdateInput] = Field(default_factory=list)
    original_conflicts: list[ConflictItem] = Field(default_factory=list)


class ConflictApplyResponse(BaseModel):
    status: str
    message: str
    solved: bool
    rolled_back: bool
    resolved_codes: list[str]
    remaining_conflicts: list[dict[str, Any]]
    report: dict[str, Any]
    assignments: list[AssignmentRead]
    applied: list[str]


class AdjustInterpretRequest(BaseModel):
    text: str


class AdjustUpdateItem(BaseModel):
    """修改现有规则：fields 只能是白名单字段。"""

    rule_id: int
    fields: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None


class AdjustRemoveItem(BaseModel):
    rule_id: int
    reason: str | None = None


class AdjustInterpretResponse(BaseModel):
    add: list[PreviewRule] = Field(default_factory=list)
    update: list[AdjustUpdateItem] = Field(default_factory=list)
    remove: list[AdjustRemoveItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class AdjustApplyRequest(BaseModel):
    add: list[RuleCreate] = Field(default_factory=list)
    update: list[AdjustUpdateItem] = Field(default_factory=list)
    remove: list[int] = Field(default_factory=list)


class AdjustApplyResponse(BaseModel):
    solved: bool
    rolled_back: bool
    applied: list[str] = Field(default_factory=list)
    status: str
    message: str = ""
    report: dict[str, Any] = Field(default_factory=dict)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    assignments: list[AssignmentRead] = Field(default_factory=list)
