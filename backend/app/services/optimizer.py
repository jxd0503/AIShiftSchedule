# pyright: reportAny=false, reportExplicitAny=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportImplicitStringConcatenation=false, reportUnreachable=false
"""两阶段求解的后台优化与候选管理。

第一阶段在请求内同步跑短时限；成功后（或用户确认继续后）把同一模型、
同一目标函数交给后台线程继续搜索。后台只生成候选快照，绝不覆盖当前排班。
"""
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlmodel import Session, delete, select

from ..models import Assignment, Employee, Plan, PlanCandidate, Rule, Setting, Shift
from .soft_weights import SoftWeights, load_soft_weights, weight_items
from . import solver as solver_service
from .validator import evaluate

# 持续优化的实际时间上限（仅作防御，正常应由「证明最优」或用户停止结束）
CONTINUOUS_SECONDS = 7 * 24 * 3600.0

DEFAULT_FIRST_PHASE_SECONDS = 180.0
DEFAULT_MAX_SECONDS = 900.0
DEFAULT_MIN_IMPROVEMENT = 1.0
DEFAULT_NUM_SEARCH_WORKERS = 0  # 0 表示按 CPU/内存自动计算
DEFAULT_RANDOM_SEED = 42
# 并行搜索上限：留 2 个核给界面与系统，最多 14 个 worker
MAX_SEARCH_WORKERS = 14
WORKER_RESERVED_CORES = 2


def _available_memory_gb() -> float | None:
    """当前可用物理内存（GB）：Windows 用 GlobalMemoryStatusEx，其它平台返回 None。"""
    if os.name != "nt":
        return None
    try:
        import ctypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return float(status.ullAvailPhys) / (1024 ** 3)
    except Exception:  # pragma: no cover - 平台差异，失败时退回按核数计算
        return None


def default_workers() -> int:
    """自动 worker 数：CPU 核数 - 2，最多 14；内存不足时进一步下调。"""
    cores = os.cpu_count() or 4
    workers = max(1, min(cores - WORKER_RESERVED_CORES, MAX_SEARCH_WORKERS))
    free_gb = _available_memory_gb()
    if free_gb is not None:
        # 实测单 worker 峰值约 0.3GB，按 0.5GB 预留，既用满性能又留出安全余量
        workers = min(workers, max(2, int(free_gb * 2)))
    return max(1, workers)


def _session() -> Session:
    from ..db import engine

    return Session(engine)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _setting_float(session: Session, key: str, default: float) -> float:
    row = session.get(Setting, key)
    if row is None:
        return default
    try:
        return float(row.value)
    except (TypeError, ValueError):
        return default


def _setting_int(session: Session, key: str, default: int) -> int:
    row = session.get(Setting, key)
    if row is None:
        return default
    try:
        return int(float(row.value))
    except (TypeError, ValueError):
        return default


def _resolve_workers(session: Session) -> int:
    """worker 数：设置为 0/缺省时按硬件自动计算，并统一限制在安全上限内。"""
    value = _setting_int(session, "solve_num_search_workers", DEFAULT_NUM_SEARCH_WORKERS)
    if value <= 0:
        return default_workers()
    # 即使手工配置了更大的值，也不超过本机可用的安全并行度
    hardware_cap = max(1, (os.cpu_count() or 4) - WORKER_RESERVED_CORES)
    return max(1, min(value, MAX_SEARCH_WORKERS, hardware_cap))


@dataclass
class SolveConfig:
    first_phase_seconds: float
    max_seconds: float
    min_improvement: float
    num_search_workers: int
    random_seed: int
    soft_weights: SoftWeights = field(default_factory=SoftWeights)


