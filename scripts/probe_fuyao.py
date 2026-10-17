"""数据口径探测脚本：用真实 Key 核实接口语义，结论写入 docs/DATA_SOURCES.md 与 docs/AI_USAGE.md。

只输出结构性结论（条数、字段、空值分布、口径判断、耗时），不落盘任何原始数据。
用法（仓库根目录）：cd backend && uv run python ../scripts/probe_fuyao.py
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import get_settings  # noqa: E402

CN = timezone(timedelta(hours=8))
S = get_settings()
LAT: list[float] = []


def ms(d: datetime) -> int:
    return int(d.timestamp() * 1000)


def d(ms_: int | None) -> str:
    return "None" if ms_ is None else datetime.fromtimestamp(ms_ / 1000, CN).strftime("%Y-%m-%d %H:%M")


async def get(c: httpx.AsyncClient, path: str, **params):
    t0 = time.perf_counter()
    r = await c.get(path, params=params)
    LAT.append(time.perf_counter() - t0)
    try:
        body = r.json()
    except Exception:
        return {"_http": r.status_code, "_text": r.text[:200]}
    body["_http"] = r.status_code
    return body


def head(title: str):
    print(f"\n## {title}")


async def main():
    if not S.fuyao_api_key:
        print("未配置 FUYAO_API_KEY")
        return
    async with httpx.AsyncClient(base_url=S.fuyao_base_url, headers={"X-api-key": S.fuyao_api_key}, timeout=30) as c:
        head("代码表")
        allrows, off = [], 0
        while True:
            b = await get(c, "/api/meta/tickers/list", asset_type="a-share", limit=10000, offset=off)
            items = (b.get("data") or {}).get("item") or []
            allrows += items
            if len(items) < 10000:
                break
            off += 10000
        print(f"code={b.get('code')} A股数={len(allrows)} 含ST={sum('ST' in (x.get('name') or '') for x in allrows)} "
              f"含退={sum('退' in (x.get('name') or '') for x in allrows)} 交易所={Counter(x.get('exchange') for x in allrows)}")
        print("样例:", [(x["thscode"], x.get("name"), x.get("list_date")) for x in allrows[:3]])

        head("交易日历")
        b = await get(c, "/api/a-share/calendar/trading-days")
        days = (b.get("data") or {}).get("item") or []
        print(f"code={b.get('code')} 天数={len(days)} 首={days[0]['date'] if days else None} 末={days[-1]['date'] if days else None}")

        head("行情快照（批量上限探测）")
        codes = [x["thscode"] for x in allrows[:150]]
        for n in (100, 150):
            b = await get(c, "/api/a-share/prices/snapshot", thscodes=",".join(codes[:n]))
            data = b.get("data") or {}
            print(f"n={n} code={b.get('code')} msg={b.get('message')} 返回={len(data.get('item') or [])} ts={d(data.get('timestamp'))}")

        head("估值快照")
        b = await get(c, "/api/a-share/valuations/snapshot", thscodes="600519.SH,000001.SZ,601398.SH,300750.SZ,688981.SH")
        data = b.get("data") or {}
        print(f"code={b.get('code')} ts={d(data.get('timestamp'))}")
        for it in data.get("item") or []:
            print("  ", {k: it.get(k) for k in ("thscode", "name", "pe_ttm", "pb_mrq", "ps_ttm", "pcf_ttm")})
        b = await get(c, "/api/a-share/valuations/snapshot", thscodes=",".join(codes[:101]))
        print(f"101 个: code={b.get('code')} msg={b.get('message')}")

        head("指数成分")
        for code in ("000300.SH", "000905.SH", "000852.SH", "000016.SH", "399006.SZ", "000688.SH", "000906.SH", "399300.SZ"):
            b = await get(c, "/api/a-share-index/constituents/ths-stock-list", thscode=code)
            items = (b.get("data") or {}).get("item") or []
            print(f"{code}: code={b.get('code')} msg={b.get('message')} 成分={len(items)} ts={d((b.get('data') or {}).get('timestamp'))}")

        head("同花顺行业/概念目录")
        cat = {}
        for tag in ("industry", "cn_concept"):
            b = await get(c, "/api/a-share-index/catalog/ths-index-list", tag=tag)
            items = (b.get("data") or {}).get("item") or []
            cat[tag] = items
            print(f"{tag}: code={b.get('code')} 数量={len(items)} 样例={[(x['thscode'], x['name']) for x in items[:8]]}")
            print("  代码前缀分布:", Counter(x["thscode"][:4] for x in items).most_common(8))
        # 行业层级：抽样成员数
        sample = cat["industry"][:: max(1, len(cat["industry"]) // 25)][:25]
        sizes = []
        for it in sample:
            b = await get(c, "/api/a-share-index/constituents/ths-stock-list", thscode=it["thscode"])
            n = len((b.get("data") or {}).get("item") or [])
            sizes.append((it["thscode"], it["name"], n))
        print("行业成员数抽样:", sizes)

        head("利润表季报（累计口径核实）")
        for code in ("600519.SH", "000001.SZ"):
            b = await get(c, "/api/a-share/financials/income-statements", thscode=code, period="quarterly", limit=8)
            items = (b.get("data") or {}).get("item") or []
            print(f"{code}: code={b.get('code')} 期数={len(items)}")
            for it in items:
                print(f"   {it.get('fiscal_year')} {it.get('fiscal_period')} 期末={d(it.get('period_end_ms'))} 披露={d(it.get('report_date_ms'))} "
                      f"营收={it.get('operating_income')} 成本={it.get('operating_costs')} 净利={it.get('net_profit')} 归母={it.get('parent_holder_net_profit')} eps={it.get('basic_eps')}")
        b = await get(c, "/api/a-share/financials/income-statements", thscode="600519.SH", period="annual", limit=2)
        for it in (b.get("data") or {}).get("item") or []:
            print(f"   年报 {it.get('fiscal_year')} {it.get('fiscal_period')} 营收={it.get('operating_income')} 披露={d(it.get('report_date_ms'))}")

        head("财务指标")
        for code, rep in (("600519.SH", "2026-2"), ("600519.SH", "2025-2"), ("600519.SH", "2026-3"), ("600036.SH", "2026-2"), ("300750.SZ", "2026-2")):
            b = await get(c, "/api/a-share/financials/indicators", thscode=code, report=rep)
            data = b.get("data") or {}
            flat = {i["index_id"]: i["value"] for a in data.get("abilities") or [] for i in a.get("indicators") or []}
            nulls = [k for k, v in flat.items() if v is None]
            print(f"{code} {rep}: code={b.get('code')} msg={b.get('message')} 指标数={len(flat)} 空值={nulls}")
            if flat:
                print("   ", {k: flat.get(k) for k in ("operating_income_yoy_growth_ratio", "net_profit_yoy_growth_ratio", "index_weighted_avg_roe", "sale_gross_margin", "assets_debt_ratio", "net_profit_cash_content")})

        head("历史K线与指数K线")
        now = datetime.now(CN)
        start = ms(datetime(now.year - 4, 1, 1, tzinfo=CN))
        for adj in ("backward", "none"):
            b = await get(c, "/api/a-share/prices/historical", thscode="600519.SH", interval="1d", start=start, end=ms(now), adjust=adj)
            items = (b.get("data") or {}).get("item") or []
            print(f"600519.SH adjust={adj}: code={b.get('code')} 条数={len(items)} 首={d(items[0]['date_ms']) if items else None} 末={d(items[-1]['date_ms']) if items else None} 末收={items[-1]['close_price'] if items else None}")
        b = await get(c, "/api/a-share-index/prices/historical", thscode="000300.SH", interval="1d", start=ms(now - timedelta(days=365 * 5 - 10)), end=ms(now))
        items = (b.get("data") or {}).get("item") or []
        print(f"000300.SH 指数: code={b.get('code')} msg={b.get('message')} 条数={len(items)} 末={d(items[-1]['date_ms']) if items else None}")

        head("分红事件")
        b = await get(c, "/api/a-share/corporate-actions/adjustment-factors", thscode="600519.SH", **{"from": (now - timedelta(days=400)).strftime("%Y-%m-%d")})
        print(f"code={b.get('code')} 事件={[(d(x['ex_date_ms']), x.get('dividend_per_share'), x.get('per_share_bonus')) for x in (b.get('data') or {}).get('item') or []]}")

        head("错误码样例")
        for path, params in (("/api/a-share/valuations/snapshot", {"thscodes": "999999.SH"}),
                             ("/api/a-share/financials/indicators", {"thscode": "600519.SH", "report": "2026-9"}),
                             ("/api/a-share/prices/historical", {"thscode": "600519.SH", "interval": "1d"})):
            b = await get(c, path, **params)
            print(f"{path} {params}: http={b.get('_http')} code={b.get('code')} msg={b.get('message')}")

        head("并发与限流（20 并发 × 3 轮 财务指标）")
        lat0 = len(LAT)
        res = Counter()
        sample_codes = [x["thscode"] for x in allrows[1000:1060]]
        for rnd in range(3):
            bs = await asyncio.gather(*[get(c, "/api/a-share/financials/indicators", thscode=cd, report="2026-2") for cd in sample_codes[rnd * 20:(rnd + 1) * 20]])
            res.update(f"http{b.get('_http')}/code{b.get('code')}" for b in bs)
        lat = LAT[lat0:]
        print(f"结果分布={dict(res)} 延迟 p50={statistics.median(lat):.2f}s max={max(lat):.2f}s")
        print(f"\n全部请求 {len(LAT)} 次，p50={statistics.median(LAT):.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
