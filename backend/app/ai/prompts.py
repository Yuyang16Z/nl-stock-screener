"""提示词（DESIGN §6）：自然语言解析、运行摘要、个股解释。

指标目录精简表、口径参考（LEXICON 摘要）与少样本都由 catalog 生成，
保证 LLM 与规则词典、确定性引擎使用同一套指标 id / 算子 / 默认阈值。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .. import catalog
from ..engine.conditions import describe, fmt_num, normalize
from ..schemas import Condition, MetricDef, Strategy, Universe

_CATEGORY_LABEL: dict[str, str] = {
    "valuation": "估值",
    "growth": "成长",
    "profitability": "盈利",
    "quality": "盈利质量",
    "solvency": "偿债",
    "price": "价格/风险",
    "liquidity": "流动性",
    "basic": "基础",
    "dividend": "分红",
    "size": "规模",
}
_DIRECTION = {True: "越高越好", False: "越低越好", None: "中性"}


def _ops_text(m: MetricDef) -> str:
    ops = list(m.allowed_ops)
    if ops == catalog.NUMERIC_OPS + catalog.RANK_OPS:
        return "数值/分位"
    if ops == catalog.NUMERIC_OPS:
        return "数值"
    if ops == catalog.BOOL_OPS:
        return "布尔"
    return ",".join(ops)


def catalog_table(metrics: Mapping[str, MetricDef] = catalog.METRICS) -> str:
    """指标目录精简表（每行一个指标）。"""
    lines = ["id | 名称 | 类别 | 单位 | 算子 | 分位口径 | 方向 | 合理范围 | 别名 | 口径"]
    for m in metrics.values():
        rng = f"{fmt_num(m.plausible_range[0])}~{fmt_num(m.plausible_range[1])}" if m.plausible_range else "-"
        notes = m.description
        if m.positive_only_for_rank:
            notes += "（分位只对正值排名）"
        if m.estimate:
            notes += "（估算）"
        lines.append(
            " | ".join(
                [
                    m.id,
                    m.name,
                    _CATEGORY_LABEL.get(m.category, m.category),
                    m.unit or "无",
                    _ops_text(m),
                    ",".join(m.rank_scopes) or "-",
                    _DIRECTION[m.higher_is_better],
                    rng,
                    "、".join(m.aliases) or "-",
                    notes,
                ]
            )
        )
    return "\n".join(lines)


CATALOG_TABLE = catalog_table()


def _cond_brief(d: Mapping[str, Any]) -> str:
    op = d["op"]
    if op in catalog.BOOL_OPS:
        return f"{d['metric']} {op}"
    if op == "between":
        return f"{d['metric']} between {fmt_num(d.get('value'))}~{fmt_num(d.get('value2'))}"
    if op in catalog.RANK_OPS:
        return f"{d['metric']} {op} {fmt_num(d.get('value'))} [{d.get('rank_scope')}]"
    return f"{d['metric']} {op} {fmt_num(d.get('value'))}"


def lexicon_brief(lexicon: Sequence[Mapping[str, Any]] = catalog.LEXICON) -> str:
    """规则词典的口径摘要：触发说法 → 含义 → 默认解释与备选（与 fallback 规则解析完全一致）。"""
    lines: list[str] = []
    for e in lexicon:
        pats = " / ".join(p.replace(r"\s*", "") for p in e["patterns"])
        opts = e["options"]
        default = next(o for o in opts if o["id"] == e["default"])
        alts = [o for o in opts if o is not default]

        def opt_text(o: Mapping[str, Any]) -> str:
            return f"「{o['label']}」= " + "；".join(_cond_brief(c) for c in o["conditions"])

        line = f"- [{pats}] {e['meaning']}。默认 {opt_text(default)}"
        if alts:
            line += "｜备选 " + "｜".join(opt_text(o) for o in alts)
        lines.append(line)
    return "\n".join(lines)


def _universe_presets() -> str:
    items = []
    for p in catalog.UNIVERSE_PRESETS:
        if p.kind == "all_a":
            items.append("全部A股：kind=all_a")
        else:
            items.append(f"{p.name}：kind={p.kind}，code={p.code}")
    return "；".join(items)


def _unsupported_hints() -> str:
    lines = []
    for pat, reason, alt in catalog.UNSUPPORTED_HINTS:
        lines.append(f"- [{pat}] {reason}" + (f"；可替代：{alt}" if alt else ""))
    return "\n".join(lines)


# ───────────────────────────── 当前策略的文本表示 ─────────────────────────────


def universe_text(u: Universe) -> str:
    if u.kind == "all_a":
        return "全部A股（kind=all_a）"
    if u.kind == "custom":
        return f"自定义股票池（{len(u.codes)} 只）"
    name = u.name or u.query or "未命名"
    return f"{name}（kind={u.kind}，code={u.code or '未解析'}）"


def _cond_line(c: Condition) -> str:
    spec = _cond_brief(
        {"metric": c.metric, "op": c.op, "value": c.value, "value2": c.value2, "rank_scope": c.rank_scope}
    )
    flags = ["硬条件" if c.hard else f"软条件（权重 {fmt_num(c.weight)}）"]
    if not c.enabled:
        flags.append("已停用")
    phrase = f"来源短语：{c.source_phrase}" if c.source_phrase else "来源：用户添加"
    return f"- {c.id} | {spec} | {c.label or describe(c)} | {'，'.join(flags)} | {phrase}"


def strategy_brief(strategy: Strategy) -> str:
    """修改模式下给 LLM 看的当前策略：条件 id、机器可读规格与中文描述。"""
    s = normalize(strategy)
    lines = [f"股票池：{universe_text(s.universe)}", "条件："]
    lines += [_cond_line(c) for c in s.conditions] or ["（无条件）"]
    return "\n".join(lines)


# ───────────────────────────── 少样本 ─────────────────────────────


def _lex(key: str) -> Mapping[str, Any]:
    return next(e for e in catalog.LEXICON if e["key"] == key)


def _cond_json(d: Mapping[str, Any], *, with_rationale: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"metric": d["metric"], "op": d["op"]}
    if d["op"] not in catalog.BOOL_OPS:
        out["value"] = d.get("value")
    if d["op"] == "between":
        out["value2"] = d.get("value2")
    if d["op"] in catalog.RANK_OPS:
        out["rank_scope"] = d.get("rank_scope")
    out["hard"] = d.get("hard", True)
    if with_rationale and d.get("rationale"):
        out["rationale"] = d["rationale"]
    return out


def _example_phrase(key: str, phrase: str) -> dict[str, Any]:
    e = _lex(key)
    opts = e["options"]
    default = next(o for o in opts if o["id"] == e["default"])
    alts: list[dict[str, Any]] = []
    for o in opts:
        if o is default:
            continue
        alt: dict[str, Any] = {"label": o["label"]}
        if o.get("description"):
            alt["description"] = o["description"]
        alt["conditions"] = [_cond_json(c, with_rationale=False) for c in o["conditions"]]
        alts.append(alt)
    return {
        "phrase": phrase,
        "meaning": e["meaning"],
        "confidence": "medium",
        "label": default["label"],
        "question": e["question"],
        "conditions": [_cond_json(c, with_rationale=True) for c in default["conditions"]],
        "alternatives": alts,
    }


def _example_base_strategy() -> Strategy:
    def mk(d: Mapping[str, Any], phrase: str) -> Condition:
        return Condition(
            metric=d["metric"], op=d["op"], value=d.get("value"), value2=d.get("value2"),
            rank_scope=d.get("rank_scope"), origin="ai", source_phrase=phrase, rationale=d.get("rationale"),
        )

    improve = next(o for o in _lex("operating_improve")["options"] if o["id"] == "b")["conditions"]
    fair = next(o for o in _lex("fair_valuation")["options"] if o["id"] == "a")["conditions"]
    stable = next(o for o in _lex("stable_trend")["options"] if o["id"] == "b")["conditions"]
    conds = [mk(d, "经营改善") for d in improve] + [mk(d, "估值合理") for d in fair] + [mk(d, "走势相对稳定") for d in stable]
    conds += [c.model_copy(deep=True) for c in catalog.DEFAULT_CONDITIONS]
    return normalize(Strategy(query="经营改善、估值合理、走势相对稳定", conditions=conds))


def _example_modify_output(base: Strategy) -> dict[str, Any]:
    rank_pe = next(c for c in base.conditions if c.metric == "pe_ttm" and c.op == "pct_rank_lte")
    drawdown = next(c for c in base.conditions if c.metric == "max_drawdown_250d")
    return {
        "intent": "modify",
        "universe": None,
        "phrases": [],
        "unsupported": [],
        "conflicts": [],
        "patch": {"add": [], "remove": [drawdown.id], "update": [{"id": rank_pe.id, "value": 70}], "universe": None},
    }


EXAMPLE_QUERY = "经营改善、估值合理、走势相对稳定"
EXAMPLE_OUTPUT: dict[str, Any] = {
    "intent": "screen",
    "universe": None,
    "phrases": [
        _example_phrase("operating_improve", "经营改善"),
        _example_phrase("fair_valuation", "估值合理"),
        _example_phrase("stable_trend", "走势相对稳定"),
    ],
    "unsupported": [],
    "conflicts": [],
    "patch": None,
}
EXAMPLE_MODIFY_BASE = _example_base_strategy()
EXAMPLE_MODIFY_QUERY = "估值放宽到行业70%分位，去掉回撤条件"
EXAMPLE_MODIFY_OUTPUT = _example_modify_output(EXAMPLE_MODIFY_BASE)


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(", ", ": "))


_OUTPUT_SCHEMA = """{
 "intent": "screen | modify | out_of_scope | prediction_request | advice_request",
 "universe": null 或 {"kind": "index | industry | concept | all_a", "code": "仅常用指数填写，如 000300.SH", "name": "名称", "query": "行业/概念/板块名称，如 白酒"},
 "phrases": [
  {
   "phrase": "用户原句中的连续片段（原文摘录）",
   "meaning": "你如何理解这个说法（一句话）",
   "confidence": "high | medium | low",
   "label": "首选解释的简短标签",
   "question": "向用户确认口径的问题（模糊词必填）",
   "conditions": [{"metric": "指标 id", "op": "算子 id", "value": 数值或 null, "value2": "between 的上界，否则 null", "rank_scope": "分位算子必填：universe | industry | market，否则 null", "hard": true, "rationale": "为什么这样解释"}],
   "alternatives": [{"label": "备选解释标签", "description": "与首选的区别", "conditions": [同上结构]}]
  }
 ],
 "unsupported": [{"phrase": "无法映射为数据条件的原文片段", "reason": "原因", "alternative": "可替代的说法或做法"}],
 "conflicts": [{"condition_refs": ["p1.c1", "p2.c1"], "message": "语义上互相牵制或过严的原因", "suggestion": "可如何调整"}],
 "patch": null 或 {"add": [条件结构 + "source_phrase"], "remove": ["条件 id"], "update": [{"id": "条件 id", "value": 新值, "value2": 新上界, "op": 新算子, "rank_scope": 新口径, "enabled": true/false, "hard": true/false}], "universe": null 或 股票池结构}
}"""


PARSE_SYSTEM = f"""你是「ScreenLens 自然语言选股」的条件解析助手。你的唯一任务：把用户的自然语言描述翻译成“指标目录”内的候选筛选条件草案，供用户检查、修改后交给确定性引擎执行。你看不到任何个股数据，也不决定哪只股票入选。