def resolve_config(plan: Plan, session: Session) -> SolveConfig:
    """全局 Setting 为默认，计划级字段可覆盖。

    两段预算分开计算：
    - 段 A（同步首解）：solve_first_phase_seconds > 全局 Setting（默认 180s）；
    - 段 B（后台优化）：solve_max_seconds > 全局 Setting（默认 900s）。

    兼容字段 solve_seconds 已废弃、不再参与：它同时被当作总上限和段 A 上限时，
    新计划默认 30 会把后台预算裁没，段 A 用掉十几秒后段 B 只剩十几秒，来不及产出候选。
    """
    first = float(
        plan.solve_first_phase_seconds
        if plan.solve_first_phase_seconds is not None
        else _setting_float(
            session, "solve_first_phase_seconds", DEFAULT_FIRST_PHASE_SECONDS
        )
    )
    max_seconds = float(
        plan.solve_max_seconds
        if plan.solve_max_seconds is not None
        else _setting_float(session, "solve_max_seconds", DEFAULT_MAX_SECONDS)
    )
    first = min(first, max_seconds)
    min_improvement = float(
        plan.solve_min_improvement
        if plan.solve_min_improvement is not None
        else _setting_float(session, "solve_min_improvement", DEFAULT_MIN_IMPROVEMENT)
    )
    return SolveConfig(
        first_phase_seconds=max(1.0, first),
        max_seconds=max(1.0, max_seconds),
        min_improvement=max(0.0, min_improvement),
        num_search_workers=_resolve_workers(session),
        random_seed=_setting_int(session, "solve_random_seed", DEFAULT_RANDOM_SEED),
        soft_weights=load_soft_weights(session),
    )


def load_solve_context(
    session: Session, plan_id: int
) -> tuple[Plan, list[Employee], list[Shift], list[Rule], list[Assignment]]:
    plan = session.get(Plan, plan_id)
    if plan is None:
        raise ValueError("计划不存在")
    employees = list(
        session.exec(select(Employee).where(Employee.plan_id == plan_id)).all()
    )
    shifts = list(session.exec(select(Shift).where(Shift.plan_id == plan_id)).all())
    rules = list(session.exec(select(Rule).where(Rule.plan_id == plan_id)).all())
    locked = list(
        session.exec(
            select(Assignment).where(
                Assignment.plan_id == plan_id, Assignment.locked == True
            )
        ).all()
    )
    return plan, employees, shifts, rules, locked


def serialize_assignments(assignments: list[Assignment]) -> list[dict[str, Any]]:
    return [
        {
            "employee_id": item.employee_id,
            "date": item.date,
            "shift_id": item.shift_id,
            "start_time": item.start_time,
            "end_time": item.end_time,
            "hours": item.hours,
            "locked": item.locked,
            "source": item.source,
        }
        for item in assignments
    ]


def build_assignment_rows(plan_id: int, payload: list[dict[str, Any]]) -> list[Assignment]:
    return [
        Assignment(
            plan_id=plan_id,
            employee_id=row["employee_id"],
            date=row["date"],
            shift_id=row.get("shift_id"),
            start_time=row["start_time"],
            end_time=row["end_time"],
            hours=row["hours"],
            locked=bool(row.get("locked")),
            source=row.get("source", "solver"),
        )
        for row in payload
    ]


# ---------------------------------------------------------------------------- 候选


def _plan_params(plan: Plan) -> dict[str, Any]:
    return {
        "coverage_start": plan.coverage_start,
        "coverage_end": plan.coverage_end,
        "latest_end_time": plan.latest_end_time,
        "latest_end_time_effective": plan.latest_end_time_effective,
        "overnight_next_rest": plan.overnight_next_rest,
        "max_consecutive_days": plan.max_consecutive_days,
    }


@dataclass
class HintCandidate:
    """段 A 的初始解提示。

    stale=True 表示这份排班生成时的计划口径与当前不一致（例如最晚收班时间改过），
    它可能已经违反当前硬约束：只能当搜索方向（AddHint），不能再固定变量使用。
    """

    assignments: list[Assignment]
    stale: bool = False


def hint_from_latest(
    session: Session, plan: Plan, current: list[Assignment] | None
) -> HintCandidate | None:
    """热启动用的初始解：优先用当前排班，其次用最近一次已接受的候选。

    口径变过时不再直接丢弃：解虽然可能已违反当前硬约束，但作为提示仍有参考
    价值，返回 stale=True 让调用方先固定试探、失败后只提示不固定重试。
    """
    expected = _plan_params(plan)
    if current:
        try:
            current_report = json.loads(plan.solve_report) if plan.solve_report else {}
        except json.JSONDecodeError:
            current_report = {}
        stored = current_report.get("params") or {}
        return HintCandidate(current, stale=stored != expected)
    plan_id = plan.id or 0
    row = session.exec(
        select(PlanCandidate)
        .where(PlanCandidate.plan_id == plan_id, PlanCandidate.status == "accepted")
        .order_by(PlanCandidate.id.desc())  # pyright: ignore[reportAttributeAccessIssue, reportOptionalMemberAccess]
    ).first()
    if row is None:
        return None
    try:
        report = json.loads(row.validate_report_json or "{}")
        payload = json.loads(row.assignments_json or "[]")
    except json.JSONDecodeError:
        return None
    if not payload:
        return None
    stored = report.get("params") or {}
    return HintCandidate(build_assignment_rows(plan_id, payload), stale=stored != expected)


