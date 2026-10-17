"""explain：模板句子（数值+来源+类型）、LLM 数字校验与合规校验回退（假 LLM，不访问网络）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.ai.explain import (
    extract_numbers,
    llm_run_summary,
    llm_stock_explanation,
    template_run_summary,
    template_stock_explanation,
)
from app.schemas import (
    CondEval,
    Condition,
    ConditionStat,
    DatasetStatus,
    Evidence,
    ReportPeriod,
    ResolvedUniverse,
    ScreenResult,
    SourceRef,
    StockResult,
    Strategy,
)

NOW = datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc)


class FakeLLM:
    enabled = True
    model = "fake-model"

    def __init__(self, response: Any = None, exc: Exception | None = None) -> None:
        self.response, self.exc, self.calls = response, exc, 0

    async def chat_json(self, system: str, user: str, **kw: Any) -> dict:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.response


def _strategy() -> Strategy:
    return Strategy(conditions=[
        Condition(id="c1", metric="revenue_yoy", op="gt", value=0),
        Condition(id="c2", metric="pe_ttm", op="pct_rank_lte", value=50, rank_scope="industry"),
        Condition(id="c3", metric="ocf_to_profit", op="gt", value=80),
    ])


def _stock() -> StockResult:
    src = SourceRef(provider="fuyao", endpoint="/api/a-share/financials/indicators", field="yoy_revenue")
    ev1 = Evidence(metric="revenue_yoy", value=12.3, unit="%", display="12.30%", period="2026年中报（2026-2）", sources=[src])
    ev2 = Evidence(
        metric="pe_ttm", value=18.5, unit="倍", display="18.50 倍", period="实时快照",
        sources=[SourceRef(provider="fuyao", endpoint="/api/a-share/valuations/snapshot", field="pe_ttm")],
    )
    return StockResult(
        thscode="600000.SH", name="示例股份", industry="银行", bucket="missing", missing_conditions=["c3"],
        evaluations=[
            CondEval(condition_id="c1", status="pass", value=12.3, display="12.30%", threshold="> 0%", evidence=ev1),
            CondEval(condition_id="c2", status="pass", value=18.5, display="18.50 倍", threshold="行业内分位 ≤ 50%",
                     rank_pct=23.4, rank_group_size=42, evidence=ev2),
            CondEval(condition_id="c3", status="missing", reason="经营现金流字段缺失（request_id=r-1）"),
        ],
    )


def _result() -> ScreenResult:
    return ScreenResult(
        run_id="r1", status="done", strategy=_strategy(), created_at=NOW,
        universe=ResolvedUniverse(kind="index", code="000300.SH", name="沪深300", size=300, members=[], note="成分股为当前时点"),
        report_period=ReportPeriod(report="2026-2", label="2026年中报", yoy_report="2025-2", quarters=2, rule="法定截止日规则"),
        trade_date="2026-10-16",
        included=[_stock().model_copy(update={"bucket": "included"})] * 12,
        excluded=[_stock().model_copy(update={"bucket": "excluded"})] * 280,
        missing=[_stock()] * 8,
        condition_stats=[
            ConditionStat(condition_id="c1", label="营业收入同比 > 0%", hard=True, passed=200, failed=95, missing=5, sole_blocker=10, drop_effect=30),
            ConditionStat(condition_id="c2", label="市盈率TTM 在所属行业内分位 ≤ 50%", hard=True, passed=150, failed=150, sole_blocker=40, drop_effect=55),
            ConditionStat(condition_id="c3", label="经营现金流/净利润 > 80%", hard=True, passed=180, failed=112, missing=8, sole_blocker=5, drop_effect=20),
        ],
        datasets=[
            DatasetStatus(dataset="valuations", label="估值快照", endpoint="/api/a-share/valuations/snapshot", status="ok", requested=300, succeeded=300),
            DatasetStatus(dataset="income", label="利润表（季报序列）", endpoint="/api/a-share/financials/income-statements",
                          status="partial", requested=300, succeeded=280, failed=20),
        ],
    )


def test_extract_numbers_dates_and_signs() -> None:
    assert extract_numbers("截至 2026-10-16，同比 -3.5%，报告期 2026-2") == [2026, 10, 16, -3.5, 2026, 2]


def test_template_stock_sentences_have_values_sources_and_kinds() -> None:
    e = template_stock_explanation(_stock(), _strategy())
    assert e.generator == "template" and e.verified
    texts = "\n".join(s.text for s in e.sentences)
    assert "12.30%" in texts and "/api/a-share/financials/indicators · yoy_revenue" in texts
    assert "2026年中报" in texts and "18.50 倍" in texts
    kinds = {s.kind for s in e.sentences}
    assert {"fact", "inference", "uncertain", "caveat"} <= kinds
    unc = [s for s in e.sentences if s.kind == "uncertain" and "c3" in s.refs]
    assert unc and "无法判定" in unc[-1].text
    assert e.sentences[-1].kind == "caveat"


def test_template_run_summary() -> None:
    e = template_run_summary(_result())
    texts = "\n".join(s.text for s in e.sentences)
    assert "沪深300" in texts and "300 只" in texts and "入选 12 只" in texts and "数据缺失 8 只" in texts
    assert "市盈率TTM 在所属行业内分位 ≤ 50%" in texts and "55" in texts  # 主要卡点
    assert "利润表（季报序列）" in texts and "部分失败" in texts
    assert "2026年中报" in texts and "2026-10-16" in texts
    assert e.sentences[-1].kind == "caveat"


async def test_llm_summary_ok() -> None:
    good = {"sentences": [
        {"text": "股票池沪深300共 300 只，入选 12 只、排除 280 只、数据缺失 8 只。", "kind": "fact", "refs": []},
        {"text": "去掉行业估值分位条件后入选数为 55 只。", "kind": "fact", "refs": ["c2", "zzz"]},
        {"text": "结果不构成投资建议。", "kind": "caveat", "refs": []},
    ]}
    e = await llm_run_summary(_result(), FakeLLM(good))
    assert e.generator == "llm" and e.verified
    assert e.sentences[1].refs == ["c2"]


async def test_llm_wrong_number_falls_back_to_template() -> None:
    bad = {"sentences": [{"text": "本次入选 15 只股票。", "kind": "fact", "refs": []}]}
    e = await llm_run_summary(_result(), FakeLLM(bad))
    assert e.generator == "template"
    assert any(n.startswith("AI 文本未通过数字校验，已改用模板：") and "15" in n for n in e.verification_notes)

    bad_stock = {"sentences": [{"text": "营业收入同比为 13.5%。", "kind": "fact", "refs": ["c1"]}]}
    e = await llm_stock_explanation(_stock(), _strategy(), FakeLLM(bad_stock))
    assert e.generator == "template" and any("13.5" in n for n in e.verification_notes)


async def test_llm_banned_words_fall_back() -> None:
    bad = {"sentences": [{"text": "营业收入同比为 12.30%，建议买入。", "kind": "fact", "refs": ["c1"]}]}
    e = await llm_stock_explanation(_stock(), _strategy(), FakeLLM(bad))
    assert e.generator == "template"
    assert any("不合规" in n for n in e.verification_notes)


async def test_llm_stock_ok_and_errors() -> None:
    good = {"sentences": [
        {"text": "营业收入同比为 12.30%，满足 > 0% 的条件。", "kind": "fact", "refs": ["c1"]},
        {"text": "市盈率在行业内分位为 23.4%，处于较低水平。", "kind": "inference", "refs": ["c2"]},
        {"text": "经营现金流数据缺失，无法判定。", "kind": "uncertain", "refs": ["c3"]},
    ]}
    e = await llm_stock_explanation(_stock(), _strategy(), FakeLLM(good))
    assert e.generator == "llm" and e.sentences[-1].kind == "caveat"

    e = await llm_stock_explanation(_stock(), _strategy(), FakeLLM(exc=RuntimeError("down")))
    assert e.generator == "template" and any("生成失败" in n for n in e.verification_notes)
    e = await llm_stock_explanation(_stock(), _strategy(), FakeLLM({"sentences": [{"text": "x", "kind": "guess"}]}))
    assert e.generator == "template"
