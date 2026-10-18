"""筛选结果解释（DESIGN §6.4）：模板解释（确定性）+ LLM 解释（数字校验 + 合规校验，不通过回退模板）。

句子类型：直接引用字段数值 = fact；基于分位/阈值的定性判断 = inference；缺失/不适用 = uncertain；口径与局限 = caveat。

LLM 解释的确定性后处理：
- 运行摘要：不把数据集状态交给 LLM，LLM 写到数据集状态的句子直接删除；数据集“部分失败/获取失败”句由模板按运行记录生成后追加；
- 个股解读：事实句缺报告期/时点时，按 refs 指向的条件证据在句末补“（期间；来源：接口·字段）”。
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, get_args

from .. import catalog
from ..engine.conditions import SCOPE_SHORT, describe, fmt_num, normalize
from ..schemas import (
    CondEval,
    Condition,
    Evidence,
    Explanation,
    ExplanationSentence,
    ScreenResult,
    SentenceKind,
    SourceRef,
    StockResult,
    Strategy,
)
from . import compliance, prompts

__all__ = [
    "extract_numbers",
    "llm_run_summary",
    "llm_stock_explanation",
    "template_run_summary",
    "template_stock_explanation",
]

LLM_TIMEOUT_S = 20.0
_KINDS: frozenset[str] = frozenset(get_args(SentenceKind))
_BUCKET_LABEL = {"included": "入选", "excluded": "排除", "missing": "数据缺失"}
_PROVIDER_LABEL = {"fuyao": "扶摇", "ifind": "iFinD", "derived": "派生计算", "user": "用户", "system": "系统"}
_STATUS_LABEL = {"pass": "通过", "fail": "未通过", "missing": "数据缺失", "not_applicable": "不适用", "skipped": "未参与判定"}
_DS_STATUS_LABEL = {"partial": "部分失败", "failed": "获取失败", "skipped": "已跳过"}
_STOCK_CAVEAT = "以上判定只基于所列数据条件与对应报告期/时点的数据，不代表未来表现，不构成投资建议。"
_SUMMARY_CAVEAT = "以上统计只描述本次条件在当前数据上的筛选结果，不预测股价表现，不构成投资建议。"
_FAIL_PREFIX = "AI 文本未通过数字校验，已改用模板："
_CALL_FAIL_PREFIX = "AI 文本生成失败，已改用模板："
# 校验范围：只核对数字是否出现在证据中，不核对每句定性表述（比较、归因）
VERIFIED_NOTE = "文中数字均出现在引擎证据中（不代表每句定性表述都经逐条核对）"
_DATASET_NOTE = "数据集状态句由系统按运行记录生成（非 AI 撰写）"
# 运行摘要里关于数据集/接口获取状态的说法：LLM 拿不到数据集状态，写出来的都是推断，删除
_DATASET_CLAIM = re.compile(
    r"数据集|数据源|(?:获取|取数|调用|部分)失败|已跳过|未请求|(?<![A-Za-z])(?:skipped|partial|failed|succeeded)(?![A-Za-z])",
    re.IGNORECASE,
)

PostProcess = Callable[[list[ExplanationSentence]], tuple[list[ExplanationSentence], list[str]]]

# 数字校验：日期整体拆成年/月/日；减号只有在前面不是数字/字母时才算负号（“2026-2”→ 2026 与 2）
_DATE = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?")
_NUMBER = re.compile(r"(?:(?<![0-9A-Za-z.])[-−])?\d+(?:\.\d+)?")
_ABS_TOL = 0.05
_REL_TOL = 0.005


def _s(text: str, kind: str, refs: Iterable[str] = ()) -> ExplanationSentence:
    return ExplanationSentence(text=text, kind=kind, refs=list(refs))  # type: ignore[arg-type]


# ───────────────────────────── 数字抽取与校验 ─────────────────────────────


def extract_numbers(text: str) -> list[float]:
    """抽取文本中的数字（日期拆为年、月、日）。"""
    out: list[float] = []
    rest = text or ""

    def take_date(m: re.Match[str]) -> str:
        out.extend(float(g) for g in m.groups())
        return " "

    rest = _DATE.sub(take_date, rest)
    for m in _NUMBER.finditer(rest):
        tok = m.group(0).replace("−", "-")
        try:
            out.append(float(tok))
        except ValueError:
            continue
    return out


def _collect_numbers(obj: Any, out: set[float]) -> None:
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        if math.isfinite(float(obj)):
            out.add(float(obj))
        return
    if isinstance(obj, str):
        out.update(extract_numbers(obj))
        return
    if isinstance(obj, Mapping):
        for v in obj.values():
            _collect_numbers(v, out)
        return
    if isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_numbers(v, out)


def _allowed(x: float, allowed: Iterable[float]) -> bool:
    for a in allowed:
        if abs(x - a) <= _ABS_TOL or (a != 0 and abs(x - a) / abs(a) <= _REL_TOL):
            return True
    return False


def _verify(
    raw: Any, facts: Mapping[str, Any], valid_refs: set[str], drop: Callable[[str], bool] | None = None,
) -> tuple[list[ExplanationSentence] | None, list[str]]:
    """校验 LLM 输出：结构、句子类型、禁语、数字。通过返回 (句子, 说明)，否则 (None, [原因])。

    drop(text) 为真的句子（如运行摘要里关于数据集状态的说法）在禁语检查之后直接删除，不参与数字校验。
    """
    if not isinstance(raw, Mapping) or not isinstance(raw.get("sentences"), list) or not raw["sentences"]:
        return None, ["输出缺少 sentences 数组"]
    allowed: set[float] = set()
    _collect_numbers(facts, allowed)
    sentences: list[ExplanationSentence] = []
    dropped = 0
    for i, item in enumerate(raw["sentences"], 1):
        if not isinstance(item, Mapping) or not isinstance(item.get("text"), str) or not item["text"].strip():
            return None, [f"第 {i} 句结构不合法"]
        text = item["text"].strip()
        kind = item.get("kind")
        if kind not in _KINDS:
            return None, [f"第 {i} 句类型「{kind}」不合法"]
        _, violations = compliance.sanitize_output(text)
        if violations:
            return None, [f"第 {i} 句含不合规表述（{'、'.join(violations)}）"]
        if drop is not None and drop(text):
            dropped += 1
            continue
        for x in extract_numbers(text):
            if not _allowed(x, allowed):
                return None, [f"第 {i} 句中的数字 {fmt_num(x)} 在证据中找不到"]
        refs = item.get("refs") if isinstance(item.get("refs"), list) else []
        sentences.append(_s(text, kind, [str(r) for r in refs if str(r) in valid_refs]))
    if not any(s.kind != "caveat" for s in sentences):
        return None, ["AI 文本没有可用的解释内容" + (f"（已删除 {dropped} 句数据集状态描述）" if dropped else "")]
    if sentences[-1].kind != "caveat":
        sentences.append(_s(_STOCK_CAVEAT if "stock" in facts else _SUMMARY_CAVEAT, "caveat"))
    notes = [VERIFIED_NOTE]
    if dropped:
        notes.append(f"已删除 AI 文本中 {dropped} 句关于数据集状态的描述（AI 未获得数据集状态，以系统生成的数据集说明为准）")
    return sentences, notes


async def _llm_explain(
    llm: Any, system: str, user: str, facts: Mapping[str, Any], valid_refs: set[str], template: Explanation,
    *, drop: Callable[[str], bool] | None = None, finish: PostProcess | None = None,
) -> Explanation:
    """调用 LLM → 校验（drop 删除指定句子）→ finish 做确定性后处理；任何失败回退模板并在 verification_notes 写明原因。"""
    if llm is None or not getattr(llm, "enabled", True):
        return template.model_copy(update={"verification_notes": [*template.verification_notes, _CALL_FAIL_PREFIX + "未配置大模型"]})
    try:
        raw = await asyncio.wait_for(llm.chat_json(system, user, temperature=0.2, max_tokens=1200), timeout=LLM_TIMEOUT_S)
    except asyncio.TimeoutError:
        reason = f"AI 接口超时（超过 {LLM_TIMEOUT_S:g} 秒）"
        return template.model_copy(update={"verification_notes": [*template.verification_notes, _CALL_FAIL_PREFIX + reason]})
    except Exception as e:  # noqa: BLE001 — 解释失败不影响结果，回退模板
        reason = str(e).strip() or type(e).__name__
        return template.model_copy(update={"verification_notes": [*template.verification_notes, _CALL_FAIL_PREFIX + reason[:120]]})
    sentences, notes = _verify(raw, facts, valid_refs, drop)
    if sentences is None:
        return template.model_copy(update={"verified": True, "verification_notes": [*template.verification_notes, _FAIL_PREFIX + notes[0]]})
    if finish is not None:
        sentences, extra = finish(sentences)
        notes += extra
    return Explanation(sentences=sentences, generator="llm", verified=True, verification_notes=notes)


def _before_trailing_caveats(sentences: Sequence[ExplanationSentence], extra: Sequence[ExplanationSentence]) -> list[ExplanationSentence]:
    """把 extra 插在末尾提示句（caveat）之前：正文之后、“不构成投资建议”之前。"""
    i = len(sentences)
    while i > 0 and sentences[i - 1].kind == "caveat":
        i -= 1
    return [*sentences[:i], *extra, *sentences[i:]]


# ───────────────────────────── 个股解释 ─────────────────────────────


def _source_text(src: SourceRef | None) -> str:
    if src is None:
        return ""
    parts = [_PROVIDER_LABEL.get(src.provider, src.provider)]
    if src.endpoint:
        parts.append(src.endpoint)
    head = " ".join(parts)
    return f"{head} · {src.field}" if src.field else head


def _cond_label(cond: Condition | None, cid: str) -> str:
    if cond is None:
        return cid
    return cond.label or describe(cond)


def _metric_name(cond: Condition | None, ev: CondEval) -> str:
    mid = cond.metric if cond else (ev.evidence.metric if ev.evidence else "")
    m = catalog.METRICS.get(mid)
    return m.name if m else (mid or "指标")


def _rank_sentence(cond: Condition, ev: CondEval) -> ExplanationSentence | None:
    if ev.rank_pct is None or cond.op not in catalog.RANK_OPS:
        return None
    m = catalog.METRICS.get(cond.metric)
    pct = ev.rank_pct
    level = "偏低" if pct <= 30 else "偏高" if pct >= 70 else "居中"
    scope = SCOPE_SHORT.get(cond.rank_scope or "", "参考组内")
    group = f"（参考组 {ev.rank_group_size} 只）" if ev.rank_group_size else ""
    direction = ""
    if m is not None and m.higher_is_better is False and level != "居中":
        direction = "，按该指标“越低越好”的口径" + ("处于相对有利的位置" if level == "偏低" else "处于相对不利的位置")
    elif m is not None and m.higher_is_better is True and level != "居中":
        direction = "，按该指标“越高越好”的口径" + ("处于相对有利的位置" if level == "偏高" else "处于相对不利的位置")
    return _s(f"{m.name if m else cond.metric}在{scope}的分位为 {fmt_num(round(pct, 1))}%{group}，属于{level}水平{direction}。", "inference", [cond.id])


def template_stock_explanation(stock: StockResult, strategy: Strategy) -> Explanation:
    """逐条件：事实句（数值+阈值+报告期/窗口+来源字段）；分位 → 推断句；缺失/不适用 → 不确定句；末尾提示句。"""
    conds = {c.id: c for c in normalize(strategy).conditions}
    name = f"{stock.name}（{stock.thscode}）" if stock.name else stock.thscode
    out: list[ExplanationSentence] = []
    if stock.bucket == "included":
        tail = "，其中部分条件数据缺失，按“保留并标记”策略带标记入选" if stock.flagged else ""
        out.append(_s(f"{name}满足全部启用的硬条件，归入“入选”{tail}。", "fact"))
    elif stock.bucket == "excluded":
        labels = "、".join(f"「{_cond_label(conds.get(i), i)}」" for i in stock.failed_conditions)
        out.append(_s(f"{name}有 {len(stock.failed_conditions)} 条硬条件未通过（{labels or '见下文'}），归入“排除”。", "fact", stock.failed_conditions))
    else:
        labels = "、".join(f"「{_cond_label(conds.get(i), i)}」" for i in stock.missing_conditions)
        out.append(_s(f"{name}有 {len(stock.missing_conditions)} 条硬条件因数据缺失无法判定（{labels or '见下文'}），归入“数据缺失”。", "uncertain", stock.missing_conditions))
    if stock.near_miss:
        out.append(_s("该股只差少量条件或接近阈值，阈值小幅调整可能改变其分组。", "inference"))

    notes: list[str] = []
    for ev in stock.evaluations:
        cond = conds.get(ev.condition_id)
        label = _cond_label(cond, ev.condition_id)
        refs = [ev.condition_id]
        if ev.status in ("pass", "fail"):
            evd = ev.evidence
            period = f"（{evd.period}）" if evd and evd.period else ""
            src = _source_text(evd.sources[0]) if evd and evd.sources else ""
            src_text = f"，来源：{src}" if src else ""
            threshold = f"，阈值 {ev.threshold}" if ev.threshold else ""
            soft = "" if cond is None or cond.hard else "（软条件，只计分）"
            out.append(_s(
                f"「{label}」{soft}：{_metric_name(cond, ev)}为 {ev.display}{period}{threshold}，判定{_STATUS_LABEL[ev.status]}{src_text}。",
                "fact", refs,
            ))
            if cond is not None:
                rank = _rank_sentence(cond, ev)
                if rank is not None:
                    out.append(rank)
            if evd and evd.note and evd.note not in notes:
                notes.append(evd.note)
        elif ev.status in ("missing", "not_applicable"):
            reason = ev.reason or ("数据缺失" if ev.status == "missing" else "该指标对这只股票不适用")
            out.append(_s(f"「{label}」：{_STATUS_LABEL[ev.status]}（{reason}），无法判定，不按通过处理。", "uncertain", refs))
        else:
            out.append(_s(f"「{label}」：{_STATUS_LABEL.get(ev.status, ev.status)}（{ev.reason or '条件已停用或被跳过'}）。", "uncertain", refs))
    for note in notes:
        out.append(_s(f"口径提示：{note}。", "caveat"))
    out.append(_s(_STOCK_CAVEAT, "caveat"))
    return Explanation(sentences=out, generator="template", verified=True, verification_notes=["模板生成：数值直接取自判定证据"])


def _stock_facts(stock: StockResult, strategy: Strategy) -> dict[str, Any]:
    conds = {c.id: c for c in normalize(strategy).conditions}
    rows = []
    for ev in stock.evaluations:
        cond = conds.get(ev.condition_id)
        evd = ev.evidence
        row: dict[str, Any] = {
            "id": ev.condition_id,
            "label": _cond_label(cond, ev.condition_id),
            "hard": cond.hard if cond else True,
            "status": _STATUS_LABEL.get(ev.status, ev.status),
            "value": ev.display,
            "threshold": ev.threshold,
        }
        if ev.rank_pct is not None:
            row["rank_pct"] = round(ev.rank_pct, 1)
            row["rank_group_size"] = ev.rank_group_size
        if evd is not None:
            if evd.period:
                row["period"] = evd.period
            if evd.sources:
                row["source"] = _source_text(evd.sources[0])
            if evd.note:
                row["note"] = evd.note
        if ev.reason:
            row["reason"] = ev.reason
        rows.append(row)
    return {
        "stock": {"thscode": stock.thscode, "name": stock.name, "industry": stock.industry},
        "bucket": _BUCKET_LABEL[stock.bucket],
        "flagged": stock.flagged,
        "near_miss": stock.near_miss,
        "failed_conditions": stock.failed_conditions,
        "missing_conditions": stock.missing_conditions,
        "conditions": rows,
    }


_SENTENCE_END = re.compile(r"[。！？!?；;.]+$")


def _source_brief(src: SourceRef) -> str:
    """“接口·字段”（无接口时用提供方名称）。"""
    head = src.endpoint or _PROVIDER_LABEL.get(src.provider, src.provider)
    return f"{head}·{src.field}" if src.field else head


_PERIOD_INNER = re.compile(r"（([^（）]+)）")


def _mentions_period(text: str, period: str) -> bool:
    """句子是否已写出该证据的期间：整句期间、括号前的主体（“2026年中报”），或括号内的代码/时点（“2026-2”“截至2026-10-16”）。

    括号内的片段按数字边界匹配，避免“2026-1”误中“2026-10-16”。
    """
    core = period.split("（", 1)[0].strip()
    if period in text or (len(core) >= 2 and core in text):
        return True
    m = _PERIOD_INNER.search(period)
    inner = m.group(1).strip() if m else ""
    return len(inner) >= 4 and re.search(rf"(?<!\d){re.escape(inner)}(?!\d)", text) is not None


def _period_tail(evd: Evidence) -> str:
    return f"{evd.period}；来源：{_source_brief(evd.sources[0])}" if evd.sources else str(evd.period)


def _with_periods(sentences: list[ExplanationSentence], stock: StockResult) -> tuple[list[ExplanationSentence], list[str]]:
    """kind=fact 且 refs 指向条件的句子若没写期间，句末补“（期间；来源：接口·字段）”（取自该条件的证据）。"""
    evs = {ev.condition_id: ev for ev in stock.evaluations}
    out: list[ExplanationSentence] = []
    patched = 0
    for s in sentences:
        tails: list[str] = []
        if s.kind == "fact":
            for r in s.refs:
                ev = evs.get(r)
                evd = ev.evidence if ev is not None else None
                if evd is None or not evd.period or _mentions_period(s.text, evd.period):
                    continue
                tail = _period_tail(evd)
                if tail not in tails:
                    tails.append(tail)
        if tails:
            m = _SENTENCE_END.search(s.text)
            body, end = (s.text[: m.start()], m.group(0)) if m else (s.text, "。")
            # 概括多个条件的句子只提示去看证据表，避免一句话挂多段括号
            suffix = f"（{tails[0]}）" if len(tails) == 1 else "（各条件的报告期/时点与来源见下方证据表）"
            s = s.model_copy(update={"text": body + suffix + end})
            patched += 1
        out.append(s)
    notes = [f"已为 {patched} 句事实句补充报告期/时点与来源（取自对应条件的引擎证据）"] if patched else []
    return out, notes


async def llm_stock_explanation(stock: StockResult, strategy: Strategy, llm: Any) -> Explanation:
    template = template_stock_explanation(stock, strategy)
    facts = _stock_facts(stock, strategy)
    refs = {ev.condition_id for ev in stock.evaluations} | {c.id for c in strategy.conditions if c.id}
    return await _llm_explain(
        llm, prompts.STOCK_SYSTEM, prompts.build_stock_user(facts), facts, refs, template,
        finish=lambda sentences: _with_periods(sentences, stock),
    )


# ───────────────────────────── 运行摘要 ─────────────────────────────


def _pool(result: ScreenResult) -> tuple[str, int]:
    n = len(result.included) + len(result.excluded) + len(result.missing)
    if result.universe is not None:
        return result.universe.name, result.universe.size or n
    u = result.strategy.universe
    return (u.name or u.query or "未命名股票池"), n


def _dataset_sentences(result: ScreenResult) -> list[ExplanationSentence]:
    """数据集“部分失败/获取失败”的不确定句（确定性模板，状态用中文）；本策略不需要而跳过的数据集不写。"""
    return [
        _s(
            f"数据集「{ds.label}」{_DS_STATUS_LABEL[ds.status]}（成功 {ds.succeeded}/{ds.requested}），"
            "依赖它的条件可能被判为数据缺失，相关结果需谨慎解读。",
            "uncertain",
        )
        for ds in result.datasets
        if ds.status in ("partial", "failed")
    ]


def template_run_summary(result: ScreenResult) -> Explanation:
    """股票池与规模、入选/排除/缺失数、主要卡点、数据集异常、报告期与价格截止日。"""
    out: list[ExplanationSentence] = []
    name, size = _pool(result)
    n_in, n_ex, n_mi = len(result.included), len(result.excluded), len(result.missing)
    if result.status != "done":
        out.append(_s(f"本次运行状态为“{result.status}”{('：' + result.error) if result.error else ''}，以下统计可能不完整。", "uncertain"))
    out.append(_s(f"股票池为{name}，共 {size} 只股票；入选 {n_in} 只，排除 {n_ex} 只，数据缺失 {n_mi} 只。", "fact"))
    if result.universe is not None and result.universe.note:
        out.append(_s(f"股票池口径：{result.universe.note}。", "caveat"))

    hard = [st for st in result.condition_stats if st.hard]
    by_drop = [st for st in hard if st.drop_effect is not None and st.drop_effect > n_in]
    if by_drop:
        top = max(by_drop, key=lambda st: (st.drop_effect or 0, st.sole_blocker))
        out.append(_s(
            f"影响最大的条件是「{top.label}」：通过 {top.passed} 只、未通过 {top.failed} 只、数据缺失 {top.missing} 只；"
            f"去掉该条件后入选数为 {top.drop_effect} 只（当前 {n_in} 只）。",
            "fact", [top.condition_id],
        ))
    blockers = [st for st in hard if st.sole_blocker > 0]
    if blockers:
        top_b = max(blockers, key=lambda st: st.sole_blocker)
        if not by_drop or top_b.condition_id != max(by_drop, key=lambda st: (st.drop_effect or 0, st.sole_blocker)).condition_id:
            out.append(_s(f"「{top_b.label}」是 {top_b.sole_blocker} 只股票被排除的唯一原因。", "fact", [top_b.condition_id]))
        else:
            out.append(_s(f"其中 {top_b.sole_blocker} 只股票只因「{top_b.label}」被排除，该条件是主要卡点。", "inference", [top_b.condition_id]))
    if n_mi > 0:
        miss = [st for st in result.condition_stats if st.missing > 0]
        worst = max(miss, key=lambda st: st.missing) if miss else None
        tail = f"；缺失最多的条件是「{worst.label}」（{worst.missing} 只）" if worst else ""
        out.append(_s(f"有 {n_mi} 只股票因关键数据缺失无法判定，未计入入选{tail}。", "uncertain", [worst.condition_id] if worst else []))
    out += _dataset_sentences(result)
    for c in result.conflicts:
        if c.kind == "data_coverage":
            out.append(_s(c.message, "uncertain", c.condition_ids))
    rp, td = result.report_period, result.trade_date
    if rp is not None or td:
        parts = []
        if rp is not None:
            parts.append(f"财务数据报告期为{rp.label}（{rp.report}）")
        if td:
            parts.append(f"价格数据截至 {td}")
        out.append(_s("，".join(parts) + "。", "fact"))
    else:
        out.append(_s("本次结果未记录报告期与价格截止日，数据时点无法确认。", "uncertain"))
    out.append(_s(_SUMMARY_CAVEAT, "caveat"))
    return Explanation(sentences=out, generator="template", verified=True, verification_notes=["模板生成：数值直接取自运行统计"])


def _summary_facts(result: ScreenResult) -> dict[str, Any]:
    """交给 LLM 的运行事实。不含任何数据集状态：数据集说明由模板按运行记录确定性生成（见 llm_run_summary）。"""
    name, size = _pool(result)
    facts: dict[str, Any] = {
        "universe": {"name": name, "size": size},
        "counts": {"included": len(result.included), "excluded": len(result.excluded), "missing": len(result.missing)},
        "conditions": [
            {
                "id": st.condition_id, "label": st.label, "hard": st.hard, "passed": st.passed, "failed": st.failed,
                "missing": st.missing, "not_applicable": st.not_applicable, "sole_blocker": st.sole_blocker,
                "included_if_removed": st.drop_effect,
            }
            for st in result.condition_stats
        ],
    }
    if result.report_period is not None:
        facts["report_period"] = {"label": result.report_period.label, "report": result.report_period.report}
    if result.trade_date:
        facts["trade_date"] = result.trade_date
    return facts


def _dataset_claim(text: str) -> bool:
    return bool(_DATASET_CLAIM.search(text))


async def llm_run_summary(result: ScreenResult, llm: Any) -> Explanation:
    """LLM 写正文（不涉及数据集状态，写了也删除）；数据集“部分失败/获取失败”句由模板生成，插在末尾提示句之前。"""
    template = template_run_summary(result)
    facts = _summary_facts(result)
    refs = {st.condition_id for st in result.condition_stats}
    ds_sentences = _dataset_sentences(result)

    def finish(sentences: list[ExplanationSentence]) -> tuple[list[ExplanationSentence], list[str]]:
        if not ds_sentences:
            return sentences, []
        return _before_trailing_caveats(sentences, ds_sentences), [_DATASET_NOTE]

    return await _llm_explain(
        llm, prompts.SUMMARY_SYSTEM, prompts.build_summary_user(facts), facts, refs, template,
        drop=_dataset_claim, finish=finish,
    )