def mark_pending_candidates_stale(session: Session | None = None) -> int:
    """进程重启/计划重建后，残留 pending 候选一律 stale，绝不自动替换。"""
    own = session is None
    session = session or _session()
    try:
        candidates = session.exec(
            select(PlanCandidate).where(PlanCandidate.status == "pending")
        ).all()
        for candidate in candidates:
            candidate.status = "stale"
        if candidates:
            session.commit()
        return len(candidates)
    finally:
        if own:
            session.close()


def _mark_pending(plan_id: int, session: Session, status: str) -> int:
    """把该计划所有 pending 候选迁移到指定终态（stale/discarded）。"""
    candidates = session.exec(
        select(PlanCandidate).where(
            PlanCandidate.plan_id == plan_id, PlanCandidate.status == "pending"
        )
    ).all()
    for candidate in candidates:
        candidate.status = status
    return len(candidates)


def stale_pending_candidates(plan_id: int, session: Session) -> int:
    return _mark_pending(plan_id, session, "stale")


def discard_pending_candidates(plan_id: int, session: Session) -> int:
    """取消后台优化时丢弃候选；与「新求解使旧候选失效」的 stale 语义区分。"""
    return _mark_pending(plan_id, session, "discarded")


def _create_candidate(
    session: Session,
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    rules: list[Rule],
    assignments: list[Assignment],
    elapsed_seconds: float,
    objective: float,
    attribution: dict[str, Any] | None = None,
) -> PlanCandidate:
    """候选入库前重新校验；报告里的 objective 用求解器目标值，其余分项复用 validator。"""
    report = evaluate(plan, employees, shifts, rules, assignments)
    # evaluate 本身不计算目标值（固定为 0），这里回填与候选一致的求解目标
    report["objective"] = int(round(objective))
    # 归因沿用求解器逐项累加的结果，保证候选报告与 objective 一致
    if attribution:
        report["attribution"] = attribution
    report["status"] = "FEASIBLE"
    # 段 B 的候选来自完整软目标优化，不再是「仅满足硬约束」方案
    report["feasibility_only"] = False
    report["quality_optimized"] = True
    # 求解说明里展示本轮生效的计划级口径（与第一阶段报告保持一致）
    report["params"] = {
        "coverage_start": plan.coverage_start,
        "coverage_end": plan.coverage_end,
        "latest_end_time": plan.latest_end_time,
        "latest_end_time_effective": plan.latest_end_time_effective,
        "overnight_next_rest": plan.overnight_next_rest,
        "max_consecutive_days": plan.max_consecutive_days,
    }
    breakdown = report.get("breakdown", {})
    candidate = PlanCandidate(
        plan_id=plan.id or 0,
        objective=float(report["objective"]),
        breakdown_json=json.dumps(breakdown, ensure_ascii=False),
        assignments_json=json.dumps(serialize_assignments(assignments), ensure_ascii=False),
        validate_report_json=json.dumps(report, ensure_ascii=False, default=str),
        status="pending",
        created_at=_now_iso(),
        elapsed_seconds=float(elapsed_seconds),
    )
    session.add(candidate)
    session.commit()
    session.refresh(candidate)
    return candidate


def candidate_payload(candidate: PlanCandidate) -> dict[str, Any]:
    return {
        "id": candidate.id,
        "plan_id": candidate.plan_id,
        "objective": candidate.objective,
        "breakdown": json.loads(candidate.breakdown_json or "{}"),
        "status": candidate.status,
        "created_at": candidate.created_at,
        "elapsed_seconds": candidate.elapsed_seconds,
        "assignments_count": len(json.loads(candidate.assignments_json or "[]")),
    }


def list_candidates(plan_id: int, session: Session) -> list[dict[str, Any]]:
    rows = session.exec(
        select(PlanCandidate)
        .where(PlanCandidate.plan_id == plan_id)
        .order_by(PlanCandidate.id.desc())  # pyright: ignore[reportAttributeAccessIssue, reportOptionalMemberAccess]
    ).all()
    return [candidate_payload(row) for row in rows]


