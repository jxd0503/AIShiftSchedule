from dataclasses import dataclass, fields
from typing import Any

from sqlmodel import Session, select

from ..models import Setting

# 软约束权重分层：数字越大越不该被牺牲。
# 单位（unit）说明归一化方式：
#   - 人次·天：每人每天记 1 次违约（软休规则未命中）
#   - 小时：按缺口的实际小时数累加（过短班次、与 8h 的偏差）
#   - 人·小时：覆盖缺口按「缺口人数 × 缺口小时」累加，整天缺口记 8 人·小时
#   - 人次：按缺员/超员人数累加（软人数上限的超员）
#   - 班次：按班次数累加（超额班次、8h 班次奖励）
#   - 30分钟：按偏离覆盖起点的 30 分钟段数累加
#   - 当量：求解器松弛变量自身的单位（人/天/次，取决于规则类型）
# 这样不同类别的「条数」不会天然压过违约量大的类别。


@dataclass(frozen=True)
class SoftWeightSpec:
    key: str
    field: str
    label: str
    unit: str
    priority: int
    default: float
    rationale: str


SOFT_WEIGHT_SPECS: tuple[SoftWeightSpec, ...] = (
    SoftWeightSpec(
        key="soft_weight_soft_rest",
        field="soft_rest",
        label="软休未命中",
        unit="人次·天",
        priority=1,
        default=1000.0,
        rationale="员工显式写下的「尽量某天休息」，属于个人偏好，优先级最高；"
        "按人次·天计，避免只命中少数员工却被当成整体满足。",
    ),
    SoftWeightSpec(
        key="soft_weight_short_shift",
        field="short_shift",
        label="过短班次",
        unit="小时（低于 5 小时部分）",
        priority=2,
        default=600.0,
        rationale="不足 5 小时的班次属于明显不合理安排；按短缺小时数计权，"
        "4h 班记 1×600=600，加上非 8h 偏差后仍低于一个人整天的覆盖缺口（8×120=960），"
        "避免用空缺去换掉短班。",
    ),
    SoftWeightSpec(
        key="soft_weight_coverage",
        field="coverage",
        label="覆盖缺口",
        unit="人·小时（缺口人数 × 缺口小时）",
        priority=3,
        default=120.0,
        rationale="覆盖缺口按人·小时累计：一个整天缺口=8×120=960，"
        "高于 4h 短班的总代价 720，求解器会优先排短班而不是留空缺；"
        "同时 960 低于软休未命中的 1000，个人显式偏好仍排在全局覆盖之前。"
        "按缺口实际小时数归一化，1 小时时段缺口不会被当成 1 个整天缺口。"
        "此条与 hours_deviation（非 8 小时班偏差）解耦：它只修正覆盖缺口的定标倒挂，"
        "不改变 8 小时偏好强度。",
    ),
    SoftWeightSpec(
        key="soft_weight_overstaff",
        field="overstaff",
        label="超员人数",
        unit="人次",
        priority=3,
        default=300.0,
        rationale="软人数上限被突破属于「多排人」的另一种违约，"
        "不随覆盖窗口长短放大，因此单独按人次计权，与覆盖缺口分开归因。",
    ),
    SoftWeightSpec(
        key="soft_weight_other_soft",
        field="other_soft",
        label="其他软规则",
        unit="当量",
        priority=3,
        default=300.0,
        rationale="软性工时条件、连休上限、周期天数等规则，与覆盖缺口同属全局层面；"
        "按求解器松弛变量的当量计权，规则条数多也不会天然放大。",
    ),
    SoftWeightSpec(
        key="soft_weight_monthly_deviation",
        field="monthly_deviation",
        label="月工时偏离",
        unit="小时（低于或超出目标）",
        priority=3,
        default=50.0,
        rationale="员工档案的月工时目标是软性指标：低于或超出目标都按小时计惩罚，"
        "让排班围绕目标分布，同时允许为休息偏好或覆盖需求适度让步。"
        "只惩罚缺口会诱导无上限地多排 8h 班来赚取 8 小时班奖励；"
        "双向偏差把权重压低在覆盖缺口（120/人·小时）之下，覆盖需求仍是第一优先级。"
        "显式 MONTHLY_HOURS 规则仍按规则硬软处理，不经过此权重。",
    ),
    SoftWeightSpec(
        key="soft_weight_long_shift",
        field="long_shift",
        label="超额班次",
        unit="班次",
        priority=4,
        default=100.0,
        rationale="超过 8 小时的班次，属于最容易让步的偏好；"
        "按班次计，避免长时间班次被多次累计惩罚而挤掉覆盖。",
    ),
    SoftWeightSpec(
        key="soft_weight_hours_deviation",
        field="hours_deviation",
        label="非 8 小时班偏差",
        unit="小时",
        priority=4,
        default=40.0,
        rationale="与 8 小时的偏差（双向），是最低优先级的质量偏好，按小时计。"
        "曾尝试降到 30，但实测会实质削弱「每天尽量排 8 小时」："
        "2027-10 恰好 8h 占比 76.9%→50.0%、非 8h 班次 24→54、长班 7→17，"
        "且 1200 秒预算下 objective 仍为 49790（A 为 32677），"
        "属权重偏好变坏而非收敛不足，故回退到 40 以保持 8 小时班次约束强度。",
    ),
    SoftWeightSpec(
        key="soft_weight_start_deviation",
        field="start_deviation",
        label="上班时刻偏离",
        unit="30分钟",
        priority=4,
        default=2.0,
        rationale="常规班越接近覆盖起点越贴合实际，属辅助性偏好，权重很小。",
    ),
    SoftWeightSpec(
        key="soft_weight_eight_hour_bonus",
        field="eight_hour_bonus",
        label="8 小时班奖励",
        unit="班次（负项）",
        priority=4,
        default=1.0,
        rationale="正好 8 小时的班次轻微加分（目标里的负项），用于在同等条件下优先选 8h 班。",
    ),
    SoftWeightSpec(
        key="soft_weight_consecutive_pair",
        field="consecutive_pair",
        label="连续上班对",
        unit="对",
        priority=4,
        default=1.0,
        rationale="鼓励工作日之间插入休息，属最弱的辅助偏好，保持原有的 1 分/对。",
    ),
)


