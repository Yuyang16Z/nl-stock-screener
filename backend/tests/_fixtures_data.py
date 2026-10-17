"""数据层测试共用：设置工厂、信封构造、合成市场与假客户端。

所有代码之外的数值（价格、估值、财务、分红、日期序列）均为虚构，仅用于验证计算与流程。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from app.config import Settings
from app.data.fuyao import FuyaoError, FuyaoResponse, fault_error, parse_envelope
from app.data.periods import CN_TZ, date_to_ms

BASE_URL = "https://fuyao.test"
IFIND_URL = "https://ifind.test/mcp"
TODAY = date(2026, 10, 17)  # 周六
LAST_TRADE_DAY = date(2026, 10, 16)
FIXED_NOW = datetime(2026, 10, 17, 9, 30, tzinfo=CN_TZ)


def make_settings(**overrides: Any) -> Settings:
    """不读 .env、不依赖真实 Key 的设置。"""
    values: dict[str, Any] = {
        "fuyao_api_key": "test-key",
        "fuyao_base_url": BASE_URL,
        "fuyao_max_concurrency": 4,
        "fuyao_rps": 0.0,
        "fuyao_timeout_s": 2.0,
        "ifind_mcp_token": None,
        "ifind_mcp_url": IFIND_URL,
        "ifind_timeout_s": 2.0,
        "llm_api_key": None,
        "data_dir": "./data-test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def envelope(data: Any, *, code: int = 0, message: str = "success", request_id: str = "rid-ok") -> dict[str, Any]:
    return {"code": code, "message": message, "request_id": request_id, "data": data}


def make_resp(
    endpoint: str,
    data: Any,
    *,
    params: dict[str, Any] | None = None,
    request_id: str = "rid-ok",
    dataset: str | None = None,
) -> FuyaoResponse:
    resp, _ = parse_envelope(
        200,
        json.dumps(envelope(data, request_id=request_id), ensure_ascii=False).encode(),
        endpoint=endpoint,
        params=params or {},
        dataset=dataset,
        fetched_at=FIXED_NOW,
    )
    return resp


def trading_days(end: date, n: int) -> list[date]:
    """截至 end 的最近 n 个工作日（合成日历，不含节假日）。"""
    out: list[date] = []
    d = end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def bars_payload(
    days: list[date],
    closes: list[float],
    turnovers: list[float] | None = None,
    volumes: list[float] | None = None,
) -> dict[str, Any]:
    items = []
    for i, (d, c) in enumerate(zip(days, closes)):
        items.append(
            {
                "date_ms": date_to_ms(d),
                "open_price": c,
                "high_price": c,
                "low_price": c,
                "close_price": c,
                "volume": volumes[i] if volumes is not None else 1_000_000.0,
                "turnover": turnovers[i] if turnovers is not None else 2.0e8,
            }
        )
    return {"timestamp": date_to_ms(days[-1]) if days else None, "item": items}


ABILITY_OF = {
    "calculate_operating_income_yoy_growth_ratio": "growth",
    "calculate_parent_holder_net_profit_yoy_growth_ratio": "growth",
    "calculate_operating_profit_yoy_growth_ratio": "growth",
    "sale_gross_margin": "profitability",
    "sale_net_interest_ratio": "profitability",
    "index_weighted_avg_roe": "profitability",
    "index_deduct_weighted_avg_roe": "profitability",
    "current_ratio": "solvency",
    "assets_debt_ratio": "solvency",
    "net_profit_cash_content": "cash-flow",
    "operating_cash_flow_net_divide_income": "cash-flow",
}


def indicators_payload(code: str, report: str, values: dict[str, Any]) -> dict[str, Any]:
    abilities: dict[str, list[dict[str, Any]]] = {a: [] for a in ("growth", "profitability", "solvency", "operation", "cash-flow")}
    for k, v in values.items():
        abilities[ABILITY_OF.get(k, "operation")].append({"index_id": k, "value": v})
    return {"thscode": code, "report": report, "abilities": [{"ability": a, "indicators": inds} for a, inds in abilities.items()]}


def income_item(code: str, year: int, fp: str, revenue: float | None, parent_np: float | None = None, report_date: date | None = None) -> dict[str, Any]:
    month = {"Q1": 3, "Q2": 6, "Q3": 9, "Q4": 12, "FY": 12}[fp]
    end = date(year, month, 30 if month in (6, 9) else 31)
    return {
        "thscode": code,
        "ticker": code[:6],
        "period": "quarterly",
        "fiscal_year": year,
        "fiscal_period": fp,
        "report_date_ms": date_to_ms(report_date or (end + timedelta(days=40))),
        "period_end_ms": date_to_ms(end),
        "currency": "CNY",
        "operating_income": revenue,
        "parent_holder_net_profit": parent_np,
        "net_profit": parent_np,
    }


# ───────────────────────────── 合成市场 ─────────────────────────────

TICKERS: list[tuple[str, str, str]] = [
    ("600001.SH", "测试甲", "2010-01-04"),
    ("600002.SH", "测试乙银行", "2008-05-05"),
    ("000003.SZ", "*ST测试丙", "2012-03-01"),
    ("300004.SZ", "测试丁", "2026-03-02"),
    ("600005.SH", "测试戊退", "2005-01-04"),
    ("688006.SH", "测试己", "2019-07-22"),
    ("000007.SZ", "测试庚", "2011-06-01"),
    ("000008.SZ", "测试辛", "2013-09-09"),
    ("600009.SH", "测试壬", "2014-02-14"),
    ("600010.SH", "测试癸", "2015-05-05"),
    ("600011.SH", "测试子", "2016-08-08"),
    ("600012.SH", "测试丑", "2017-10-10"),
]
HS300_MEMBERS = ["600001.SH", "600002.SH", "000003.SZ", "300004.SZ"]
INDUSTRY_INDICES: dict[str, tuple[str, list[str]]] = {
    "881100.TI": ("测试制造", ["600001.SH", "000003.SZ", "300004.SZ", "688006.SH", "000007.SZ", "000008.SZ", "600009.SH", "600010.SH", "600011.SH", "600012.SH"]),
    "881110.TI": ("测试电子", ["600001.SH", "300004.SZ", "688006.SH", "000007.SZ", "000008.SZ", "600009.SH", "600010.SH", "600011.SH"]),
    "881111.TI": ("测试半导体", ["600001.SH", "300004.SZ", "688006.SH"]),
    "881200.TI": ("测试银行", ["600002.SH"]),
    "881300.TI": ("测试小类", ["000003.SZ", "600012.SH"]),
}
CONCEPTS: list[tuple[str, str]] = [("886001.TI", "测试白酒概念"), ("886002.TI", "测试芯片概念")]
PE: dict[str, float | None] = {
    "600001.SH": 15.5, "600002.SH": 5.2, "000003.SZ": -12.0, "300004.SZ": 45.0, "600005.SH": 8.0, "688006.SH": 60.0,
    "000007.SZ": 20.0, "000008.SZ": None, "600009.SH": 25.0, "600010.SH": -3.0, "600011.SH": 33.0, "600012.SH": 18.0,
}


class SyntheticMarket:
    """按 (path, params) 返回合成信封；fail_rules 可让指定请求失败。"""

    def __init__(self) -> None:
        self.tickers = list(TICKERS)
        self.indices: dict[str, list[str]] = {"000300.SH": list(HS300_MEMBERS)}
        self.indices.update({c: m for c, (_, m) in INDUSTRY_INDICES.items()})
        self.indices.update({"886001.TI": ["600001.SH", "000007.SZ"], "886002.TI": ["688006.SH"]})
        self.catalog: dict[str, list[tuple[str, str]]] = {
            "industry": [(c, n) for c, (n, _) in INDUSTRY_INDICES.items()],
            "cn_concept": list(CONCEPTS),
        }
        self.names = {c: n for c, n, _ in self.tickers}
        self.days = trading_days(LAST_TRADE_DAY, 300)
        self.history_end: dict[str, date] = {}  # 个股日K截止日（模拟停牌）
        self.bench_days: list[date] | None = None  # 基准日K日期（默认同 self.days）
        self.fail_rules: list[Callable[[str, dict[str, Any], str], FuyaoError | None]] = []
        self.indicator_values: dict[tuple[str, str], dict[str, Any]] = {}
        self._build_indicators()

    def _build_indicators(self) -> None:
        def full(rev: str, np_: str, roe: str, gm: str | None, cur: str | None) -> dict[str, Any]:
            return {
                "calculate_operating_income_yoy_growth_ratio": rev,
                "calculate_parent_holder_net_profit_yoy_growth_ratio": np_,
                "calculate_operating_profit_yoy_growth_ratio": np_,
                "index_weighted_avg_roe": roe,
                "index_deduct_weighted_avg_roe": roe,
                "sale_gross_margin": gm,
                "sale_net_interest_ratio": "10.0",
                "net_profit_cash_content": "90.0",
                "operating_cash_flow_net_divide_income": "8.0",
                "assets_debt_ratio": "45.0",
                "current_ratio": cur,
            }

        self.indicator_values = {
            ("600001.SH", "2026-2"): full("12.5", "8.0", "6.0", "35.0", "1.8"),
            ("600001.SH", "2025-2"): full("9.0", "6.0", "5.0", "33.0", "1.7"),
            ("600002.SH", "2026-2"): full("3.0", "2.0", "5.5", None, None),
            ("600002.SH", "2025-2"): full("4.0", "3.0", "5.8", None, None),
            ("000003.SZ", "2026-2"): full("-5.0", "-30.0", "-2.0", "15.0", "0.9"),
            ("000003.SZ", "2025-2"): full("1.0", "2.0", "1.0", "18.0", "1.0"),
            ("300004.SZ", "2026-2"): full("30.0", "25.0", "4.0", "40.0", "2.5"),
            ("300004.SZ", "2025-2"): full("20.0", "15.0", "3.0", "38.0", "2.4"),
        }

    # ── 路由 ──

    def handle(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        p = {k: str(v) for k, v in params.items()}
        rid = f"rid-{path.rsplit('/', 1)[-1]}-{p.get('thscode') or p.get('tag') or p.get('offset') or 'batch'}"
        if path == "/api/meta/tickers/list":
            offset, limit = int(p.get("offset", "0")), int(p.get("limit", "1000"))
            page = self.tickers[offset : offset + limit]
            items = [{"thscode": c, "ticker": c[:6], "name": n, "exchange": c[-2:], "list_date": d} for c, n, d in page]
            return envelope({"timestamp": date_to_ms(TODAY), "item": items}, request_id=rid)
        if path == "/api/a-share-index/catalog/ths-index-list":
            items = [{"thscode": c, "name": n} for c, n in self.catalog.get(p.get("tag", "cn_concept"), [])]
            return envelope({"timestamp": date_to_ms(TODAY), "item": items}, request_id=rid)
        if path == "/api/a-share-index/constituents/ths-stock-list":
            code = p.get("thscode", "")
            if code not in self.indices:
                return envelope(None, code=3001, message="标的不存在", request_id=rid)
            items = [{"thscode": c, "ticker": c[:6], "name": self.names.get(c)} for c in self.indices[code]]
            return envelope({"timestamp": date_to_ms(TODAY), "item": items}, request_id=rid)
        if path == "/api/a-share/valuations/snapshot":
            codes = [c for c in p.get("thscodes", "").split(",") if c]
            items = []
            for i, c in enumerate(codes):
                if c not in self.names:
                    continue
                pe = PE.get(c)
                items.append({"thscode": c, "ticker": c[:6], "name": self.names[c], "pe_ttm": pe, "pe_mrq": pe, "pb_mrq": 1.0 + 0.5 * (i % 5), "ps_ttm": 2.0, "pcf_ttm": 12.0})
            return envelope({"timestamp": date_to_ms(LAST_TRADE_DAY) + 54_000_000, "total": len(items), "item": items}, request_id=rid)
        if path == "/api/a-share/prices/snapshot":
            codes = [c for c in p.get("thscodes", "").split(",") if c]
            items = [{"thscode": c, "ticker": c[:6], "last_price": 20.0, "price_change_ratio_pct": 1.5, "turnover": 3.0e8} for c in codes if c in self.names]
            return envelope({"timestamp": date_to_ms(LAST_TRADE_DAY) + 54_000_000, "total": len(items), "item": items}, request_id=rid)
        if path == "/api/a-share/financials/indicators":
            code, report = p.get("thscode", ""), p.get("report", "")
            values = self.indicator_values.get((code, report))
            if values is None:
                return envelope(None, code=3002, message="数据未就绪", request_id=rid)
            return envelope(indicators_payload(code, report, values), request_id=rid)
        if path == "/api/a-share/financials/income-statements":
            code = p.get("thscode", "")
            items = [
                income_item(code, 2026, "Q2", 6.0e9, 1.2e9),
                income_item(code, 2026, "Q1", 2.8e9, 0.5e9),
                income_item(code, 2025, "Q4", 1.0e10, 2.0e9),
                income_item(code, 2025, "Q3", 7.2e9, 1.4e9),
                income_item(code, 2025, "Q2", 4.5e9, 0.9e9),
                income_item(code, 2025, "Q1", 2.0e9, 0.4e9),
            ]
            return envelope({"timestamp": items[0]["period_end_ms"], "item": items}, request_id=rid)
        if path == "/api/a-share/prices/historical":
            code = p.get("thscode", "")
            k = sum(map(ord, code)) % 7
            days = trading_days(self.history_end[code], 300) if code in self.history_end else self.days
            closes = [10.0 * (k + 1) * math.exp(0.0005 * t + 0.02 * math.sin(t / 7.0 + k)) for t in range(len(days))]
            turnovers = [1.0e8 * (k + 1) * (1.0 + 0.1 * math.sin(t / 5.0)) for t in range(len(days))]
            return envelope(bars_payload(days, closes, turnovers), request_id=rid)
        if path == "/api/a-share-index/prices/historical":
            bdays = self.bench_days or self.days
            closes = [4000.0 * math.exp(0.0003 * t + 0.01 * math.sin(t / 9.0)) for t in range(len(bdays))]
            return envelope({**bars_payload(bdays, closes, [3.0e11] * len(bdays), [2.0e8] * len(bdays)), "adjust": None}, request_id=rid)
        if path == "/api/a-share/corporate-actions/adjustment-factors":
            code = p.get("thscode", "")
            items = []
            if code == "600001.SH":
                items = [{"ticker": code[:6], "ex_date_ms": date_to_ms(date(2026, 6, 20)), "dividend_per_share": 0.5, "per_share_bonus": 0.0}]
            return envelope({"thscode": code, "ticker": code[:6], "item": items}, request_id=rid)
        if path == "/api/a-share/calendar/trading-days":
            return envelope({"timestamp": date_to_ms(TODAY), "item": [{"date_ms": date_to_ms(d), "date": d.strftime("%Y%m%d")} for d in self.days[-5:]]}, request_id=rid)
        return envelope(None, code=1001, message="unknown path", request_id=rid)


class FakeClient:
    """假扶摇客户端：与 FuyaoClient.get 同签名，经同一信封解析，支持故障注入与失败规则。"""

    def __init__(self, market: SyntheticMarket) -> None:
        self.market = market
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    async def get(self, path: str, params: dict[str, Any] | None, ttl_s: int, dataset: str, faults: Any = frozenset()) -> FuyaoResponse:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        self.calls.append((path, clean, dataset))
        if dataset in faults:
            raise fault_error(dataset, path, clean)
        for rule in self.market.fail_rules:
            err = rule(path, clean, dataset)
            if err is not None:
                raise err
        env = self.market.handle(path, clean)
        resp, _ = parse_envelope(
            200, json.dumps(env, ensure_ascii=False).encode(), endpoint=path, params=clean, dataset=dataset, fetched_at=FIXED_NOW
        )
        return resp

    def count(self, dataset: str) -> int:
        return sum(1 for _, _, ds in self.calls if ds == dataset)


def fail_when(dataset: str, thscode: str, *, code: int = 5003, kind: str = "upstream_unavailable", request_id: str = "rid-fail") -> Callable[[str, dict[str, Any], str], FuyaoError | None]:
    """构造失败规则：指定数据集、指定股票的请求返回错误。"""

    def rule(path: str, params: dict[str, Any], ds: str) -> FuyaoError | None:
        if ds == dataset and (params.get("thscode") == thscode or thscode in str(params.get("thscodes", "")).split(",")):
            return FuyaoError("上游不可用", kind=kind, code=code, request_id=request_id, endpoint=path, dataset=ds, params=params)  # type: ignore[arg-type]
        return None

    return rule
