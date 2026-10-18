"""parse_query：规则降级、LLM 正常输出、严格校验、异常降级、修改模式、合规拦截（假 LLM，不访问网络）。"""

from __future__ import annotations

import copy
from typing import Any

from app.ai import fallback, prompts
from app.ai.llm import LLMError
from app.ai.parser import parse_query
from app.schemas import ParseRequest, UniverseOption

EXAMPLE = "经营改善、估值合理、走势相对稳定"


class FakeLLM:
    enabled = True
    model = "fake-model"

    def __init__(self, response: Any = None, exc: Exception | None = None) -> None:
        self.response = response
        self.exc = exc
        self.calls: list[tuple[str, str]] = []

    async def chat_json(self, system: str, user: str, **kw: Any) -> dict:
        self.calls.append((system, user))
        if self.exc is not None:
            raise self.exc
        return copy.deepcopy(self.response)


class FakeSearcher:
    def __init__(self, options: list[UniverseOption]) -> None:
        self.options = options
        self.queries: list[str] = []

    async def search_universes(self, q: str) -> list[UniverseOption]:
        self.queries.append(q)
        return list(self.options)


def _c(metric: str, op: str, value: float | None = None, scope: str | None = None, **kw: Any) -> dict:
    d = {"metric": metric, "op": op, "value": value, "rank_scope": scope, "hard": True}
    d.update(kw)
    return d


def _screen_output() -> dict:
    return {
        "intent": "screen",
        "universe": None,
        "phrases": [
            {
                "phrase": "估值合理", "meaning": "估值处于行业中低水平", "confidence": "medium", "label": "行业分位≤50%",
                "question": "“估值合理”用哪种口径？",
                "conditions": [_c("pe_ttm", "gt", 0, rationale="剔除亏损"), _c("pe_ttm", "pct_rank_lte", 50, "industry")],
                "alternatives": [{"label": "PE 低于 25 倍", "conditions": [_c("pe_ttm", "between", 0, value2=25)]}],
            },
            {
                "phrase": "ROE 大于 15%", "meaning": "年化 ROE > 15%", "confidence": "high",
                "conditions": [_c("roe_annualized", "gt", 15)], "alternatives": [],
            },
        ],
        "unsupported": [{"phrase": "政策受益", "reason": "资讯判断", "alternative": "用概念板块作股票池"}],
        "conflicts": [
            {"condition_refs": ["p1.c2", "p2.c1"], "message": "低估值与高盈利同时要求可能较严"},
            {"condition_refs": ["p9.c9"], "message": "引用不存在的条件"},
        ],
        "patch": None,
    }


async def test_example_rule_fallback_without_llm() -> None:
    r = await parse_query(ParseRequest(query=EXAMPLE), None, None)
    assert r.parser == "rule_fallback" and r.llm_error
    assert r.intent == "screen" and r.strategy is not None
    phrases = {c.phrase for c in r.clarifications}
    assert {"经营改善", "估值合理", "走势相对稳定"} <= phrases
    uq = next(c for c in r.clarifications if c.question == "股票池选哪个？")
    assert [o.label for o in uq.options] == ["沪深300", "中证500", "中证1000", "全部A股"]
    ids = {c.id for c in r.strategy.conditions}
    assert {"sys_st", "sys_new"} <= ids and len(ids) >= 5


async def test_llm_json_to_conditions_and_clarifications() -> None:
    llm = FakeLLM(_screen_output())
    r = await parse_query(ParseRequest(query="估值合理、ROE 大于 15%，最好政策受益"), llm, None)
    assert r.parser == "llm" and r.llm_model == "fake-model" and r.llm_error is None
    s = r.strategy
    assert s is not None and s.universe.code == "000300.SH"
    ai = [c for c in s.conditions if c.origin == "ai"]
    assert [c.id for c in ai] == ["c1", "c2", "c3"]
    assert {c.id for c in s.conditions} >= {"sys_st", "sys_new"}
    # 只有 medium 短语生成澄清；默认项条件与策略一致（同 id），其他选项 id 形如 q1_b_1
    q1 = next(c for c in r.clarifications if c.phrase == "估值合理")
    default = next(o for o in q1.options if o.id == q1.default_option_id)
    phrase_conds = [c for c in s.conditions if c.source_phrase == "估值合理"]
    assert [c.model_dump() for c in default.conditions] == [c.model_dump() for c in phrase_conds]
    other = next(o for o in q1.options if o.id != q1.default_option_id)
    assert other.conditions[0].id == "q1_b_1"
    assert not any(c.phrase == "ROE 大于 15%" for c in r.clarifications)
    assert any(c.question == "股票池选哪个？" for c in r.clarifications)
    ai_conf = [c for c in r.conflicts if c.source == "ai"]
    assert len(ai_conf) == 1 and ai_conf[0].condition_ids == ["c2", "c3"]
    assert r.unsupported and r.unsupported[0].phrase == "政策受益"


