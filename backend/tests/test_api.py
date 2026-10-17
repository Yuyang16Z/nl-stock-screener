"""API 集成测试：假 Repository（合成快照，数值虚构）+ llm=None，不发出任何真实网络请求。"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from app import catalog
from app.config import Settings
from app.data.fuyao import FuyaoResponse
from app.main import create_app
from app.schemas import DatasetStatus, FeatureSnapshot, ResolvedUniverse, UniverseOption
from tests._fixtures_engine import _GENERATORS, error, synthetic_snapshot

CN = timezone(timedelta(hours=8))
N_STOCKS = 12


class FakeRepo:
    """只实现 services 用到的接口；build_snapshot 返回固定种子的合成快照，故障数据集对应指标为 error。"""

    def __init__(self) -> None:
        self.build_calls = 0

    async def resolve_universe(self, u, *, faults=frozenset()):  # noqa: ANN001
        snap = synthetic_snapshot(N_STOCKS, metrics=["pe_ttm"])
        return ResolvedUniverse(kind=u.kind, code=u.code, name=u.name or "测试股票池", size=N_STOCKS, members=sorted(snap.stocks))

    async def search_universes(self, q: str):
        return [UniverseOption(kind="index", code="000300.SH", name="沪深300", group="常用指数")]

    async def trading_days(self, *, faults=frozenset()):  # noqa: ANN001
        return FuyaoResponse(endpoint="/api/a-share/calendar/trading-days", params={}, data={"item": []}, fetched_at=datetime.now(CN))

    async def history(self, code, start, end, adjust="backward", *, faults=frozenset()):  # noqa: ANN001
        d0 = date(2026, 1, 5)
        items = [
            {"date_ms": int(datetime(d.year, d.month, d.day, tzinfo=CN).timestamp() * 1000), "close_price": 10 + i * 0.1,
             "turnover": 1e8, "volume": 1e6}
            for i, d in enumerate(d0 + timedelta(days=k) for k in range(30))
        ]
        return FuyaoResponse(endpoint="/api/a-share/prices/historical", params={"thscode": code}, data={"item": items},
                             request_id="req-fake", fetched_at=datetime.now(CN))

    async def build_snapshot(self, strategy, *, faults=frozenset(), progress=None, metrics=None, members=None, base=None):  # noqa: ANN001
        self.build_calls += 1
        if progress:
            progress(0.5, "合成取数")
        enabled = {c.metric for c in strategy.conditions if c.enabled}
        want = set(metrics) if metrics is not None else enabled | set(catalog.DISPLAY_METRICS)
        if base is not None:
            want |= set(base.metrics_available)
        want = sorted(m for m in want if m in _GENERATORS)
        snap = synthetic_snapshot(N_STOCKS, metrics=want, missing_rate=0.0, evidence=True)
        datasets = []
        for ds in sorted(catalog.datasets_for(want)):
            label, endpoint = catalog.DATASETS[ds]
            if ds in faults:
                datasets.append(DatasetStatus(dataset=ds, label=label, endpoint=endpoint, status="failed",
                                              requested=N_STOCKS, failed=N_STOCKS, errors=["【演示故障注入】模拟不可用"]))
            else:
                datasets.append(DatasetStatus(dataset=ds, label=label, endpoint=endpoint, status="ok",
                                              requested=N_STOCKS, succeeded=N_STOCKS))
        for st in snap.stocks.values():
            for m in list(st.metrics):
                if set(catalog.METRICS[m].datasets) & set(faults):
                    st.metrics[m] = error(m)
        return FeatureSnapshot(**{**snap.model_dump(), "datasets": datasets, "metrics_available": want})


def make_settings(tmp_path) -> Settings:  # noqa: ANN001
    return Settings(
        _env_file=None, fuyao_api_key="test-key-not-real", llm_api_key=None, ifind_mcp_token=None,
        data_dir=str(tmp_path), demo_faults_enabled=True, warm_universes="", max_deep_eval=800,
    )


@pytest.fixture
async def client(tmp_path):  # noqa: ANN001
    app = create_app(make_settings(tmp_path), repository=FakeRepo(), llm=None, ifind=None, background=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as c:
            yield c


def strategy_body(**extra) -> dict:  # noqa: ANN003
    body = {
        "name": "测试策略",
        "universe": {"kind": "index", "code": "000300.SH", "name": "沪深300", "codes": []},
        "conditions": [
            {"metric": "pe_ttm", "op": "pct_rank_lte", "value": 60, "rank_scope": "universe"},
            {"metric": "revenue_yoy", "op": "gt", "value": -50},
            {"metric": "volatility_250d", "op": "lt", "value": 70},
            {"id": "sys_st", "metric": "is_st", "op": "is_false", "origin": "system"},
        ],
        "missing_policy": "exclude",
    }
    body.update(extra)
    return body


async def run_screen(client: httpx.AsyncClient, strategy: dict, simulate: list[str] | None = None) -> dict:
    r = await client.post("/api/screen", json={"strategy": strategy, "simulate_failures": simulate or []})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    for _ in range(200):
        lite = (await client.get(f"/api/screen/{run_id}?lite=1")).json()
        assert lite["included"] == []
        if lite["status"] in ("done", "failed"):
            break
        await asyncio.sleep(0.02)
    full = (await client.get(f"/api/screen/{run_id}")).json()
    return full


async def test_health_catalog(client: httpx.AsyncClient) -> None:
    h = (await client.get("/api/health")).json()
    names = {s["name"]: s for s in h["services"]}
    assert names["扶摇"]["ok"] is True and h["ok"] is True
    assert names["AI"]["configured"] is False and names["iFinD"]["configured"] is False
    cat = (await client.get("/api/catalog")).json()
    assert len(cat["metrics"]) == len(catalog.METRICS) and cat["disclaimer"]
    u = (await client.get("/api/universes/search", params={"q": "沪深"})).json()
    assert u and u[0]["code"] == "000300.SH"


async def test_parse_rule_fallback_and_validate(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/parse", json={"query": "经营改善、估值合理、走势相对稳定"})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["parser"] == "rule_fallback" and res["intent"] == "screen" and res["strategy"]["conditions"]
    v = (await client.post("/api/strategy/validate", json=res["strategy"])).json()
    assert v["can_run"] is True and all(c["id"] for c in v["strategy"]["conditions"])
    bad = strategy_body(conditions=[{"metric": "pe_ttm", "op": "gt", "value": 30}, {"metric": "pe_ttm", "op": "lt", "value": 10}])
    v2 = (await client.post("/api/strategy/validate", json=bad)).json()
    assert v2["can_run"] is False


async def test_screen_whatif_detail_compare(client: httpx.AsyncClient) -> None:
    res = await run_screen(client, strategy_body())
    assert res["status"] == "done", res.get("error")
    total = len(res["included"]) + len(res["excluded"]) + len(res["missing"])
    assert total == N_STOCKS and res["summary"]["sentences"] and res["funnel"]
    assert {s["condition_id"] for s in res["condition_stats"]} >= {"sys_st"}
    run_id = res["run_id"]

    loose = strategy_body()
    loose["conditions"] = [c for c in loose["conditions"] if c["metric"] != "volatility_250d"]
    w = await client.post(f"/api/screen/{run_id}/whatif", json=loose)
    assert w.status_code == 200, w.text
    wj = w.json()
    assert wj["result"]["status"] == "done" and len(wj["result"]["included"]) >= len(res["included"])
    assert wj["diff"]["changes_text"]

    code = (res["included"] or res["excluded"])[0]["thscode"]
    d = await client.get(f"/api/screen/{run_id}/stocks/{code}")
    assert d.status_code == 200, d.text
    dj = d.json()
    assert dj["info"]["thscode"] == code and dj["prices"] and dj["evaluations"]
    assert (await client.get(f"/api/screen/{run_id}/stocks/999999.SH")).status_code == 404

    e = await client.post(f"/api/screen/{run_id}/explain/{code}")
    assert e.status_code == 200 and e.json()["sentences"]

    codes = [s["thscode"] for s in (res["included"] + res["excluded"])[:3]]
    cmp_ = await client.post("/api/compare", json={"run_id": run_id, "thscodes": codes})
    assert cmp_.status_code == 200, cmp_.text
    cj = cmp_.json()
    assert len(cj["stocks"]) == 3 and any(r["metric"] == "pe_ttm" for r in cj["rows"])
    assert all(abs(pts[0]["close"] - 100) < 1e-6 for pts in cj["prices"].values())

    assert (await client.get("/api/screen/nope")).status_code == 404


async def test_simulated_failure_is_missing_not_pass(client: httpx.AsyncClient) -> None:
    res = await run_screen(client, strategy_body(), simulate=["valuations"])
    assert res["status"] == "done", res.get("error")
    assert res["warnings"][0].startswith("本次为故障演示")
    assert any("估值快照" in w and "失败" in w for w in res["warnings"])
    pe_id = next(c["id"] for c in res["strategy"]["conditions"] if c["metric"] == "pe_ttm")
    for sr in res["included"] + res["excluded"] + res["missing"]:
        ev = next(e for e in sr["evaluations"] if e["condition_id"] == pe_id)
        assert ev["status"] != "pass"
    assert res["included"] == []
    assert {ds["dataset"]: ds["status"] for ds in res["datasets"]}["valuations"] == "failed"


async def test_simulate_failures_rejected_when_disabled(tmp_path) -> None:  # noqa: ANN001
    s = make_settings(tmp_path).model_copy(update={"demo_faults_enabled": False})
    app = create_app(s, repository=FakeRepo(), llm=None, ifind=None, background=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/screen", json={"strategy": strategy_body(), "simulate_failures": ["valuations"]})
            assert r.status_code == 400 and "故障演示" in r.json()["detail"]


async def test_conflict_run_fails_with_message(client: httpx.AsyncClient) -> None:
    bad = strategy_body(conditions=[{"metric": "pe_ttm", "op": "gt", "value": 30}, {"metric": "pe_ttm", "op": "lt", "value": 10}])
    res = await run_screen(client, bad)
    assert res["status"] == "failed" and "冲突" in res["error"]


async def test_strategies_crud(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/strategies", json={"name": "我的策略", "strategy": strategy_body()})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert len(sid) == 12
    assert [s["id"] for s in (await client.get("/api/strategies")).json()] == [sid]
    assert (await client.get(f"/api/strategies/{sid}")).json()["name"] == "我的策略"
    assert (await client.delete(f"/api/strategies/{sid}")).status_code == 200
    g = await client.get(f"/api/strategies/{sid}")
    assert g.status_code == 404 and g.json()["detail"]


async def test_monitor_create_and_run(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/monitors", json={"name": "每日监控", "strategy": strategy_body(), "frequency": "daily"})
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["id"] and m["next_run_at"]
    run_id = (await client.post(f"/api/monitors/{m['id']}/run")).json()["run_id"]
    entry = None
    for _ in range(200):
        mon = (await client.get(f"/api/monitors/{m['id']}")).json()
        entry = next((x for x in mon["runs"] if x["run_id"] == run_id), None)
        if entry and entry["status"] in ("done", "failed"):
            break
        await asyncio.sleep(0.02)
    assert entry is not None and entry["status"] == "done", entry
    assert entry["included_count"] is not None
    run2 = (await client.post(f"/api/monitors/{m['id']}/run")).json()["run_id"]
    for _ in range(200):
        mon = (await client.get(f"/api/monitors/{m['id']}")).json()
        entry = next((x for x in mon["runs"] if x["run_id"] == run2), None)
        if entry and entry["status"] in ("done", "failed"):
            break
        await asyncio.sleep(0.02)
    assert entry["status"] == "done" and entry["diff"] is not None and entry["diff"]["base_run_id"] == run_id
    assert (await client.post("/api/monitors/nope/run")).status_code == 404
    assert (await client.delete(f"/api/monitors/{m['id']}")).status_code == 200