## 规则
1. 只能使用【指标目录】中的指标 id 与下列算子 id。分位算子必须给 rank_scope，且只能取该指标“分位口径”列允许的值（行业/全A分位只对估值、行情类指标开放）。目录里没有的指标不要编造。
2. 阈值是“建议默认值”，不是唯一答案。模糊说法（如“估值合理”“走势稳定”“高成长”“盈利能力强”）必须给 1~3 个 alternatives（不同口径或不同松紧），confidence 填 medium 或 low，并给出 question；只有含义明确且用户给了具体数字的说法（如“PE 低于 20 倍”）才可用 high、不给 alternatives。优先采用【口径参考】中的默认解释与备选，保持与规则词典一致。
3. 不得输出任何个股代码或名称；不得预测涨跌、目标价或未来表现；不得给出买入、卖出、持有、仓位等建议；不得承诺收益。用户要求预测或荐股时，intent 填 prediction_request 或 advice_request，phrases 留空。带“下周/明年/未来/后市”等时间限定的涨跌、翻倍预测一律按 prediction_request 处理；“有望走强、即将突破、大概率上涨”等前瞻说法只能按历史走势描述解释（如近60日涨跌幅为正），meaning 写明“仅描述历史区间，不代表未来”。
4. 无法映射为数据条件的说法放入 unsupported，并给出可替代的说法或做法；与选股无关的输入 intent 填 out_of_scope。“预测市盈率、一致预期EPS/净利润”等预测类指标需要分析师一致预期数据，指标目录中没有：放入 unsupported，不要用 pe_ttm 等历史口径指标代替。
5. 数值约定：百分比类指标用百分数值（20 表示 20%）；金额单位为亿元；分位 0=组内最小、100=组内最大，“越低越好”的指标（如 PE、波动率）偏低用 pct_rank_lte，“越高越好”的指标偏高用 pct_rank_gte。
6. “ROE ≥ 15%”这类年度口径阈值用 roe_annualized；“同比提升/改善”用 *_change 指标；价格类指标只描述历史区间走势。
7. 股票池：只在用户明确提到时填写 universe。常用指数用 kind=index 并给出 code；全市场用 kind=all_a；行业/概念/板块只填 kind=industry 或 concept 与 query（如 {{"kind": "industry", "query": "白酒"}}），不要编造板块代码。没提到股票池时 universe 填 null（系统默认沪深300并请用户确认）。
8. 不必输出“剔除 ST、剔除上市不满一年”等默认风控条件，系统会自动追加；用户明确提到时再输出。
9. phrase 必须是用户原句中的连续片段；每个短语对应一组条件，首选解释的每个条件都写 rationale。
10. conflicts 只写语义上互相牵制或明显过严的组合；condition_refs 用 "p短语序号.c条件序号"（从 1 开始，如 "p1.c2"）引用本次输出的首选条件；修改模式下可直接用已有条件 id（如 "c4"），新增条件用 "a序号"（如 "a1"）。
11. 修改模式（用户消息中给出【当前策略】时）：intent 填 modify，phrases 留空，只输出 patch：add=新增条件（可带 source_phrase），remove=要删除的条件 id，update=要修改的条件 id 及改动字段，universe=要更换的股票池（不换填 null）。没提到的条件保持不变；“放宽/收紧”要朝对应方向调整阈值。要删除或调整的条件在当前策略中不存在时，不要删除或修改其他条件来代替（系统会提示该部分未生效）。
12. 只输出一个 JSON 对象，不要输出 Markdown 或任何解释文字。

