"""compliance：输入拦截、可转化意图、输出禁语清洗（最小用例集）。"""

from __future__ import annotations

from app.ai import compliance


def test_check_input_categories() -> None:
    n = compliance.check_input("明天哪只股票会涨？")
    assert n is not None and n.level == "block" and n.category == "prediction" and "明天" in n.matched
    n = compliance.check_input("推荐几只股票")
    assert n is not None and n.level == "block" and n.category == "advice"
    n = compliance.check_input("找稳赚不赔的股票")
    assert n is not None and n.level == "block" and n.category == "return_promise"
    assert compliance.check_input("经营改善、估值合理、走势相对稳定") is None
    assert compliance.check_input("近一年涨幅翻倍的股票") is None


def test_convertible_intent() -> None:
    out = compliance.convertible_intent("有潜力的股票")
    assert out is not None and compliance.UP_TREND_DESCRIPTION in out
    assert compliance.convertible_intent("明天哪只股票会涨？") is None


def test_sanitize_output() -> None:
    clean, v = compliance.sanitize_output("营收同比增长 12%。建议买入。本结果不构成投资建议。")
    assert "建议买入" in v and "建议买入" not in clean
    assert "不构成投资建议" in clean and "营收同比增长" in clean
    assert compliance.sanitize_output("该股并非稳赚。")[1] == []


# ── #1 时间限定的“翻倍”预测：只拦截，不给可执行草案 ──

TIME_LIMITED_DOUBLING = ["帮我找下周能翻倍的票", "下周最可能翻倍的票", "明年能翻倍的股票"]


def test_time_limited_doubling_is_blocked_and_not_convertible() -> None:
    for q in TIME_LIMITED_DOUBLING:
        n = compliance.check_input(q)
        assert n is not None and n.level == "block" and n.category == "prediction", q
        assert compliance.convertible_intent(q) is None, q


def test_convertible_intent_refuses_future_words_and_doubling() -> None:
    for q in ["后市会涨的股票", "接下来看看会涨的股票", "今年内会涨的股票", "会翻倍的股票", "会涨停的股票", "能大涨的股票",
              "推荐近一年涨幅翻倍的股票"]:
        assert compliance.convertible_intent(q) is None, q
    out = compliance.convertible_intent("会涨的股票")
    assert out is not None and compliance.UP_TREND_DESCRIPTION in out and "翻倍" not in out
    # 前瞻副词不留在改写句里
    out = compliance.convertible_intent("大概率会涨的股票")
    assert out is not None and "大概率" not in out and compliance.UP_TREND_DESCRIPTION in out


# ── #11 “有望走强”类前瞻措辞至少 warn ──


def test_forward_looking_phrases_warn_with_history_hint() -> None:
    n = compliance.check_input("有望走强的低估值股")
    assert n is not None and n.level == "warn" and "有望走强" in n.matched
    hint = compliance.history_warning(n)
    assert hint is not None and "已按历史走势描述处理，不代表未来" in hint and "有望走强" in hint
    for q in ["即将突破的股票", "大概率上涨的银行股", "有机会回升的公司", "有望反弹的股票"]:
        n = compliance.check_input(q)
        assert n is not None and n.level in ("warn", "block"), q
    # 去重：“有望反弹”已包含“反弹”
    assert compliance.history_warning(compliance.check_input("有望反弹的股票")) == (
        "“有望反弹”涉及对未来走势的判断，已按历史走势描述处理，不代表未来表现。"
    )
    assert compliance.history_warning(compliance.check_input("找黑马")) is None  # 只有弱预测词：不写“已按历史处理”
    # 输出侧禁语“看涨”在输入侧至少 warn；“看看涨幅”“查看涨跌幅”“看多少只”不误判；“汽车后市场”不是“后市”
    n = compliance.check_input("看涨的股票")
    assert n is not None and n.level == "warn" and "已按历史走势描述处理" in (compliance.history_warning(n) or "")
    for q in ["看看涨幅前十的", "查看涨跌幅", "帮我看多少只满足", "汽车后市场概念里涨幅大的"]:
        assert compliance.check_input(q) is None, q


# ── #12 句首过渡词、预测类指标名不按预测请求拦截 ──


def test_transition_word_and_forecast_metric_name_not_blocked() -> None:
    n = compliance.check_input("接下来看业绩增长快的公司")
    assert n is None or n.level != "block"
    assert compliance.check_input("预测市盈率低的股票") is None
    # 真正的预测仍拦截：时间词 + 情态词 + 业绩；时间词 + 涨跌；“预测股价”
    for q in ["接下来会涨的股票", "明年业绩会增长的公司", "未来业绩会增长吗", "接下来盈利能改善的公司", "预测股价", "后市看好的股票"]:
        n = compliance.check_input(q)
        assert n is not None and n.level == "block" and n.category == "prediction", q
    # 时间词 + 业绩增长/改善：对未来基本面的判断，拦截（与 LLM 路径一致；审查回退项）
    for q in ["未来三年业绩增长快的公司", "未来业绩增长快的公司", "明年盈利改善的股票"]:
        n = compliance.check_input(q)
        assert n is not None and n.level == "block" and n.category == "prediction", q
