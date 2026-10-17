"""自然语言解析入口（DESIGN §6.2）：合规前置 → LLM → 严格校验 → 澄清/股票池/默认风控 → 冲突检测。

LLM 只产出“候选条件草案”；所有条件必须通过 catalog 的指标/算子/分位口径校验，未通过的丢弃并写 warning。
LLM 不可用、调用异常或输出结构不合法时，降级到规则词典解析（fallback.parse），parser="rule_fallback"。
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, get_args

from ..config import get_settings
from ..engine.conditions import describe, normalize, semantic_key, structural_problems
from ..engine.conflicts import detect_conflicts
from ..schemas import (
    Clarification,
    ComplianceNotice,
    Condition,
    Conflict,
    Interpretation,
    Op,
    ParseRequest,
    ParseResult,
    RankScope,
    Strategy,
    UniverseOption,
    Unsupported,
)
from . import compliance, fallback, prompts
from .fallback import (
    OptionDraft,
    PhraseDraft,
    UniverseDraft,
    assemble_screen,
    condition_changes,
    direction_verb,
    preset_option,
    preset_universe_draft,
    resolve_board,
    universe_choice_clarification,
    universe_key,
    universe_label,
)
from .llm import LLMClient, LLMError

__all__ = ["UniverseSearcher", "parse_query"]

_VALID_OPS: frozenset[str] = frozenset(get_args(Op))
_VALID_SCOPES: frozenset[str] = frozenset(get_args(RankScope))
_CONFIDENCE = ("high", "medium", "low")
_LETTERS = "bcdefghij"
_PARSE_TIMEOUT_S = 60.0
# 分位阈值越界不丢弃：保留并交给冲突检测提示（DESIGN §6.2“阈值越界 → 保留”）
_KEEP_PROBLEMS = frozenset({"rank_out_of_range"})


class UniverseSearcher(Protocol):
    async def search_universes(self, q: str) -> list[UniverseOption]: ...


class _InvalidOutput(Exception):
    """LLM 输出结构不合法（触发规则降级）。"""


# ───────────────────────────── 小工具 ─────────────────────────────


def _clean(text: Any, default: str | None = None) -> str | None:
    """LLM 给出的用户可见文本：去空白并删除含禁语的句子；为空时用 default。"""
    if not isinstance(text, str):
        return default
    cleaned, _ = compliance.sanitize_output(text.strip())
    return cleaned or default


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
    elif isinstance(v, str):
        try:
            x = float(v.strip().rstrip("%％"))
        except ValueError:
            raise ValueError(f"阈值「{v}」不是数字") from None
    else:
        raise ValueError("阈值不是数字")
    if not math.isfinite(x):
        raise ValueError("阈值不是有效数字")
    return x


def _spec_text(d: Mapping[str, Any]) -> str:
    return f"{d.get('metric', '?')} {d.get('op', '?')}"


def _build_condition(d: Any, *, source_phrase: str) -> tuple[Condition | None, str | None]:
    """LLM 条件 dict → 校验后的 Condition（origin="ai"）；不合法返回 (None, 原因)。"""
    if not isinstance(d, Mapping):
        return None, "条件不是对象"
    metric, op = d.get("metric"), d.get("op")
    if not isinstance(metric, str) or not metric.strip():
        return None, "缺少指标 id"
    if not isinstance(op, str) or op not in _VALID_OPS:
        return None, f"非法算子「{op}」"
    try:
        value, value2 = _num(d.get("value")), _num(d.get("value2"))
        weight = _num(d.get("weight"))
    except ValueError as e:
        return None, str(e)
    scope = d.get("rank_scope")
    if op in ("pct_rank_lte", "pct_rank_gte"):
        if scope is not None and scope not in _VALID_SCOPES:
            return None, f"非法分位口径「{scope}」"
    else:
        scope = None
    if op in ("is_true", "is_false"):
        value = value2 = None
    elif op != "between":
        value2 = None
    hard = d.get("hard")
    cond = Condition(
        metric=metric.strip(), op=op, value=value, value2=value2, rank_scope=scope,  # type: ignore[arg-type]
        hard=hard if isinstance(hard, bool) else True, weight=weight if weight and weight > 0 else 1.0,
        origin="ai", source_phrase=source_phrase, rationale=_clean(d.get("rationale")),
    )
    probs = [p for p in structural_problems(cond) if p.code not in _KEEP_PROBLEMS]
    if probs:
        return None, "；".join(p.message for p in probs)
    return cond, None


def _valid_conditions(specs: Any, phrase: str, warnings: list[str], where: str) -> list[tuple[int, Condition]]:
    """逐条校验，返回 [(原序号 1-based, 条件)]；丢弃的写 warning。"""
    out: list[tuple[int, Condition]] = []
    if not isinstance(specs, list):
        return out
    for j, d in enumerate(specs, 1):
        cond, why = _build_condition(d, source_phrase=phrase)
        if cond is None:
            spec = _spec_text(d) if isinstance(d, Mapping) else str(d)[:30]
            warnings.append(f"已丢弃 AI 给出的条件「{spec}」（{where}）：{why}")
            continue
        out.append((j, cond))
    return out


def _blocked_by_ai(intent: str) -> ComplianceNotice:
    category = "advice" if intent == "advice_request" else "prediction"
    what = "荐股或买卖建议" if category == "advice" else "涨跌预测"
    return ComplianceNotice(
        level="block", category=category,  # type: ignore[arg-type]
        message=f"AI 判断该输入属于{what}类请求。{fallback.BLOCKED_HINT}",
    )


def _unsupported_items(raw: Any) -> list[Unsupported]:
    out: list[Unsupported] = []
    if not isinstance(raw, list):
        return out
    for u in raw:
        if not isinstance(u, Mapping):
            continue
        phrase = _clean(u.get("phrase"))
        if not phrase:
            continue
        out.append(Unsupported(
            phrase=phrase, reason=_clean(u.get("reason"), "暂无可映射的数据条件") or "暂无可映射的数据条件",
            alternative=_clean(u.get("alternative")),
        ))
    return out


async def _universe_draft(raw: Any, query: str, searcher: UniverseSearcher | None) -> UniverseDraft:
    """LLM 股票池 → UniverseDraft：常用指数/全A 直接用；行业/概念/板块名交给 searcher。"""
    if not isinstance(raw, Mapping):
        return UniverseDraft()
    kind = raw.get("kind")
    code = raw.get("code") if isinstance(raw.get("code"), str) else None
    name = raw.get("name") if isinstance(raw.get("name"), str) else None
    q = raw.get("query") if isinstance(raw.get("query"), str) else None

    def phrase_for(text: str | None) -> str:
        return text if text and text in query else f"股票池：{text or '未命名'}"

    if kind == "all_a":
        return preset_universe_draft(None, "全部A股", "all_a", phrase_for(name or "全部A股"), origin="ai")
    if kind == "index" and code:
        p = preset_option(code)
        if p is not None:
            return preset_universe_draft(p.code, p.name, "index", phrase_for(name or p.name), origin="ai")
    board = (q or name or "").strip()
    if board:
        return await resolve_board(
            board, phrase_for(board), searcher, origin="ai",
            kind_hint=kind if kind in ("industry", "concept") else None,
        )
    if kind == "index" and code:
        d = UniverseDraft()
        d.warnings.append(f"AI 给出的指数代码「{code}」不在常用指数中，已保留默认股票池沪深300，可在股票池选择中搜索。")
        return d
    return UniverseDraft()


def _ai_conflicts(raw: Any, ref_map: Mapping[str, str], strategy: Strategy, existing: Sequence[Conflict]) -> list[Conflict]:
    """AI 冲突：只有引用的条件都存在时才合并（source="ai"）。"""
    out: list[Conflict] = []
    if not isinstance(raw, list):
        return out
    ids_in_strategy = {c.id for c in strategy.conditions}
    used = {c.id for c in existing}
    for k, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            continue
        refs = item.get("condition_refs")
        message = _clean(item.get("message"))
        if not isinstance(refs, list) or not refs or not message:
            continue
        ids: list[str] = []
        for r in refs:
            rid = ref_map.get(str(r).strip(), str(r).strip())
            if rid not in ids_in_strategy:
                ids = []
                break
            if rid not in ids:
                ids.append(rid)
        if not ids:
            continue
        cid = f"ai:{k}:{'+'.join(ids)}"
        if cid in used:
            continue
        used.add(cid)
        out.append(Conflict(
            id=cid, severity="warning", kind="tension", condition_ids=ids, message=message,
            suggestion=_clean(item.get("suggestion")), source="ai",
        ))
    return out


# ───────────────────────────── 筛选模式 ─────────────────────────────


async def _from_llm_screen(query: str, raw: Mapping[str, Any], searcher: UniverseSearcher | None) -> ParseResult:
    warnings: list[str] = []
    phrases_raw = raw.get("phrases") if raw.get("phrases") is not None else []
    if not isinstance(phrases_raw, list):
        raise _InvalidOutput("phrases 不是数组")
    unsupported = _unsupported_items(raw.get("unsupported"))

    drafts: list[PhraseDraft] = []
    ref_map: dict[str, str] = {}
    n = 0
    for i, p in enumerate(phrases_raw, 1):
        if not isinstance(p, Mapping):
            warnings.append(f"已忽略 AI 输出中第 {i} 个不合法的短语。")
            continue
        phrase = (p.get("phrase") or "").strip() if isinstance(p.get("phrase"), str) else ""
        if not phrase:
            warnings.append(f"已忽略 AI 输出中第 {i} 个缺少原文片段的短语。")
            continue
        meaning = _clean(p.get("meaning"), phrase) or phrase
        confidence = p.get("confidence") if p.get("confidence") in _CONFIDENCE else "medium"
        primary = _valid_conditions(p.get("conditions"), phrase, warnings, f"短语“{phrase}”")
        alts: list[OptionDraft] = []
        alts_raw = p.get("alternatives") if isinstance(p.get("alternatives"), list) else []
        for a_idx, a in enumerate(alts_raw, 1):
            if not isinstance(a, Mapping):
                continue
            label = _clean(a.get("label"), f"备选 {a_idx}") or f"备选 {a_idx}"
            conds = [c for _, c in _valid_conditions(a.get("conditions"), phrase, warnings, f"“{phrase}”的备选「{label}」")]
            if not conds:
                warnings.append(f"已丢弃“{phrase}”的备选「{label}」：没有可执行的条件。")
                continue
            alts.append(OptionDraft(id="", label=label, description=_clean(a.get("description")), conditions=conds))
        promoted = False
        if not primary and alts:
            first = alts.pop(0)
            primary = [(0, c) for c in first.conditions]
            promoted = True
            warnings.append(f"“{phrase}”的首选解释没有可执行的条件，已改用备选「{first.label}」。")
        if not primary:
            unsupported.append(Unsupported(
                phrase=phrase, reason="AI 给出的条件未通过指标目录校验", alternative="可在条件编辑器中手动添加条件",
            ))
            continue
        for j, c in primary:
            n += 1
            if not promoted:
                ref_map[f"p{i}.c{j}"] = f"c{n}"
        conds = [c for _, c in primary]
        options: list[OptionDraft] | None = None
        if alts or confidence != "high":
            label = _clean(p.get("label"), meaning) or meaning
            options = [OptionDraft(id="a", label=label, description=None, conditions=conds)]
            for k, a in enumerate(alts):
                a.id = _LETTERS[k] if k < len(_LETTERS) else f"o{k + 2}"
                options.append(a)
        drafts.append(PhraseDraft(
            phrase=phrase, meaning=meaning, conditions=conds, confidence=confidence,  # type: ignore[arg-type]
            question=_clean(p.get("question")), options=options, default_option_id="a",
        ))

    if phrases_raw and not drafts:
        raise _InvalidOutput("AI 给出的条件均未通过指标目录校验" + (f"（{warnings[0]}）" if warnings else ""))

    ud = await _universe_draft(raw.get("universe"), query, searcher)
    unsupported += ud.unsupported
    warnings += ud.warnings
    if not drafts and not ud.mentioned:
        return ParseResult(
            intent="out_of_scope", unsupported=unsupported, warnings=warnings + [fallback.OUT_OF_SCOPE_HINT],
        )
    strategy, interps, clars = assemble_screen(query, drafts, ud, origin="ai")
    if not drafts:
        warnings.append(fallback.NO_CONDITION_HINT)
    conflicts = detect_conflicts(strategy)
    conflicts += _ai_conflicts(raw.get("conflicts"), ref_map, strategy, conflicts)
    return ParseResult(
        intent="screen", strategy=strategy, interpretations=interps, clarifications=clars,
        conflicts=conflicts, unsupported=unsupported, warnings=warnings,
    )


# ───────────────────────────── 修改模式 ─────────────────────────────

_UPDATABLE = ("value", "value2", "op", "rank_scope", "enabled", "hard", "weight")


def _apply_update(c: Condition, upd: Mapping[str, Any]) -> tuple[Condition | None, str | None]:
    n = c.model_copy(deep=True)
    try:
        for key in _UPDATABLE:
            if key not in upd:
                continue
            v = upd[key]
            if key in ("value", "value2"):
                setattr(n, key, _num(v))
            elif key == "weight":
                w = _num(v)
                if w is not None and w > 0:
                    n.weight = w
            elif key == "op":
                if v not in _VALID_OPS:
                    return None, f"非法算子「{v}」"
                n.op = v
            elif key == "rank_scope":
                if v is not None and v not in _VALID_SCOPES:
                    return None, f"非法分位口径「{v}」"
                n.rank_scope = v
            elif isinstance(v, bool):
                setattr(n, key, v)
    except ValueError as e:
        return None, str(e)
    if n.op not in ("pct_rank_lte", "pct_rank_gte"):
        n.rank_scope = None
    if n.op in ("is_true", "is_false"):
        n.value = n.value2 = None
    elif n.op != "between":
        n.value2 = None
    probs = [p for p in structural_problems(n) if p.code not in _KEEP_PROBLEMS]
    if probs:
        return None, "；".join(p.message for p in probs)
    n.label = None
    return n, None


def _fresh_cid(used: set[str]) -> str:
    k = 1
    while f"c{k}" in used:
        k += 1
    used.add(f"c{k}")
    return f"c{k}"


async def _from_llm_modify(
    query: str, raw: Mapping[str, Any], base: Strategy, searcher: UniverseSearcher | None,
) -> ParseResult:
    patch = raw.get("patch")
    if not isinstance(patch, Mapping):
        raise _InvalidOutput("修改模式缺少 patch")
    base_n = normalize(base)
    conds = [c.model_copy(deep=True) for c in base_n.conditions]
    used = {c.id for c in conds}
    changes: list[str] = []
    warnings: list[str] = []
    touched: list[str] = []
    ref_map: dict[str, str] = {}

    removes = patch.get("remove") if isinstance(patch.get("remove"), list) else []
    removed: set[str] = set()
    for rid in removes:
        rid = str(rid).strip()
        target = next((c for c in conds if c.id == rid), None)
        if target is None:
            warnings.append(f"AI 要删除的条件「{rid}」不存在，已忽略。")
            continue
        removed.add(rid)
        changes.append(f"删除条件：{describe(target)}")
    conds = [c for c in conds if c.id not in removed]

    for upd in patch.get("update") if isinstance(patch.get("update"), list) else []:
        if not isinstance(upd, Mapping):
            continue
        uid = str(upd.get("id", "")).strip()
        idx = next((i for i, c in enumerate(conds) if c.id == uid), None)
        if idx is None:
            if uid not in removed:
                warnings.append(f"AI 要修改的条件「{uid}」不存在，已忽略。")
            continue
        before = conds[idx]
        after, why = _apply_update(before, upd)
        if after is None:
            warnings.append(f"AI 对条件「{describe(before)}」的修改未通过校验，已忽略：{why}")
            continue
        diff = condition_changes(before, after, verb=direction_verb(before, after))
        if diff:
            changes += diff
            touched.append(after.id)
            conds[idx] = after

    for k, d in enumerate(patch.get("add") if isinstance(patch.get("add"), list) else [], 1):
        phrase = d.get("source_phrase") if isinstance(d, Mapping) and isinstance(d.get("source_phrase"), str) else query
        cond, why = _build_condition(d, source_phrase=phrase)
        if cond is None:
            spec = _spec_text(d) if isinstance(d, Mapping) else str(d)[:30]
            warnings.append(f"已丢弃 AI 新增的条件「{spec}」：{why}")
            continue
        if any(c.enabled and semantic_key(c) == semantic_key(cond) for c in conds):
            warnings.append(f"已有条件「{describe(cond)}」，未重复添加。")
            continue
        cond.id = _fresh_cid(used)
        ref_map[f"a{k}"] = cond.id
        conds.append(cond)
        touched.append(cond.id)
        changes.append(f"新增条件：{describe(cond)}")

    universe = base_n.universe.model_copy(deep=True)
    clars: list[Clarification] = []
    unsupported = _unsupported_items(raw.get("unsupported"))
    if isinstance(patch.get("universe"), Mapping):
        ud = await _universe_draft(patch.get("universe"), query, searcher)
        unsupported += ud.unsupported
        warnings += ud.warnings
        if ud.mentioned:
            if universe_key(ud.universe) != universe_key(universe):
                changes.append(f"股票池：{universe_label(universe)} → {universe_label(ud.universe)}")
                universe = ud.universe
            else:
                warnings.append(f"股票池已是「{universe_label(universe)}」。")
            if ud.choices:
                clars.append(universe_choice_clarification("q1", ud.choice_query or universe_label(ud.universe), ud.choices))

    new = base_n.model_copy(deep=True)
    new.conditions, new.universe = conds, universe
    strategy = normalize(new)
    if not changes:
        warnings.append(fallback.NO_MODIFICATION_HINT)
    conflicts = detect_conflicts(strategy)
    conflicts += _ai_conflicts(raw.get("conflicts"), ref_map, strategy, conflicts)
    interps = [Interpretation(
        phrase=query, meaning="；".join(changes) if changes else "未识别到可执行的修改",
        condition_ids=touched, confidence="medium", origin="ai",
    )]
    return ParseResult(
        intent="modify", strategy=strategy, interpretations=interps, clarifications=clars, conflicts=conflicts,
        unsupported=unsupported, changes=changes, warnings=warnings,
    )


# ───────────────────────────── 入口 ─────────────────────────────


async def _rule_fallback(
    query: str, base: Strategy | None, searcher: UniverseSearcher | None, reason: str, extra_warnings: Sequence[str] = (),
) -> ParseResult:
    result = await fallback.parse(query, base, searcher)
    result.parser = "rule_fallback"
    result.llm_model = None
    result.llm_error = reason
    if extra_warnings:
        result.warnings = [*extra_warnings, *result.warnings]
    return result


async def parse_query(req: ParseRequest, llm: LLMClient | None, searcher: UniverseSearcher | None) -> ParseResult:
    """自然语言 → 条件草案（筛选模式）或补丁（修改模式，req.base_strategy 非空）。"""
    q = (req.query or "").strip()
    base = req.base_strategy
    if not fallback._meaningful(q):
        return await fallback.parse(q, base, searcher)

    # 1) 合规前置：预测/荐股/收益承诺不交给 LLM，能转化的由规则生成只描述历史数据的草案
    notice = compliance.check_input(q)
    if notice is not None and notice.level == "block":
        return await fallback.blocked_result(q, notice, base, searcher)

    # 2) LLM
    if llm is None or not getattr(llm, "enabled", True):
        return await _rule_fallback(q, base, searcher, "未配置大模型（LLM 不可用），已使用规则词典解析")
    try:
        raw = await asyncio.wait_for(
            llm.chat_json(prompts.PARSE_SYSTEM, prompts.build_parse_user(q, base)), timeout=_PARSE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return await _rule_fallback(q, base, searcher, f"AI 解析超时（超过 {_PARSE_TIMEOUT_S:g} 秒），已使用规则词典解析")
    except LLMError as e:
        return await _rule_fallback(q, base, searcher, f"AI 解析失败（{e.kind}）：{e}；已使用规则词典解析")
    except Exception as e:  # noqa: BLE001 — 任何异常都降级，主链路不中断
        return await _rule_fallback(q, base, searcher, f"AI 解析异常（{type(e).__name__}），已使用规则词典解析")
    if not isinstance(raw, Mapping):
        return await _rule_fallback(q, base, searcher, "AI 输出不是 JSON 对象，已使用规则词典解析")

    # 3) 严格校验与组装
    intent = raw.get("intent")
    try:
        if intent in ("prediction_request", "advice_request"):
            result = ParseResult(intent=intent, compliance=_blocked_by_ai(intent), warnings=[fallback.BLOCKED_HINT])
        elif intent == "out_of_scope" and base is None:
            result = ParseResult(
                intent="out_of_scope", unsupported=_unsupported_items(raw.get("unsupported")),
                warnings=[fallback.OUT_OF_SCOPE_HINT],
            )
        elif base is not None:
            result = await _from_llm_modify(q, raw, base, searcher)
        else:
            result = await _from_llm_screen(q, raw, searcher)
    except _InvalidOutput as e:
        return await _rule_fallback(q, base, searcher, f"AI 输出不合法：{e}；已使用规则词典解析")
    except Exception as e:  # noqa: BLE001 — 输出结构异常（类型不符等）同样降级
        return await _rule_fallback(q, base, searcher, f"AI 输出无法解析（{type(e).__name__}），已使用规则词典解析")

    result.parser = "llm"
    result.llm_model = getattr(llm, "model", None) or get_settings().llm_model
    if notice is not None and result.compliance is None:
        result.compliance = notice
    return result
