# pyright: reportAny=false, reportExplicitAny=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false, reportImplicitStringConcatenation=false
# 说明：模型返回的 JSON 结构天然是动态的，解析层用 Any 逐字段校验；
# 这里只关闭由此产生的类型噪音，真实错误仍然保留。
import json
import logging
import re
import time
from datetime import date, timedelta
from typing import Any

from openai import APITimeoutError, OpenAI
from openai.types.chat import ChatCompletionMessageParam
from sqlmodel import Session, select

from ..models import TYPE_DEFAULT_HARD, Employee, Plan, Rule, Setting, Shift
from ..schemas import ParseResponse, PreviewRule
from .calendar_service import (
    effective_hours,
    intersect_dates,
    month_bounds,
    month_dates,
    normalize_hhmm,
    parse_interval,
)
from .rule_fields import (
    COUNT_OPS,
    DAY_COUNT_TYPES,
    HOURS_TYPES,
    PERIODS,
    ROLE_TYPES,
    SHIFT_REF_TYPES,
    SINGLE_ROLE_TYPES,
    TIME_OPS,
    WEEKDAY_TYPES,
    normalize_count,
    normalize_hours_op,
    normalize_role,
    normalize_shift_id,
    normalize_weekdays,
    normalize_work_hours,
)

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
PARSE_TIMEOUT_SECONDS = 180
# 冲突修改解析没有本地兜底，拆成两次较短尝试，总等待上限仍约 180 秒
ADVICE_TIMEOUT_SECONDS = 90
logger = logging.getLogger(__name__)

RULE_TYPES = set(TYPE_DEFAULT_HARD)
EMPLOYEE_REQUIRED = {
    "PAID_LEAVE",
    "FIXED_SHIFT",
    "FIXED_TIME",
    "SHIFT_LIMIT",
}
# 人员可选：留空表示全员生效
EMPLOYEE_OPTIONAL = {
    "REST",
    "MUST_WORK",
    "SHIFT_HOURS",
    "MAX_REST_DAYS",
    "MIN_REST_DAYS",
    "MAX_WORK_DAYS",
    "MAX_CONSECUTIVE_WORK",
    "MONTHLY_HOURS",
    "BAN_SHIFT",
}
# 「全部人员」的常见写法：解析成全员规则，而不是报「员工名单里没有」
ALL_EMPLOYEE_WORDS = {"全员", "全部人员", "所有人", "全体", "全部", "全体员工"}
SHIFT_REQUIRED = {"FIXED_SHIFT", "SHIFT_STAFF", "BAN_SHIFT", "SINGLE_ROLE_SHIFT"}
TIME_REQUIRED = {"FIXED_TIME", "ALL_START_AT", "SEGMENT_STAFF", "START_STAFF"}
RANGE_REQUIRED = {"SEGMENT_STAFF"}
COUNT_REQUIRED = {
    "DAY_STAFF",
    "SEGMENT_STAFF",
    "SHIFT_STAFF",
    "START_STAFF",
    "END_STAFF",
    "MAX_STAFF",
    "MIN_REST_DAYS",
    "MAX_WORK_DAYS",
    "MAX_CONSECUTIVE_WORK",
    "MONTHLY_HOURS",
}
# 排班调整（update）允许改的字段所适用的规则类型；白名单与字段校验口径见 rule_fields
ADJUST_COUNT_TYPES = {
    "DAY_STAFF",
    "SEGMENT_STAFF",
    "START_STAFF",
    "END_STAFF",
    "SHIFT_STAFF",
    "MAX_STAFF",
    "MAX_REST_DAYS",
    "MIN_REST_DAYS",
    "MAX_WORK_DAYS",
    "MAX_CONSECUTIVE_WORK",
    "MONTHLY_HOURS",
    "SINGLE_ROLE_SHIFT",
}
ADJUST_TIME_TYPES = {
    "SEGMENT_STAFF",
    "START_STAFF",
    "END_STAFF",
    "FIXED_TIME",
    "ALL_START_AT",
    "SHIFT_LIMIT",
}
# 支持改「上班时刻比较方向」与「统计周期」的类型
ADJUST_TIME_OP_TYPES = {"START_STAFF", "END_STAFF", "SHIFT_LIMIT"}
# 支持改「人数方向」（ge=至少 / le=至多）的类型
ADJUST_COUNT_OP_TYPES = {"START_STAFF", "END_STAFF"}
ADJUST_PERIOD_TYPES = DAY_COUNT_TYPES
# 强制表述：命中后即便模型给了 hard=false 也按硬约束处理
HARD_PATTERNS = (
    re.compile(r"必须|一定要|务必|不能|不许|不允许|不可以|不得"),
    re.compile(r"(要|需要|得)\s*\d+\s*(个|名)?人"),
    re.compile(r"(要|需要)\s*(有人|人)\s*(在|于)?\s*\d{1,2}:\d{2}"),
)
# 放宽表述：与强制表述同时出现时保持模型判断并提示，不做猜测
SOFT_PATTERNS = re.compile(r"尽量|最好|优先|能.{0,3}就|(?<!不)可以")
# 人数上限表述：系统只支持最少人数，命中后按硬约束处理并提示
LIMIT_PATTERN = re.compile(r"(只|最多|不超过)[^0-9\n]{0,3}\d+\s*(个|名)?\s*人")

SYSTEM_PROMPT = """你是排班约束解析器。把用户的中文排班要求转换成严格的 JSON。

只允许以下规则类型：
- REST：员工休息。需要 date_start、date_end，可选 employee_name（留空表示全员）。
- MUST_WORK：员工必须上班（不能休息）。需要 date_start、date_end，可选 employee_name（留空表示全员）。与同日 REST 同时存在时以 MUST_WORK 为准。
- PAID_LEAVE：员工有薪休假，一天计 8 小时工时，当天不安排上班、不计入上班人数。需要 employee_name、count（天数，必须等于用户说出的天数，未说时为 1）；date_start、date_end 表示可选日期范围：都留空表示由系统在当月休息区间内挑日期；只填 date_start 且 count=N 表示从该日起连续 N 天；填 date_start 与 date_end 且 count=N 表示在该区间内选 N 天。
- FIXED_SHIFT：员工上指定班次。需要 employee_name、date_start、date_end、shift_name。
- FIXED_TIME：员工按指定时刻上班。需要 employee_name、date_start、date_end、start_time。
- ALL_START_AT：全员当天统一按指定时刻上班（特殊班次豁免）。需要 date_start、date_end、start_time。
- DAY_STAFF：每天至少 N 人上普通班。需要 date_start、date_end、count。
- SEGMENT_STAFF：每天指定时段至少 N 人覆盖。需要 date_start、date_end、start_time、end_time、count。
- START_STAFF：每天在指定时刻上班的人数满足条件（按班次开始时间统计，不要求连续覆盖）。需要 date_start、date_end、start_time、count，可选 time_op（eq=开始时刻等于该时刻，默认；le=开始时刻不晚于该时刻；ge=开始时刻不早于该时刻），可选 count_op（ge=人数至少 count，默认；le=人数至多 count）。
- SHIFT_STAFF：每天指定班次至少 N 人（盘点）。需要 date_start、date_end、shift_name、count。
- END_STAFF：每天在指定时刻下班的人数满足条件（按班次结束时间统计）。需要 date_start、date_end、end_time、count，可选 time_op（eq=结束时刻等于该时刻，默认；le=结束时刻不晚于该时刻；ge=结束时刻不早于该时刻），可选 count_op（ge=人数至少 count，默认；le=人数至多 count）。用户说「有三人 20:00 下班」时输出 END_STAFF（end_time=20:00、count=3、count_op=ge）。
- SHIFT_LIMIT：只限制员工「上班日」能排的班次，不强制其上班（休息日自动让路）。需要 employee_name、date_start、date_end，并给出下列条件之一：shift_name（该员工上班日只能上该班次），或 start_time+time_op（上班时刻必须满足条件），或 end_time+time_op（下班时刻必须满足条件）。用户说「某人上班日必须早班/只能上早班」「某人必须排晚班」时使用该类型，不要用 FIXED_TIME 或 FIXED_SHIFT。
- SINGLE_ROLE_SHIFT：当某岗位当天上班人数不超过 count 时，这些上班的人都必须排入指定班次（典型：「仅 1 名主管上班时，该主管必须上 A2I」，count=1）。需要 role、shift_name、date_start、date_end，count 默认 1；可选 weekdays。若当天该岗位上人数超过 count，本规则不生效（此时用其它规则表达 7:30/20:30 等要求）。
- MAX_STAFF：每天上班人数不超过 N（少排可以，多排不允许）。需要 date_start、date_end、count（0~99，count=0 表示当天不排任何人/停业）。
- SHIFT_HOURS：指定日期内，员工当天班次的工时需满足条件。需要 date_start、date_end、work_hours、hours_op（eq/ge/le）、可选 weekdays、可选 employee_name。hard 默认 false。
- MAX_REST_DAYS：指定日期内，员工连续休息天数不得超过 count（count 为允许的最大连休天数）。需要 date_start、date_end、count，可选 employee_name。hard 默认 true。
- MIN_REST_DAYS：指定日期内，每名员工休息天数不少于 count。需要 date_start、date_end、count（1~31）、period（week=每个自然周至少休 N 天；month=整个区间至少休 N 天，默认 month），可选 employee_name（留空表示全员）。hard 默认 true。
- MAX_WORK_DAYS：指定日期内，每名员工上班天数不超过 count。字段同 MIN_REST_DAYS：date_start、date_end、count（1~31）、period（week=每个自然周最多上 N 天；month=整段区间最多上 N 天，默认 month），可选 employee_name（留空表示全员）。hard 默认 true。
- MAX_CONSECUTIVE_WORK：指定日期内，员工连续上班天数不得超过 count。需要 date_start、date_end、count（1~31），可选 employee_name（留空表示全员）。hard 默认 true。该规则与计划级「连续上班上限」同时生效，求解时自动取更严格值；日期范围跨月时只按计划月份内裁剪。
- BAN_SHIFT：禁止员工（留空表示全员）在指定日期内被排入某班次。需要 date_start、date_end、shift_name（必须在班次目录内），可选 employee_name。hard 默认 true。
- MONTHLY_HOURS：员工整月总工时，硬等值。需要 count（目标小时数，1~744），可选 employee_name（留空表示全员）。出现「每人工时N」「每人月工时N」「每人N小时」「月度工时N」「工时N」时输出该类型，员工留空，不要写入 warnings。

规则：
1. 日期一律输出 YYYY-MM-DD。用户说“3号”“下周三”等，按给定月份换算；用户写“11.2”“11/2”“11-2”时按计划月份的年月换算，例如计划月份为 2026-11 时输出 2026-11-02，不要输出其它格式。
2. 时间一律输出 HH:MM，24 小时制。用户说“早上8点”输出 08:00，“晚上10点”输出 22:00。计划的覆盖时段为 __COVERAGE_START__-__COVERAGE_END__，涉及「覆盖 / 持续在岗」的 SEGMENT_STAFF 以用户消息中的覆盖时段为准，不要写死默认窗口。
3. 班次名称必须来自班次目录；员工姓名必须来自员工名单；无法匹配时保持原词，不要编造。
4. 不确定的规则不要输出，改为写入 warnings。但本提示词已列出的规则类型能表达的要求，必须输出规则，不得写入 warnings。
5. 用户说“不能休”“不能休息”“不许休”“不可以休”“不允许休息”“必须上班”“必须到岗”“要上班”“得上班”时输出 MUST_WORK，不要写成 REST，也不要只写进 warnings；同一天已有 REST 时照常输出 MUST_WORK。用户说「全员不能休息」「全员必须上班」「全员不许休」「大家周一到周五不能休」等全员含义时，必须逐员工展开：对上下文员工名单中的每名员工、每个工作日各输出一条 MUST_WORK，employee_name 分别填各员工姓名，禁止输出 employee_name 为空的全局 MUST_WORK。
6. 用户按星期描述休息（例如“周二周五周天休息”）时，展开成当月所有对应日期的 REST 规则，可逐日输出或按连续区间输出。用户按星期描述不能休或必须上班（例如“周一周四不能休”）时，同样展开成当月所有周一和周四的 MUST_WORK 规则，每条都要写出具体 YYYY-MM-DD 日期，不能把星期填进日期，也不能写入 warnings。某天同时出现“休息”和“不能休”时，该天同时输出 REST 与 MUST_WORK。示例：计划月份为 2026-11 时，“周一周四不能休”应输出 11-02、11-05、11-09、11-12、11-16、11-19、11-23、11-26、11-30 九条 MUST_WORK。
7. 用户说“有薪休假”“带薪假”“POFF”“休假一天”时输出 PAID_LEAVE。用户说“两天有薪休假”“休假 3 天”时，count 必须等于该天数，不得默认为 1：只给起始日期时 date_start 填起始日、date_end 留空、count 填天数；给区间且区间比天数宽时 date_start、date_end 填区间、count 填天数，表示区间内选 N 天；完全没给日期时 date_start、date_end 留空、count 填天数。这些情况都不要写入 warnings。
8. 用户说“每天尽量 N 人上班”“每天至少 N 人”时输出一条覆盖整个计划月份的 DAY_STAFF，count=N，hard=false。用户说“周六周日可以 N 人”“周X可以 N 人”时，展开成当月所有对应日期的 DAY_STAFF，count=N，hard=false。同一天同时被每日人数和周几人数覆盖时，按更宽松的 N 输出一条 DAY_STAFF，不要输出两条。“实在排不开”是软性放宽，不得写入 warnings。出现「最多 / 不超过 / 只排 N 人」时不要用 DAY_STAFF，改用 MAX_STAFF。
9. 用户描述「两人上班，一个 7:30 上班、一个 8:00 上班」「一人 7:30 到岗、一人 8 点开始」这类同一班次内不同人不同到岗时刻的要求时：
   - 点名到人（如「张三 7:30 上班」）→ 输出 FIXED_TIME；
   - 未点名 → 每个时刻输出一条 START_STAFF，start_time 为该时刻、count 为该时刻的人数；
   - 用户同时说明了总人数（如「两人上班」「需要 2 个人」）→ 再输出一条 DAY_STAFF，count 为总人数；
   - 禁止用 SEGMENT_STAFF 表达到岗时刻分布；SEGMENT_STAFF 只用于「某段时间内必须持续有 N 人在岗」的覆盖要求。
10. 用户说“某日都要上 HH:MM”“某日统一 HH:MM 上班”“某日全部 HH:MM 到岗”时输出 ALL_START_AT，date_start 与 date_end 为该日期，start_time 为 HH:MM，hard=true。
11. hard 字段判定（优先级从高到低）：
    a) 出现「必须 / 一定要 / 务必 / 不能 / 不许 / 不允许 / 不可以 / 不得」→ hard=true（对任何类型都适用，包括每日人数、时段覆盖、上班时刻人数）；
    b) 出现「要 / 需要 / 得」+ 数量或时刻（如「当天要 2 人上班」「需要 1 人在 7:30 到岗」）→ 该条人数/时段规则 hard=true；
    c) 「只 / 最多 / 不超过 + N 人」→ 输出 MAX_STAFF，count=N、hard=true；若同时出现「尽量 / 最好」则 hard=false；
    d) 出现「尽量 / 最好 / 优先 / 能…就 / 可以」→ hard=false；
    e) 其余情况沿用类型默认：普通休息、必须上班、有薪休假、指定班次、指定上班时间、盘点、班次限制、岗位单人班次 → true；每日人数、时段覆盖、上班时刻人数、下班时刻人数 → false。
12. 出现「尽量/最好」→ hard=false；出现「不得/不能/必须」→ hard=true。
    出现「周X / 周末 / 周六周日」→ weekdays 填 1-7（6,7 表示周六周日），不要拆成多条日期规则。
13. 只要班次目录给出了工时信息，涉及「排 N 小时的班次」「N 小时以上/以下」的要求必须用 SHIFT_HOURS 表达，不得再输出「未提供班次时长目录」或「现有规则类型不支持」。
14. 特殊语义必须按以下优先级转译，禁止「无法表达就直接丢弃」：
    a) 有专用类型 → 直接输出（含 MIN_REST_DAYS / MAX_WORK_DAYS / BAN_SHIFT，以及 START_STAFF 的 time_op）；
    b) 无专用类型但可等价组合 → 用现有类型组合表达，不得写进 warnings：
       - 「那天不排任何人 / 停业一天 / 全员休息（要求无人上班）」→ 一条 MAX_STAFF，count=0、hard=true，不要逐员工展开 REST；
       - 「某天之后都 X」→ 拆成两段日期区间分别输出；
       - 「除了周末都 X」→ weekdays="1,2,3,4,5"；
       - 「周末 A 人、工作日 B 人」→ 两条同类型规则分别带 weekdays="6,7" 与 "1,2,3,4,5"；
       - 「至少 1 人 8 点前到岗」→ START_STAFF start_time=08:00、time_op=le、count=1；
       - 「全员做某事」「全员不能休息」「全员必须上班」「全员不许休」→ 一条 MUST_WORK，employee_name 留空表示全员；「全员某天休息」→ 一条 REST，employee_name 留空表示全员。不要逐员工展开；
    c) 确实无法表达才写入 warnings，且必须包含：原句片段 + 为什么无法表达 + 一条可直接复制的改写示例。
15. 「最多上 N 天 / 这个月只能上 N 天」→ MAX_WORK_DAYS，count=N；「每周最多上 N 天」→ period="week"。「至少休 N 天 / 每月至少休 N 天」→ MIN_REST_DAYS，count=N、period="month"；「每周至少休 N 天」→ period="week"。「最多连续上 N 天 / 连上 N 天后必须休 / 张三最多连上 3 天」→ MAX_CONSECUTIVE_WORK，count=N，点名到人时填 employee_name，未点名留空表示全员。这几类别再用 REST 逐日展开近似，也不要写进 warnings。
16. 到岗时刻的比较方向用 time_op：「7:30 整到岗」→ eq；「7:30 之前到岗 / 7:30 前到岗」→ le；「8 点以后 / 8 点之后到岗」→ ge；未说明方向时省略（默认 eq）。人数方向用 count_op：「不超过 / 至多 / 最多 + N 人」→ le；「不少于 / 至少 + N 人」→ ge（默认，可省略）。例如「8 点以后到岗的不超过 1 人」→ START_STAFF start_time=08:00、time_op=ge、count=1、count_op=le；「7:30 之前到岗的人不少于 2 个」→ START_STAFF start_time=07:30、time_op=le、count=2（count_op=ge 默认，可省略）。
17. 「不要给我排某班次 / 全员不要排夜班 / 不要排 2GR」→ BAN_SHIFT，shift_name 必须来自班次目录；匹配不到具体班次时写入 warnings 并列出候选班次名，禁止编造班次，也不要建议用户改人工排班。
18. MAX_STAFF 的 count 允许为 0：表示当天不排任何人（停业 / 全员休息），hard=true。
19. 「有人 20:00 下班 / 20 点下班 N 人 / 三名员工 20:00 下班」→ END_STAFF：end_time 填下班时刻，count 填人数，count_op 默认 ge；需要「不超过」时用 count_op=le。这与「20:00 前持续在岗」不同，不要用 SEGMENT_STAFF 近似。
20. 出现岗位/分组限定（如「主管」「组长」「岗位：主管」「每天最好 2 名主管」）时，人数类规则（DAY_STAFF / SEGMENT_STAFF / START_STAFF / END_STAFF / SHIFT_STAFF / MAX_STAFF）额外输出 role 字段填岗位名，不要拆成逐员工规则，也不要写进 warnings。「每天最好 2 名主管，排不开 1 名也行」→ 两条 DAY_STAFF（count=2、hard=false 与 count=1、hard=true），role="主管"。
21. 只输出一个 JSON 对象，不要 Markdown 代码块，不要解释文字。

示例：计划月份为 2026-11，用户输入“5号-9号休息，有两天有薪休假”时输出：
{"rules":[{"type":"REST","employee_name":"张三","date_start":"2026-11-05","date_end":"2026-11-09","shift_name":null,"start_time":null,"end_time":null,"count":null,"hard":true,"raw_text":"5号-9号休息"},{"type":"PAID_LEAVE","employee_name":"张三","date_start":null,"date_end":null,"shift_name":null,"start_time":null,"end_time":null,"count":2,"hard":true,"raw_text":"有两天有薪休假"}],"warnings":[]}
其中有薪休假留空日期并给出 count=2，系统只在 11-05 至 11-09 的休息区间内挑 2 天，不会落到区间外。

示例：计划月份为 2026-11，用户输入「每天尽量排8小时的班次，极少情况排8小时以上，每人连休不得超过两天，周六周日排8小时及以上的班次」时输出：
{"rules":[{"type":"SHIFT_HOURS","date_start":"2026-11-01","date_end":"2026-11-30","work_hours":8,"hours_op":"eq","hard":false,"raw_text":"每天尽量排8小时的班次"},{"type":"SHIFT_HOURS","date_start":"2026-11-01","date_end":"2026-11-30","work_hours":8,"hours_op":"le","hard":false,"raw_text":"极少情况排8小时以上"},{"type":"MAX_REST_DAYS","date_start":"2026-11-01","date_end":"2026-11-30","count":2,"hard":false,"raw_text":"每人连休不得超过两天"},{"type":"SHIFT_HOURS","date_start":"2026-11-01","date_end":"2026-11-30","work_hours":8,"hours_op":"ge","weekdays":"6,7","hard":false,"raw_text":"周六周日排8小时及以上的班次"}],"warnings":[]}
工时偏好（尽量排 N 小时）→ SHIFT_HOURS hard=false；连休上限默认是软约束，hard=false；周末「8 小时及以上」是工时偏好 → SHIFT_HOURS hours_op=ge、weekdays="6,7"、hard=false，
不要输出 SEGMENT_STAFF，也不要拆成多条规则。

示例：用户输入「两人上班时 一个7:30上班 一个8:00上班」时输出：
{"rules":[{"type":"DAY_STAFF","employee_name":null,"date_start":"2026-11-03","date_end":"2026-11-03","shift_name":null,"start_time":null,"end_time":null,"count":2,"hard":false,"raw_text":"两人上班"},{"type":"START_STAFF","employee_name":null,"date_start":"2026-11-03","date_end":"2026-11-03","shift_name":null,"start_time":"07:30","end_time":null,"count":1,"hard":false,"raw_text":"一个7:30上班"},{"type":"START_STAFF","employee_name":null,"date_start":"2026-11-03","date_end":"2026-11-03","shift_name":null,"start_time":"08:00","end_time":null,"count":1,"hard":false,"raw_text":"一个8:00上班"}],"warnings":[]}
（示例中的日期按用户原句里的日期或计划月份替换，不要固定写死 11-03，此处仅为格式示例。）

示例：用户输入「11 月 12 号不要排任何人上班」时输出：
{"rules":[{"type":"MAX_STAFF","employee_name":null,"date_start":"2026-11-12","date_end":"2026-11-12","count":0,"hard":true,"raw_text":"11 月 12 号不要排任何人上班"}],"warnings":[]}
（停业 / 全员休息统一用 MAX_STAFF count=0，不要逐员工写 REST。）

示例：用户输入「早上 7:30 之前到岗的人不少于 2 个」时输出：
{"rules":[{"type":"START_STAFF","employee_name":null,"date_start":"2026-11-01","date_end":"2026-11-30","start_time":"07:30","time_op":"le","count":2,"hard":true,"raw_text":"早上 7:30 之前到岗的人不少于 2 个"}],"warnings":[]}
（「之前/前」→ le；「以后/之后」→ ge；「整」→ eq；未说明方向时省略 time_op。）

示例：用户输入「每个人每周至少休一天，每个人每月至少休 4 天，我这个月最多上 20 天班」时输出：
{"rules":[{"type":"MIN_REST_DAYS","date_start":"2026-11-01","date_end":"2026-11-30","count":1,"period":"week","hard":true,"raw_text":"每个人每周至少休一天"},{"type":"MIN_REST_DAYS","date_start":"2026-11-01","date_end":"2026-11-30","count":4,"period":"month","hard":true,"raw_text":"每个人每月至少休 4 天"},{"type":"MAX_WORK_DAYS","date_start":"2026-11-01","date_end":"2026-11-30","count":20,"period":"month","hard":true,"raw_text":"我这个月最多上 20 天班"}],"warnings":[]}
（个人约束 scope=employee 时 employee_name 固定为当前员工；不要用 REST 逐日展开近似。）

示例：用户输入「不要给我排 2GR 班次」时输出：
{"rules":[{"type":"BAN_SHIFT","employee_name":"张三","date_start":"2026-11-01","date_end":"2026-11-30","shift_name":"2GR","hard":true,"raw_text":"不要给我排 2GR 班次"}],"warnings":[]}
（全员禁止时 employee_name 填 null；班次必须来自班次目录。）

输出结构：
{"rules":[{"type":"REST","employee_name":"张三","date_start":"2026-11-03","date_end":"2026-11-03","shift_name":null,"start_time":null,"end_time":null,"count":null,"hard":true,"raw_text":"3号张三休息"}],"warnings":[]}"""

