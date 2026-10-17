from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.data.periods import (
    CN_TZ,
    DAY_MS,
    date_to_ms,
    history_window,
    make_report_period,
    ms_to_date,
    ms_to_datetime,
    next_report,
    now_cn,
    parse_date,
    parse_report,
    previous_annual_report,
    quarters_in_report,
    report_deadline,
    report_label,
    report_period_end,
    uniform_report_period,
    yoy_report,
)


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        # 一季报与上年年报截止同为 4/30：当天仍视为未披露完，5/1 起为当年一季报
        (date(2026, 4, 30), "2025-3"),
        (date(2026, 5, 1), "2026-1"),
        (date(2026, 8, 31), "2026-1"),
        (date(2026, 9, 1), "2026-2"),
        (date(2026, 10, 17), "2026-2"),  # DESIGN 示例
        (date(2026, 10, 31), "2026-2"),
        (date(2026, 11, 1), "2026-3"),
        (date(2026, 11, 5), "2026-3"),  # DESIGN 示例
        (date(2026, 12, 31), "2026-3"),
        (date(2027, 1, 1), "2026-3"),
        (date(2027, 3, 1), "2026-3"),  # DESIGN 示例：次年 3/1
        (date(2027, 4, 30), "2026-3"),  # 次年 4/30：年报与一季报截止日当天
        (date(2027, 5, 1), "2027-1"),  # 次年 5/1：一季报晚于上年年报
    ],
)
def test_uniform_report_period_boundaries(today: date, expected: str) -> None:
    rp = uniform_report_period(today)
    assert rp.report == expected
    assert rp.label == report_label(expected)
    assert rp.yoy_report == yoy_report(expected)
    assert rp.quarters == int(expected[-1])
    assert "法定披露截止日" in rp.rule


def test_uniform_rule_text_mentions_deadlines() -> None:
    rp = uniform_report_period(date(2026, 10, 17))
    assert "2026-08-31 已过" in rp.rule
    assert "2026年三季报截止日 2026-10-31 未到" in rp.rule


def test_report_helpers() -> None:
    assert report_label("2026-2") == "2026年中报"
    assert report_label("2026-1") == "2026年一季报"
    assert report_label("2026-3") == "2026年三季报"
    assert report_label("2025-4") == "2025年年报"
    assert yoy_report("2026-2") == "2025-2"
    assert quarters_in_report("2026-2") == 2
    assert quarters_in_report("2025-4") == 4
    assert previous_annual_report("2026-3") == "2025-4"
    assert next_report("2026-3") == "2026-4"
    assert next_report("2026-4") == "2027-1"
    assert report_period_end("2026-2") == date(2026, 6, 30)
    assert report_period_end("2026-3") == date(2026, 9, 30)
    assert report_deadline("2026-4") == date(2027, 4, 30)
    assert parse_report(" 2026-3 ") == (2026, 3)


@pytest.mark.parametrize("bad", ["2026-5", "2026-0", "26-2", "2026Q2", "", "abc"])
def test_parse_report_rejects_bad_format(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_report(bad)


def test_make_report_period_user_specified() -> None:
    rp = make_report_period("2026-3", date(2026, 10, 17))
    assert rp.report == "2026-3"
    assert rp.label == "2026年三季报"
    assert rp.yoy_report == "2025-3"
    assert rp.quarters == 3
    assert rp.rule.startswith("用户指定报告期")
    assert "未到" in rp.rule  # 截止日未到：如实提示会有缺失

    past = make_report_period("2025-4", date(2026, 10, 17))
    assert past.rule == "用户指定报告期"

    auto = make_report_period(None, date(2026, 10, 17))
    assert auto.report == "2026-2"


def test_time_conversions_use_shanghai() -> None:
    # 文档示例：2023-01-01 00:00:00+08:00 → 1672502400000；2024-12-31 00:00:00+08:00 → 1735574400000
    assert date_to_ms(date(2023, 1, 1)) == 1672502400000
    assert date_to_ms(date(2024, 12, 31)) == 1735574400000
    assert ms_to_date(1735574400000) == date(2024, 12, 31)
    dt = ms_to_datetime(1672502400000)
    assert dt.utcoffset() == timedelta(hours=8)
    assert (dt.year, dt.month, dt.day, dt.hour) == (2023, 1, 1, 0)
    assert now_cn().utcoffset() == timedelta(hours=8)


def test_parse_date_formats() -> None:
    assert parse_date("2001-08-27") == date(2001, 8, 27)
    assert parse_date("20250523") == date(2025, 5, 23)
    assert parse_date("2025/05/23") == date(2025, 5, 23)
    assert parse_date("2025-05-23 00:00:00") == date(2025, 5, 23)
    assert parse_date("") is None
    assert parse_date(None) is None
    assert parse_date("not-a-date") is None


def test_history_window_is_stable_and_within_limits() -> None:
    today = date(2026, 10, 17)
    start, end = history_window(today)
    assert start == date_to_ms(date(2022, 1, 1))
    assert end == date_to_ms(today) + DAY_MS - 1000
    end_dt = ms_to_datetime(end)
    assert (end_dt.date(), end_dt.hour, end_dt.minute, end_dt.second) == (today, 23, 59, 59)
    # 同一天多次调用 → 同一参数（筛选与回测共用缓存键）
    assert history_window(today) == (start, end)
    # 跨度 < 5 年（指数历史仅近 5 年；个股窗口 ≤ 10 年）
    span_days = (end - start) / DAY_MS
    assert span_days < 5 * 365
    assert ms_to_datetime(start) == datetime(2022, 1, 1, tzinfo=CN_TZ)
