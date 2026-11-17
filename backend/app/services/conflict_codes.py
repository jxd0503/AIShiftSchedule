"""冲突/提示码常量。

码值历史上是字符串字面量，这里集中命名以便调用方按语义引用：

- SOFT_RELAXED = "P011"：软约束被违反，但求解器已用 slack 放宽（历史码值，保持稳定）。
- RULE_INEFFECTIVE = "P012"：规则配置冗余/不会生效，仅作建议提示，不阻塞求解。
"""

SOFT_RELAXED = "P011"
RULE_INEFFECTIVE = "P012"