ADJUST_PROMPT = """你现在负责把用户对「已经生成的排班」的不满，翻译成对规则的新增 / 修改 / 删除。

限制：
1. 只能通过规则增删改实现，禁止修改计划参数（工时偏差、覆盖时段、连续上班上限等）与员工参数（月工时、每日上限等）。
2. 用户提到的人名必须映射为上下文里真实存在的员工；班次必须映射为班次目录里的真实名称；无法映射的写进 warnings，不要编造。
3. 只输出一个 JSON 对象，不要 Markdown，不要解释 JSON 之外的内容。

字段说明：
- add：字段与常规约束解析一致（type / employee_name / date_start / date_end / shift_name / start_time / end_time / count / hard / raw_text）。
- update：只改现有规则的字段，结构 {"rule_id": 12, "fields": {...}, "reason": "..."}；fields 只允许：
  - date_start、date_end（YYYY-MM-DD，且 date_start <= date_end）
  - hard（true / false）
  - count（人数类 DAY_STAFF / SEGMENT_STAFF / START_STAFF / END_STAFF / SHIFT_STAFF 取 1~99；
    岗位单人班次 SINGLE_ROLE_SHIFT 取 1~99（阈值，默认 1）；
    MAX_STAFF 取 0~99，0 表示当天不排任何人；MAX_REST_DAYS 取 1~15 天；
    MIN_REST_DAYS / MAX_WORK_DAYS 取 1~31 天）
  - start_time（HH:MM，仅 SEGMENT_STAFF / START_STAFF / FIXED_TIME / ALL_START_AT / SHIFT_LIMIT）
  - end_time（HH:MM，仅 SEGMENT_STAFF / END_STAFF / SHIFT_LIMIT）
  - work_hours（0~24 小时，仅 SHIFT_HOURS）
  - hours_op（eq / ge / le，仅 SHIFT_HOURS）
  - weekdays（"1,2" 形式的星期集合，1=周一…7=周日；支持 DAY_STAFF / SEGMENT_STAFF / START_STAFF /
    END_STAFF / SHIFT_LIMIT / SINGLE_ROLE_SHIFT / REST / SHIFT_HOURS / MAX_REST_DAYS，空字符串表示每天）
  - shift_id（班次目录中的真实班次 id，仅 FIXED_SHIFT / SHIFT_STAFF / START_STAFF / BAN_SHIFT / SINGLE_ROLE_SHIFT）
  - time_op（eq / le / ge，仅 START_STAFF / END_STAFF / SHIFT_LIMIT）：eq=时刻相等，le=不晚于，ge=不早于
  - count_op（ge / le，仅 START_STAFF / END_STAFF）：ge=至少（默认），le=至多
  - role（岗位名，人数类规则与岗位单人班次规则可用）：只统计该岗位的员工，空字符串表示不限岗位
  - period（week / month，仅 MIN_REST_DAYS / MAX_WORK_DAYS）：week=按自然周，month=整段区间
  不得在 fields 里写 type、employee_id、shift_name 等其它字段；改人请新增并删除原规则。
- remove：{"rule_id": 15, "reason": "..."}，rule_id 必须来自下方规则清单。
- hard 判定与常规解析一致：「必须 / 一定要 / 不能 / 不许 / 要 N 人 / 需要 N 人」→ hard=true；「尽量 / 最好 / 优先 / 可以」→ hard=false。
- 「只排 N 个人 / 最多 N 人」→ 输出 MAX_STAFF，count=N、hard=true（「尽量」时 hard=false）；不要再用 DAY_STAFF 表达上限。
- 「那天不排人 / 停业」→ MAX_STAFF count=0；「每周/每月最多上 N 天」→ MAX_WORK_DAYS；「每周/每月至少休 N 天」→ MIN_REST_DAYS；
  「不要排某班次」→ BAN_SHIFT，不要用 REST 逐日展开近似，也不要建议改人工排班。

示例：
- 「11-04 张三不要上班」→ add 一条 REST（张三、2026-11-04~2026-11-04、hard=true）
- 「删掉 11-03 两人上班的要求」→ remove 对应 DAY_STAFF / SEGMENT_STAFF 规则的 id
- 「11-05 只排 1 个人」→ add 一条 MAX_STAFF（count=1、hard=true）；若当天已有 DAY_STAFF 需求，可同时 update 其 count
- 「11-12 停业一天」→ add 一条 MAX_STAFF（count=0、hard=true）
- 「把每天 8 小时的硬要求放宽到 7 小时」→ update 对应 SHIFT_HOURS 的 work_hours=7（hours_op 不变）
- 「这条工时条件改成至少 7 小时」→ update work_hours=7 且 hours_op="ge"
- 「这条休息规则只在周末生效」→ update weekdays="6,7"
- 「7:30 之前到岗的人数改少一点」→ update 对应 START_STAFF 的 count，必要时 time_op="le"
- 「以后每周三刘蕾都休息」→ 按当月所有周三展开成多条 REST（或按现有解析器能力合并为区间 REST）
- 「11-03 甲和乙都必须休息，但当天要 2 人上班」→ 两条 REST hard=true + 一条 DAY_STAFF count=2 hard=true

输出结构：
{"add":[{"type":"REST","employee_name":"张三","date_start":"2026-11-04","date_end":"2026-11-04","shift_name":null,"start_time":null,"end_time":null,"count":null,"hard":true,"raw_text":"11-04 张三不要上班"}],"update":[{"rule_id":12,"fields":{"count":1},"reason":"当天只需 1 人"}],"remove":[{"rule_id":15,"reason":"取消该时段人数要求"}],"warnings":[]}"""


def get_settings(session: Session) -> dict[str, str]:
    rows = {row.key: row.value for row in session.exec(select(Setting)).all()}
    return {
        "llm_base_url": rows.get("llm_base_url") or DEFAULT_BASE_URL,
        "llm_model": rows.get("llm_model") or DEFAULT_MODEL,
        "llm_api_key": rows.get("llm_api_key") or "",
    }