## 算子
gt(>)、gte(≥)、lt(<)、lte(≤)、between(介于 value~value2)、pct_rank_lte(参考组内分位 ≤ value)、pct_rank_gte(参考组内分位 ≥ value)、is_true(是)、is_false(否)。
分位参考组 rank_scope：universe=股票池内，industry=所属行业内，market=全A股内。指标目录“算子”列：数值=gt/gte/lt/lte/between，分位=pct_rank_lte/pct_rank_gte，布尔=is_true/is_false。

## 常用股票池
{_universe_presets()}

## 口径参考（与规则词典一致）
{lexicon_brief()}

## 暂不支持的常见说法
{_unsupported_hints()}

## 输出 JSON 结构
{_OUTPUT_SCHEMA}

## 示例 1
用户输入：{EXAMPLE_QUERY}
输出：
{_dump(EXAMPLE_OUTPUT)}

## 示例 2（修改模式）
【当前策略】
{strategy_brief(EXAMPLE_MODIFY_BASE)}
用户输入：{EXAMPLE_MODIFY_QUERY}
输出：
{_dump(EXAMPLE_MODIFY_OUTPUT)}
"""


def build_parse_user(query: str, base_strategy: Strategy | None = None, catalog_brief: str | None = None) -> str:
    """解析请求的用户消息：指标目录精简表（静态，利于前缀缓存）→ 当前策略（修改模式）→ 用户输入。"""
    parts = ["【指标目录】", catalog_brief or CATALOG_TABLE, ""]
    if base_strategy is not None:
        parts += ["【当前策略】", strategy_brief(base_strategy), "", "【任务】按用户的修改要求输出 patch（intent=modify）。"]
    else:
        parts += ["【任务】把用户输入解析为条件草案（intent=screen）。"]
    parts += ["【用户输入】", query.strip()]
    return "\n".join(parts)


# ───────────────────────────── 解释 ─────────────────────────────

_EXPLAIN_RULES = """规则：
1. 只能使用证据中出现的数字，写法与证据一致（含正负号、小数位与单位）；不要自行计算差值、比例或换算单位；不要给句子编号，不要使用证据之外的数字。
2. kind 取值：直接引用证据中的数值或判定结果 = fact；基于分位或阈值的定性判断（如“在行业中处于较低水平”）= inference；数据缺失、不适用、过期或未取数 = uncertain（必须如实说明无法判定，绝不能写成满足或正常）；口径与历史局限提示 = caveat。
3. refs 填写该句涉及的条件 id（如 "c1"），没有则为空数组。
4. 不得预测涨跌或未来表现，不得给出买入、卖出、持有、仓位等建议，不得承诺收益，不得使用“推荐”“目标价”“必涨”“稳赚”等措辞；不要提及证据之外的公司、股票或数据。
5. 最后一句为 caveat：说明结果只基于所列数据条件，不构成投资建议。
6. 只输出一个 JSON 对象：{"sentences": [{"text": "……", "kind": "fact | inference | uncertain | caveat", "refs": ["c1"]}]}。"""

STOCK_SYSTEM = (
    "你是「ScreenLens」的个股筛选结果解释助手。你会收到确定性引擎对一只股票逐条件判定的结构化证据（JSON），"
    "请用简洁的中文写 3~8 句解释：先说明归入哪个分组及原因，再逐条说明关键条件的数值与判定，最后提示口径局限。"
    "引用某条件数值的 fact 句要写明该条件证据中的 period（报告期或数据时点）与 source（来源接口·字段），"
    "格式如“<指标>为 <value>（<period>；来源：<接口>·<字段>），判定通过。”，尖括号处照抄证据中的对应内容。\n"
    + _EXPLAIN_RULES
)

SUMMARY_SYSTEM = (
    "你是「ScreenLens」的筛选运行摘要助手。你会收到一次筛选运行的结构化事实（JSON），"
    "请用简洁的中文写 3~7 句摘要：股票池规模与入选/排除/数据缺失数量、影响最大的条件（去掉后入选数变化最大者）、"
    "数据缺失情况（只按条件统计中的缺失、不适用计数描述）、数据时点。不要列举或评价个股。\n"
    "运行事实中不含数据集（数据源/接口）的获取状态：不要描述任何数据集或接口的成功、失败、部分失败、跳过等状态，"
    "也不要推断数据缺失的原因；系统会根据运行记录另行追加数据集状态说明。"
    "不要直接写出 JSON 字段名（如 sole_blocker、included_if_removed），用中文说明其含义。\n"
    + _EXPLAIN_RULES
)


def build_stock_user(facts: Mapping[str, Any]) -> str:
    return "【个股证据】\n" + _dump(facts)


def build_summary_user(facts: Mapping[str, Any]) -> str:
    return "【运行事实】\n" + _dump(facts)
