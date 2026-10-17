"""可追溯性核验：跑一次真实筛选，再绕过产品、直接调用扶摇接口核对证据里的原始数值。

检查项
1. 入选股票的每个硬条件都是 pass（keep_flagged 策略下允许带标记的缺失）；缺失/失败/不适用从不算通过。
2. 每条 pass/fail 判定都带证据：数值、单位、来源接口、字段、数据时点。
3. 抽样非派生证据，按证据里的 endpoint + params + field 重新请求扶摇，比对数值。
4. 抽样价格派生指标（区间涨跌、波动率、回撤），用重新拉取的后复权日K独立重算并比对。
5. 摘要若由 AI 生成，必须标记已通过数字校验。
6. 数据集状态汇总。
7. 跨数据源：估算市值与 iFinD 总市值比对。

用法：cd backend && uv run python ../scripts/verify_traceability.py [--base http://localhost:8000] [--query 题面示例] [--sample 40]
输出：控制台 + docs/VERIFICATION_RUN.md（只含统计与少量样例，不落盘原始数据）
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import get_settings  # noqa: E402

S = get_settings()


def close_enough(a: float, b: float, rel: float = 1e-6, abs_: float = 1e-6) -> bool:
    return math.isclose(a, b, rel_tol=rel, abs_tol=abs_)


def flat_indicators(data: dict) -> dict[str, str | None]:
    return {i["index_id"]: i["value"] for a in data.get("abilities") or [] for i in a.get("indicators") or []}


async def refetch_raw(fy: httpx.AsyncClient, src: dict, code: str):
    """按证据来源重新取原始字段值。"""
    endpoint, field, params = src.get("endpoint"), src.get("field"), dict(src.get("params") or {})
    if not endpoint or not field:
        return None, "证据缺少 endpoint/field"
    r = await fy.get(endpoint, params=params)
    body = r.json()
    if body.get("code") != 0:
        return None, f"重取失败 code={body.get('code')} {body.get('message')}"
    data = body.get("data") or {}
    if "abilities" in data:
        v = flat_indicators(data).get(field)
        return (None if v is None else float(v)), None
    for it in data.get("item") or []:
        if it.get("thscode") in (None, code):
            v = it.get(field)
            return (None if v is None else float(v)), None
    return None, "重取结果中找不到该股票"


async def recompute_price_metric(fy: httpx.AsyncClient, code: str, metric: str, src: dict) -> float | None:
    params = dict(src.get("params") or {})
    if not params.get("start"):
        return None
    r = await fy.get("/api/a-share/prices/historical", params={**params, "thscode": code, "interval": "1d", "adjust": "backward"})
    items = (r.json().get("data") or {}).get("item") or []
    closes = [x["close_price"] for x in items if x.get("close_price") is not None]
    if metric.startswith("return_"):
        n = int(metric.split("_")[1].rstrip("d"))
        return (closes[-1] / closes[-1 - n] - 1) * 100 if len(closes) > n else None
    window = closes[-251:]
    if metric == "volatility_250d":
        rets = [math.log(window[i] / window[i - 1]) for i in range(1, len(window))]
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((x - mu) ** 2 for x in rets) / (len(rets) - 1))
        return sd * math.sqrt(252) * 100
    if metric == "max_drawdown_250d":
        peak, mdd = window[0], 0.0
        for p in window:
            peak = max(peak, p)
            mdd = max(mdd, 1 - p / peak)
        return mdd * 100
    return None


def _cn_amount_to_yi(text: str) -> float | None:
    """'1.6614万亿' / '2231.6806亿' / '12345678元' → 亿元。"""
    t = text.strip().replace(",", "")
    try:
        if t.endswith("万亿"):
            return float(t[:-2]) * 1e4
        if t.endswith("亿"):
            return float(t[:-1])
        if t.endswith("万"):
            return float(t[:-1]) / 1e4
        return float(t.rstrip("元")) / 1e8
    except ValueError:
        return None


async def ifind_market_caps(names: list[str]) -> dict[str, float]:
    """用 iFinD get_security_indicators 独立取“最新总市值”（亿元），返回 {thscode: 值}。"""
    if not S.ifind_mcp_token:
        return {}
    h = {"Authorization": S.ifind_mcp_token, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(S.ifind_mcp_url, headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "verify", "version": "0.1"}}})
        if r.headers.get("mcp-session-id"):
            h["Mcp-Session-Id"] = r.headers["mcp-session-id"]
        await c.post(S.ifind_mcp_url, headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = await c.post(S.ifind_mcp_url, headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "get_security_indicators", "arguments": {"market": "A股", "query": " ".join(names) + " 最新总市值"}}})
        body = r.json() if "json" in r.headers.get("content-type", "") else {}
        try:
            import json as _json
            inner = _json.loads(body["result"]["content"][0]["text"])
            table = inner["data"]["answer"]
        except Exception:
            return {}
        out: dict[str, float] = {}
        for line in table.splitlines():
            cells = [x.strip() for x in line.strip().strip("|").split("|")]
            if len(cells) >= 3 and cells[0][:6].isdigit():
                v = _cn_amount_to_yi(cells[2])
                if v is not None:
                    out[cells[0]] = v
        return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--query", default="经营改善、估值合理、走势相对稳定")
    ap.add_argument("--sample", type=int, default=40)
    ap.add_argument("--seed", type=int, default=20261017)
    args = ap.parse_args()
    random.seed(args.seed)
    lines: list[str] = []

    def out(s: str = ""):
        print(s)
        lines.append(s)

    async with httpx.AsyncClient(base_url=args.base, timeout=60) as app, \
            httpx.AsyncClient(base_url=S.fuyao_base_url, headers={"X-api-key": S.fuyao_api_key or ""}, timeout=30) as fy:
        t0 = time.perf_counter()
        parsed = (await app.post("/api/parse", json={"query": args.query})).json()
        strategy = parsed["strategy"]
        out(f"# 可追溯性核验报告\n\n- 运行时间：{datetime.now():%Y-%m-%d %H:%M}\n- 查询：{args.query}\n- 解析器：{parsed.get('parser')}（{parsed.get('llm_model') or '无'}）")
        out(f"- 条件：" + "；".join(c.get("label") or c["metric"] for c in strategy["conditions"] if c.get("enabled", True)))
        run_id = (await app.post("/api/screen", json={"strategy": strategy})).json()["run_id"]
        while True:
            lite = (await app.get(f"/api/screen/{run_id}", params={"lite": 1})).json()
            if lite["status"] in ("done", "failed"):
                break
            await asyncio.sleep(1.5)
        res = (await app.get(f"/api/screen/{run_id}")).json()
        out(f"- 运行：{run_id}，状态 {res['status']}，耗时 {time.perf_counter() - t0:.1f} 秒")
        if res["status"] != "done":
            out(f"- 失败原因：{res.get('error')}")
            return
        u = res["universe"]
        out(f"- 股票池：{u['name']} {u['size']} 只；入选 {len(res['included'])}、排除 {len(res['excluded'])}、缺失 {len(res['missing'])}")
        out(f"- 报告期：{(res.get('report_period') or {}).get('label')}；价格截至 {res.get('trade_date')}")

        hard = {c["id"] for c in res["strategy"]["conditions"] if c.get("enabled", True) and c.get("hard", True)}
        policy = res["strategy"].get("missing_policy", "exclude")
        bad_incl = [s["thscode"] for s in res["included"]
                    if any(e["condition_id"] in hard and e["status"] != "pass" and not (policy == "keep_flagged" and e["status"] in ("missing", "not_applicable"))
                           for e in s["evaluations"])]
        out(f"\n## 1. 入选判定一致性\n- 入选股票中存在非通过的硬条件：{len(bad_incl)} 只 {'✓' if not bad_incl else '✗ ' + ','.join(bad_incl[:10])}")

        evals = [(s["thscode"], e) for b in ("included", "excluded", "missing") for s in res[b] for e in s["evaluations"]]
        judged = [(c, e) for c, e in evals if e["status"] in ("pass", "fail")]
        no_ev = [(c, e["condition_id"]) for c, e in judged
                 if not e.get("evidence") or not e["evidence"].get("sources") or e["evidence"].get("unit") is None]
        out(f"\n## 2. 证据完整性\n- pass/fail 判定 {len(judged)} 条，缺少证据或来源的 {len(no_ev)} 条 {'✓' if not no_ev else '✗'}")

        raw = [(c, e) for c, e in judged if e["evidence"] and not e["evidence"].get("derived")
               and any(s.get("provider") == "fuyao" and s.get("field") for s in e["evidence"]["sources"])]
        sample = random.sample(raw, min(args.sample, len(raw)))
        ok = mism = err = 0
        examples = []
        for code, e in sample:
            src = next(s for s in e["evidence"]["sources"] if s.get("provider") == "fuyao" and s.get("field"))
            v, why = await refetch_raw(fy, src, code)
            mine = e["evidence"]["value"]
            if why:
                err += 1
                examples.append(f"| {code} | {src['field']} | {mine} | — | {why} |")
            elif v is not None and isinstance(mine, (int, float)) and close_enough(float(mine), v, rel=1e-4, abs_=1e-4):
                ok += 1
                if len(examples) < 6:
                    examples.append(f"| {code} | {src['field']} | {mine} | {v} | 一致 |")
            else:
                mism += 1
                examples.append(f"| {code} | {src['field']} | {mine} | {v} | **不一致** |")
        out(f"\n## 3. 原始字段重取比对（抽样 {len(sample)} / {len(raw)}）\n- 一致 {ok}，不一致 {mism}，重取失败 {err}")
        out("\n| 股票 | 字段 | 产品证据值 | 直接重取值 | 结论 |\n|---|---|---|---|---|")
        for x in examples:
            out(x)

        price = [(c, e) for c, e in judged if e["evidence"] and e["evidence"].get("metric") in ("return_20d", "return_60d", "return_250d", "volatility_250d", "max_drawdown_250d")]
        psample = random.sample(price, min(10, len(price)))
        pok = pbad = 0
        for code, e in psample:
            src = next((s for s in e["evidence"]["sources"] if s.get("endpoint", "").endswith("/prices/historical")), None)
            for inp in e["evidence"].get("inputs") or []:
                src = src or next((s for s in inp.get("sources") or [] if s.get("endpoint", "").endswith("/prices/historical")), None)
            v = await recompute_price_metric(fy, code, e["evidence"]["metric"], src or {}) if src else None
            if v is not None and close_enough(float(e["evidence"]["value"]), v, rel=1e-3, abs_=0.05):
                pok += 1
            else:
                pbad += 1
                out(f"- 价格指标重算不一致：{code} {e['evidence']['metric']} 产品={e['evidence']['value']} 重算={v}")
        out(f"\n## 4. 价格派生指标独立重算（抽样 {len(psample)}）\n- 一致 {pok}，不一致/无法重算 {pbad}")

        summ = res.get("summary") or {}
        out(f"\n## 5. 摘要\n- 生成方式：{summ.get('generator')}，数字校验：{summ.get('verified')}；备注：{'；'.join(summ.get('verification_notes') or []) or '无'}")
        ds = [f"{d['label']}={d['status']}({d['succeeded']}/{d['requested']})" for d in res["datasets"]]
        out(f"\n## 6. 数据集状态\n- " + "；".join(ds))

        # 7. 跨数据源：估算市值 vs iFinD 总市值（加一个只计分不过滤的软条件，让引擎为入选股票计算估算市值）
        strat2 = dict(res["strategy"])
        strat2["conditions"] = list(strat2["conditions"]) + [{"metric": "market_cap_est", "op": "gte", "value": 0, "hard": False, "origin": "user"}]
        rid2 = (await app.post("/api/screen", json={"strategy": strat2})).json()["run_id"]
        while (await app.get(f"/api/screen/{rid2}", params={"lite": 1})).json()["status"] not in ("done", "failed"):
            await asyncio.sleep(1.5)
        res2 = (await app.get(f"/api/screen/{rid2}")).json()
        caps = {}
        for s_ in res2.get("included", [])[:5]:
            for e in s_["evaluations"]:
                if e.get("evidence") and e["evidence"].get("metric") == "market_cap_est" and isinstance(e["evidence"].get("value"), (int, float)):
                    caps[s_["thscode"]] = (s_.get("name"), float(e["evidence"]["value"]))
        ref = await ifind_market_caps([n for n, _ in caps.values() if n])
        out(f"\n## 7. 跨数据源核验：估算市值 vs iFinD 总市值（{len(caps)} 只）")
        if not ref:
            out("- iFinD 未配置或调用失败，跳过")
        else:
            out("| 股票 | 产品估算（亿元） | iFinD 总市值（亿元） | 相对差 |\n|---|---|---|---|")
            for code, (name, mine) in caps.items():
                theirs = ref.get(code)
                diff = f"{(mine / theirs - 1) * 100:+.2f}%" if theirs else "—"
                out(f"| {name} {code} | {mine:.1f} | {theirs:.1f} | {diff} |" if theirs else f"| {name} {code} | {mine:.1f} | — | — |")

    (ROOT / "docs" / "VERIFICATION_RUN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