def save_settings(
    session: Session, base_url: str, model: str, api_key: str
) -> dict[str, str]:
    for key, value in (
        ("llm_base_url", base_url.strip()),
        ("llm_model", model.strip()),
        ("llm_api_key", api_key.strip()),
    ):
        row = session.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=value)
        else:
            row.value = value
        session.add(row)
    session.commit()
    return get_settings(session)


def extract_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("模型未返回 JSON 对象")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("模型返回的 JSON 不是对象")
    return payload


def _shift_catalog_lines(shifts: list[Shift], limit: int = 60) -> str:
    """带属性的班次目录：代码｜起-止｜工时｜属性；超过 limit 行时按「工时 + 起始时刻」聚合。"""
    enabled = [item for item in shifts if item.enabled]

    def hours_text(item: Shift) -> str:
        hours = effective_hours(item.work_hours, item.start_time, item.end_time)
        return f"{hours:g}h"

    def attr_text(item: Shift) -> str:
        if item.is_off:
            return "休息"
        if item.is_leave:
            return "有薪休假"
        return "特殊（盘点）" if item.is_special else "普通"

    def line(item: Shift) -> str:
        return (
            f"- {item.name} | {item.start_time}-{item.end_time} | {hours_text(item)} | {attr_text(item)}"
        )

    lines: list[str] = []
    if len(enabled) <= limit:
        lines = [line(item) for item in enabled]
    else:
        # 超长目录按「工时 + 起始时刻」聚合，同组只展示一行并列出成员
        groups: dict[tuple[str, str], list[Shift]] = {}
        for item in enabled:
            groups.setdefault((hours_text(item), item.start_time), []).append(item)
        for (hours, start), items in sorted(groups.items()):
            names = "、".join(item.name for item in items)
            sample = items[0]
            lines.append(
                f"- {names} | {start}-{sample.end_time} | {hours} | {attr_text(sample)}（{len(items)} 个）"
            )
    result = "\n".join(lines) or "（无）"
    disabled_codes = [item.name for item in shifts if not item.enabled]
    if disabled_codes:
        result += "\n停用：" + "、".join(disabled_codes)
    return result


def _render_system_prompt(plan: Plan) -> str:
    """把计划参数注入提示词：覆盖时段只来自计划设置，不写死在提示里。"""
    return SYSTEM_PROMPT.replace("__COVERAGE_START__", plan.coverage_start).replace(
        "__COVERAGE_END__", plan.coverage_end
    )


def build_messages(
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    scope: str,
    employee: Employee | None,
    text: str,
    extra_context: str | None = None,
) -> list[ChatCompletionMessageParam]:
    employee_lines = "\n".join(
        f"- {item.name}（月工时 {item.monthly_hours}，每日最长 {item.max_daily_hours}，"
        f"{'可排盘点' if item.allow_special else '不排盘点'}）"
        for item in employees
    )
    shift_lines = _shift_catalog_lines(shifts)
    month_start, month_end = month_bounds(plan.month)
    if scope == "employee" and employee is not None:
        scope_line = f"当前只解析员工「{employee.name}」的个人约束，employee_name 固定填这个名字。"
    else:
        scope_line = "当前解析全局约束，涉及员工时填写 employee_name。"
    content = (
        f"计划月份：{plan.month}（{month_start} 至 {month_end}）\n"
        f"覆盖时段：{plan.coverage_start}-{plan.coverage_end}\n"
        f"计划参数：连续上班上限 {plan.max_consecutive_days} 天（0 表示不限制；"
        "员工或日期细分可用 MAX_CONSECUTIVE_WORK 规则，求解时取更严格值）\n\n"
        f"员工名单：\n{employee_lines or '（无）'}\n\n"
        f"班次目录（代码｜起-止｜工时｜属性）：\n{shift_lines}\n\n"
        f"{scope_line}\n\n用户约束原文：\n{text}"
    )
    if extra_context:
        content = f"{content}\n\n{extra_context}"
    return [
        {"role": "system", "content": _render_system_prompt(plan)},
        {"role": "user", "content": content},
    ]


def _normalize_date(
    value: str, month_start: str, month_end: str, warnings: list[str], label: str
) -> tuple[str | None, str | None]:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None, f"日期格式无法识别：{value}"
    if parsed.isoformat() < month_start:
        warnings.append(f"{label} 的日期 {value} 早于计划月份，已截断到 {month_start}")
        return month_start, None
    if parsed.isoformat() > month_end:
        warnings.append(f"{label} 的日期 {value} 晚于计划月份，已截断到 {month_end}")
        return month_end, None
    return parsed.isoformat(), None


CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
CN_NUMBER = re.compile(r"[零一二两三四五六七八九十百]+")
COUNT_SUFFIX = re.compile(r"(?:天|人|个|名|位)$")


def chinese_to_int(value: object) -> int | None:
    """把「两 / 十 / 十二 / 三十一」这类中文数字转成整数；非数字返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if not CN_NUMBER.fullmatch(text):
        return None
    if "百" in text:
        left, _, right = text.partition("百")
        hundreds = CN_DIGITS.get(left, 1) if left else 1
        remainder_text = right.lstrip("零")
        remainder = chinese_to_int(remainder_text) if remainder_text else 0
        if remainder is None:
            return None
        return hundreds * 100 + remainder
    if "十" in text:
        left, _, right = text.partition("十")
        if left and left not in CN_DIGITS:
            return None
        if right and right not in CN_DIGITS:
            return None
        # 「十二」= 12，「二十」= 20，「三十一」= 31；单独「十」= 10
        return (CN_DIGITS.get(left, 1) if left else 1) * 10 + (CN_DIGITS.get(right, 0) if right else 0)
    total = 0
    for char in text:
        if char not in CN_DIGITS:
            return None
        total = total * 10 + CN_DIGITS[char]
    return total


def parse_count_number(value: object) -> int | None:
    """count 字段归一化：兼容阿拉伯数字与「两 / 两天 / 两个人 / 十二」等中文写法。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(" ", "")
    if not text:
        return None
    # 「两个人」「3人」「5天」可能叠加多个量词后缀，逐个剥离
    while COUNT_SUFFIX.search(text):
        text = COUNT_SUFFIX.sub("", text)
    if text.isdigit():
        return int(text)
    return chinese_to_int(text)


def normalize_rule(
    index: int,
    raw: dict[str, Any],
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    scope: str,
    employee: Employee | None,
) -> tuple[PreviewRule, list[str]]:
    warnings: list[str] = []
    notes: list[str] = []
    month_start, month_end = month_bounds(plan.month)
    employee_by_name = {item.name.strip(): item for item in employees}
    shift_by_name = {item.name.strip(): item for item in shifts}

    type_value = str(raw.get("type") or "").strip().upper()
    if type_value not in RULE_TYPES:
        notes.append(f"无法识别的约束类型：{raw.get('type')}")

    date_start = str(raw.get("date_start") or "").strip()
    date_end = str(raw.get("date_end") or date_start).strip()
    if type_value == "PAID_LEAVE" and not date_start:
        final_start = None
        final_end = None
    else:
        normalized_start, start_note = _normalize_date(date_start, month_start, month_end, warnings, "规则")
        normalized_end, end_note = _normalize_date(date_end, month_start, month_end, warnings, "规则")
        for note in (start_note, end_note):
            if note:
                notes.append(note)
        if normalized_start and normalized_end and normalized_start > normalized_end:
            notes.append("开始日期晚于结束日期")
        final_start = normalized_start or month_start
        final_end = normalized_end or final_start

    target_employee = employee if scope == "employee" else None
    employee_name = raw.get("employee_name")
    if scope != "employee" and isinstance(employee_name, str):
        employee_name = employee_name.strip()
        if employee_name in ALL_EMPLOYEE_WORDS:
            # 「全员/全部人员」是范围描述，不是具体员工姓名
            employee_name = None
        else:
            found = employee_by_name.get(employee_name)
            if found is not None:
                target_employee = found
            elif type_value in EMPLOYEE_REQUIRED:
                notes.append(f"员工名单中没有「{employee_name}」")
            elif type_value in EMPLOYEE_OPTIONAL:
                # 可选人员的类型若点名但匹配不到，不能退化成全员规则
                notes.append(f"员工名单中没有「{employee_name}」，无法按该员工生效")
    if type_value in EMPLOYEE_REQUIRED and target_employee is None:
        notes.append("缺少员工")
    if type_value not in EMPLOYEE_REQUIRED and type_value not in EMPLOYEE_OPTIONAL:
        # 人数类 / 全员类规则不带人员；可选人员的类型保留解析出的员工
        target_employee = None

    shift_name = raw.get("shift_name")
    shift = None
    if isinstance(shift_name, str) and shift_name.strip():
        shift_name = shift_name.strip()
        shift = shift_by_name.get(shift_name)
        if shift is None and type_value in SHIFT_REQUIRED:
            notes.append(f"班次目录中没有「{shift_name}」")
    if type_value in SHIFT_REQUIRED and shift is None:
        notes.append("缺少班次")

    start_time = raw.get("start_time")
    end_time = raw.get("end_time")
    if isinstance(start_time, str) and start_time.strip():
        try:
            start_time = normalize_hhmm(start_time)
        except ValueError:
            notes.append(f"上班时间格式无法识别：{start_time}")
            start_time = None
    else:
        start_time = None
    if isinstance(end_time, str) and end_time.strip():
        try:
            end_time = normalize_hhmm(end_time)
        except ValueError:
            notes.append(f"结束时间格式无法识别：{end_time}")
            end_time = None
    else:
        end_time = None
    if type_value in TIME_REQUIRED and start_time is None:
        notes.append("缺少上班时间")
    if type_value in RANGE_REQUIRED and end_time is None:
        notes.append("缺少时段结束时间")

    count_value = raw.get("count")
    count = parse_count_number(count_value) if count_value not in (None, "") else None
    if count_value not in (None, "") and count is None:
        notes.append(f"人数无法识别：{count_value}")
    if type_value in COUNT_REQUIRED and (count is None or count < 0):
        notes.append("缺少有效人数")
    if type_value == "MONTHLY_HOURS" and (count is None or not 1 <= count <= 744):
        notes.append("缺少有效的月度目标工时（1~744 小时）")
    if (type_value in DAY_COUNT_TYPES or type_value == "MAX_CONSECUTIVE_WORK") and (
        count is None or not 1 <= count <= 31
    ):
        notes.append("缺少有效天数（1~31）")
    if type_value == "PAID_LEAVE":
        count = 1 if count is None or count < 1 else count

    # 上班时刻比较方向（START_STAFF / END_STAFF / SHIFT_LIMIT）：eq=等于，le=不晚于，ge=不早于
    time_op = None
    if type_value in ADJUST_TIME_OP_TYPES:
        default_op = "le" if type_value == "SHIFT_LIMIT" else "eq"
        raw_op = str(raw.get("time_op") or default_op).strip().lower()
        time_op = raw_op if raw_op in TIME_OPS else default_op
        if raw_op not in TIME_OPS and raw.get("time_op") is not None:
            notes.append(f"时刻条件无法识别：{raw.get('time_op')}，按{'不晚于' if default_op == 'le' else '等于'}处理")
    elif raw.get("time_op") not in (None, ""):
        notes.append(f"{type_value} 不支持时刻条件，time_op 已忽略")

    # 人数方向（START_STAFF / END_STAFF）：ge=至少（默认），le=至多
    count_op = None
    if type_value in ADJUST_COUNT_OP_TYPES:
        raw_op = str(raw.get("count_op") or "ge").strip().lower()
        count_op = raw_op if raw_op in COUNT_OPS else "ge"
        if raw_op not in COUNT_OPS and raw.get("count_op") is not None:
            notes.append(f"人数条件无法识别：{raw.get('count_op')}，按至少处理")
    elif raw.get("count_op") not in (None, ""):
        notes.append(f"{type_value} 不支持人数条件，count_op 已忽略")

    # 岗位/分组限定（人数类规则可选；岗位单人班次必填）
    role = None
    raw_role = raw.get("role")
    if isinstance(raw_role, str) and raw_role.strip():
        if type_value in ROLE_TYPES or type_value in SINGLE_ROLE_TYPES:
            role, role_note = normalize_role(raw_role)
            if role_note:
                notes.append(role_note)
        else:
            notes.append(f"{type_value} 不支持岗位限定，role 已忽略")
    elif type_value in SINGLE_ROLE_TYPES:
        notes.append("缺少岗位")

    # 统计周期（MIN_REST_DAYS / MAX_WORK_DAYS）：week=每周，month=每月
    period = None
    if type_value in ADJUST_PERIOD_TYPES:
        raw_period = str(raw.get("period") or "month").strip().lower()
        period = raw_period if raw_period in PERIODS else "month"
        if raw_period not in PERIODS and raw.get("period") is not None:
            notes.append(f"统计周期无法识别：{raw.get('period')}，按每月处理")
    elif raw.get("period") not in (None, ""):
        notes.append(f"{type_value} 不支持统计周期，period 已忽略")

    # 班次工时条件（SHIFT_HOURS）：work_hours 必填，hours_op 默认等于
    work_hours = None
    hours_op = None
    if type_value == "SHIFT_HOURS":
        raw_hours = raw.get("work_hours")
        try:
            work_hours = float(raw_hours) if raw_hours is not None else None
        except (TypeError, ValueError):
            work_hours = None
        if work_hours is None or not 0.5 <= work_hours <= 24:
            notes.append("缺少有效的班次工时（0.5~24 小时）")
        raw_op = str(raw.get("hours_op") or "eq").strip().lower()
        hours_op = raw_op if raw_op in ("eq", "ge", "le") else "eq"
        if raw_op not in ("eq", "ge", "le") and raw.get("hours_op") is not None:
            notes.append(f"工时比较方式无法识别：{raw.get('hours_op')}，按等于处理")

    # 按星期限定（weekdays）："6,7" 表示周六周日，非法值丢弃并提示
    weekdays = None
    raw_weekdays = raw.get("weekdays")
    if raw_weekdays not in (None, ""):
        parsed_days: list[int] = []
        dropped: list[str] = []
        for part in str(raw_weekdays).replace("，", ",").split(","):
            text = part.strip()
            if text.isdigit() and 1 <= int(text) <= 7:
                value = int(text)
                if value not in parsed_days:
                    parsed_days.append(value)
            elif text:
                dropped.append(text)
        if parsed_days:
            weekdays = ",".join(str(value) for value in sorted(parsed_days))
        if dropped:
            notes.append(f"星期无法识别：{'、'.join(dropped)}，已忽略")

    hard_value = raw.get("hard")
    hard = TYPE_DEFAULT_HARD.get(type_value, True) if hard_value is None else bool(hard_value)

    preview = PreviewRule(
        index=index,
        employee_id=target_employee.id if target_employee else None,
        employee_name=target_employee.name if target_employee else (
            employee_name if isinstance(employee_name, str) else None
        ),
        type=type_value or str(raw.get("type") or "UNKNOWN"),
        date_start=final_start,
        date_end=final_end,
        shift_id=shift.id if shift else None,
        shift_name=shift.name if shift else (shift_name if isinstance(shift_name, str) else None),
        start_time=start_time,
        end_time=end_time,
        count=count,
        work_hours=work_hours,
        hours_op=hours_op,
        weekdays=weekdays,
        time_op=time_op,
        count_op=count_op,
        period=period,
        role=role,
        hard=hard,
        raw_text=raw.get("raw_text") if isinstance(raw.get("raw_text"), str) else None,
        valid=not notes,
        note="；".join(notes) if notes else None,
    )
    return preview, warnings


