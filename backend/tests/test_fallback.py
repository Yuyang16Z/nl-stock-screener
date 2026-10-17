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
