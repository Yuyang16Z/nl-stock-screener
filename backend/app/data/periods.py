"""报告期规则与日期工具：时间统一按 Asia/Shanghai。"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo

from ..schemas import ReportPeriod


def _cn_tz() -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Shanghai")
    except Exception:  # 无 tzdata 的环境：中国自 1991 年起不实行夏令时，固定 UTC+8 等价
        return timezone(timedelta(hours=8), name="Asia/Shanghai")


CN_TZ: tzinfo = _cn_tz()
DAY_MS = 86_400_000

_REPORT_RE = re.compile(r"^(\d{4})-([1-4])$")
_QUARTER_NAME = {1: "一季报", 2: "中报", 3: "三季报", 4: "年报"}
_PERIOD_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


# ───────────────────────────── 时间工具 ─────────────────────────────


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def today_cn() -> date:
    return now_cn().date()


def ms_to_datetime(ms: int | float) -> datetime:
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=CN_TZ)


def ms_to_date(ms: int | float) -> date:
    return ms_to_datetime(ms).date()


def date_to_datetime(d: date) -> datetime:
    """当日 00:00（Asia/Shanghai）。"""
    return datetime.combine(d, time(0, 0), tzinfo=CN_TZ)


def date_to_ms(d: date) -> int:
    """当日 00:00（Asia/Shanghai）的毫秒戳，与扶摇 date_ms 口径一致。"""
    return int(date_to_datetime(d).timestamp() * 1000)


def parse_date(value: str | None) -> date | None:
    """解析 yyyy-MM-dd / yyyyMMdd / yyyy/MM/dd（可带时间部分）；失败返回 None。"""
    if not value:
        return None
    text = str(value).strip()
    for fmt, size in (("%Y-%m-%d", 10), ("%Y/%m/%d", 10), ("%Y%m%d", 8)):
        try:
            return datetime.strptime(text[:size], fmt).date()
        except ValueError:
            continue
    return None


# ───────────────────────────── 报告期 ─────────────────────────────


def parse_report(report: str) -> tuple[int, int]:
    """'2026-2' → (2026, 2)；格式非法抛 ValueError。"""
    m = _REPORT_RE.match(str(report or "").strip())
    if not m:
        raise ValueError(f"报告期格式应为 yyyy-1 ~ yyyy-4（1 一季报、2 中报、3 三季报、4 年报），收到 {report!r}")
    return int(m.group(1)), int(m.group(2))


def format_report(year: int, quarter: int) -> str:
    return f"{year}-{quarter}"


def report_label(report: str) -> str:
    y, q = parse_report(report)
    return f"{y}年{_QUARTER_NAME[q]}"


def yoy_report(report: str) -> str:
    y, q = parse_report(report)
    return format_report(y - 1, q)


def quarters_in_report(report: str) -> int:
    return parse_report(report)[1]


def previous_annual_report(report: str) -> str:
    y, _ = parse_report(report)
    return format_report(y - 1, 4)


def next_report(report: str) -> str:
    y, q = parse_report(report)
    return format_report(y, q + 1) if q < 4 else format_report(y + 1, 1)


def report_period_end(report: str) -> date:
    y, q = parse_report(report)
    m, d = _PERIOD_END[q]
    return date(y, m, d)


def report_deadline(report: str) -> date:
    """法定披露截止日：一季报 4/30、中报 8/31、三季报 10/31、年报次年 4/30。"""
    y, q = parse_report(report)
    if q == 1:
        return date(y, 4, 30)
    if q == 2:
        return date(y, 8, 31)
    if q == 3:
        return date(y, 10, 31)
    return date(y + 1, 4, 30)


def _period(report: str, rule: str) -> ReportPeriod:
    return ReportPeriod(
        report=report,
        label=report_label(report),
        yoy_report=yoy_report(report),
        quarters=quarters_in_report(report),
        rule=rule,
    )


def uniform_report_period(today: date) -> ReportPeriod:
    """“全部公司都应已披露”的最近报告期：截止日当天仍可能有公司在披露，次日起才视为已全部披露。

    候选按报告期由新到旧检查；年报与次年一季报截止日同为 4/30，5/1 起最近期为当年一季报。
    """
    y = today.year
    for rep in (format_report(y, 3), format_report(y, 2), format_report(y, 1), format_report(y - 1, 4), format_report(y - 1, 3)):
        deadline = report_deadline(rep)
        if today > deadline:
            nxt = next_report(rep)
            rule = (
                f"法定披露截止日规则：{report_label(rep)}披露截止日 {deadline.isoformat()} 已过，"
                f"下一期{report_label(nxt)}截止日 {report_deadline(nxt).isoformat()} 未到，取全部公司都应已披露的最近一期"
            )
            return _period(rep, rule)
    raise AssertionError("unreachable: 上年三季报截止日总早于今天")  # pragma: no cover


def make_report_period(report: str | None, today: date) -> ReportPeriod:
    """report 为空 → 法定截止日规则；否则按用户指定（截止日未到时在 rule 中如实提示）。"""
    if not report:
        return uniform_report_period(today)
    rep = format_report(*parse_report(report))
    deadline = report_deadline(rep)
    rule = "用户指定报告期"
    if today <= deadline:
        rule += f"（法定披露截止日 {deadline.isoformat()} 未到，部分公司可能尚未披露，相关指标会显示为缺失）"
    return _period(rep, rule)


def history_window(today: date) -> tuple[int, int]:
    """历史日K统一取数窗口：(today.year−4)-01-01 00:00 ~ today 23:59:59（Asia/Shanghai）。

    筛选与回测共用同一窗口参数，从而共用同一缓存键；跨度 < 5 年，满足个股 10 年、指数近 5 年的接口限制。
    """
    start = date_to_ms(date(today.year - 4, 1, 1))
    end = date_to_ms(today) + DAY_MS - 1000
    return start, end