SOFT_QUALIFIERS = ("尽量", "最好", "尽可能", "希望", "优先")
HARD_QUALIFIERS = ("必须", "务必", "一定", "不得", "不能", "不可以", "禁止", "全员", "都要")
WEEKDAY_CHARS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "日": 7,
    "天": 7,
}


def _text_weekdays(text: str | None) -> set[int]:
    """从原文提取周几集合：支持「周二」「星期天」「周末/周六日/周六周日」。"""
    text = text or ""
    days: set[int] = set()
    if any(word in text for word in ("周末", "周六日", "周六周日", "星期六日")):
        days.update({6, 7})
    for match in re.finditer(r"(?:周|星期)([一二三四五六日天])", text):
        days.add(WEEKDAY_CHARS[match.group(1)])
    return days


def _rest_is_hard(text: str | None) -> bool:
    """个人休息默认硬约束：只有「尽量/最好/尽可能」这类限定词才降为软。"""
    text = text or ""
    if any(word in text for word in SOFT_QUALIFIERS):
        return False
    if any(word in text for word in HARD_QUALIFIERS):
        return True
    return True


# 请假语义：带薪假与普通休息的工时口径不同（8h/天计数 vs 0），必须确定性区分
PAID_LEAVE_WORDS = ("请假", "年假", "病假", "调休", "回老家", "休假", "带薪假", "有薪休假")
# 人数类规则的硬/软判定词：硬词优先，其次软词，都没有则默认硬
HEADCOUNT_HARD_WORDS = ("必须", "需要", "保证", "至少")
HEADCOUNT_SOFT_WORDS = (*SOFT_QUALIFIERS, "可以", "允许", "实在排不开")
# 人数型规则：原文明确写「至少 N 人」时是硬下限，带模动词才降软
HEADCOUNT_RULE_TYPES = {
    "DAY_STAFF",
    "SEGMENT_STAFF",
    "START_STAFF",
    "END_STAFF",
    "SHIFT_STAFF",
}
MIN_HEADCOUNT_PATTERN = re.compile(
    r"(至少|最少|不少于|不低于|起码)\s*\d+\s*(个|名)?\s*人"
)


def _text_is_paid_leave(text: str | None) -> bool:
    return any(word in (text or "") for word in PAID_LEAVE_WORDS)


def _headcount_strength(text: str | None) -> tuple[bool, str | None]:
    """人数规则强度判定链：返回 (hard, 依据词)；无依据词时默认硬。"""
    text = text or ""
    for word in HEADCOUNT_HARD_WORDS:
        if word in text:
            return True, word
    for word in HEADCOUNT_SOFT_WORDS:
        if word in text:
            return False, word
    return True, None


def _day_class(day: str) -> str:
    weekday = date.fromisoformat(day).isoweekday()
    if weekday == 3:
        return "wed"
    if weekday >= 6:
        return "weekend"
    return "weekday"


DAY_CLASS_WEEKDAYS = {"weekday": "1,2,4,5", "wed": "3", "weekend": "6,7"}
DAY_CLASS_COUNT = {"weekday": 2, "wed": 1, "weekend": 1}


def _rule_days(rule: PreviewRule, month: str) -> set[str]:
    """规则在计划月内实际生效的日期集合（日期区间 ∩ weekdays）。"""
    month_start, month_end = month_bounds(month)
    days = intersect_dates(rule.date_start or month_start, rule.date_end or month_end, month)
    weekdays = {
        int(part)
        for part in (rule.weekdays or "").split(",")
        if part.strip().isdigit()
    }
    if weekdays:
        days = [day for day in days if date.fromisoformat(day).isoweekday() in weekdays]
    return set(days)


def _merge_key(rule: PreviewRule) -> tuple[Any, ...]:
    """同源规则合并键：同类型、同人数、同强度、同星期集合才允许合并。

    weekdays 纳入合并键，保证工作日/周三/周末的分层不会被合并污染；
    合并后仍要用「展开日期集合逐日相等」做最终裁决。
    """
    return (
        rule.type,
        rule.employee_id,
        rule.weekdays,
        rule.shift_id,
        rule.count,
        rule.hard,
        rule.start_time,
        rule.end_time,
        rule.work_hours,
        rule.hours_op,
        rule.time_op,
        rule.count_op,
        rule.period,
    )


def _postprocess_previews(
    previews: list[PreviewRule],
    plan: Plan,
    shifts: list[Shift],
    warnings: list[str],
) -> list[PreviewRule]:
    """确定性归一化：强度、星期集合、同日固定班、去重、跨夜班次日休息。"""
    month_start, month_end = month_bounds(plan.month)
    shift_by_id = {item.id: item for item in shifts if item.id is not None}

    for rule in previews:
        if not rule.valid:
            continue
        # 请假/带薪假的确定性映射：工时口径与普通休息不同，按关键词整段改写类型
        if rule.type in ("REST", "PAID_LEAVE"):
            if _text_is_paid_leave(rule.raw_text):
                if rule.type != "PAID_LEAVE":
                    warnings.append(
                        f"「{rule.raw_text}」含请假语义，已按带薪假（PAID_LEAVE）落库"
                    )
                rule.type = "PAID_LEAVE"
                rule.hard = True
                if rule.date_start and rule.date_end:
                    # 区间整段映射：天数按区间实际天数回写，避免只取首日或随机截取
                    rule.count = max(1, len(_rule_days(rule, plan.month)))
            elif rule.type == "REST":
                rule.hard = _rest_is_hard(rule.raw_text)
            else:
                # 模型已明确给出 PAID_LEAVE（即使原文没带请假词）时不得降级成 REST
                rule.hard = True
        if rule.type == "MAX_REST_DAYS":
            rule.hard = False
        # weekday 为空时按原文补全；"周六日/周末" → {6,7}
        if rule.type in WEEKDAY_TYPES:
            derived = _text_weekdays(rule.raw_text)
            existing = {
                int(part)
                for part in (rule.weekdays or "").split(",")
                if part.strip().isdigit()
            }
            merged = existing | derived
            if merged and merged != existing:
                rule.weekdays = ",".join(str(value) for value in sorted(merged))
                warnings.append(
                    f"{rule.type} 的星期按原文补全为 {rule.weekdays}"
                )

    # 每日在岗人数分档：判定链固定为「依据词 → 强度」，再按工作日/周三/周末分层
    normalized: list[PreviewRule] = []
    for rule in previews:
        if rule.type != "DAY_STAFF" or not rule.valid:
            normalized.append(rule)
            continue
        days = intersect_dates(rule.date_start or month_start, rule.date_end or month_end, plan.month)
        weekdays = {
            int(part)
            for part in (rule.weekdays or "").split(",")
            if part.strip().isdigit()
        }
        if weekdays:
            days = [day for day in days if date.fromisoformat(day).isoweekday() in weekdays]
        if not days:
            normalized.append(rule)
            continue
        grouped: dict[str, list[str]] = {}
        for day in days:
            grouped.setdefault(_day_class(day), []).append(day)
        # 只有覆盖整月的通例才按分档上调人数；带明确日期的单日要求保留原人数，
        # 避免把「12 号要 1 人上班」误升级成工作日 2 人。
        full_month = (
            (rule.date_start or month_start) == month_start
            and (rule.date_end or month_end) == month_end
        )
        # 判定依据词：必须/需要/保证/至少 → 硬；尽量/最好/尽可能/可以/允许 → 软
        hard, basis = _headcount_strength(rule.raw_text)
        for class_name, class_days in grouped.items():
            count = rule.count or 0
            if class_name == "weekday":
                # 显式写了周几（如「周一排 2 个」）时保留原人数，不按工作日通例上调
                count = max(count, 1) if weekdays else (
                    max(count, DAY_CLASS_COUNT["weekday"]) if full_month else max(count, 1)
                )
                class_hard = hard
                class_basis = f"{basis} → {'硬' if hard else '软'}" if basis else "无强度词（默认硬）"
            elif class_name == "wed":
                # 周三允许降档：通例降到 1（仍是硬下限）；显式写了周三则保留原人数
                count = max(count, 1) if weekdays else (1 if full_month else max(count, 1))
                class_hard = True
                class_basis = "周三原文人数 → 硬" if weekdays else "周三可降档 → 硬"
            else:
                # 周末严格按词面判定，不与工作日口径互相污染；
                # 通例里的周末档位取默认 1，显式写周末则保留词面人数
                count = max(count, 1) if weekdays else (
                    DAY_CLASS_COUNT["weekend"] if full_month else max(count, 1)
                )
                class_hard = hard
                class_basis = f"{basis} → {'硬' if hard else '软'}" if basis else "无强度词（默认硬）"
            # 原规则显式限定周几时，拆分后仍保留这些星期，不能扩散到整个档位
            if weekdays:
                class_weekdays = {int(part) for part in DAY_CLASS_WEEKDAYS[class_name].split(",")}
                class_weekdays &= weekdays
                weekday_text = ",".join(str(value) for value in sorted(class_weekdays))
            else:
                weekday_text = DAY_CLASS_WEEKDAYS[class_name]
            normalized.append(
                rule.model_copy(
                    update={
                        "weekdays": weekday_text,
                        "date_start": min(class_days),
                        "date_end": max(class_days),
                        "count": count,
                        "hard": class_hard,
                        "hard_basis": class_basis,
                    }
                )
            )
    previews = normalized

    # 同一句展开的同源规则合并：先按合并键分组，再用「展开日期集合逐日相等」裁决，
    # 宁可少合并，也不允许跨过空档把没被任何一条覆盖的日期补进来
    groups: dict[tuple[Any, ...], list[int]] = {}
    for position, rule in enumerate(previews):
        if not rule.valid:
            continue
        groups.setdefault(_merge_key(rule), []).append(position)
    replacements: dict[int, PreviewRule] = {}
    dropped: set[int] = set()
    merge_count = 0
    # 合并时允许补进来的「空档日期」：同一员工已有硬性休息/带薪假的日期，
    # 这些天本来就不上班，补进休息规则不改变任何排班语义（例如年假把周二四休息切成两段）
    blocked_days: dict[int, set[str]] = {}
    for rule in previews:
        if rule.type not in ("REST", "PAID_LEAVE") or not rule.hard:
            continue
        if rule.employee_id is None:
            continue
        blocked_days.setdefault(rule.employee_id, set()).update(_rule_days(rule, plan.month))

    def _merged_candidate(members: list[PreviewRule]) -> PreviewRule | None:
        """把一组同签名规则尝试压成一条；展开日期集合不相等时返回 None。"""
        if len(members) < 2:
            return None
        if not all(member.date_start and member.date_end for member in members):
            return None
        union: set[str] = set()
        for member in members:
            union |= _rule_days(member, plan.month)
        if not union:
            return None
        start = min(str(member.date_start) for member in members)
        end = max(str(member.date_end) for member in members)
        span = set(intersect_dates(start, end, plan.month))
        weekdays = sorted({date.fromisoformat(day).isoweekday() for day in union})
        candidate = members[0].model_copy(
            update={
                "date_start": start,
                "date_end": end,
                "weekdays": None if union == span else ",".join(str(value) for value in weekdays),
            }
        )
        # 语义等价是合并的唯一前提：原日期一天都不能丢；补进来的空档必须是该员工
        # 已经硬性休息/带薪假的日期，否则不合并
        candidate_days = _rule_days(candidate, plan.month)
        if union - candidate_days:
            return None
        extra = candidate_days - union
        if extra and not extra <= blocked_days.get(members[0].employee_id or 0, set()):
            return None
        return candidate

    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        # 按日期推进：只在「下一段并入后展开集合仍逐日相等」时才并进来，
        # 否则就地收束当前段、从下一段重新开始，避免整组被空档拖累全不合并
        ordered = sorted(
            ((index, previews[index]) for index in indexes),
            key=lambda item: (str(item[1].date_start), str(item[1].date_end)),
        )
        batch: list[tuple[int, PreviewRule]] = []

        def flush() -> None:
            nonlocal merge_count
            if len(batch) < 2:
                return
            candidate = _merged_candidate([member for _, member in batch])
            if candidate is None:
                return
            replacements[batch[0][0]] = candidate
            dropped.update(index for index, _ in batch[1:])
            merge_count += len(batch) - 1

        for index, member in ordered:
            if batch:
                trial = [m for _, m in batch] + [member]
                if _merged_candidate(trial) is not None:
                    batch.append((index, member))
                    continue
                flush()
            batch = [(index, member)]
        flush()
    if merge_count:
        warnings.append(f"已合并 {merge_count} 条同句展开的重复规则")
    previews = [
        replacements.get(position, previews[position])
        for position in range(len(previews))
        if position not in dropped
    ]

    # 跨午夜固定班的日界约定：排班仍记在「开始日」，但休息日取「班次结束所在自然日」，
    # 即 开始日 + 1（例如 11-19 21:00 → 11-20 06:00，则 11-20 为休息日）。
    # 该约定与班次归属日保持一致，避免同日两个固定班被拆到不同日期。
    existing_rest: dict[int, set[str]] = {}
    for rule in previews:
        if rule.type == "REST" and rule.employee_id is not None and rule.hard:
            days = intersect_dates(rule.date_start or month_start, rule.date_end or month_end, plan.month)
            weekdays = {
                int(part)
                for part in (rule.weekdays or "").split(",")
                if part.strip().isdigit()
            }
            if weekdays:
                days = [day for day in days if date.fromisoformat(day).isoweekday() in weekdays]
            existing_rest.setdefault(rule.employee_id, set()).update(days)
    extra: list[PreviewRule] = []
    for rule in previews:
        if rule.type != "FIXED_SHIFT" or not rule.valid or rule.shift_id is None:
            continue
        shift = shift_by_id.get(rule.shift_id)
        if shift is None:
            continue
        if parse_interval(shift.start_time, shift.end_time)[1] < 1440:
            continue
        days = intersect_dates(rule.date_start or month_start, rule.date_end or month_end, plan.month)
        weekdays = {
            int(part)
            for part in (rule.weekdays or "").split(",")
            if part.strip().isdigit()
        }
        if weekdays:
            days = [day for day in days if date.fromisoformat(day).isoweekday() in weekdays]
        for day in days:
            next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
            if not month_start <= next_day <= month_end:
                continue
            employee_id = rule.employee_id
            if employee_id is None or next_day in existing_rest.get(employee_id, set()):
                continue
            existing_rest.setdefault(employee_id, set()).add(next_day)
            extra.append(
                rule.model_copy(
                    update={
                        "type": "REST",
                        "date_start": next_day,
                        "date_end": next_day,
                        "shift_id": None,
                        "shift_name": None,
                        "count": None,
                        "weekdays": None,
                        "hard": True,
                        "raw_text": f"{rule.shift_name or '跨夜班'}结束后次日休息（系统自动生成）",
                        "note": "跨午夜班次日休息",
                    }
                )
            )
    if extra:
        previews.extend(extra)
        warnings.append(f"跨午夜班已自动生成 {len(extra)} 条次日硬休息")

    for position, rule in enumerate(previews):
        rule.index = position
    return previews


