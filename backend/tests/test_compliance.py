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