async def test_unknown_metric_and_invalid_conditions_dropped() -> None:
    out = _screen_output()
    out["phrases"][1]["conditions"] = [
        _c("magic_score", "gt", 1),  # 未知指标
        _c("roe_annualized", "approx", 15),  # 非法算子
        _c("roe_annualized", "gt", None),  # 缺阈值
        _c("revenue_yoy", "pct_rank_gte", 80, "industry"),  # 口径不允许
        _c("roe_annualized", "gt", 15),
    ]
    r = await parse_query(ParseRequest(query="估值合理、ROE 大于 15%"), FakeLLM(out), None)
    assert r.parser == "llm"
    metrics = [c.metric for c in r.strategy.conditions]
    assert "magic_score" not in metrics and "revenue_yoy" not in metrics
    assert metrics.count("roe_annualized") == 1
    dropped = [w for w in r.warnings if w.startswith("已丢弃")]
    assert len(dropped) == 4


async def test_all_conditions_invalid_falls_back() -> None:
    out = {"intent": "screen", "phrases": [{"phrase": "估值合理", "conditions": [_c("magic", "gt", 1)]}]}
    r = await parse_query(ParseRequest(query=EXAMPLE), FakeLLM(out), None)
    assert r.parser == "rule_fallback" and "不合法" in (r.llm_error or "")
    assert r.strategy is not None and r.clarifications


async def test_llm_exception_falls_back() -> None:
    for exc in (LLMError("timeout", "AI 接口超时（超过 40 秒）"), RuntimeError("boom")):
        llm = FakeLLM(exc=exc)
        r = await parse_query(ParseRequest(query=EXAMPLE), llm, None)
        assert r.parser == "rule_fallback" and r.llm_error and r.llm_model is None
        assert r.strategy is not None and len(r.clarifications) >= 4
        assert llm.calls


async def test_invalid_output_shape_falls_back() -> None:
    r = await parse_query(ParseRequest(query=EXAMPLE), FakeLLM({"intent": "screen", "phrases": "oops"}), None)
    assert r.parser == "rule_fallback" and "phrases" in (r.llm_error or "")


async def test_universe_query_via_searcher() -> None:
    out = _screen_output()
    out["universe"] = {"kind": "industry", "query": "白酒"}
    one = FakeSearcher([UniverseOption(kind="industry", code="881273.TI", name="白酒", group="同花顺行业", size_hint=20)])
    r = await parse_query(ParseRequest(query="白酒行业里估值合理"), FakeLLM(out), one)
    assert r.strategy.universe.code == "881273.TI" and one.queries == ["白酒"]
    assert not any(c.question == "股票池选哪个？" for c in r.clarifications)

    two = FakeSearcher([
        UniverseOption(kind="industry", code="881273.TI", name="白酒", group="同花顺行业"),
        UniverseOption(kind="concept", code="885xxx.TI", name="白酒概念", group="同花顺概念"),
    ])
    r = await parse_query(ParseRequest(query="白酒里估值合理"), FakeLLM({**out, "universe": {"kind": None, "query": "白酒"}}), two)
    assert any(c.phrase.startswith("股票池") and len(c.options) == 2 for c in r.clarifications)

    r = await parse_query(ParseRequest(query="火星行业里估值合理"), FakeLLM({**out, "universe": {"kind": "industry", "query": "火星"}}), FakeSearcher([]))
    assert any("火星" in u.reason for u in r.unsupported)
    assert r.strategy.universe.code == "000300.SH"