def normalize_payload(
    payload: dict[str, Any],
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    scope: str,
    employee: Employee | None,
) -> ParseResponse:
    raw_rules = payload.get("rules")
    if raw_rules is None:
        raw_rules = []
    if not isinstance(raw_rules, list):
        raise ValueError("模型返回的 rules 不是数组")
    warnings = [str(item) for item in payload.get("warnings") or []]
    previews: list[PreviewRule] = []
    for index, raw in enumerate(raw_rules):
        if not isinstance(raw, dict):
            warnings.append(f"第 {index + 1} 条规则格式不正确，已跳过")
            continue
        preview, extra_warnings = normalize_rule(
            index, raw, plan, employees, shifts, scope, employee
        )
        previews.append(preview)
        warnings.extend(extra_warnings)
    previews = _postprocess_previews(previews, plan, shifts, warnings)
    # 相同的提示只保留一条，避免兜底文案刷屏
    deduped: list[str] = []
    for item in warnings:
        if item not in deduped:
            deduped.append(item)
    warnings = deduped
    if not previews and warnings:
        # 没解析出任何规则时给出可操作的改写建议
        warnings[0] = (
            f"未能解析出可执行规则：{warnings[0]}。"
            "可尝试拆成更具体的句子，例如「11月每个工作日排 8 小时的班次」。"
        )
    response = ParseResponse(rules=previews, warnings=warnings)
    # 全部规则都无效时给出可操作提示（含改写建议），前端可直接展示
    response.hint = failure_hint(previews, warnings)
    return response


def failure_hint(previews: list[PreviewRule], warnings: list[str]) -> str | None:
    """预览失败兜底：没有可应用的规则时，生成带改写建议的提示。"""
    if not previews or any(item.valid for item in previews):
        return None
    reasons = [item.note for item in previews if item.note]
    unique: list[str] = []
    for item in reasons:
        if item not in unique:
            unique.append(item)
    reason = "；".join(unique) if unique else (warnings[0] if warnings else "未能识别出规则")
    return (
        f"未能解析出可执行规则：{reason}。"
        "可尝试拆成更具体的句子，例如「11月每个工作日排 8 小时的班次」。"
    )


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rule_context(
    employees: list[Employee],
    rules: list[Rule],
    limit: int = 60,
    shifts: list[Shift] | None = None,
) -> str:
    """给调整解析器的现有规则清单，用于 update / remove 时引用真实 id。"""
    employee_map = {item.id: item.name for item in employees}
    shift_map = {item.id: item.name for item in shifts or [] if item.id is not None}
    lines = ["现有规则（id / 类型 / 人员 / 日期 / 时刻 / 人数 / 硬软）："]
    for rule in rules[:limit]:
        owner = employee_map.get(rule.employee_id, "全员")
        period = (
            rule.date_start
            if rule.date_start == rule.date_end
            else f"{rule.date_start}~{rule.date_end}"
        )
        moment = ""
        if rule.start_time:
            moment = f" {rule.start_time}-{rule.end_time}" if rule.end_time else f" {rule.start_time}"
        elif rule.end_time:
            moment = f" {rule.end_time} 下班"
        if rule.type in ("START_STAFF", "END_STAFF", "SHIFT_LIMIT") and rule.time_op and rule.time_op != "eq":
            moment += "（不晚于）" if rule.time_op == "le" else "（不早于）"
        count = f" {rule.count} 人" if rule.count is not None else ""
        if rule.type in ("START_STAFF", "END_STAFF") and rule.count is not None and (rule.count_op or "ge") == "le":
            count = f" 至多 {rule.count} 人"
        hours = ""
        if rule.type == "SHIFT_HOURS" and rule.work_hours is not None:
            op_text = {"eq": "=", "ge": "≥", "le": "≤"}.get(rule.hours_op or "eq", "=")
            hours = f" 工时{op_text}{rule.work_hours:g}h"
        if rule.type == "MAX_REST_DAYS" and rule.count is not None:
            count = f" 最多连休 {rule.count} 天"
        if rule.type in DAY_COUNT_TYPES and rule.count is not None:
            verb = "至少休" if rule.type == "MIN_REST_DAYS" else "最多上"
            scope = "每周" if (rule.period or "month") == "week" else "每月"
            count = f" {scope}{verb} {rule.count} 天"
        if rule.type == "BAN_SHIFT" and rule.shift_id is not None:
            count = f" 禁止班次 {shift_map.get(rule.shift_id, f'#{rule.shift_id}')}"
        week = f" 周{rule.weekdays}" if rule.weekdays else ""
        lines.append(
            f"#{rule.id} {rule.type} / {owner} / {period}{moment}{count}{hours}{week} / "
            f"{'硬' if rule.hard else '软'}"
        )
    if len(rules) > limit:
        lines.append(f"（共 {len(rules)} 条，仅列出前 {limit} 条）")
    return "\n".join(lines)