@dataclass(frozen=True)
class SoftWeights:
    soft_rest: float = 1000.0
    short_shift: float = 600.0
    coverage: float = 120.0
    overstaff: float = 300.0
    other_soft: float = 300.0
    monthly_deviation: float = 50.0
    long_shift: float = 100.0
    hours_deviation: float = 40.0
    start_deviation: float = 2.0
    eight_hour_bonus: float = 1.0
    consecutive_pair: float = 1.0


SPEC_BY_KEY = {spec.key: spec for spec in SOFT_WEIGHT_SPECS}


def _specs_by_field() -> dict[str, SoftWeightSpec]:
    return {spec.field: spec for spec in SOFT_WEIGHT_SPECS}


def weights_from_values(values: dict[str, Any]) -> SoftWeights:
    """从 Setting 原始值构造权重；缺失或非法值回退到默认值。"""
    kwargs: dict[str, float] = {}
    for spec in SOFT_WEIGHT_SPECS:
        raw = values.get(spec.key)
        try:
            value = float(raw) if raw is not None and str(raw).strip() else spec.default
        except (TypeError, ValueError):
            value = spec.default
        kwargs[spec.field] = max(0.0, value)
    return SoftWeights(**kwargs)


def load_soft_weights(session: Session) -> SoftWeights:
    rows = {row.key: row.value for row in session.exec(select(Setting)).all()}
    return weights_from_values(rows)


def save_soft_weights(session: Session, updates: dict[str, float]) -> SoftWeights:
    """按 Setting key 保存权重；未知 key 或负值直接忽略，避免脏配置进库。"""
    for key, value in updates.items():
        spec = SPEC_BY_KEY.get(key)
        if spec is None or value is None:
            continue
        try:
            normalized = max(0.0, float(value))
        except (TypeError, ValueError):
            continue
        row = session.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=str(normalized))
        else:
            row.value = str(normalized)
        session.add(row)
    session.commit()
    return load_soft_weights(session)


def weight_items(weights: SoftWeights) -> list[dict[str, Any]]:
    """把当前权重展开成界面/报告可读的条目。"""
    specs = _specs_by_field()
    rows: list[dict[str, Any]] = []
    for spec in SOFT_WEIGHT_SPECS:
        value = float(getattr(weights, spec.field))
        rows.append(
            {
                "key": spec.key,
                "field": spec.field,
                "label": spec.label,
                "unit": spec.unit,
                "priority": spec.priority,
                "value": value,
                "default": spec.default,
                "rationale": spec.rationale,
                "changed": abs(value - spec.default) > 1e-9,
            }
        )
    # 防御性排序：按优先级，再按默认权重从高到低
    rows.sort(key=lambda row: (row["priority"], -float(specs[row["field"]].default)))
    return rows


def scheme_name(weights: SoftWeights) -> str:
    changed = [
        spec.label
        for spec in SOFT_WEIGHT_SPECS
        if abs(float(getattr(weights, spec.field)) - spec.default) > 1e-9
    ]
    if not changed:
        return "默认方案（4 级优先级）"
    return "自定义方案（已调整：" + "、".join(changed) + "）"


def unit_weight(weights: SoftWeights, field: str) -> float:
    if field not in {spec.field for spec in SOFT_WEIGHT_SPECS}:
        return 0.0
    return float(getattr(weights, field))


# 供求解器按字段取权重时做静态检查（避免拼写错误）
WEIGHT_FIELDS = {spec.field for spec in SOFT_WEIGHT_SPECS}
assert {item.name for item in fields(SoftWeights)} == WEIGHT_FIELDS
