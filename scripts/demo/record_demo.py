"""自动录制演示视频：Playwright 驱动真实页面，逐步叠加两行字幕（区域名称 + 功能说明）并高亮当前区域。

依赖（不进项目依赖）：pip install playwright imageio-ffmpeg && python -m playwright install chromium
字幕：subtitles.json（cap=标题，sub=说明）；每步停留时间按阅读速度（约 6 字/秒）计算。
用法：python record_demo.py <BASE_URL> <OUT_DIR>
输出：<OUT_DIR>/*.webm（录屏）与 <OUT_DIR>/timeline.json（每步开始时间，秒）。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import Locator, Page, sync_playwright

BASE, OUT = sys.argv[1].rstrip("/"), Path(sys.argv[2])
_SUBS = json.load(open(Path(__file__).resolve().parent / "subtitles.json", encoding="utf-8"))
for _s in _SUBS:  # 阅读时长：说明约 7.5 字/秒 + 标题扫读，另留 0.8 秒
    _s["dur"] = round(max(3.5, (len(_s["cap"]) * 0.4 + len(_s["sub"])) / 7.5 + 0.8), 2)
STEPS = {s["id"]: s for s in _SUBS}
ORDER = [s["id"] for s in _SUBS]
W, H = 1440, 900

OVERLAY = """
(() => {
  const mk = () => {
    if (document.getElementById('__cap')) return;
    const css = (el, s) => Object.assign(el.style, s);
    const cap = document.createElement('div'); cap.id = '__cap';
    css(cap, {position:'fixed', left:'50%', bottom:'22px', transform:'translateX(-50%)', width:'1180px', padding:'12px 26px 14px',
      background:'rgba(15,23,42,.92)', color:'#fff', font:'400 20px/1.55 "PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif',
      borderRadius:'12px', zIndex:2147483647, boxShadow:'0 8px 28px rgba(0,0,0,.28)', textAlign:'center', pointerEvents:'none'});
    const hl = document.createElement('div'); hl.id = '__hl';
    css(hl, {position:'fixed', border:'3px solid #f59e0b', borderRadius:'10px', boxShadow:'0 0 0 9999px rgba(15,23,42,.16)',
      zIndex:2147483646, pointerEvents:'none', transition:'all .35s ease', display:'none'});
    const st = document.createElement('div'); st.id = '__step';
    css(st, {position:'fixed', right:'18px', top:'74px', padding:'4px 12px', background:'#1f4fd8', color:'#fff',
      font:'600 14px "PingFang SC",sans-serif', borderRadius:'999px', zIndex:2147483647, pointerEvents:'none'});
    document.body.append(cap, hl, st);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mk); else mk();
})();
"""

CARD = """
([title, sub]) => {
  let c = document.getElementById('__card');
  if (!c) { c = document.createElement('div'); c.id = '__card'; document.body.appendChild(c); }
  Object.assign(c.style, {position:'fixed', inset:'0', zIndex:2147483645, display:'flex', flexDirection:'column', alignItems:'center',
    justifyContent:'center', background:'linear-gradient(135deg,#0f2a6b 0%,#1f4fd8 60%,#3b82f6 100%)', color:'#fff',
    font:'600 22px "PingFang SC","Microsoft YaHei",sans-serif', textAlign:'center', gap:'18px'});
  c.innerHTML = `<div style="font-size:52px;font-weight:700;letter-spacing:1px">${title}</div>
    <div style="font-size:24px;opacity:.92;max-width:1000px;line-height:1.6">${sub}</div>
    <div style="font-size:16px;opacity:.75;margin-top:18px">扶摇（同花顺金融数据 API）· iFinD MCP · DeepSeek ｜ 仅供研究学习，不构成投资建议</div>`;
}
"""

timeline: list[dict] = []
T0 = 0.0


def cap(page: Page, sid: str) -> None:
    i = ORDER.index(sid) + 1
    page.evaluate("([t, b, s]) => { const c = document.getElementById('__cap'); if (c) { c.innerHTML = '';"
                  " const h = document.createElement('div'); h.textContent = t; Object.assign(h.style, {font: '700 23px/1.4 inherit', color: '#fbbf24', marginBottom: '4px'});"
                  " const d = document.createElement('div'); d.textContent = b; c.append(h, d); }"
                  " const e = document.getElementById('__step'); if (e) e.textContent = s; }",
                  [STEPS[sid]["cap"], STEPS[sid]["sub"], f"{i}/{len(ORDER)}"])


def hl(page: Page, loc: Locator, pad: int = 6) -> None:
    try:
        loc.scroll_into_view_if_needed(timeout=4000)
        page.wait_for_timeout(250)
        b = loc.bounding_box()
        if b:
            page.evaluate("([x,y,w,h]) => { const e = document.getElementById('__hl'); if (e) Object.assign(e.style,"
                          " {display:'block', left:x+'px', top:y+'px', width:w+'px', height:h+'px'}); }",
                          [b["x"] - pad, max(b["y"] - pad, 2), b["width"] + 2 * pad, min(b["height"] + 2 * pad, H - 8)])
    except Exception as e:  # noqa: BLE001 — 高亮失败不影响录制
        print("highlight failed:", e)


def unhl(page: Page) -> None:
    page.evaluate("() => { const e = document.getElementById('__hl'); if (e) e.style.display = 'none'; }")


def card(page: Page, show: bool, title: str = "", sub: str = "") -> None:
    if show:
        page.evaluate(CARD, [title, sub])
    else:
        page.evaluate("() => { const c = document.getElementById('__card'); if (c) c.remove(); }")


def step(page: Page, sid: str, fn=None, extra: float = 0.5) -> None:
    t = time.monotonic()
    timeline.append({"id": sid, "t": round(t - T0, 3)})
    cap(page, sid)
    if fn is not None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — 单步失败继续录后面的
            print(f"[{sid}] action failed:", e)
    remain = STEPS[sid]["dur"] + extra - (time.monotonic() - t)
    if remain > 0:
        page.wait_for_timeout(remain * 1000)


def card_of(page: Page, text: str) -> Locator:
    # 卡片可能嵌套（外层结果卡包含内层卡），取最内层
    return page.locator(".ant-card").filter(has=page.get_by_text(text, exact=True)).last


def main() -> None:
    global T0
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": W, "height": H}, record_video_dir=str(OUT), record_video_size={"width": W, "height": H},
                                  locale="zh-CN", timezone_id="Asia/Shanghai")
        ctx.add_init_script(OVERLAY)
        page = ctx.new_page()
        T0 = time.monotonic()
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(600)

        def title() -> None:
            card(page, True, "ScreenLens 选股解释器",
                 "自然语言意图 → 可检查、可修改、可执行的数据条件<br>确定性引擎在真实数据上逐条判定，每个数字都可追溯")
        step(page, "title", title)
        card(page, False)

        step(page, "header", lambda: hl(page, page.locator("header").first))

        def input_() -> None:
            hl(page, page.locator("textarea[aria-label='选股描述']"), pad=10)
            page.wait_for_timeout(1200)
            page.get_by_text("经营改善、估值合理、走势相对稳定", exact=True).first.click()
            page.wait_for_timeout(700)
            page.get_by_role("button", name="解析意图").click()
            page.get_by_text("需要你确认的口径").first.wait_for(timeout=60000)
        step(page, "input", input_)

        step(page, "understand", lambda: hl(page, card_of(page, "AI 理解")))

        def clarify() -> None:
            sec = page.get_by_text("需要你确认的口径").first
            hl(page, sec.locator("xpath=ancestor::div[1]"))
            page.wait_for_timeout(1500)
            alt = page.get_by_text("年化波动率 ≤ 30% 且近250日最大回撤 ≤ 25%", exact=True).first
            alt.scroll_into_view_if_needed()
            hl(page, alt.locator("xpath=ancestor::label[1]"))
            alt.click()
            page.wait_for_timeout(2600)
            page.get_by_text("近250日年化波动率处于股票池后 50%", exact=True).first.click()
        step(page, "clarify", clarify)

        step(page, "conditions", lambda: hl(page, card_of(page, "筛选条件").locator(".ant-table").first))

        def run() -> None:
            btn = page.get_by_role("button", name="执行筛选")
            hl(page, btn, pad=8)
            btn.click()
            page.get_by_text("结果解读").first.wait_for(timeout=180000)
        step(page, "run", run)

        def overview() -> None:
            unhl(page)
            hl(page, card_of(page, "筛选结果"))
        step(page, "overview", overview)

        step(page, "summary", lambda: hl(page, page.get_by_text("结果解读").first.locator("xpath=..")))

        def table() -> None:
            t = page.locator(".ant-table").first
            hl(page, t)
        step(page, "table", table)

        def drawer() -> None:
            unhl(page)
            page.get_by_role("button", name="依据").first.click()
            d = page.locator(".ant-drawer-content").first
            d.wait_for(timeout=30000)
            page.wait_for_timeout(2500)
            hl(page, d, pad=0)
            page.wait_for_timeout(3500)
            box = d.bounding_box()
            if box:
                page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                for _ in range(8):
                    page.mouse.wheel(0, 420)
                    page.wait_for_timeout(350)
        step(page, "drawer", drawer)
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        unhl(page)

        def impact() -> None:
            page.get_by_role("tab", name="条件影响").click()
            page.wait_for_timeout(800)
            hl(page, card_of(page, "筛选漏斗"))
            page.wait_for_timeout(3500)
            hl(page, card_of(page, "逐条件影响"))
        step(page, "impact", impact)

        def whatif() -> None:
            row = (page.locator("tr").filter(has_text="年化波动率").filter(has_text="在股票池内分位")
                   .filter(has=page.locator("button").filter(has_text="试算去掉")).first)
            btn = row.locator("button").filter(has_text="试算去掉").first
            hl(page, row)
            page.wait_for_timeout(1200)
            btn.click()
            page.wait_for_timeout(2500)
            hl(page, card_of(page, "试试别的阈值"))
        step(page, "whatif", whatif)

        def provenance() -> None:
            page.get_by_role("tab", name="数据口径与时点").click()
            page.wait_for_timeout(800)
            hl(page, card_of(page, "数据集与接口状态"))
        step(page, "provenance", provenance)

        def backtest() -> None:
            bar = page.get_by_role("button", name="保存策略").locator("xpath=ancestor::div[contains(@class,'ant-space') or contains(@class,'ant-card-extra')][1]")
            hl(page, bar)
            page.get_by_role("button", name="保存策略").click()
            page.get_by_role("button", name="保存", exact=True).click()
            page.wait_for_timeout(900)
            page.get_by_role("button", name="转为监控").click()
            page.get_by_role("button", name="创建监控").click()
            page.wait_for_timeout(900)
            unhl(page)
            page.locator("button").filter(has_text=re.compile(r"^\s*回测\s*$")).first.click()
            page.get_by_role("button", name="开始回测").click()
            page.get_by_text("累计收益").first.wait_for(timeout=180000)
            page.wait_for_timeout(1200)
            hl(page, page.locator(".ant-drawer-content").first, pad=0)
            page.wait_for_timeout(1500)
        step(page, "backtest", backtest)
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)
        unhl(page)

        def fault() -> None:
            page.get_by_text("异常演示（故障注入）").first.click()
            page.wait_for_timeout(600)
            box = page.get_by_text("模拟估值接口失败(valuations)").first
            hl(page, box.locator("xpath=ancestor::div[contains(@class,'ant-collapse-item')][1]"))
            box.click()
            page.wait_for_timeout(800)
            page.get_by_role("button", name="执行筛选").click()
            page.get_by_text("本次为故障演示").first.wait_for(timeout=180000)
            page.wait_for_timeout(800)
            hl(page, page.get_by_text("本次为故障演示").first.locator("xpath=ancestor::div[contains(@class,'ant-alert')][1]"))
        step(page, "fault", fault)
        unhl(page)

        def compliance() -> None:
            ta = page.locator("textarea[aria-label='选股描述']")
            ta.scroll_into_view_if_needed()
            ta.fill("明天哪只股票会涨？")
            page.get_by_role("button", name="解析意图").click()
            page.wait_for_timeout(2500)
            hl(page, card_of(page, "AI 理解"))
        step(page, "compliance", compliance)
        unhl(page)

        def library() -> None:
            page.get_by_role("link", name="我的策略").first.click()
            page.wait_for_timeout(1500)
            hl(page, page.locator(".ant-card, .ant-tabs").first)
            page.wait_for_timeout(2200)
            page.get_by_role("tab", name="监控任务").first.click()
        step(page, "library", library)
        unhl(page)

        def end() -> None:
            card(page, True, "AI 理解与解释 · 引擎判定 · 数据工具负责事实",
                 "https://screenlens-fkko.onrender.com<br>github.com/Yuyang16Z/nl-stock-screener")
        step(page, "end", end, extra=1.2)

        video = page.video.path() if page.video else None
        ctx.close()
        browser.close()
    total = round(time.monotonic() - T0, 2)
    (OUT / "timeline.json").write_text(json.dumps({"steps": timeline, "total": total, "video": str(video)}, ensure_ascii=False, indent=1))
    print("video:", video, "total:", total)


if __name__ == "__main__":
    main()