def accept_candidate(session: Session, plan: Plan, candidate_id: int) -> dict[str, Any]:
    candidate = session.get(PlanCandidate, candidate_id)
    if candidate is None or candidate.plan_id != plan.id:
        raise ValueError("候选不存在")
    if candidate.status != "pending":
        raise ValueError("候选已失效")
    payload = json.loads(candidate.assignments_json or "[]")
    report = json.loads(candidate.validate_report_json or "{}")
    session.exec(delete(Assignment).where(Assignment.plan_id == plan.id))  # pyright: ignore[reportArgumentType]
    for row in build_assignment_rows(plan.id or 0, payload):
        session.add(row)
    plan.solve_report = json.dumps(report, ensure_ascii=False, default=str)
    plan.status = "solved"
    plan.last_solve_at = _now_iso()
    candidate.status = "accepted"
    stale_pending_candidates(plan.id or 0, session)
    session.commit()
    return {"candidate": candidate_payload(candidate), "report": report}


def discard_candidate(session: Session, plan: Plan, candidate_id: int) -> dict[str, Any]:
    candidate = session.get(PlanCandidate, candidate_id)
    if candidate is None or candidate.plan_id != plan.id:
        raise ValueError("候选不存在")
    if candidate.status == "pending":
        candidate.status = "discarded"
        session.commit()
    return candidate_payload(candidate)


# ------------------------------------------------------------------------ 后台任务


@dataclass
class OptimizeTask:
    task_id: str
    plan_id: int
    started_at: float
    deadline_at: float
    # 总预算（段 A + 段 B 共用），用于进度展示「已用 / 预算」
    budget_seconds: float = 0.0
    # stage: A=只求硬约束可行解，B=带完整软目标优化，done/infeasible=终态
    stage: str = "A"
    status: str = "running"
    objective: float | None = None
    breakdown: dict[str, Any] | None = None
    error: str | None = None
    elapsed_seconds: float = 0.0
    # ---- 进度可视化数据（全部来自 CP-SAT 真实回报，不编造百分比）----
    improvements: list[dict[str, Any]] = field(default_factory=list)
    best_bound: float | None = None
    improvement_count: int = 0
    last_improvement_at: float | None = None
    # 段 A 耗时；进入段 B 后用于把两段的求解时间拼成同一条时间轴
    stage_a_seconds: float | None = None
    solver_time_offset: float = 0.0
    segment_mark_seconds: float | None = None
    quality_optimized: bool = False
    # 持续优化：段 B 不设时间上限，直到证明最优或用户手动停止
    continuous: bool = False
    # 是否已证明最优（CP-SAT 返回 OPTIMAL）
    optimal: bool = False
    # 正在运行的 CpSolver 实例：取消时直接 StopSearch，避免旧任务空转占满 CPU
    cp_solver: Any = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


_LOCK = threading.Lock()
_ACTIVE: dict[int, OptimizeTask] = {}
_TERMINAL: dict[int, OptimizeTask] = {}


def _finish(plan_id: int, task: OptimizeTask, status: str, error: str | None = None) -> None:
    with _LOCK:
        task.status = status
        task.error = error
        if task.elapsed_seconds <= 0:
            task.elapsed_seconds = max(0.0, time.monotonic() - task.started_at)
        if _ACTIVE.get(plan_id) is task:
            del _ACTIVE[plan_id]
        _TERMINAL[plan_id] = task


def active_task(plan_id: int) -> OptimizeTask | None:
    with _LOCK:
        return _ACTIVE.get(plan_id)


def terminal_task(plan_id: int) -> OptimizeTask | None:
    with _LOCK:
        return _TERMINAL.get(plan_id)


def _request_stop(task: OptimizeTask) -> None:
    """请求停止该任务：置取消标记，并让 CP-SAT 立刻结束当前搜索。

    只置标记不够：CP-SAT 不做解回报时标记永远读不到，旧任务会空转到预算结束，
    与新任务抢 CPU（实测会把后续首解耗时从 10 秒拖到 18 秒）。
    """
    task.cancel_event.set()
    solver_instance = task.cp_solver
    if solver_instance is None:
        return
    try:
        solver_instance.StopSearch()
    except Exception:  # pragma: no cover - 求解器已结束/不支持时忽略
        pass