def normalize_adjust_fields(
    rule: Rule,
    raw_fields: dict[str, Any],
    plan: Plan,
    shifts: list[Shift] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """校验 update 的字段白名单，返回 (可用字段, 被丢弃字段的提示)。

    白名单与取值范围统一由 services/rule_fields 提供，与 build_rule 的写入校验同口径。
    """
    warnings: list[str] = []
    normalized: dict[str, Any] = {}
    month_start, month_end = month_bounds(plan.month)
    for key, value in raw_fields.items():
        if key in ("date_start", "date_end"):
            text = value.strip() if isinstance(value, str) else ""
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", text):
                warnings.append(f"规则 #{rule.id} 的 {key} 不是 YYYY-MM-DD，已忽略")
                continue
            if not month_start <= text <= month_end:
                warnings.append(f"规则 #{rule.id} 的 {key} 不在计划月份内，已忽略")
                continue
            normalized[key] = text
        elif key == "hard":
            if not isinstance(value, bool):
                warnings.append(f"规则 #{rule.id} 的 hard 必须是布尔值，已忽略")
                continue
            normalized[key] = value
        elif key == "count":
            if rule.type not in ADJUST_COUNT_TYPES:
                warnings.append(f"规则 #{rule.id} 不是人数类规则，count 已忽略")
                continue
            count, note = normalize_count(rule.type, value)
            if note:
                warnings.append(f"规则 #{rule.id} 的 count {note}，已忽略")
                continue
            normalized[key] = count
        elif key == "start_time":
            if rule.type not in ADJUST_TIME_TYPES:
                warnings.append(f"规则 #{rule.id} 不支持改上班时刻，start_time 已忽略")
                continue
            text = value.strip() if isinstance(value, str) else ""
            if not re.match(r"^\d{2}:\d{2}$", text):
                warnings.append(f"规则 #{rule.id} 的 start_time 不是 HH:MM，已忽略")
                continue
            normalized[key] = text
        elif key == "end_time":
            if rule.type not in ("SEGMENT_STAFF", "END_STAFF", "SHIFT_LIMIT"):
                warnings.append(f"规则 #{rule.id} 只有时段覆盖/下班时刻/班次限制规则可改 end_time，已忽略")
                continue
            text = value.strip() if isinstance(value, str) else ""
            if not re.match(r"^\d{2}:\d{2}$", text):
                warnings.append(f"规则 #{rule.id} 的 end_time 不是 HH:MM，已忽略")
                continue
            normalized[key] = text
        elif key == "work_hours":
            if rule.type not in HOURS_TYPES:
                warnings.append(f"规则 #{rule.id} 不是班次工时规则，work_hours 已忽略")
                continue
            hours, note = normalize_work_hours(value)
            if note:
                warnings.append(f"规则 #{rule.id} 的 work_hours {note}，已忽略")
                continue
            normalized[key] = hours
        elif key == "hours_op":
            if rule.type not in HOURS_TYPES:
                warnings.append(f"规则 #{rule.id} 不是班次工时规则，hours_op 已忽略")
                continue
            op, note = normalize_hours_op(value)
            if note:
                warnings.append(f"规则 #{rule.id} 的 hours_op {note}，已忽略")
                continue
            # 改成「不少于」时必须有工时阈值，否则条件无意义
            target = raw_fields.get("work_hours")
            if op == "ge" and target is None and rule.work_hours is None:
                warnings.append(f"规则 #{rule.id} 改成不少于时缺少工时阈值，hours_op 已忽略")
                continue
            normalized[key] = op
        elif key == "weekdays":
            if rule.type not in WEEKDAY_TYPES:
                warnings.append(f"规则 #{rule.id} 不支持按星期限定，weekdays 已忽略")
                continue
            text = value if isinstance(value, str) else ""
            weekdays, note = normalize_weekdays(text)
            if note:
                warnings.append(f"规则 #{rule.id} 的 weekdays：{note}")
            # 空字符串表示每天都生效
            normalized[key] = weekdays
        elif key == "time_op":
            if rule.type not in ADJUST_TIME_OP_TYPES:
                warnings.append(f"规则 #{rule.id} 不支持改时刻条件，time_op 已忽略")
                continue
            text = value.strip().lower() if isinstance(value, str) else ""
            if text not in TIME_OPS:
                warnings.append(
                    f"规则 #{rule.id} 的 time_op 只能是 eq（等于）、le（不晚于）、ge（不早于），已忽略"
                )
                continue
            normalized[key] = text
        elif key == "count_op":
            if rule.type not in ADJUST_COUNT_OP_TYPES:
                warnings.append(f"规则 #{rule.id} 不支持改人数条件，count_op 已忽略")
                continue
            text = value.strip().lower() if isinstance(value, str) else ""
            if text not in COUNT_OPS:
                warnings.append(
                    f"规则 #{rule.id} 的 count_op 只能是 ge（至少）或 le（至多），已忽略"
                )
                continue
            normalized[key] = text
        elif key == "period":
            if rule.type not in ADJUST_PERIOD_TYPES:
                warnings.append(f"规则 #{rule.id} 不支持改统计周期，period 已忽略")
                continue
            text = value.strip().lower() if isinstance(value, str) else ""
            if text not in PERIODS:
                warnings.append(f"规则 #{rule.id} 的 period 只能是 week（每周）或 month（每月），已忽略")
                continue
            normalized[key] = text
        elif key == "shift_id":
            if rule.type not in SHIFT_REF_TYPES:
                warnings.append(f"规则 #{rule.id} 不支持改班次，shift_id 已忽略")
                continue
            if shifts is None:
                warnings.append(f"规则 #{rule.id} 的 shift_id 缺少班次目录，无法校验，已忽略")
                continue
            shift_id, note = normalize_shift_id(value, shifts)
            if note:
                warnings.append(f"规则 #{rule.id} 的 shift_id {note}，已忽略")
                continue
            normalized[key] = shift_id
        elif key == "role":
            if rule.type not in ROLE_TYPES:
                warnings.append(f"规则 #{rule.id} 不支持岗位限定，role 已忽略")
                continue
            role, note = normalize_role(value)
            if note:
                warnings.append(f"规则 #{rule.id} 的 role {note}")
            normalized[key] = role
        elif key == "employee_id":
            # 改人等于换一条规则，应新增 + 删除，避免破坏规则归属
            warnings.append(f"规则 #{rule.id} 不支持直接改人员，请新增并删除原规则")
        else:
            warnings.append(f"规则 #{rule.id} 的字段 {key} 不在白名单内，已忽略")
    if (
        normalized.get("date_start")
        and normalized.get("date_end")
        and normalized["date_start"] > normalized["date_end"]
    ):
        warnings.append(f"规则 #{rule.id} 的开始日期晚于结束日期，已忽略日期改动")
        normalized.pop("date_start", None)
        normalized.pop("date_end", None)
    return normalized, warnings


def normalize_adjustment(
    payload: dict[str, Any],
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    rules: list[Rule],
) -> dict[str, Any]:
    """把模型输出规范化为 add / update / remove 三段，全部只校验不写库。"""
    warnings: list[str] = [str(item) for item in payload.get("warnings") or []]
    raw_add = payload.get("add")
    parsed = normalize_payload(
        {"rules": raw_add if isinstance(raw_add, list) else [], "warnings": []},
        plan,
        employees,
        shifts,
        "global",
        None,
    )
    warnings.extend(parsed.warnings)
    adds: list[dict[str, Any]] = []
    for preview in parsed.rules:
        if not preview.valid:
            warnings.append(f"新增规则（{preview.raw_text or preview.type}）无效：{preview.note}")
            continue
        adds.append(preview.model_dump())

    rule_map = {rule.id: rule for rule in rules if rule.id is not None}
    updates: list[dict[str, Any]] = []
    for raw in payload.get("update") or []:
        if not isinstance(raw, dict):
            warnings.append("修改项格式不正确，已忽略")
            continue
        rule_id = _as_int(raw.get("rule_id"))
        if rule_id is None or rule_id not in rule_map:
            warnings.append(f"规则 #{raw.get('rule_id')} 不存在，已忽略")
            continue
        rule = rule_map[rule_id]
        raw_fields: dict[str, Any] = (
            raw["fields"] if isinstance(raw.get("fields"), dict) else {}
        )
        fields, field_warnings = normalize_adjust_fields(rule, raw_fields, plan, shifts)
        warnings.extend(field_warnings)
        if not fields:
            continue
        reason = raw.get("reason")
        updates.append(
            {
                "rule_id": rule_id,
                "fields": fields,
                "reason": reason if isinstance(reason, str) else None,
            }
        )

    removes: list[dict[str, Any]] = []
    for raw in payload.get("remove") or []:
        if not isinstance(raw, dict):
            warnings.append("删除项格式不正确，已忽略")
            continue
        rule_id = _as_int(raw.get("rule_id"))
        if rule_id is None or rule_id not in rule_map:
            warnings.append(f"规则 #{raw.get('rule_id')} 不存在，已忽略")
            continue
        reason = raw.get("reason")
        removes.append(
            {"rule_id": rule_id, "reason": reason if isinstance(reason, str) else None}
        )

    return {"add": adds, "update": updates, "remove": removes, "warnings": warnings}


def _chat_json(
    messages: list[ChatCompletionMessageParam],
    settings: dict[str, str],
    timeout: int = 180,
) -> dict[str, Any]:
    """模型调用 + JSON 提取；测试通过 monkeypatch 此函数避免真实网络请求。"""
    client = OpenAI(
        base_url=settings["llm_base_url"],
        api_key=settings["llm_api_key"],
        timeout=timeout,
        max_retries=0,
    )
    response = client.chat.completions.create(
        model=settings["llm_model"],
        messages=messages,
        temperature=0,
    )
    return extract_json(response.choices[0].message.content or "")


def parse_schedule_adjustment(
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    rules: list[Rule],
    text: str,
    settings: dict[str, str],
) -> dict[str, Any]:
    """把「对排班不满意」的自然语言解析为规则的新增 / 修改 / 删除，不写库。"""
    if not settings["llm_api_key"].strip():
        raise ValueError("尚未配置模型 API Key")
    messages = build_messages(
        plan,
        employees,
        shifts,
        "global",
        None,
        text,
        extra_context=_rule_context(employees, rules, shifts=shifts),
    ) + [{"role": "system", "content": ADJUST_PROMPT}]
    payload = _chat_json(messages, settings, timeout=180)
    result = normalize_adjustment(payload, plan, employees, shifts, rules)
    # 只对新增规则做 hard 兜底；update 里显式给出的 hard 不动
    previews = [PreviewRule(**item) for item in result["add"]]
    hints = apply_hard_hints(previews, text)
    hints += apply_deterministic_hints(previews, text, plan)
    result["add"] = [item.model_dump() for item in previews]
    # 与模型自带的提示可能重复，去重后再返回
    result["warnings"] = list(dict.fromkeys(list(result["warnings"]) + hints))
    return result


def _limit_mentions_count(text: str, count: float | None) -> bool:
    """判断「只/最多/不超过 N 人」中的 N 是否等于该条规则的人数。

    同一句里可能并存「至少 1 人」与「最多 3 人」两条人数规则，
    光看整句命中上限表述会把「至少 1 人」误改成「最多 1 人」。
    """
    if count is None:
        return False
    for match in LIMIT_PATTERN.finditer(text):
        numbers = re.findall(r"\d+", match.group(0))
        if numbers and int(numbers[-1]) == int(count):
            return True
    return False


def apply_hard_hints(rules: list[PreviewRule], text: str) -> list[str]:
    """按原文修正 LLM 解析结果：强制词把 soft 改为 hard，上限语义改为 MAX_STAFF。"""
    warnings: list[str] = []
    for rule in rules:
        # 优先用该条规则自己的原文，为空时退回整句
        source = (rule.raw_text or "").strip() or text.strip()
        forced = any(pattern.search(source) for pattern in HARD_PATTERNS) or (
            rule.type in HEADCOUNT_RULE_TYPES
            and MIN_HEADCOUNT_PATTERN.search(source) is not None
        )
        limited = (
            _limit_mentions_count(source, rule.count)
            if rule.count is not None
            else LIMIT_PATTERN.search(source) is not None
        )
        soft = SOFT_PATTERNS.search(source) is not None
        # 「只 / 最多 / 不超过 N 人」本质是上限：DAY_STAFF 改为 MAX_STAFF
        if limited and rule.type == "DAY_STAFF":
            rule.type = "MAX_STAFF"
            warnings.append(f"「{source}」为人数上限语义，已按 MAX_STAFF 处理")
        if rule.hard:
            continue
        # 连休上限始终是软约束：显式更长的休息要求优先，这里只提示不改成硬约束
        if rule.type == "MAX_REST_DAYS" and re.search(r"不得超过|不能超过|最多连休", source):
            warnings.append(f"「{source}」为连续休息上限语义，已保持软约束（显式休息优先）")
            continue
        if soft:
            # 同时出现强制/上限与放宽表述：保持模型判断，交给人工确认
            if forced or limited:
                warnings.append(f"「{source}」同时包含放宽表述，请确认是否需要硬约束")
            continue
        if forced or limited:
            rule.hard = True
            warnings.append(f"「{source}」包含强制表述，已按硬约束处理")
    return warnings


# ==== 确定性交叉校验：原文里有确定信号的字段，一律以原文为准 ====
# 只在「可以确定性判定」时纠正；留意的是模型偶发丢字段/写反方向，而不是同义改写或语序变化。

WEEKDAY_LABELS = {value: f"周{key}" for key, value in WEEKDAY_CHARS.items()}
WEEKDAY_LABELS[7] = "周日"
WEEKDAY_RANGE_PATTERN = re.compile(
    r"(?:周|星期)([一二三四五六日天])\s*(?:到|至|~|-|—)\s*(?:周|星期)?([一二三四五六日天])"
)
WEEKDAY_SINGLE_PATTERN = re.compile(r"(?:周|星期)([一二三四五六日天])")
# 「周六周日」是同一条要求下的两天合并，不是两组歧义信号
WEEKEND_PAIR_PATTERN = re.compile(r"周(?:六|日)(?:、|和|与|及)?周(?:六|日)")
WEEKEND_DAY_PATTERN = re.compile(r"星期?六日|周六日|周日六")
EXPLICIT_DATE_PATTERN = re.compile(r"\d+\s*(?:月|号|日)")

HOURS_LE_BEFORE = re.compile(
    r"(最多|至多|不超过|不多于|不高于|不得超过|不能超过)\s*(\d+(?:\.\d+)?)\s*个?\s*小时"
)
HOURS_GE_BEFORE = re.compile(
    r"(至少|不少于|不低于|起码|最少)\s*(\d+(?:\.\d+)?)\s*个?\s*小时"
)
HOURS_GE_AFTER = re.compile(r"(\d+(?:\.\d+)?)\s*个?\s*小时\s*及?\s*(?:以上|往上)")
HOURS_LE_AFTER = re.compile(r"(\d+(?:\.\d+)?)\s*个?\s*小时\s*及?\s*(?:以内|以下|之内)")
HOURS_EQ = re.compile(r"(?:正好|恰好|刚好)\s*(\d+(?:\.\d+)?)\s*个?\s*小时")
# 「极少/偶尔…排 8 小时以上」表达的是“尽量别超过”的上限意图，不能按字面 ge 纠正
HEDGE_PATTERN = re.compile(r"极少|偶尔|很少|有时|不要|避免|尽量|最好|不必")

COUNT_LE = re.compile(
    r"(最多|至多|不超过|不得超过|不能超过|只|仅)[^0-9\n]{0,3}(\d+)\s*个?\s*名?\s*人"
)
COUNT_GE = re.compile(
    r"(至少|不少于|不低于|起码|最少)[^0-9\n]{0,3}(\d+)\s*个?\s*名?\s*人"
)


def _expand_day_range(start: str | None, end: str | None) -> list[str]:
    """把日期区间展开成逐日列表（空区间返回空列表）。"""
    if not start:
        return []
    begin = date.fromisoformat(start)
    finish = date.fromisoformat(end or start)
    return [(begin + timedelta(days=offset)).isoformat() for offset in range((finish - begin).days + 1)]


def _weekday_set(value: Any) -> set[int]:
    return {int(part) for part in str(value or "").replace(" ", "").split(",") if part.isdigit()}


def _weekday_text(values: set[int]) -> str:
    return "、".join(WEEKDAY_LABELS[item] for item in sorted(values))


def _weekday_signals(text: str) -> list[set[int]]:
    """从原文提取星期信号；同一句里出现多组互不相同表述时由调用方判定为歧义。"""
    rest = text
    signals: list[set[int]] = []
    for match in WEEKDAY_RANGE_PATTERN.finditer(text):
        first, last = WEEKDAY_CHARS[match.group(1)], WEEKDAY_CHARS[match.group(2)]
        if first <= last:
            signals.append(set(range(first, last + 1)))
        else:
            signals.append(set(list(range(first, 8)) + list(range(1, last + 1))))
        rest = rest.replace(match.group(0), "")
    for keyword, values in (("周末", {6, 7}), ("工作日", {1, 2, 3, 4, 5})):
        if keyword in rest:
            signals.append(set(values))
            rest = rest.replace(keyword, "")
    for match in WEEKEND_PAIR_PATTERN.finditer(rest):
        signals.append({6, 7})
        rest = rest.replace(match.group(0), "")
    for match in WEEKEND_DAY_PATTERN.finditer(rest):
        signals.append({6, 7})
        rest = rest.replace(match.group(0), "")
    for match in WEEKDAY_SINGLE_PATTERN.finditer(rest):
        signals.append({WEEKDAY_CHARS[match.group(1)]})
    unique: list[set[int]] = []
    for item in signals:
        if item not in unique:
            unique.append(item)
    return unique


def _hours_direction(source: str, work_hours: float | None) -> str | None:
    """从原文量词推工时方向；数字必须与该规则的 work_hours 一致，冲突或歧义则放弃。"""
    if work_hours is None:
        return None
    target = float(work_hours)
    found: set[str] = set()
    for match in HOURS_LE_BEFORE.finditer(source):
        if float(match.group(2)) == target:
            found.add("le")
    for match in HOURS_GE_BEFORE.finditer(source):
        if float(match.group(2)) == target:
            found.add("ge")
    for match in HOURS_GE_AFTER.finditer(source):
        if float(match.group(1)) != target:
            continue
        if HEDGE_PATTERN.search(source[max(0, match.start() - 8) : match.start()]):
            # 「极少/偶尔…排 8 小时以上」表达的是“尽量别超过”的上限意图
            found.add("le")
        else:
            found.add("ge")
    for match in HOURS_LE_AFTER.finditer(source):
        if float(match.group(1)) == target:
            found.add("le")
    for match in HOURS_EQ.finditer(source):
        if float(match.group(1)) == target:
            found.add("eq")
    return found.pop() if len(found) == 1 else None


def _count_direction(source: str, count: float | None) -> str | None:
    """从原文量词推人数方向；只认与该规则 count 一致的表述。"""
    if count is None:
        return None
    target = float(count)
    found: set[str] = set()
    for match in COUNT_LE.finditer(source):
        if float(match.group(2)) == target:
            found.add("le")
    for match in COUNT_GE.finditer(source):
        if float(match.group(2)) == target:
            found.add("ge")
    return found.pop() if len(found) == 1 else None


def _check_weekdays(rule: PreviewRule, source: str, month_start: str, month_end: str, warnings: list[str]):
    """星期表述与日期集合必须自洽；以原文的星期为准。"""
    signals = _weekday_signals(source)
    if len(signals) != 1:
        # 无信号或同句多组星期表述（如「工作日…周末…」）时不做推断
        return
    target = next(iter(signals))
    current = _weekday_set(rule.weekdays)
    days = _expand_day_range(rule.date_start, rule.date_end)
    explicit = EXPLICIT_DATE_PATTERN.search(source) is not None
    if days and all(date.fromisoformat(day).isoweekday() in target for day in days):
        # 日期本身已全部落在该星期几，补 weekdays 只是冗余，不做改动
        return
    if current == target:
        if days and not any(date.fromisoformat(day).isoweekday() in target for day in days):
            if explicit:
                return
            old = f"{rule.date_start}~{rule.date_end or rule.date_start}"
            rule.date_start, rule.date_end = month_start, month_end
            warnings.append(
                f"系统已修正日期范围：{old} → {month_start}~{month_end}"
                f"（依据：原文仅出现「{_weekday_text(target)}」，原范围内没有任何该星期几的日期）"
            )
        return
    old_value = rule.weekdays or "空"
    rule.weekdays = ",".join(str(item) for item in sorted(target))
    warnings.append(
        f"系统已按原文修正 weekdays：{old_value} → {rule.weekdays}"
        f"（依据：原文出现「{_weekday_text(target)}」）"
    )
    if not explicit and (rule.date_start or rule.date_end):
        old = f"{rule.date_start}~{rule.date_end or rule.date_start}"
        rule.date_start, rule.date_end = month_start, month_end
        warnings.append(
            f"系统已修正日期范围：{old} → {month_start}~{month_end}"
            f"（依据：原文是「每{_weekday_text(target)}」的周期表述，未给具体日期）"
        )


def _check_directions(rule: PreviewRule, source: str, warnings: list[str]):
    if rule.work_hours is not None:
        wanted = _hours_direction(source, rule.work_hours)
        if wanted and wanted != rule.hours_op:
            old = rule.hours_op or "空"
            rule.hours_op = wanted
            warnings.append(
                f"系统已按原文修正 hours_op：{old} → {wanted}（依据：原文量词「{source.strip()}」）"
            )
    if rule.count is not None and rule.count_op:
        wanted = _count_direction(source, rule.count)
        if wanted and wanted != rule.count_op:
            old = rule.count_op
            rule.count_op = wanted
            warnings.append(
                f"系统已按原文修正 count_op：{old} → {wanted}（依据：原文量词「{source.strip()}」）"
            )


def _check_hard_flag(rule: PreviewRule, source: str, warnings: list[str]):
    default = TYPE_DEFAULT_HARD.get(rule.type, True)
    forced = any(pattern.search(source) for pattern in HARD_PATTERNS)
    limited = (
        _limit_mentions_count(source, rule.count)
        if rule.count is not None
        else LIMIT_PATTERN.search(source) is not None
    )
    soft = SOFT_PATTERNS.search(source) is not None
    if rule.hard and (soft or not default) and not forced and not limited:
        rule.hard = False
        warnings.append(
            f"系统已按默认口径修正 hard：{rule.type} True → False"
            f"（依据：原文{'含放宽表述' if soft else '无强制表述'}，{rule.type} 按软约束处理）"
        )
    elif not rule.hard and default and not soft:
        rule.hard = True
        warnings.append(
            f"系统已按默认口径修正 hard：{rule.type} False → True"
            f"（依据：原文无放宽表述，{rule.type} 默认为硬约束）"
        )


def _final_hard_warnings(
    rules: list[PreviewRule], original_hard: dict[int, bool]
) -> list[str]:
    """按最终落库值生成 hard 修正提示：中途改过又改回的规则不输出误导文案。"""
    hints: list[str] = []
    for rule in rules:
        if not rule.valid:
            continue
        before = original_hard.get(id(rule))
        if before is None or before == rule.hard:
            continue
        hints.append(
            f"系统已按最终口径修正 hard：{rule.type} "
            f"{'True' if before else 'False'} → {'True' if rule.hard else 'False'}"
            f"（最终落库 hard={'True' if rule.hard else 'False'}）"
        )
    return hints


def _check_paid_leave_window(rules: list[PreviewRule], month_start: str, month_end: str, warnings: list[str]):
    """带薪假的区间必须落在同批 REST 给出的休息窗口内。"""
    windows: dict[int, list[str]] = {}
    for rule in rules:
        if not rule.valid or rule.type != "REST" or rule.employee_id is None:
            continue
        days = [
            day
            for day in _expand_day_range(rule.date_start, rule.date_end)
            if month_start <= day <= month_end
        ]
        weekdays = _weekday_set(rule.weekdays)
        if weekdays:
            days = [day for day in days if date.fromisoformat(day).isoweekday() in weekdays]
        windows.setdefault(rule.employee_id, []).extend(days)
    for rule in rules:
        if not rule.valid or rule.type != "PAID_LEAVE" or rule.employee_id is None:
            continue
        window = sorted(set(windows.get(rule.employee_id) or []))
        if not window or not rule.date_start:
            continue
        low, high = window[0], window[-1]
        old_end = rule.date_end or rule.date_start
        if low <= rule.date_start and old_end <= high:
            continue
        old = f"{rule.date_start}~{old_end}"
        rule.date_start, rule.date_end = low, high
        warnings.append(
            f"系统已把带薪假日期收敛到同批休息窗口：{old} → {low}~{high}"
            f"（依据：同批休息规则给出 {low}~{high}，假期必须落在窗口内）"
        )


def apply_deterministic_hints(rules: list[PreviewRule], text: str, plan: Plan) -> list[str]:
    """归一化层交叉校验：凡原文可确定性判定的字段，一律以原文为准，并逐条留痕。"""
    month_start, month_end = month_bounds(plan.month)
    warnings: list[str] = []
    for rule in rules:
        if not rule.valid:
            continue
        source = (rule.raw_text or "").strip() or text.strip()
        if rule.type in WEEKDAY_TYPES:
            _check_weekdays(rule, source, month_start, month_end, warnings)
        _check_directions(rule, source, warnings)
        _check_hard_flag(rule, source, warnings)
    _check_paid_leave_window(rules, month_start, month_end, warnings)
    return warnings


# 文本兜底恢复：模型偶尔会把本可表达的句子直接写成「无法表达」且不下发规则。
# 这里只恢复原文有确定数值/量词信号的条款，避免凭空猜测。
CLAUSE_SPLIT = re.compile(r"[，,。；;\n]")
HOURS_TEXT = re.compile(r"(\d+(?:\.\d+)?)\s*个?\s*小时")
REST_LIMIT_TEXT = re.compile(
    r"连(?:休|续休息)[^，,。；;\n]{0,10}?"
    r"(?:不得超过|不能超过|不超过|最多)"
    r"[^0-9零一二两三四五六七八九十]{0,4}"
    r"([0-9零一二两三四五六七八九十]{1,4})\s*天"
)
PEOPLE_TEXT = re.compile(r"([0-9零一二两三四五六七八九十]{1,3})\s*个?\s*(?:名|位)?\s*人")
# 月度总工时的确定性表达：每人工时 N / 每人月工时 N / 月度工时 N / 工时 N
MONTHLY_HOURS_TEXT = re.compile(
    r"(每人(?:月)?工时|每月总工时|月度工时|月工时|工时)"
    r"[^0-9零一二两三四五六七八九十百]{0,4}"
    r"([0-9]{1,3}|[零一二两三四五六七八九十百]{1,5})"
)
MONTHLY_PER_PERSON_TEXT = re.compile(
    r"每人\s*([0-9]{1,3}|[零一二两三四五六七八九十百]{1,5})\s*个?\s*小时"
)
UNEXPRESSIBLE_MARK = re.compile(r"无法表达\s*[:：]\s*")


def _weekday_key(weekdays: str | None) -> tuple[int, ...]:
    return tuple(sorted(_weekday_set(weekdays)))


def _clauses(text: str) -> list[str]:
    return [item.strip() for item in CLAUSE_SPLIT.split(text) if item.strip()]


def _mentions_shift_hours(clause: str) -> bool:
    if "班" in clause or "工时" in clause:
        return True
    return "小时" in clause and any(
        word in clause
        for word in ("尽量", "最好", "极少", "偶尔", "很少", "以上", "以内", "以下", "至少", "最多")
    )


def _recover_shift_hours(
    rules: list[PreviewRule],
    clause: str,
    month_start: str,
    month_end: str,
    plan: Plan,
) -> PreviewRule | None:
    if not _mentions_shift_hours(clause):
        return None
    values = {float(match) for match in HOURS_TEXT.findall(clause)}
    if len(values) != 1:
        return None
    hours = values.pop()
    if not 0 < hours <= 24:
        return None
    signals = _weekday_signals(clause)
    weekdays = None
    if len(signals) == 1:
        weekdays = ",".join(str(item) for item in sorted(next(iter(signals))))
    op = _hours_direction(clause, hours) or "eq"
    key = (round(hours, 2), op, _weekday_key(weekdays))
    for rule in rules:
        if (
            rule.valid
            and rule.type == "SHIFT_HOURS"
            and rule.work_hours is not None
            and (round(rule.work_hours, 2), rule.hours_op or "eq", _weekday_key(rule.weekdays)) == key
        ):
            return None
    return PreviewRule(
        index=len(rules),
        type="SHIFT_HOURS",
        date_start=month_start,
        date_end=month_end,
        work_hours=hours,
        hours_op=op,
        weekdays=weekdays,
        hard=False,
        raw_text=clause,
    )


def _recover_max_rest_days(
    rules: list[PreviewRule], clause: str, month_start: str, month_end: str
) -> PreviewRule | None:
    match = REST_LIMIT_TEXT.search(clause)
    if not match:
        return None
    limit = chinese_to_int(match.group(1))
    if limit is None or not 1 <= limit <= 15:
        return None
    for rule in rules:
        if rule.valid and rule.type == "MAX_REST_DAYS" and rule.count == limit:
            return None
    return PreviewRule(
        index=len(rules),
        type="MAX_REST_DAYS",
        date_start=month_start,
        date_end=month_end,
        count=limit,
        hard=False,
        raw_text=clause,
    )


def _recover_monthly_hours(
    rules: list[PreviewRule], clause: str, month_start: str, month_end: str
) -> PreviewRule | None:
    """原文写了「每人工时 N」，就不允许退化成 warning。"""
    if any(rule.valid and rule.type == "MONTHLY_HOURS" for rule in rules):
        return None
    value = None
    match = MONTHLY_HOURS_TEXT.search(clause)
    if match:
        value = chinese_to_int(match.group(2))
    if value is None:
        match = MONTHLY_PER_PERSON_TEXT.search(clause)
        if match:
            value = chinese_to_int(match.group(1))
    if value is None or not 1 <= value <= 744:
        return None
    return PreviewRule(
        index=len(rules),
        type="MONTHLY_HOURS",
        date_start=month_start,
        date_end=month_end,
        count=value,
        hard=True,
        raw_text=clause,
    )


def recover_missing_rules(
    rules: list[PreviewRule], text: str, plan: Plan
) -> tuple[list[PreviewRule], set[str]]:
    """按原文补回模型漏掉的确定性规则；返回新增规则与被恢复的原文条款。"""
    month_start, month_end = month_bounds(plan.month)
    working = list(rules)
    recovered: list[PreviewRule] = []
    clauses: set[str] = set()
    has_day_staff = any(rule.valid and rule.type == "DAY_STAFF" for rule in working)

    for clause in _clauses(text):
        added = _recover_shift_hours(working, clause, month_start, month_end, plan)
        if added is not None:
            working.append(added)
            recovered.append(added)
            clauses.add(clause)
        added = _recover_max_rest_days(working, clause, month_start, month_end)
        if added is not None:
            working.append(added)
            recovered.append(added)
            clauses.add(clause)
        added = _recover_monthly_hours(working, clause, month_start, month_end)
        if added is not None:
            working.append(added)
            recovered.append(added)
            clauses.add(clause)

    if not has_day_staff:
        daily: dict[tuple[int, tuple[int, ...]], PreviewRule] = {}
        for clause in _clauses(text):
            match = PEOPLE_TEXT.search(clause)
            if not match:
                continue
            count = chinese_to_int(match.group(1))
            if count is None or not 1 <= count <= 60:
                continue
            signals = _weekday_signals(clause)
            if "每天" in clause and not signals:
                weekdays = None
            elif len(signals) == 1:
                weekdays = ",".join(str(item) for item in sorted(next(iter(signals))))
            else:
                continue
            key = (count, _weekday_key(weekdays))
            if key in daily:
                continue
            added = PreviewRule(
                index=len(working) + len(recovered),
                type="DAY_STAFF",
                date_start=month_start,
                date_end=month_end,
                count=count,
                weekdays=weekdays,
                hard=False,
                raw_text=clause,
            )
            daily[key] = added
            recovered.append(added)
            clauses.add(clause)

    return recovered, clauses


def drop_recovered_warnings(warnings: list[str], clauses: set[str]) -> list[str]:
    """去掉「无法表达」中已被文本兜底恢复掉的条款，保留真正无法表达的提示。"""
    if not clauses:
        return list(warnings)
    kept: list[str] = []
    for warning in warnings:
        match = UNEXPRESSIBLE_MARK.search(warning)
        if match:
            body = warning[match.end():]
            body = re.split(r"。可尝试|。建议|。请", body)[0]
            parts = [item.strip() for item in CLAUSE_SPLIT.split(body) if item.strip()]
            if parts and all(
                any(part in clause or clause in part for clause in clauses) for part in parts
            ):
                continue
        kept.append(warning)
    return kept


# ==== 模型超时后的本地确定性兜底 ====
# 只提取原文有明确日期/数量/关键词的条款；歧义内容进入 missing，由用户手动补充。
FALLBACK_NOTICE = (
    f"模型在 {PARSE_TIMEOUT_SECONDS} 秒内未返回，已改用本地关键词规则确定性提取；"
    "这不是完整的 AI 解析结果，请逐条核对。"
)
FALLBACK_RULE_NOTE = "本地兜底：按原文关键词提取"
FALLBACK_TIME_RANGE = re.compile(
    r"(\d{1,2}):(\d{2})\s*(?:-|~|—|到|至)\s*(\d{1,2}):(\d{2})"
)
FALLBACK_DATE_RANGE = re.compile(
    r"(?:(\d{1,2})\s*月\s*)?(\d{1,2})\s*[日号]?"
    r"\s*(?:-|~|—|到|至)\s*"
    r"(?:(\d{1,2})\s*月\s*)?(\d{1,2})\s*[日号]"
)
FALLBACK_MONTH_DAY = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?")
FALLBACK_DAY_ONLY = re.compile(r"(\d{1,2})\s*[日号]")
FALLBACK_PAID_LEAVE = re.compile(r"有薪|带薪|POFF|年假|调休|休假")
FALLBACK_MUST_WORK = re.compile(
    r"不能休|不许休|不允许休息|不可以休|不得休|必须上班|必须到岗|要上班|得上班"
)
FALLBACK_REST = re.compile(r"休息|不上班")
FALLBACK_SHIFT_VERB = re.compile(r"上|排|到岗|值班")
FALLBACK_CONSECUTIVE = re.compile(
    r"连续\s*(?:上班|工作)?[^0-9零一二两三四五六七八九十]{0,6}"
    r"(\d{1,2}|[零一二两三四五六七八九十]{1,3})\s*天"
)
FALLBACK_ZERO_STAFF = re.compile(r"不排任何人|停业|全员休息")
FALLBACK_DAY_COUNT = re.compile(r"(\d{1,2}|[零一二两三四五六七八九十]{1,3})\s*天")


def _fallback_safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _fallback_clip(start: date, end: date, plan: Plan) -> tuple[str, str] | None:
    """把原文日期区间裁剪到计划月份内；完全落在月外时返回 None。"""
    if end < start:
        return None
    days = intersect_dates(start.isoformat(), end.isoformat(), plan.month)
    if not days:
        return None
    return days[0], days[-1]


def _fallback_date_range(clause: str, plan: Plan) -> tuple[str, str] | None:
    year, plan_month = (int(part) for part in plan.month.split("-"))
    cleaned = re.sub(r"\d{1,2}:\d{2}", " ", clause)
    match = FALLBACK_DATE_RANGE.search(cleaned)
    if match:
        month1_text, day1_text, month2_text, day2_text = match.groups()
        month1 = int(month1_text) if month1_text else plan_month
        start = _fallback_safe_date(year, month1, int(day1_text))
        if start is None:
            return None
        if month2_text:
            end = _fallback_safe_date(start.year, int(month2_text), int(day2_text))
            if end is None or end < start:
                end = _fallback_safe_date(start.year + 1, int(month2_text), int(day2_text))
        else:
            end = _fallback_safe_date(start.year, month1, int(day2_text))
            if end is None or end < start:
                next_month = month1 % 12 + 1
                next_year = start.year + (1 if month1 == 12 else 0)
                end = _fallback_safe_date(next_year, next_month, int(day2_text))
        if end is None or end < start:
            return None
        return _fallback_clip(start, end, plan)
    match = FALLBACK_MONTH_DAY.search(cleaned)
    if match:
        picked = _fallback_safe_date(year, int(match.group(1)), int(match.group(2)))
        return _fallback_clip(picked, picked, plan) if picked else None
    match = FALLBACK_DAY_ONLY.search(cleaned)
    if match:
        picked = _fallback_safe_date(year, plan_month, int(match.group(1)))
        return _fallback_clip(picked, picked, plan) if picked else None
    return None


def _fallback_compress(days: list[str]) -> list[tuple[str, str]]:
    runs: list[tuple[str, str]] = []
    for day in days:
        if runs and (date.fromisoformat(day) - date.fromisoformat(runs[-1][1])).days == 1:
            runs[-1] = (runs[-1][0], day)
        else:
            runs.append((day, day))
    return runs


def _fallback_weekday_ranges(clause: str, plan: Plan) -> list[tuple[str, str]]:
    signals = _weekday_signals(clause)
    if len(signals) != 1:
        return []
    picked = next(iter(signals))
    days = [
        item
        for item in month_dates(plan.month)
        if date.fromisoformat(item).isoweekday() in picked
    ]
    return _fallback_compress(days)


def _fallback_targets(
    clause: str, scope: str, employee: Employee | None, employees: list[Employee]
) -> list[Employee]:
    if scope == "employee":
        return [employee] if employee is not None else []
    return [item for item in employees if item.name and item.name in clause]


def _fallback_leave_count(clause: str, start: str, end: str) -> int:
    match = FALLBACK_DAY_COUNT.search(clause)
    if match:
        value = chinese_to_int(match.group(1))
        if value is not None and 1 <= value <= 31:
            return value
    span = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    return max(1, min(span, 31))


def _fallback_employee_rules(
    clause: str,
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    scope: str,
    employee: Employee | None,
) -> list[PreviewRule]:
    targets = _fallback_targets(clause, scope, employee, employees)
    if not targets:
        return []
    date_range = _fallback_date_range(clause, plan)
    ranges = [date_range] if date_range else _fallback_weekday_ranges(clause, plan)
    if not ranges:
        return []
    rules: list[PreviewRule] = []
    for target in targets:
        for start, end in ranges:
            if FALLBACK_PAID_LEAVE.search(clause):
                rule_type = "PAID_LEAVE"
                count = _fallback_leave_count(clause, start, end)
            elif FALLBACK_MUST_WORK.search(clause):
                rule_type = "MUST_WORK"
                count = None
            elif FALLBACK_REST.search(clause):
                rule_type = "REST"
                count = None
            else:
                rule_type = ""
                count = None
            if rule_type:
                rules.append(
                    PreviewRule(
                        index=0,
                        employee_id=target.id,
                        employee_name=target.name,
                        type=rule_type,
                        date_start=start,
                        date_end=end,
                        count=count,
                        hard=True,
                        raw_text=clause,
                        origin="fallback",
                        note=FALLBACK_RULE_NOTE,
                    )
                )
                continue
            shift = next(
                (item for item in shifts if item.name and item.name in clause), None
            )
            if shift is None or not FALLBACK_SHIFT_VERB.search(clause):
                continue
            rules.append(
                PreviewRule(
                    index=0,
                    employee_id=target.id,
                    employee_name=target.name,
                    type="FIXED_SHIFT",
                    date_start=start,
                    date_end=end,
                    shift_id=shift.id,
                    shift_name=shift.name,
                    hard=True,
                    raw_text=clause,
                    origin="fallback",
                    note=FALLBACK_RULE_NOTE,
                )
            )
    return rules


def _fallback_extra_rules(
    clause: str,
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    scope: str,
    employee: Employee | None,
) -> list[PreviewRule]:
    month_start, month_end = month_bounds(plan.month)
    rules: list[PreviewRule] = []
    # 停业 / 当天不排任何人
    if FALLBACK_ZERO_STAFF.search(clause):
        date_range = _fallback_date_range(clause, plan) or (month_start, month_end)
        rules.append(
            PreviewRule(
                index=0,
                type="MAX_STAFF",
                date_start=date_range[0],
                date_end=date_range[1],
                count=0,
                hard=True,
                raw_text=clause,
                origin="fallback",
                note=FALLBACK_RULE_NOTE,
            )
        )
        return rules
    # 时段覆盖：显式 HH:MM-HH:MM + 人数
    time_match = FALLBACK_TIME_RANGE.search(clause)
    people_match = PEOPLE_TEXT.search(clause)
    if time_match and people_match:
        count = chinese_to_int(people_match.group(1))
        if count is not None and 1 <= count <= 60:
            start_time = normalize_hhmm(f"{int(time_match.group(1)):02d}:{time_match.group(2)}")
            end_time = normalize_hhmm(f"{int(time_match.group(3)):02d}:{time_match.group(4)}")
            date_range = _fallback_date_range(clause, plan) or (month_start, month_end)
            soft = any(word in clause for word in HEADCOUNT_SOFT_WORDS)
            hard = not soft and (
                any(word in clause for word in HEADCOUNT_HARD_WORDS)
                or MIN_HEADCOUNT_PATTERN.search(clause) is not None
            )
            rules.append(
                PreviewRule(
                    index=0,
                    type="SEGMENT_STAFF",
                    date_start=date_range[0],
                    date_end=date_range[1],
                    start_time=start_time,
                    end_time=end_time,
                    count=count,
                    hard=hard,
                    raw_text=clause,
                    origin="fallback",
                    note=FALLBACK_RULE_NOTE,
                )
            )
            return rules
    # 连续上班上限
    consecutive = FALLBACK_CONSECUTIVE.search(clause)
    if consecutive:
        count = chinese_to_int(consecutive.group(1))
        if count is not None and 1 <= count <= 31:
            date_range = _fallback_date_range(clause, plan) or (month_start, month_end)
            targets = _fallback_targets(clause, scope, employee, employees)
            if not targets:
                targets = [None]  # type: ignore[list-item]
            for target in targets:
                rules.append(
                    PreviewRule(
                        index=0,
                        employee_id=target.id if target else None,
                        employee_name=target.name if target else None,
                        type="MAX_CONSECUTIVE_WORK",
                        date_start=date_range[0],
                        date_end=date_range[1],
                        count=count,
                        hard=True,
                        raw_text=clause,
                        origin="fallback",
                        note=FALLBACK_RULE_NOTE,
                    )
                )
            return rules
    return _fallback_employee_rules(
        clause, plan, employees, shifts, scope, employee
    )


def fallback_parse(
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    scope: str,
    employee: Employee | None,
    text: str,
) -> ParseResponse:
    """模型超时后的确定性兜底：只提取原文有明确信号的条款，并标注缺失项。"""
    clauses = _clauses(text)
    claimed: set[str] = set()
    extra_rules: list[PreviewRule] = []
    # 先跑更具体的提取（时段覆盖、员工日期规则等），避免被泛化的「每天 N 人」抢先认领
    for clause in clauses:
        extra = _fallback_extra_rules(clause, plan, employees, shifts, scope, employee)
        if extra:
            extra_rules.extend(extra)
            claimed.add(clause)
    recovered, _ = recover_missing_rules([], text, plan)
    recovered = [rule for rule in recovered if (rule.raw_text or "").strip() not in claimed]
    claimed.update((rule.raw_text or "").strip() for rule in recovered)
    rules = recovered + extra_rules
    if scope == "employee" and employee is not None:
        for rule in rules:
            if rule.type in EMPLOYEE_OPTIONAL and rule.employee_id is None:
                rule.employee_id = employee.id
                rule.employee_name = employee.name
    parsed = ParseResponse(
        rules=rules,
        warnings=[FALLBACK_NOTICE],
        mode="fallback",
        missing=[clause for clause in clauses if clause not in claimed],
    )
    # 兜底规则同样按原文做确定性 hard/星期校正（该链路不调用模型）
    hints = apply_hard_hints(parsed.rules, text)
    hints += apply_deterministic_hints(parsed.rules, text, plan)
    for index, rule in enumerate(parsed.rules):
        rule.index = index
        rule.origin = "fallback"
        rule.note = FALLBACK_RULE_NOTE
    warnings = [FALLBACK_NOTICE]
    if parsed.missing:
        warnings.append("以下原文未能确定性提取，需要手动补充：" + "；".join(parsed.missing))
    warnings.extend(hints)
    warnings.extend(item for item in parsed.warnings if item not in warnings)
    update: dict[str, Any] = {"warnings": warnings, "mode": "fallback"}
    if not parsed.rules:
        update["hint"] = (
            "模型超时，且原文没有可确定性提取的规则；请稍后重试 AI 解析，"
            "或在规则列表手动新建规则。"
        )
    return parsed.model_copy(update=update)


def _normalize_day_staff_overlap(rules: list[PreviewRule], plan: Plan) -> list[PreviewRule]:
    """把每日人数规则收敛成稳定形态：基准人数按连续段合并，放宽日期逐日单列。

    模型有三种等价但形态不同的输出：整月区间叠加周几规则、按日期区间拼、逐日展开。
    前两种会让「周末/周三可以 1 人」无法稳定表达（整月规则会把放宽重新收紧），
    这里统一按日期求有效人数：周几规则优先于每日规则，与基准人数不同的日期逐日
    成条，其余按连续段合并。
    """
    month_start, month_end = month_bounds(plan.month)
    staff = [
        rule
        for rule in rules
        if rule.valid and rule.type == "DAY_STAFF" and rule.count is not None
    ]
    if len(staff) < 2:
        return rules
    generic = [rule for rule in staff if not _weekday_set(rule.weekdays)]
    specific = [rule for rule in staff if _weekday_set(rule.weekdays)]

    def covers(rule: PreviewRule, day: str, weekday: int) -> bool:
        start = rule.date_start or ""
        end = rule.date_end or rule.date_start or ""
        if not start or not (start <= day <= end):
            return False
        weekdays = _weekday_set(rule.weekdays)
        return not weekdays or weekday in weekdays

    winners: dict[str, tuple[int, PreviewRule, bool]] = {}
    for day in _expand_day_range(month_start, month_end):
        weekday = date.fromisoformat(day).isoweekday()
        spec = [rule for rule in specific if covers(rule, day, weekday)]
        gen = [rule for rule in generic if covers(rule, day, weekday)]
        if spec:
            winner = min(spec, key=lambda rule: int(rule.count or 0))
            winners[day] = (int(winner.count or 0), winner, True)
        elif gen:
            winner = min(gen, key=lambda rule: int(rule.count or 0))
            winners[day] = (int(winner.count or 0), winner, False)
    if not winners:
        return rules

    # 覆盖天数最多的人数为基准；并列时取更严格（更大）的人数
    frequency: dict[int, int] = {}
    for count, _, _ in winners.values():
        frequency[count] = frequency.get(count, 0) + 1
    dominant = max(frequency, key=lambda value: (frequency[value], value))
    if all(count == dominant for count, _, _ in winners.values()):
        return rules

    normalized: list[PreviewRule] = []
    run_start: str | None = None
    run_end: str | None = None
    run_winner: PreviewRule | None = None

    def flush_run() -> None:
        nonlocal run_start, run_end, run_winner
        if run_start is not None and run_end is not None and run_winner is not None:
            normalized.append(
                run_winner.model_copy(
                    update={"date_start": run_start, "date_end": run_end, "weekdays": None}
                )
            )
        run_start = run_end = None
        run_winner = None

    for day in _expand_day_range(month_start, month_end):
        info = winners.get(day)
        if info is None:
            flush_run()
            continue
        count, winner, is_specific = info
        if is_specific or count != dominant:
            flush_run()
            normalized.append(winner.model_copy(update={"date_start": day, "date_end": day}))
            continue
        run_key = (
            None
            if run_winner is None
            else (run_winner.employee_id, run_winner.hard)
        )
        if run_start is None or run_key != (winner.employee_id, winner.hard):
            flush_run()
            run_start = day
            run_winner = winner
        run_end = day
    flush_run()

    staff_ids = {id(rule) for rule in staff}
    first = next(index for index, rule in enumerate(rules) if id(rule) in staff_ids)
    kept = [rule for rule in rules if id(rule) not in staff_ids]
    merged = kept[:first] + normalized + kept[first:]
    for index, rule in enumerate(merged):
        rule.index = index
    return merged


def finalize_parsed(parsed: ParseResponse, text: str, plan: Plan, scope: str) -> ParseResponse:
    """归一化之后的统一收口：文本兜底补规则 + 确定性交叉校验（不依赖模型调用）。"""
    if scope == "global":
        # 文本兜底：模型把本可表达的条款写成「无法表达」且漏发规则时，按原文确定信号补回
        recovered, recovered_clauses = recover_missing_rules(parsed.rules, text, plan)
        if recovered:
            parsed.rules = [
                rule
                for rule in parsed.rules
                if not (
                    not rule.valid
                    and (rule.raw_text or "").strip() in recovered_clauses
                )
            ] + recovered
            parsed.warnings = drop_recovered_warnings(parsed.warnings, recovered_clauses)
            parsed.hint = failure_hint(parsed.rules, parsed.warnings)
    # 每日人数与周几人数重叠时拆成逐日规则，避免整月规则把放宽部分重新收紧；
    # 已按工作日/周三/周末分档（weekdays 有值）的规则不再二次展开
    if any(
        rule.type == "DAY_STAFF" and not rule.weekdays
        for rule in parsed.rules
        if rule.valid
    ):
        parsed.rules = _normalize_day_staff_overlap(parsed.rules, plan)
    # 记录模型给出的 hard 原值，最终值确定后据此生成提示，避免“提示说改成软、落库却是硬”
    original_hard = {id(rule): rule.hard for rule in parsed.rules if rule.valid}
    # 兜底修正：模型漏判强制表述时按原文把 soft 改成 hard
    hints = apply_hard_hints(parsed.rules, text)
    # 交叉校验：星期、方向、hard、带薪假窗口等可确定性判定的字段，一律以原文为准
    hints += apply_deterministic_hints(parsed.rules, text, plan)
    # 连休上限始终是软约束：显式更长的休息要求优先，任何提示层不得把它改回硬约束
    for rule in parsed.rules:
        if rule.type == "MAX_REST_DAYS":
            rule.hard = False
        # 时段/到岗/班次人数要求：原文明确写「至少 N 人」、含强制语义或周末口径时
        # 按硬约束执行；只有带「尽量/最好/可以/实在排不开」这类模动词才降为软约束
        if rule.type in ("SEGMENT_STAFF", "START_STAFF", "END_STAFF", "SHIFT_STAFF"):
            source = (rule.raw_text or "").strip()
            if (
                MIN_HEADCOUNT_PATTERN.search(source) is not None
                or any(
                    word in source
                    for word in ("周六", "周日", "周末", "覆盖", "在岗")
                )
            ):
                rule.hard = not any(
                    word in source for word in HEADCOUNT_SOFT_WORDS
                )
    # hard 提示一律基于最终落库值生成；中途改过又改回的不再输出误导文案
    hints = [
        hint
        for hint in hints
        if "已按硬约束处理" not in hint and "系统已按默认口径修正 hard" not in hint
    ] + _final_hard_warnings(parsed.rules, original_hard)
    if any(rule.valid and rule.type == "MONTHLY_HOURS" for rule in parsed.rules):
        # 已经落库为 MONTHLY_HOURS 规则时，不能再保留「不支持整月总工时」的旧提示
        stale = lambda hint: "总工时" in hint or ("不支持" in hint and "工时" in hint)
        hints = [hint for hint in hints if not stale(hint)]
        parsed.warnings = [hint for hint in parsed.warnings if not stale(hint)]
    if hints:
        # 与模型自带的提示可能重复，去重后再返回
        merged = list(dict.fromkeys(list(parsed.warnings) + hints))
        parsed = parsed.model_copy(update={"warnings": merged})
    return parsed


def parse_constraints(
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    scope: str,
    employee: Employee | None,
    text: str,
    base_url: str,
    model: str,
    api_key: str,
) -> ParseResponse:
    if not api_key.strip():
        raise ValueError("尚未配置模型 API Key")
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=PARSE_TIMEOUT_SECONDS, max_retries=0)
    messages = build_messages(plan, employees, shifts, scope, employee, text)
    started = time.monotonic()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0,
        )
    except APITimeoutError:
        # 超时不再二次重试：主解析由路由改用确定性兜底，避免用户同步等待翻倍到 360 秒
        logger.warning(
            "约束解析超时（%.1f 秒），改由路由使用本地兜底",
            time.monotonic() - started,
        )
        raise
    content = response.choices[0].message.content or ""
    payload = extract_json(content)
    parsed = normalize_payload(payload, plan, employees, shifts, scope, employee)
    parsed = finalize_parsed(parsed, text, plan, scope)
    return parsed