async def test_modify_mode_patch_and_changes() -> None:
    base = (await fallback.parse(EXAMPLE)).strategy
    rank = next(c for c in base.conditions if c.metric == "pe_ttm" and c.op == "pct_rank_lte")
    vol = next(c for c in base.conditions if c.metric == "volatility_250d")
    out = {
        "intent": "modify",
        "phrases": [],
        "patch": {
            "add": [_c("dividend_yield_ttm", "gte", 3, source_phrase="股息率不低于3%"), _c("magic", "gt", 1)],
            "remove": [vol.id, "c99"],
            "update": [{"id": rank.id, "value": 70}],
            "universe": {"kind": "index", "code": "000905.SH", "name": "中证500"},
        },
        "conflicts": [{"condition_refs": ["a1", rank.id], "message": "股息与估值条件同时收紧"}],
    }
    llm = FakeLLM(out)
    r = await parse_query(ParseRequest(query="估值放宽到行业70%分位，去掉波动率，加上股息率不低于3%，只看中证500", base_strategy=base), llm, None)
    assert r.intent == "modify" and r.parser == "llm"
    assert "【当前策略】" in llm.calls[0][1]
    s = r.strategy
    assert vol.id not in {c.id for c in s.conditions}
    assert next(c for c in s.conditions if c.id == rank.id).value == 70
    added = next(c for c in s.conditions if c.metric == "dividend_yield_ttm")
    assert added.origin == "ai" and added.id not in {c.id for c in base.conditions}
    assert s.universe.code == "000905.SH"
    text = "\n".join(r.changes)
    assert "放宽条件" in text and "删除条件" in text and "新增条件" in text and "股票池" in text
    assert any("c99" in w for w in r.warnings) and any(w.startswith("已丢弃") for w in r.warnings)
    assert any(c.source == "ai" and added.id in c.condition_ids for c in r.conflicts)
    # 修改不改动入参
    assert any(c.id == vol.id for c in base.conditions)


async def test_modify_mode_without_patch_falls_back_to_rules() -> None:
    base = prompts.EXAMPLE_MODIFY_BASE
    r = await parse_query(ParseRequest(query=prompts.EXAMPLE_MODIFY_QUERY, base_strategy=base), FakeLLM({"intent": "modify"}), None)
    assert r.parser == "rule_fallback" and r.intent == "modify" and r.changes


async def test_compliance_block_prediction() -> None:
    llm = FakeLLM(_screen_output())
    r = await parse_query(ParseRequest(query="明天哪只股票会涨？"), llm, None)
    assert r.intent == "prediction_request"
    assert r.compliance is not None and r.compliance.level == "block" and r.compliance.category == "prediction"
    assert r.strategy is None and not llm.calls

    r = await parse_query(ParseRequest(query="推荐几只有潜力的股票"), llm, None)
    assert r.intent in ("advice_request", "prediction_request") and r.compliance.level == "block"
    assert not llm.calls


async def test_llm_reported_advice_intent() -> None:
    r = await parse_query(ParseRequest(query="帮我挑几只票"), FakeLLM({"intent": "advice_request", "phrases": []}), None)
    assert r.intent == "advice_request" and r.compliance.level == "block" and r.strategy is None


async def test_time_limited_doubling_blocked_before_llm() -> None:
    """#1：API 走的 parse_query 同样只拦截，不调用 LLM，也不返回可执行草案。"""
    for q in ["帮我找下周能翻倍的票", "下周最可能翻倍的票", "明年能翻倍的股票"]:
        llm = FakeLLM(_screen_output())
        r = await parse_query(ParseRequest(query=q), llm, None)
        assert r.intent == "prediction_request" and r.strategy is None, q
        assert r.compliance is not None and r.compliance.level == "block"
        assert not llm.calls


def _forward_output() -> dict:
    return {
        "intent": "screen",
        "universe": None,
        "phrases": [
            {"phrase": "有望走强", "meaning": "近期走势偏强（仅描述历史区间，不代表未来）", "confidence": "medium",
             "conditions": [_c("return_60d", "gt", 0)], "alternatives": []},
            {"phrase": "低估值", "meaning": "估值处于行业较低水平", "confidence": "medium",
             "conditions": [_c("pe_ttm", "gt", 0), _c("pe_ttm", "pct_rank_lte", 30, "industry")], "alternatives": []},
        ],
        "unsupported": [], "conflicts": [], "patch": None,
    }


async def test_forward_phrase_warn_on_llm_and_rule_paths() -> None:
    """#11：“有望走强”在 LLM 路径与规则降级路径都给 warn，并写明已按历史走势描述处理。"""
    llm = FakeLLM(_forward_output())
    r = await parse_query(ParseRequest(query="有望走强的低估值股"), llm, None)
    assert r.parser == "llm" and llm.calls and r.intent == "screen" and r.strategy is not None
    assert r.compliance is not None and r.compliance.level == "warn"
    assert any("已按历史走势描述处理，不代表未来" in w for w in r.warnings)

    r = await parse_query(ParseRequest(query="有望走强的低估值股"), None, None)
    assert r.parser == "rule_fallback" and r.compliance is not None and r.compliance.level == "warn"
    assert any("已按历史走势描述处理，不代表未来" in w for w in r.warnings)