def cancel_task(plan_id: int) -> bool:
    with _LOCK:
        task = _ACTIVE.get(plan_id)
    if task is None:
        return False
    _request_stop(task)
    task.status = "cancelled"
    # 用户主动停止优化时保留已找到的候选，仍可点击「应用更优解」采用它
    return True


def begin_first_phase(plan_id: int, config: SolveConfig) -> OptimizeTask:
    """注册段 A 任务（只求硬约束可行解），返回可在 /solve/status 查询的进度对象。

    段 A 在请求线程内同步求解，任务先挂到 _ACTIVE，前端轮询即可看到「段 A（求可行解）」
    的真实已用时间；段 A 结束后同一个 task 继续用于段 B，曲线不重置。
    """
    with _LOCK:
        previous = _ACTIVE.get(plan_id)
        if previous is not None:
            _request_stop(previous)
            previous.status = "cancelled"
        now = time.monotonic()
        task = OptimizeTask(
            task_id=uuid.uuid4().hex,
            plan_id=plan_id,
            started_at=now,
            deadline_at=now + config.max_seconds,
            budget_seconds=config.max_seconds,
            stage="A",
        )
        _ACTIVE[plan_id] = task
        # 新一轮任务开始，旧的终态记录不再代表当前状态
        _TERMINAL.pop(plan_id, None)
    return task


def abandon_task(task: OptimizeTask) -> None:
    """段 A 未拿到可行解（UNKNOWN/预检失败）时清掉进度对象，状态回到 idle。"""
    with _LOCK:
        if _ACTIVE.get(task.plan_id) is task:
            del _ACTIVE[task.plan_id]
        if _TERMINAL.get(task.plan_id) is task:
            del _TERMINAL[task.plan_id]


def mark_infeasible(plan_id: int, config: SolveConfig) -> OptimizeTask:
    """已证明无解：写入终态进度，前端可显示「已证明无解」。"""
    now = time.monotonic()
    task = OptimizeTask(
        task_id=uuid.uuid4().hex,
        plan_id=plan_id,
        started_at=now,
        deadline_at=now,
        budget_seconds=config.max_seconds,
        stage="infeasible",
        status="infeasible",
    )
    _finish(plan_id, task, "infeasible")
    return task


def _record_point(
    task: OptimizeTask,
    timeline_seconds: float,
    objective: float,
    best_bound: float | None,
) -> None:
    """记录一次真实的 CP-SAT 改进点（不编造任何百分比）。"""
    task.objective = objective
    if best_bound is not None:
        task.best_bound = best_bound
    task.improvement_count += 1
    task.last_improvement_at = time.monotonic()
    task.improvements.append(
        {
            "elapsed_seconds": round(max(0.0, timeline_seconds), 1),
            "objective": round(float(objective), 1),
            "stage": task.stage,
        }
    )
    # 5 分钟搜索按 2 秒节流最多 150 点，这里只保留最近 120 个，避免无限增长
    if len(task.improvements) > 120:
        del task.improvements[: len(task.improvements) - 120]