def normalize_conflict_updates(
    payload: dict[str, Any],
    plan: Plan,
    rules: list[Rule],
    shifts: list[Shift],
) -> tuple[list[dict[str, Any]], list[str]]:
    """解析冲突助手返回的 updates 段；字段校验复用 normalize_adjust_fields。"""
    warnings: list[str] = []
    rule_map = {rule.id: rule for rule in rules if rule.id is not None}
    updates: list[dict[str, Any]] = []
    for raw in payload.get("updates") or []:
        if not isinstance(raw, dict):
            warnings.append("修改项格式不正确，已忽略")
            continue
        rule_id = _as_int(raw.get("rule_id"))
        if rule_id is None or rule_id not in rule_map:
            warnings.append(f"修改项引用了不存在的规则 #{raw.get('rule_id')}，已忽略")
            continue
        rule = rule_map[rule_id]
        raw_fields: dict[str, Any] = (
            raw["fields"] if isinstance(raw.get("fields"), dict) else {}
        )
        fields, field_warnings = normalize_adjust_fields(rule, raw_fields, plan, shifts)
        warnings.extend(field_warnings)
        if not fields:
            continue
        reason = raw.get("reason")
        updates.append(
            {
                "rule_id": rule_id,
                "fields": fields,
                "reason": reason if isinstance(reason, str) else None,
            }
        )
    return updates, warnings


