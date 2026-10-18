"""fallback：规则词典解析题面示例、修改模式、合规拦截（最小用例集）。"""

from __future__ import annotations

from app.ai import fallback, prompts


async def test_example_query_rules() -> None:
    r = await fallback.parse("经营改善、估值合理、走势相对稳定")
    assert r.intent == "screen" and r.parser == "rule_fallback" and r.strategy is not None
    phrases = [c.phrase for c in r.clarifications]
    assert phrases[:3] == ["经营改善", "估值合理", "走势相对稳定"]
    assert r.clarifications[-1].question == fallback.UNIVERSE_QUESTION
    by_id = {c.id: c for c in r.strategy.conditions}
    for q in r.clarifications[:3]:
        default = next(o for o in q.options if o.id == q.default_option_id)
        assert all(by_id[c.id].metric == c.metric for c in default.conditions)


async def test_universe_keyword_and_modify() -> None:
    r = await fallback.parse("中证500中现金流好、负债低的股票")
    assert r.strategy.universe.code == "000905.SH"
    assert not any(c.question == fallback.UNIVERSE_QUESTION for c in r.clarifications)

    m = await fallback.parse(prompts.EXAMPLE_MODIFY_QUERY, prompts.EXAMPLE_MODIFY_BASE)
    assert m.intent == "modify" and any("放宽" in c for c in m.changes) and any("删除" in c for c in m.changes)


async def test_blocked_and_empty() -> None:
    r = await fallback.parse("明天哪只股票会涨？")
    assert r.intent == "prediction_request" and r.strategy is None and r.compliance.level == "block"
    e = await fallback.parse("   ")
    assert e.intent == "out_of_scope"


async def test_time_limited_doubling_blocked_without_draft() -> None:
    """#1：带时间限定的“翻倍”只拦截，不生成可执行的历史动量草案。"""
    for q in ["帮我找下周能翻倍的票", "下周最可能翻倍的票", "明年能翻倍的股票"]:
        r = await fallback.parse(q)
        assert r.intent == "prediction_request" and r.strategy is None, q
        assert r.compliance is not None and r.compliance.level == "block"
        assert not any("改写" in w for w in r.warnings) and not any("翻倍" in w for w in r.warnings)
    # 对照：不带时间限定的“会涨”仍可转化为只描述历史的草案
    r = await fallback.parse("会涨的股票")
    assert r.intent == "prediction_request" and r.strategy is not None
    assert any(c.metric == "return_60d" for c in r.strategy.conditions)


async def test_forward_phrase_warns_and_parses_as_history() -> None:
    """#11：“有望走强”给 warn 级合规提示，warnings 写明按历史走势处理。"""
    r = await fallback.parse("有望走强的低估值股")
    assert r.intent == "screen" and r.strategy is not None
    assert r.compliance is not None and r.compliance.level == "warn" and "有望走强" in r.compliance.matched
    assert any("已按历史走势描述处理，不代表未来" in w for w in r.warnings)


async def test_transition_word_and_forecast_metric_not_blocked() -> None:
    """#12：句首“接下来”不算时间限定；“预测市盈率”进入 unsupported（无一致预期数据），不按预测请求拦截。"""
    r = await fallback.parse("接下来看业绩增长快的公司")
    assert r.intent == "screen" and r.strategy is not None
    assert r.compliance is None or r.compliance.level != "block"
    assert {c.metric for c in r.strategy.conditions} & {"net_profit_yoy", "revenue_yoy"}

    r = await fallback.parse("预测市盈率低的股票")
    assert r.intent != "prediction_request"
    assert r.compliance is None or r.compliance.level != "block"
    hit = [u for u in r.unsupported if "预测市盈率" in u.phrase]
    assert hit and "一致预期" in hit[0].reason
    assert r.strategy is None or not any(c.metric == "pe_ttm" for c in r.strategy.conditions)

    # 修改模式：“放宽预测市盈率”不能拿市盈率TTM代替
    base = (await fallback.parse("经营改善、估值合理、走势相对稳定")).strategy
    r = await fallback.parse("放宽预测市盈率", base)
    assert not r.changes and any("预测市盈率" in u.phrase and "一致预期" in u.reason for u in r.unsupported)


async def test_modify_absent_category_warns() -> None:
    """#14：要删除/调整的类别在当前策略中不存在 → warning“当前策略中没有××条件，该部分未生效”。"""
    base = (await fallback.parse("经营改善、估值合理、走势相对稳定")).strategy
    assert not any(c.metric == "max_drawdown_250d" for c in base.conditions)
    vol = next(c for c in base.conditions if c.metric == "volatility_250d")

    r = await fallback.parse("估值放宽到行业70%分位，去掉回撤条件", base)
    assert r.intent == "modify" and any(c.startswith("放宽条件") for c in r.changes)
    assert not any(c.startswith("删除条件") for c in r.changes)
    assert "当前策略中没有回撤条件，该部分未生效。" in r.warnings
    assert any(c.id == vol.id for c in r.strategy.conditions)  # 没有误删波动率条件
    assert not any("回撤" in u.phrase for u in r.unsupported)

    # 同一分句里一部分存在、一部分不存在：存在的照常删除，不存在的提示
    r = await fallback.parse("去掉波动率和回撤", base)
    assert any("年化波动率" in c for c in r.changes) and "当前策略中没有回撤条件，该部分未生效。" in r.warnings
    # 只有不存在的类别：没有可执行修改，也要说明原因
    r = await fallback.parse("放宽股息条件", base)
    assert not r.changes and "当前策略中没有股息条件，该部分未生效。" in r.warnings
    r = await fallback.parse("毛利率改成30%", base)
    assert not r.changes and "当前策略中没有毛利条件，该部分未生效。" in r.warnings
    # 类别存在、或本次修改新增了该类条件：不提示
    r = await fallback.parse("去掉估值条件", base)
    assert r.changes and not any("当前策略中没有" in w for w in r.warnings)
    r = await fallback.parse("改成高毛利的", base)
    assert any(c.startswith("新增条件") for c in r.changes) and not any("当前策略中没有" in w for w in r.warnings)


def test_guard_negative_caps_adds_zero_lower_bound() -> None:
    """“PE < 30” 会让亏损股（负 PE）通过：AI/规则生成的上限条件自动改为 0~30，并给出说明。"""
    from app.ai.fallback import guard_negative_caps
    from app.schemas import Condition, Strategy

    s = Strategy(conditions=[Condition(id="c1", metric="pe_ttm", op="lt", value=30, origin="ai")])
    msgs = guard_negative_caps(s, [])
    c = s.conditions[0]
    assert (c.op, c.value, c.value2) == ("between", 0.0, 30.0) and msgs and "介于 0 ~ 上限" in msgs[0]
    # 已有正下限、或用户手动设置的条件：不改
    s2 = Strategy(conditions=[Condition(id="c1", metric="pe_ttm", op="lt", value=30, origin="ai"),
                              Condition(id="c2", metric="pe_ttm", op="gt", value=0, origin="ai")])
    assert guard_negative_caps(s2, []) == [] and s2.conditions[0].op == "lt"
    s3 = Strategy(conditions=[Condition(id="c1", metric="pe_ttm", op="lt", value=30, origin="user")])
    assert guard_negative_caps(s3, []) == [] and s3.conditions[0].op == "lt"