def _run_background(
    task: OptimizeTask,
    config: SolveConfig,
    baseline_objective: float | None,
    hint_assignments: list[Assignment] | None = None,
    start_stage: str = "B",
) -> None:
    try:
        with _session() as session:
            plan, employees, shifts, rules, locked = load_solve_context(session, task.plan_id)
            current_report = json.loads(plan.solve_report) if plan.solve_report else {}
            fallback = (
                hint_from_latest(session, plan, None) if start_stage == "A" else None
            )
        # 当前没有可用排班时，后台找到的任何可行解都值得提示；
        # 段 A 的解目标值不是真实目标值（objective=null），不能当基线
        baseline = baseline_objective
        if baseline is None and not current_report.get("feasibility_only"):
            baseline = current_report.get("objective")

        hint = hint_assignments or (fallback.assignments if fallback is not None else None)
        # 过期提示解只当搜索方向：固定它等于假设旧口径仍成立，会误判不可行
        hint_stale = bool(fallback is not None and fallback.stale)

        def bind_solver(solver_instance: Any) -> None:
            """把当前 CpSolver 挂到任务上，取消时可以立刻 StopSearch。"""
            with _LOCK:
                task.cp_solver = solver_instance

        # 段 A：仅当请求内没拿到可行解、用户确认继续时执行；找到解后自动进入段 B
        if start_stage == "A":
            with _LOCK:
                task.stage = "A"
            if hint is None:
                remaining = max(0.5, task.deadline_at - time.monotonic())
                # 段 A 只求可行：按首解预算封顶，避免持续优化模式下无上限空转
                a_budget = min(remaining, config.first_phase_seconds)
                outcome_a = solver_service.solve(
                    plan,
                    employees,
                    shifts,
                    rules,
                    locked,
                    time_limit=a_budget,
                    random_seed=config.random_seed,
                    num_search_workers=config.num_search_workers,
                    objective_mode="feasibility",
                    allow_fix_hint=not hint_stale,
                    solver_hook=bind_solver,
                    soft_weights=config.soft_weights,
                )
                if task.cancel_event.is_set() or active_task(task.plan_id) is not task:
                    _finish(task.plan_id, task, "cancelled")
                    return
                if outcome_a.status not in ("OPTIMAL", "FEASIBLE"):
                    _finish(task.plan_id, task, "done")
                    return
                hint = outcome_a.assignments
                baseline = None
            stage_a = max(0.0, time.monotonic() - task.started_at)
            with _LOCK:
                task.stage_a_seconds = stage_a
                task.solver_time_offset = stage_a
                task.segment_mark_seconds = stage_a
                task.stage = "B"

        # 段 B0：把段 A 的排班固定住，在完整软目标下算出它的真实目标值。
        # 这一步既给曲线一个真实起点，也避免「段 A 的目标值 0」污染目标比较。
        # incumbent = 当前模型下已验证可行的最好目标值；段 B 只认比它更优的解。
        incumbent: float | None = None
        if hint:
            remaining = max(0.5, task.deadline_at - time.monotonic())
            fixed = solver_service.solve(
                plan,
                employees,
                shifts,
                rules,
                locked,
                time_limit=min(10.0, remaining),
                hint_assignments=hint,
                fix_hint=True,
                random_seed=config.random_seed,
                num_search_workers=config.num_search_workers,
                solver_hook=bind_solver,
                soft_weights=config.soft_weights,
            )
            if (
                fixed.status in ("OPTIMAL", "FEASIBLE")
                and fixed.report.get("objective") is not None
            ):
                incumbent = float(fixed.report["objective"])
                hint = fixed.assignments
                _record_point(task, time.monotonic() - task.started_at, incumbent, None)
                # 段 A 方案的真实目标值就是质量基线：段 B 必须比它更好才值得提示
                baseline = incumbent
            if task.cancel_event.is_set() or active_task(task.plan_id) is not task:
                _finish(task.plan_id, task, "cancelled")
                return

        last_breakdown_at = 0.0
        best_snapshot: solver_service.SolutionSnapshot | None = None
        last_candidate_at = 0.0
        last_candidate_objective: float | None = None

        def sink(snapshot: solver_service.SolutionSnapshot) -> bool | None:
            nonlocal last_breakdown_at, best_snapshot, incumbent
            nonlocal last_candidate_at, last_candidate_objective
            # CP-SAT 的提示解只影响搜索方向，不代表已找到的解：回调可能先报出
            # 比初始解更差的目标值。这里只记录严格优于已知最好的解，曲线只会下行。
            if incumbent is None or snapshot.objective < incumbent - 1e-6:
                incumbent = snapshot.objective
                best_snapshot = snapshot
                # 段 B 的求解时间从段 A 结束时继续累加，曲线不重置
                _record_point(
                    task,
                    snapshot.elapsed_seconds + task.solver_time_offset,
                    snapshot.objective,
                    snapshot.best_bound,
                )
                # 即时提示：改进达到阈值且距上次候选 ≥15 秒时先落一个候选，
                # 用户不必等到整轮优化结束就能看到并应用更优解。
                now = time.monotonic()
                better_enough = (
                    last_candidate_objective is None
                    or last_candidate_objective - snapshot.objective
                    >= config.min_improvement
                )
                if better_enough and now - last_candidate_at >= 15.0:
                    try:
                        with _session() as session:
                            fresh_plan, fresh_employees, fresh_shifts, fresh_rules, _ = (
                                load_solve_context(session, task.plan_id)
                            )
                            stale_pending_candidates(task.plan_id, session)
                            _create_candidate(
                                session,
                                fresh_plan,
                                fresh_employees,
                                fresh_shifts,
                                fresh_rules,
                                snapshot.assignments,
                                snapshot.elapsed_seconds + task.solver_time_offset,
                                snapshot.objective,
                                attribution=snapshot.attribution,
                            )
                        last_candidate_at = now
                        last_candidate_objective = snapshot.objective
                    except Exception:
                        # 候选落库失败不影响求解本身，下一轮改进会再尝试
                        pass
            if task.cancel_event.is_set():
                return False
            # 状态里的分项指标按 5 秒节流重算，避免拖慢 CP-SAT
            now = time.monotonic()
            if now - last_breakdown_at >= 5.0:
                last_breakdown_at = now
                try:
                    with _session() as session:
                        plan, employees, shifts, rules, _ = load_solve_context(
                            session, task.plan_id
                        )
                        report = evaluate(plan, employees, shifts, rules, snapshot.assignments)
                        task.breakdown = report.get("breakdown")
                except Exception:
                    # 指标计算失败不影响求解本身
                    task.breakdown = None
            return None

        # 段 B 的实际求解从此刻开始：把 B0 的耗时也计入时间轴偏移，
        # 保证曲线点的横轴严格递增（段 A → B0 → B 优化）
        with _LOCK:
            task.solver_time_offset = time.monotonic() - task.started_at
        remaining = max(0.5, task.deadline_at - time.monotonic())
        outcome = solver_service.solve(
            plan,
            employees,
            shifts,
            rules,
            locked,
            time_limit=remaining,
            solution_callback=sink,
            random_seed=config.random_seed,
            num_search_workers=config.num_search_workers,
            hint_assignments=hint,
            objective_upper_bound=incumbent,
            solver_hook=bind_solver,
            soft_weights=config.soft_weights,
        )
        elapsed = time.monotonic() - task.started_at
        if active_task(task.plan_id) is not task:
            with _LOCK:
                task.status = "cancelled"
            return
        if task.cancel_event.is_set():
            _finish(task.plan_id, task, "cancelled")
            return
        # CP-SAT 返回 OPTIMAL：目标值已被证明最优，持续优化到此结束
        if outcome.status == "OPTIMAL":
            with _LOCK:
                task.optimal = True
        if outcome.status not in ("OPTIMAL", "FEASIBLE"):
            _finish(task.plan_id, task, "done")
            return

        assignments = (
            best_snapshot.assignments if best_snapshot is not None else outcome.assignments
        )
        # 归因必须与最终落库的排班一致：优先用同一快照的归因，回退到终解归因
        attribution = (
            best_snapshot.attribution
            if best_snapshot is not None and best_snapshot.attribution
            else outcome.report.get("attribution")
        )
        objective_value = outcome.report.get("objective")
        if objective_value is None:
            # 完整目标模式下必然有目标值；缺失说明本轮没有可用的优化结果
            _finish(task.plan_id, task, "done")
            return
        objective = float(objective_value)
        if incumbent is None or objective < incumbent - 1e-6:
            # 终解可能落在 2 秒节流的间隙里，补一个真实改进点
            incumbent = objective
            assignments = outcome.assignments
            attribution = outcome.report.get("attribution") or attribution
            _record_point(task, elapsed, objective, task.best_bound)
        else:
            # 终解不优于已知最好解时，一律沿用已知最好解的目标值，绝不倒退
            objective = incumbent
        improved = baseline is None or (float(baseline) - objective) >= config.min_improvement
        if not improved:
            # 改进幅度小于阈值：不生成候选、不推提醒（只更新状态）
            _finish(task.plan_id, task, "done")
            return
        task.quality_optimized = True
        # 已被取消或被新任务取代时，绝不落候选（被取代的任务不写终态，避免覆盖新任务）
        if active_task(task.plan_id) is not task:
            with _LOCK:
                task.status = "cancelled"
            return
        if task.cancel_event.is_set():
            _finish(task.plan_id, task, "cancelled")
            return
        with _session() as session:
            plan, employees, shifts, rules, _ = load_solve_context(session, task.plan_id)
            candidate = _create_candidate(
                session,
                plan,
                employees,
                shifts,
                rules,
                assignments,
                elapsed,
                objective,
                attribution=attribution,
            )
            task.breakdown = json.loads(candidate.breakdown_json)
        _finish(task.plan_id, task, "done")
    except Exception as exc:  # pragma: no cover - 防御性兜底
        _finish(task.plan_id, task, "failed", str(exc))