async def test_transition_word_reaches_llm() -> None:
    """#12：“接下来看业绩增长快的公司”不被合规前置拦截，正常交给 LLM 解析。"""
    out = {"intent": "screen", "universe": None, "phrases": [
        {"phrase": "业绩增长快", "meaning": "最新报告期归母净利润同比增长", "confidence": "medium",
         "conditions": [_c("net_profit_yoy", "gt", 20)], "alternatives": []},
    ], "unsupported": [], "conflicts": [], "patch": None}
    llm = FakeLLM(out)
    r = await parse_query(ParseRequest(query="接下来看业绩增长快的公司"), llm, None)
    assert llm.calls and r.parser == "llm" and r.intent == "screen"
    assert r.compliance is None or r.compliance.level != "block"
    assert any(c.metric == "net_profit_yoy" for c in r.strategy.conditions)


async def test_forecast_metric_goes_to_unsupported() -> None:
    """#12：“预测市盈率”不拦截；LLM 若用 pe_ttm 代替，丢弃该短语并放入 unsupported（无一致预期数据）。"""
    out = {"intent": "screen", "universe": None, "phrases": [
        {"phrase": "预测市盈率低", "meaning": "市盈率较低", "confidence": "medium",
         "conditions": [_c("pe_ttm", "pct_rank_lte", 30, "industry")], "alternatives": []},
    ], "unsupported": [], "conflicts": [], "patch": None}
    llm = FakeLLM(out)
    r = await parse_query(ParseRequest(query="预测市盈率低的股票"), llm, None)
    assert llm.calls and r.parser == "llm" and r.intent != "prediction_request"
    assert r.strategy is None or not any(c.metric == "pe_ttm" for c in r.strategy.conditions)
    hit = [u for u in r.unsupported if "预测市盈率" in u.phrase]
    assert hit and "一致预期" in hit[0].reason
    assert any("预测/一致预期" in w for w in r.warnings)

    r = await parse_query(ParseRequest(query="预测市盈率低的股票"), None, None)
    assert r.parser == "rule_fallback" and r.intent != "prediction_request"
    assert any("预测市盈率" in u.phrase and "一致预期" in u.reason for u in r.unsupported)

    # LLM 把指标名误判为预测请求：不整句拦截，改用规则解析（unsupported 写明无一致预期数据）
    llm = FakeLLM({"intent": "prediction_request", "phrases": []})
    r = await parse_query(ParseRequest(query="预测市盈率低的股票"), llm, None)
    assert llm.calls and r.parser == "rule_fallback" and r.intent != "prediction_request"
    assert r.compliance is None and "一致预期" in (r.llm_error or "")
    assert any("预测市盈率" in u.phrase and "一致预期" in u.reason for u in r.unsupported)
    # 普通的预测请求仍按 AI 判断拦截
    r = await parse_query(ParseRequest(query="帮我挑几只票"), FakeLLM({"intent": "prediction_request", "phrases": []}), None)
    assert r.intent == "prediction_request" and r.compliance is not None and r.compliance.level == "block"


async def test_modify_absent_category_warning_on_llm_path() -> None:
    """#14：LLM 修改路径同样提示“当前策略中没有××条件，该部分未生效”。"""
    base = (await fallback.parse(EXAMPLE)).strategy
    rank = next(c for c in base.conditions if c.metric == "pe_ttm" and c.op == "pct_rank_lte")
    vol = next(c for c in base.conditions if c.metric == "volatility_250d")
    out = {"intent": "modify", "phrases": [], "patch": {"add": [], "remove": [], "update": [{"id": rank.id, "value": 70}], "universe": None}}
    r = await parse_query(ParseRequest(query="估值放宽到行业70%分位，去掉回撤条件", base_strategy=base), FakeLLM(out), None)
    assert r.parser == "llm" and any(c.startswith("放宽条件") for c in r.changes)
    assert "当前策略中没有回撤条件，该部分未生效。" in r.warnings
    assert any(c.id == vol.id for c in r.strategy.conditions)

    # LLM 新增了该类条件：不提示
    out = {"intent": "modify", "phrases": [], "patch": {
        "add": [_c("dividend_yield_ttm", "gte", 3, source_phrase="股息率调到3%以上")], "remove": [], "update": [], "universe": None,
    }}
    r = await parse_query(ParseRequest(query="股息率调到3%以上", base_strategy=base), FakeLLM(out), None)
    assert any(c.metric == "dividend_yield_ttm" for c in r.strategy.conditions)
    assert not any("当前策略中没有" in w for w in r.warnings)