def parse_conflict_advice(
    plan: Plan,
    employees: list[Employee],
    shifts: list[Shift],
    scope: str,
    employee: Employee | None,
    text: str,
    rules: list[Rule],
    extra_context: str,
    base_url: str,
    model: str,
    api_key: str,
) -> tuple[ParseResponse, list[dict[str, Any]], list[dict[str, Any]]]:
    """解析用户对冲突的修改想法，返回规则预览、可执行的修改项与删除项。"""
    if not api_key.strip():
        raise ValueError("尚未配置模型 API Key")
    client = OpenAI(
        base_url=base_url, api_key=api_key, timeout=ADVICE_TIMEOUT_SECONDS, max_retries=0
    )
    messages = build_messages(
        plan, employees, shifts, scope, employee, text, extra_context=extra_context
    )
    retried = False
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0,
        )
    except APITimeoutError:
        retried = True
        logger.warning("冲突修改解析首次超时（%s 秒），自动重试一次", ADVICE_TIMEOUT_SECONDS)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0,
        )
    payload = extract_json(response.choices[0].message.content or "")
    parsed = normalize_payload(payload, plan, employees, shifts, scope, employee)
    warnings = list(parsed.warnings)
    if retried:
        warnings.append("解析曾超时，已自动重试一次")
    rule_ids = {rule.id for rule in rules if rule.id is not None}
    removals: list[dict[str, Any]] = []
    raw_removals = payload.get("removals")
    for raw in raw_removals if isinstance(raw_removals, list) else []:
        if not isinstance(raw, dict):
            warnings.append("删除项格式不正确，已忽略")
            continue
        rule_id = _as_int(raw.get("rule_id"))
        if rule_id is None:
            warnings.append(f"删除项的规则编号无法识别：{raw.get('rule_id')}")
            continue
        if rule_id not in rule_ids:
            warnings.append(f"删除项引用了不存在的规则 #{rule_id}，已忽略")
            continue
        reason = raw.get("reason")
        removals.append({"rule_id": rule_id, "reason": reason if isinstance(reason, str) else None})
    # 修改项：字段白名单与排班调整链路一致（normalize_adjust_fields）
    updates, update_warnings = normalize_conflict_updates(payload, plan, rules, shifts)
    warnings.extend(update_warnings)
    # 同一条规则既改又删时以删除为准，避免「改完就删」的无效操作
    removed_ids = {item["rule_id"] for item in removals}
    deduped: list[dict[str, Any]] = []
    for item in updates:
        if item["rule_id"] in removed_ids:
            warnings.append(
                f"规则 #{item['rule_id']} 同时在修改与删除中，已按删除处理"
            )
            continue
        deduped.append(item)
    return parsed.model_copy(update={"warnings": warnings}), removals, deduped


def test_connection(base_url: str, model: str, api_key: str) -> tuple[str, float]:
    started = time.perf_counter()
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=30, max_retries=0)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "只回复 OK"}],
        max_tokens=8,
        temperature=0,
    )
    elapsed = round(time.perf_counter() - started, 2)
    content = (response.choices[0].message.content or "").strip()
    return content or "已连接", elapsed