def start_after_first_phase(
    plan_id: int,
    session: Session,
    config: SolveConfig,
    phase1_elapsed: float,
    baseline_objective: float | None,
    *,
    task: OptimizeTask | None = None,
    hint_assignments: list[Assignment] | None = None,
    start_stage: str = "B",
) -> OptimizeTask | None:
    """段 A 成功后自动续跑段 B：持续优化直到证明最优或用户停止。"""
    stale_pending_candidates(plan_id, session)
    session.commit()
    if task is None:
        task = begin_first_phase(plan_id, config)
    with _LOCK:
        # 段 B 不设时间预算：一直跑，直到 CP-SAT 给出 OPTIMAL 或用户点「停止优化」
        task.continuous = True
        task.budget_seconds = 0.0
        task.deadline_at = task.started_at + CONTINUOUS_SECONDS
        if start_stage == "B":
            task.stage = "B"
            # 段 A 实际跑了（哪怕耗时短到 0.0），时间轴照常标一个分段点
            task.stage_a_seconds = phase1_elapsed
            task.solver_time_offset = phase1_elapsed
            task.segment_mark_seconds = phase1_elapsed
        else:
            task.stage = "A"
    thread = threading.Thread(
        target=_run_background,
        args=(task, config, baseline_objective, hint_assignments, start_stage),
        daemon=True,
        name=f"optimize-plan-{plan_id}",
    )
    task.thread = thread
    thread.start()
    return task


def status_payload(plan_id: int, session: Session) -> dict[str, Any]:
    task = active_task(plan_id)
    terminal = terminal_task(plan_id)
    plan = session.get(Plan, plan_id)
    candidates = [
        item
        for item in list_candidates(plan_id, session)
        if item["status"] == "pending"
    ]
    current_report = json.loads(plan.solve_report) if plan and plan.solve_report else {}

    source = task if task is not None else terminal
    if task is not None:
        phase = "background"
        status = task.status
        elapsed = time.monotonic() - task.started_at
    elif terminal is not None:
        phase = terminal.status if terminal.status in ("cancelled", "failed", "infeasible") else "done"
        status = terminal.status
        elapsed = terminal.elapsed_seconds
    else:
        phase = "idle"
        status = "idle"
        elapsed = 0.0

    objective = (
        source.objective
        if source is not None and source.objective is not None
        else current_report.get("objective")
    )
    breakdown = (
        source.breakdown
        if source is not None and source.breakdown is not None
        else current_report.get("breakdown")
    )
    if source is None:
        stage = "idle"
    elif phase == "background":
        stage = source.stage if source.stage in ("A", "B") else "A"
    elif status == "infeasible":
        stage = "infeasible"
    else:
        stage = "done"

    improvements = list(source.improvements)[-60:] if source is not None else []
    since_last = None
    if source is not None and source.last_improvement_at is not None:
        since_last = round(max(0.0, time.monotonic() - source.last_improvement_at), 1)

    return {
        "phase": phase,
        "status": status,
        "stage": stage,
        "task_id": source.task_id if source is not None else None,
        "elapsed_seconds": round(float(elapsed or 0), 1),
        "budget_seconds": round(float(source.budget_seconds), 1) if source is not None else 0.0,
        "objective": objective,
        "best_bound": source.best_bound if source is not None else None,
        "improvements": improvements,
        "improvement_count": source.improvement_count if source is not None else 0,
        "since_last_improvement_seconds": since_last,
        "segment_mark_seconds": source.segment_mark_seconds if source is not None else None,
        "stage_a_seconds": source.stage_a_seconds if source is not None else None,
        "quality_optimized": bool(source.quality_optimized) if source is not None else False,
        # 持续优化（直到最优或停止）与「已证明最优」标记，供界面提示
        "continuous": bool(source.continuous) if source is not None else False,
        "optimal": bool(source.optimal) if source is not None else False,
        "feasibility_only": bool(current_report.get("feasibility_only")),
        "breakdown": breakdown,
        "candidates": candidates,
        "plan_status": plan.status if plan else "draft",
        "error": source.error if source is not None else None,
    }


def reset_terminal(plan_id: int) -> None:
    with _LOCK:
        _TERMINAL.pop(plan_id, None)
        task = _ACTIVE.get(plan_id)
    if task is not None:
        _request_stop(task)
        task.status = "cancelled"
