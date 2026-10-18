"""规则词典解析（DESIGN §6.3）：AI 不可用时的降级解析，主链路仍可跑通。

匹配顺序：股票池关键词（UNIVERSE_LEXICON）→ 默认风控说法（剔除ST/次新）→ 预测类指标（预测市盈率/一致预期，
无一致预期数据 → unsupported）→ 规则词典（LEXICON，最长匹配优先、已匹配片段屏蔽）→ 暂不支持的说法（UNSUPPORTED_HINTS）
→ “X行业/X板块/X概念”交给股票池检索。每个命中的词典条目都生成澄清问题（默认项 = 词典 default）。
修改语句（放宽/收紧/去掉/只看）做有限支持；要删除/调整的类别在当前策略中不存在时写 warning（该部分未生效）。

本模块同时提供 LLM 解析层复用的公共构件：股票池检索与澄清、默认风控条件、草案组装、变更说明。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from .. import catalog
from ..engine.conditions import contains, describe, fmt_num, interval, normalize, op_family, semantic_key
from ..engine.conflicts import detect_conflicts
from ..schemas import (
    Clarification,
    ClarificationOption,
    ComplianceNotice,
    Condition,
    Interpretation,
    ParseResult,
    Strategy,
    Universe,
    UniverseOption,
    Unsupported,
)
from . import compliance

if TYPE_CHECKING:
    from .parser import UniverseSearcher

Origin = Literal["ai", "rule"]
Confidence = Literal["high", "medium", "low"]

UNIVERSE_DEFAULT_PHRASE = "（未指定股票池）"
UNIVERSE_QUESTION = "股票池选哪个？"
SYSTEM_PHRASE = "默认风控"
EMPTY_HINT = "请输入想筛选的股票特征，例如：“经营改善、估值合理、走势相对稳定”。"
OUT_OF_SCOPE_HINT = (
    "没有识别到可映射为数据条件的说法。可以描述财务、估值、走势、分红、流动性等数据特征，"
    "例如：“沪深300里高成长但估值不贵的公司”。"
)
NO_CONDITION_HINT = "未识别到筛选条件：当前只设置了股票池与默认风控条件，可在条件编辑器中添加条件。"
NO_MODIFICATION_HINT = "未识别到可执行的修改，可以换一种说法，例如“估值放宽到行业70%分位”“去掉回撤条件”“只看中证500”。"
BLOCKED_HINT = "本产品不预测涨跌、不提供买卖建议；可以改为描述可观察的数据特征，例如“近60日涨跌幅为正、估值合理”。"

_DEFAULT_UNIVERSE_CHOICES: tuple[tuple[str, str | None], ...] = (
    ("hs300", "000300.SH"),
    ("zz500", "000905.SH"),
    ("zz1000", "000852.SH"),
    ("all_a", None),
)
_MAX_UNIVERSE_CHOICES = 6
_LETTERS = "abcdefghij"


# ───────────────────────────── 草案结构 ─────────────────────────────


@dataclass
class OptionDraft:
    id: str
    label: str
    description: str | None
    conditions: list[Condition]


@dataclass
class PhraseDraft:
    """一个短语的解释：首选条件 + （需要澄清时）全部选项。options 为 None 表示不生成澄清。"""

    phrase: str
    meaning: str
    conditions: list[Condition]
    confidence: Confidence = "medium"
    question: str | None = None
    options: list[OptionDraft] | None = None
    default_option_id: str = "a"


@dataclass
class UniverseDraft:
    universe: Universe = field(default_factory=Universe)
    mentioned: bool = False  # 用户明确指定了股票池（未指定时生成默认股票池澄清）
    interpretations: list[Interpretation] = field(default_factory=list)
    choices: list[UniverseOption] = field(default_factory=list)  # 多个候选 → 股票池澄清
    choice_query: str | None = None
    unsupported: list[Unsupported] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ───────────────────────────── 股票池 ─────────────────────────────


def preset_option(code: str | None, kind: str = "index") -> UniverseOption | None:
    for p in catalog.UNIVERSE_PRESETS:
        if (code is None and p.kind == "all_a" and kind == "all_a") or (code is not None and p.code == code):
            return p
    return None


def universe_from_option(opt: UniverseOption) -> Universe:
    if opt.kind == "all_a":
        return Universe(kind="all_a", code=None, name="全部A股")
    return Universe(kind=opt.kind, code=opt.code, name=opt.name)


def universe_label(u: Universe) -> str:
    if u.kind == "all_a":
        return "全部A股"
    if u.kind == "custom":
        return u.name or f"自定义（{len(u.codes)} 只）"
    name = u.name or u.query or "未命名股票池"
    return f"{name}（{u.code}）" if u.code else name


def universe_key(u: Universe) -> tuple[str, str | None, tuple[str, ...], str | None]:
    return (u.kind, u.code, tuple(u.codes), u.query if not u.code else None)


def _option_description(o: UniverseOption) -> str:
    if o.kind == "all_a":
        return "全市场，先按估值/行情条件预筛，耗时较长"
    size = f"，约 {o.size_hint} 只" if o.size_hint else ""
    code = f"（{o.code}）" if o.code else ""
    return f"{o.group}{code}{size}"


def default_universe_clarification(qid: str) -> Clarification:
    """未提及股票池：默认沪深300，并请用户在沪深300/中证500/中证1000/全部A股中确认。"""
    options: list[ClarificationOption] = []
    for oid, code in _DEFAULT_UNIVERSE_CHOICES:
        p = preset_option(code, "index" if code else "all_a")
        if p is None:
            continue
        label = "全部A股" if p.kind == "all_a" else p.name
        options.append(ClarificationOption(id=oid, label=label, description=_option_description(p), universe=universe_from_option(p)))
    return Clarification(
        id=qid, phrase=UNIVERSE_DEFAULT_PHRASE, question=UNIVERSE_QUESTION, options=options, default_option_id=options[0].id,
    )


def universe_choice_clarification(qid: str, query: str, choices: Sequence[UniverseOption]) -> Clarification:
    """检索到多个候选股票池：默认第一个（检索相关度最高）。"""
    options = [
        ClarificationOption(id=f"u{i}", label=o.name, description=_option_description(o), universe=universe_from_option(o))
        for i, o in enumerate(choices[:_MAX_UNIVERSE_CHOICES], 1)
    ]
    return Clarification(
        id=qid, phrase=f"股票池：{query}", question=f"“{query}”对应哪个股票池？", options=options, default_option_id=options[0].id,
    )


async def search_universes(searcher: UniverseSearcher, q: str) -> tuple[list[UniverseOption], str | None]:
    """调用检索；失败不抛异常，返回 ([], 错误说明)。"""
    try:
        found = await searcher.search_universes(q)
    except Exception as e:  # noqa: BLE001 — 股票池检索失败不应中断解析主链路
        detail = str(e).strip()[:60]
        return [], f"{type(e).__name__}{'：' + detail if detail else ''}"
    return [o for o in found if isinstance(o, UniverseOption) and o.kind != "custom"], None


async def resolve_board(
    query: str,
    phrase: str,
    searcher: UniverseSearcher | None,
    *,
    origin: Origin,
    kind_hint: str | None = None,
    variants: Sequence[str] | None = None,
) -> UniverseDraft:
    """行业/概念/板块名称 → 股票池：唯一匹配直接用；多匹配 → 澄清（默认第一个）；无匹配 → unsupported 并保留默认。"""
    d = UniverseDraft()
    if searcher is None:
        kind = kind_hint if kind_hint in ("industry", "concept") else "industry"
        d.universe = Universe(kind=kind, code=None, name=query, query=query)  # type: ignore[arg-type]
        d.mentioned = True
        d.interpretations.append(Interpretation(
            phrase=phrase, meaning=f"股票池：{query}（执行时按名称匹配同花顺行业/概念）", confidence="medium", origin=origin,
        ))
        d.warnings.append(f"暂时无法检索股票池「{query}」，执行时将按名称匹配，请在股票池选择中确认。")
        return d
    found: list[UniverseOption] = []
    used = query
    for q in variants or [query]:
        found, err = await search_universes(searcher, q)
        if err is not None:
            d.warnings.append(f"股票池「{q}」检索失败（{err}），已保留默认股票池沪深300，可稍后在股票池选择中搜索。")
            return d
        if found:
            used = q
            break
    if kind_hint and any(o.kind == kind_hint for o in found):
        found = [o for o in found if o.kind == kind_hint]
    if not found:
        d.unsupported.append(Unsupported(
            phrase=phrase, reason=f"未找到与“{query}”匹配的指数、同花顺行业或概念板块",
            alternative="可在股票池选择中搜索行业或概念名称，或改用沪深300、中证500等常用指数",
        ))
        return d
    first = found[0]
    d.universe = universe_from_option(first)
    d.mentioned = True
    meaning = f"股票池：{first.name}（{_option_description(first)}）"
    if len(found) > 1:
        d.choices = list(found[:_MAX_UNIVERSE_CHOICES])
        d.choice_query = used
        meaning += "；检索到多个同名候选，请确认"
    d.interpretations.append(Interpretation(
        phrase=phrase, meaning=meaning, confidence="high" if len(found) == 1 else "medium", origin=origin,
    ))
    return d


def preset_universe_draft(code: str | None, name: str, kind: str, phrase: str, *, origin: Origin) -> UniverseDraft:
    u = Universe(kind="all_a", code=None, name="全部A股") if kind == "all_a" else Universe(kind=kind, code=code, name=name)  # type: ignore[arg-type]
    meaning = "股票池：全部A股（先按估值/行情条件预筛）" if kind == "all_a" else f"股票池：{name}（{code}）"
    return UniverseDraft(
        universe=u, mentioned=True,
        interpretations=[Interpretation(phrase=phrase, meaning=meaning, confidence="high", origin=origin)],
    )


# ───────────────────────────── 条件与草案组装 ─────────────────────────────


def conditions_from_spec(specs: Iterable[Mapping[str, Any]], *, origin: Origin, source_phrase: str) -> list[Condition]:
    """词典条件简写（catalog.C 的 dict）→ Condition。"""
    return [
        Condition(
            metric=d["metric"], op=d["op"], value=d.get("value"), value2=d.get("value2"), rank_scope=d.get("rank_scope"),
            hard=d.get("hard", True), origin=origin, source_phrase=source_phrase, rationale=d.get("rationale"),
        )
        for d in specs
    ]


def lexicon_phrase(entry: Mapping[str, Any], phrase: str, *, origin: Origin = "rule") -> PhraseDraft:
    """词典条目 → 短语草案：首选 = 词典 default，全部选项都进入澄清。"""
    options = [
        OptionDraft(
            id=o["id"], label=o["label"], description=o.get("description"),
            conditions=conditions_from_spec(o["conditions"], origin=origin, source_phrase=phrase),
        )
        for o in entry["options"]
    ]
    default = next(o for o in options if o.id == entry["default"])
    return PhraseDraft(
        phrase=phrase, meaning=entry["meaning"], conditions=[c.model_copy(deep=True) for c in default.conditions],
        confidence="medium", question=entry["question"], options=options, default_option_id=default.id,
    )


def condition_set_key(conds: Iterable[Condition]) -> frozenset[tuple[object, ...]]:
    return frozenset(semantic_key(c) for c in conds)


def lexicon_entry_for(phrase: str) -> Mapping[str, Any] | None:
    for e in catalog.LEXICON:
        if any(re.search(p, phrase, re.IGNORECASE) for p in e["patterns"]):
            return e
    return None


def missing_default_conditions(conds: Sequence[Condition]) -> list[Condition]:
    """系统默认风控条件中，用户尚未就同一指标表达意图的那些（副本）。"""
    used = {c.metric for c in conds}
    return [d.model_copy(deep=True) for d in catalog.DEFAULT_CONDITIONS if d.metric not in used]


def _fresh_id(base: str, used: set[str]) -> str:
    cid, k = base, 2
    while cid in used:
        cid, k = f"{base}_{k}", k + 1
    used.add(cid)
    return cid


def _position(query: str, phrase: str) -> int:
    i = query.find(phrase) if phrase else -1
    return i if i >= 0 else len(query)


def assemble_screen(
    query: str,
    phrases: Sequence[PhraseDraft],
    ud: UniverseDraft,
    *,
    origin: Origin,
    extra_interpretations: Sequence[Interpretation] = (),
) -> tuple[Strategy, list[Interpretation], list[Clarification]]:
    """短语草案 + 股票池 → 规范化策略、逐短语理解、澄清问题。

    - 条件 id 按短语顺序编为 c1, c2…；追加默认风控条件（用户已就同指标表达意图则不追加）；
    - 澄清默认项的条件与策略中该短语的条件完全一致（同 id）；其余选项条件 id 形如 q1_b_1；
    - 股票池：多候选 → 股票池澄清；未提及 → 默认沪深300 + “股票池选哪个？”。
    """
    conds: list[Condition] = []
    phrase_ids: list[list[str]] = []
    n = 0
    for p in phrases:
        ids: list[str] = []
        for c in p.conditions:
            n += 1
            nc = c.model_copy(deep=True)
            nc.id, nc.source_phrase, nc.origin = f"c{n}", p.phrase, origin
            conds.append(nc)
            ids.append(nc.id)
        phrase_ids.append(ids)
    conds += missing_default_conditions(conds)
    strategy = normalize(Strategy(query=query, universe=ud.universe.model_copy(deep=True), conditions=conds))
    by_id = {c.id: c for c in strategy.conditions}
    used = set(by_id)

    interps: list[Interpretation] = []
    clars: list[Clarification] = []
    for p, ids in zip(phrases, phrase_ids):
        interps.append(Interpretation(phrase=p.phrase, meaning=p.meaning, condition_ids=ids, confidence=p.confidence, origin=origin))
        if not p.options:
            continue
        qid = f"q{len(clars) + 1}"
        options: list[ClarificationOption] = []
        for o in p.options:
            if o.id == p.default_option_id:
                oc = [by_id[i].model_copy(deep=True) for i in ids]
            else:
                oc = []
                for j, c in enumerate(o.conditions, 1):
                    nc = c.model_copy(deep=True)
                    nc.id = _fresh_id(f"{qid}_{o.id}_{j}", used)
                    nc.source_phrase, nc.origin = p.phrase, origin
                    nc.label = describe(nc)
                    oc.append(nc)
            options.append(ClarificationOption(id=o.id, label=o.label, description=o.description, conditions=oc))
        question = p.question or f"“{p.phrase}”你更想用哪种口径？"
        clars.append(Clarification(id=qid, phrase=p.phrase, question=question, options=options, default_option_id=p.default_option_id))

    if ud.choices:
        clars.append(universe_choice_clarification(f"q{len(clars) + 1}", ud.choice_query or universe_label(ud.universe), ud.choices))
    elif not ud.mentioned:
        clars.append(default_universe_clarification(f"q{len(clars) + 1}"))

    interps += list(ud.interpretations) + list(extra_interpretations)
    interps.sort(key=lambda it: _position(query, it.phrase))
    return strategy, interps, clars


# ───────────────────────────── 变更说明 ─────────────────────────────


def condition_changes(before: Condition, after: Condition, *, verb: str = "修改") -> list[str]:
    """同一条件前后对比的中文说明（用 describe 展示前后口径）。"""
    out: list[str] = []
    if semantic_key(before) != semantic_key(after):
        out.append(f"{verb}条件：{describe(before)} → {describe(after)}")
    if before.enabled != after.enabled:
        out.append(f"{'启用' if after.enabled else '停用'}条件：{describe(after)}")
    if before.hard != after.hard:
        out.append(f"{'改为硬条件' if after.hard else '改为软条件（只计分、不过滤）'}：{describe(after)}")
    if not after.hard and before.weight != after.weight:
        out.append(f"调整权重：{describe(after)}，{fmt_num(before.weight)} → {fmt_num(after.weight)}")
    return out


def direction_verb(before: Condition, after: Condition) -> str:
    """按区间包含关系判断是放宽、收紧还是一般修改。"""
    a, b = interval(before), interval(after)
    if a is None or b is None or op_family(before.op) != op_family(after.op) or before.metric != after.metric:
        return "修改"
    if before.op in catalog.RANK_OPS and before.rank_scope != after.rank_scope:
        return "修改"
    if contains(b, a) and not contains(a, b):
        return "放宽"
    if contains(a, b) and not contains(b, a):
        return "收紧"
    return "修改"


# ───────────────────────────── 文本扫描 ─────────────────────────────


class _Mask:
    """已匹配片段屏蔽：被占用的字符在 view() 中替换为 \\x00，后续正则不会再命中。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.taken = [False] * len(text)

    def view(self) -> str:
        return "".join("\x00" if t else ch for ch, t in zip(self.text, self.taken))

    def free(self, start: int, end: int) -> bool:
        return not any(self.taken[start:end])

    def take(self, start: int, end: int) -> None:
        for i in range(start, end):
            self.taken[i] = True


@dataclass
class _UniverseHit:
    start: int
    end: int
    code: str | None
    name: str
    kind: str

    @property
    def phrase_len(self) -> int:
        return self.end - self.start


def _scan_universe_lexicon(mask: _Mask) -> list[_UniverseHit]:
    view = mask.view()
    hits: list[_UniverseHit] = []
    for pat, code, name, kind in catalog.UNIVERSE_LEXICON:
        for m in re.finditer(pat, view, re.IGNORECASE):
            if m.end() <= m.start():
                continue
            if kind == "all_a" and re.match(r"\s*(?:内|中|的)?\s*(?:分位|排名|中位)", view[m.end() : m.end() + 6]):
                continue  # “全市场分位”是分位口径，不是股票池
            hits.append(_UniverseHit(m.start(), m.end(), code, name, kind))
    hits.sort(key=lambda h: (h.start, -h.phrase_len))
    accepted: list[_UniverseHit] = []
    for h in hits:
        if mask.free(h.start, h.end):
            mask.take(h.start, h.end)
            accepted.append(h)
    return accepted


_RISK_PHRASES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"(?:剔除|排除|去掉|去除|不要|不含|不包括|过滤掉?|非)\s*\*?ST股?", re.IGNORECASE), "sys_st", "剔除风险警示（ST）股：由默认风控条件实现"),
    (re.compile(r"(?:剔除|排除|去掉|去除|不要|不含|不包括|过滤掉?)\s*次新股?"), "sys_new", "剔除上市不满一年的次新股：由默认风控条件实现"),
)


def _scan_risk(mask: _Mask) -> list[Interpretation]:
    out: list[Interpretation] = []
    for pat, cid, meaning in _RISK_PHRASES:
        for m in pat.finditer(mask.view()):
            if mask.free(m.start(), m.end()):
                mask.take(m.start(), m.end())
                out.append(Interpretation(phrase=m.group(0), meaning=meaning, condition_ids=[cid], confidence="high", origin="rule"))
                break
    return out


@dataclass
class _LexHit:
    entry: Mapping[str, Any]
    start: int
    end: int
    phrase: str


def _scan_lexicon(mask: _Mask) -> list[_LexHit]:
    """词典匹配：所有条目的所有命中按“更长优先、条目顺序、位置”贪心选取互不重叠的片段。

    这样“分红稳定”整体命中高股息，而不会让“稳定”被走势稳定抢走；每个条目只产生一个短语（取最靠前的命中）。
    """
    view = mask.view()
    cands: list[tuple[int, int, int, int]] = []  # (-长度, 条目序号, 起点, 终点)
    for ei, e in enumerate(catalog.LEXICON):
        for p in e["patterns"]:
            for m in re.finditer(p, view, re.IGNORECASE):
                if m.end() > m.start():
                    cands.append((-(m.end() - m.start()), ei, m.start(), m.end()))
    cands.sort()
    first: dict[int, tuple[int, int]] = {}
    for _neg_len, ei, s, e in cands:
        if mask.free(s, e):
            mask.take(s, e)
            if ei not in first or s < first[ei][0]:
                first[ei] = (s, e)
    hits = [_LexHit(catalog.LEXICON[ei], s, e, mask.text[s:e]) for ei, (s, e) in first.items()]
    hits.sort(key=lambda h: h.start)
    return hits


FORECAST_METRIC = re.compile(
    r"(?:预测|预期|一致预期|前瞻|远期)\s*(?:市盈率|PE|EPS|每股收益|净利润|利润|营收|收入|增速)|一致预期", re.IGNORECASE,
)
FORECAST_REASON = "预测/一致预期类指标需要分析师一致预期数据，当前数据源未提供，无法筛选"
FORECAST_ALTERNATIVE = "可改用历史口径，例如“市盈率TTM 处于行业较低分位”“最新报告期净利润同比为正”"


def _scan_forecast(mask: _Mask) -> list[Unsupported]:
    """“预测市盈率/一致预期EPS”：无一致预期数据 → unsupported，并屏蔽该片段（先于词典扫描）。"""
    out: list[Unsupported] = []
    for m in FORECAST_METRIC.finditer(mask.view()):
        if m.end() > m.start() and mask.free(m.start(), m.end()):
            mask.take(m.start(), m.end())
            if all(u.phrase != m.group(0) for u in out):
                out.append(Unsupported(phrase=m.group(0), reason=FORECAST_REASON, alternative=FORECAST_ALTERNATIVE))
    return out


def _scan_unsupported(mask: _Mask) -> list[Unsupported]:
    out: list[Unsupported] = []
    for pat, reason, alt in catalog.UNSUPPORTED_HINTS:
        for m in re.finditer(pat, mask.view(), re.IGNORECASE):
            if m.end() > m.start() and mask.free(m.start(), m.end()):
                mask.take(m.start(), m.end())
                if all(u.phrase != m.group(0) for u in out):
                    out.append(Unsupported(phrase=m.group(0), reason=reason, alternative=alt))
    return out


_BOARD = re.compile(r"概念板块|概念股|概念|行业|板块")
_BOARD_KIND = {"行业": "industry", "概念": "concept", "概念股": "concept", "概念板块": "concept", "板块": None}
_NAME_CHAR = re.compile(r"[一-鿿A-Za-z0-9]")
_LEADING_FILLER = re.compile(
    r"^(?:请|帮我|给我|我想|我要|想要|想|看看|看下|看|找找|找出|找|选出|选|筛选出|筛选|只看|仅看|换成|换为|换到|改成|改为|改到|"
    r"切换到|切换成|限定在|限定为|属于|位于|在|从|中|里|的|是)"
)
_MAX_BOARD_NAME = 10


@dataclass
class _BoardHit:
    start: int  # 名称起点（含前导填充词）
    end: int  # 关键词终点
    variants: list[str]  # 由长到短的候选名称（逐个去掉前导填充词）
    kind_hint: str | None
    keyword: str


def _scan_boards(mask: _Mask) -> list[_BoardHit]:
    view = mask.view()
    out: list[_BoardHit] = []
    for m in _BOARD.finditer(view):
        j = m.start()
        while j > 0 and m.start() - j < _MAX_BOARD_NAME and _NAME_CHAR.match(view[j - 1]):
            j -= 1
        core = view[j : m.start()]
        variants: list[str] = []
        cur = core
        while len(cur) >= 2:
            variants.append(cur)
            stripped = _LEADING_FILLER.sub("", cur, count=1)
            if stripped == cur or len(stripped) < 2:
                break
            cur = stripped
        if not variants:
            continue
        out.append(_BoardHit(j, m.end(), variants, _BOARD_KIND.get(m.group(0)), m.group(0)))
        mask.take(j, m.end())
        view = mask.view()
    return out


def _board_phrase(text: str, hit: _BoardHit) -> str:
    name = hit.variants[-1]
    i = text.find(name + hit.keyword, hit.start)
    return text[i : i + len(name) + len(hit.keyword)] if i >= 0 else name + hit.keyword


async def _universe_from_text(
    text: str, mask: _Mask, lex_hits: list[_UniverseHit], searcher: UniverseSearcher | None,
) -> UniverseDraft:
    boards = _scan_boards(mask)
    warnings: list[str] = []
    named = [mask.text[h.start : h.end] for h in lex_hits]
    if len(lex_hits) > 1:
        warnings.append(f"检测到多个股票池（{'、'.join(named)}），已使用第一个「{lex_hits[0].name}」，可在股票池选择中切换。")
    d = UniverseDraft()
    if boards:
        b = boards[0]
        d = await resolve_board(b.variants[-1], _board_phrase(text, b), searcher, origin="rule", kind_hint=b.kind_hint, variants=b.variants)
        if d.mentioned and lex_hits:
            warnings.append(f"股票池只能选一个：已使用「{universe_label(d.universe)}」，未使用「{named[0]}」，可在股票池选择中切换。")
        if len(boards) > 1:
            warnings.append(f"检测到多个行业/板块，已使用第一个（{b.variants[-1]}{b.keyword}）。")
    if not d.mentioned and lex_hits:
        h = lex_hits[0]
        base = preset_universe_draft(h.code, h.name, h.kind, mask.text[h.start : h.end], origin="rule")
        base.unsupported, base.warnings = d.unsupported, d.warnings
        d = base
    d.warnings = warnings + d.warnings
    return d


def _meaningful(text: str) -> bool:
    return bool(_NAME_CHAR.search(text or ""))


# ───────────────────────────── 入口 ─────────────────────────────


async def parse(query: str, base_strategy: Strategy | None = None, searcher: UniverseSearcher | None = None) -> ParseResult:
    """规则解析入口（含合规前置）。base_strategy 非空 = 修改模式（有限支持）。"""
    q = (query or "").strip()
    if not _meaningful(q):
        return ParseResult(intent="out_of_scope", parser="rule_fallback", warnings=[EMPTY_HINT])
    notice = compliance.check_input(q)
    if notice is not None and notice.level == "block":
        return await blocked_result(q, notice, base_strategy, searcher)
    if base_strategy is not None:
        result = await _Modifier(base_strategy, searcher).run(q)
    else:
        result = await parse_rules(q, searcher)
    if notice is not None:
        result.compliance = notice
        hint = compliance.history_warning(notice)
        if hint and result.strategy is not None and hint not in result.warnings:
            result.warnings = [hint, *result.warnings]
    return result


async def blocked_result(
    query: str, notice: ComplianceNotice, base_strategy: Strategy | None, searcher: UniverseSearcher | None,
) -> ParseResult:
    """预测/荐股/收益承诺：intent=prediction_request|advice_request；能转化的给出仅描述历史数据的草案。"""
    intent = "advice_request" if notice.category == "advice" else "prediction_request"
    result = ParseResult(intent=intent, compliance=notice, parser="rule_fallback")  # type: ignore[arg-type]
    rewrite = compliance.convertible_intent(query) if base_strategy is None else None
    if rewrite:
        draft = await parse_rules(rewrite, searcher)
        if draft.strategy is not None and any(c.origin == "rule" for c in draft.strategy.conditions):
            result.strategy = draft.strategy
            result.interpretations = draft.interpretations
            result.clarifications = draft.clarifications
            result.conflicts = draft.conflicts
            result.unsupported = draft.unsupported
            result.warnings = [
                f"原句无法按原意执行；已改写为“{rewrite}”，并按规则词典生成只描述历史数据的条件草案。"
                "该草案不代表未来走势，不构成投资建议，请检查后再决定是否执行。",
                *draft.warnings,
            ]
            return result
    result.warnings = [BLOCKED_HINT]
    return result


async def parse_rules(query: str, searcher: UniverseSearcher | None = None) -> ParseResult:
    """规则词典解析（不含合规检查）。"""
    q = query.strip()
    mask = _Mask(q)
    uni_hits = _scan_universe_lexicon(mask)
    risk = _scan_risk(mask)
    forecast = _scan_forecast(mask)
    lex = _scan_lexicon(mask)
    unsupported = forecast + _scan_unsupported(mask)
    ud = await _universe_from_text(q, mask, uni_hits, searcher)
    unsupported += ud.unsupported
    warnings = list(ud.warnings)

    if not lex and not ud.interpretations and not risk:
        return ParseResult(
            intent="out_of_scope", unsupported=unsupported, parser="rule_fallback", warnings=warnings + [OUT_OF_SCOPE_HINT],
        )
    phrases = [lexicon_phrase(h.entry, h.phrase) for h in lex]
    strategy, interps, clars = assemble_screen(q, phrases, ud, origin="rule", extra_interpretations=risk)
    if not phrases:
        warnings.append(NO_CONDITION_HINT)
    warnings += guard_negative_caps(strategy, clars)
    return ParseResult(
        intent="screen", strategy=strategy, interpretations=interps, clarifications=clars,
        conflicts=detect_conflicts(strategy), unsupported=unsupported, parser="rule_fallback", warnings=warnings,
    )


# ───────────────────────────── 修改模式（有限支持） ─────────────────────────────

_CLAUSE_SPLIT = re.compile(r"[，,。；;！!？?\n]+|然后|并且|同时|另外")
_LOOSEN = re.compile(r"放宽|放松|宽松|松一点|松一些|松些|降低要求|降低门槛|别那么严|不要那么严|不用那么严|没那么严")
_TIGHTEN = re.compile(r"收紧|严格|更严|严一点|严一些|严些|提高要求|提高门槛|再严")
_REMOVE = re.compile(r"去掉|去除|删除|删掉|移除|取消|不要|不看|拿掉|不用|别要")
_ACTION_WORDS = re.compile(
    r"放宽|放松|宽松|收紧|严格|更严|降低要求|降低门槛|提高要求|提高门槛|别那么严|不要那么严|不用那么严|没那么严|"
    r"去掉|去除|删除|删掉|移除|取消|不要|不看|拿掉|不用|别要|只看|仅看|换成|换为|换到|改成|改为|改到|切换到|切换成|限定在|限定为|"
    r"调整为|调整到|调到|调成|设为|设成|一点|一些|稍微|再|些"
)
Mode = Literal["loosen", "tighten", "set"]
_MODE_VERB: dict[str, str] = {"loosen": "放宽", "tighten": "收紧", "set": "修改"}
_GENERIC_BODY = re.compile(r"^(?:条件|整体|全部|所有|都|的|一下|筛选|标准|门槛|要求|阈值|\s)*$")
_TARGET_VALUE = re.compile(
    r"(?:调整为|调整到|设为|设成|改成|改为|调到|调成|到|至|为|成)\s*"
    r"(?P<scope>所属行业|行业|股票池|池内|全市场|全部A股|全A股|全A)?\s*(?:内|中)?\s*(?:的)?\s*(?:分位)?\s*"
    r"(?P<num>-?\d+(?:\.\d+)?)\s*(?P<pct>[%％])?\s*(?:分位)?"
)
_SCOPE_WORDS = {"所属行业": "industry", "行业": "industry", "股票池": "universe", "池内": "universe",
                "全市场": "market", "全部A股": "market", "全A股": "market", "全A": "market"}
_CATEGORY_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"估值", ("pe_ttm", "pb_mrq", "ps_ttm", "pcf_ttm")),
    (r"成长|增长|增速", ("revenue_yoy", "net_profit_yoy", "op_profit_yoy")),
    (r"盈利能力|ROE|净资产收益率", ("roe", "roe_annualized", "roe_deducted", "roe_change")),
    (r"现金流|现金含量|盈利质量|含金量", ("ocf_to_profit", "ocf_to_revenue")),
    (r"负债|杠杆|偿债", ("debt_ratio", "current_ratio")),
    (r"走势|波动|稳定|平稳", ("volatility_250d", "max_drawdown_250d")),
    (r"回撤|抗跌", ("max_drawdown_250d",)),
    (r"涨跌幅|涨幅|跌幅|趋势|动量|超跌", ("return_20d", "return_60d", "return_250d")),
    (r"流动性|成交", ("avg_amount_20d",)),
    (r"市值|规模|大盘|小盘|蓝筹|龙头", ("market_cap_est",)),
    (r"股息|分红", ("dividend_yield_ttm",)),
    (r"毛利", ("gross_margin", "gross_margin_change")),
    (r"净利率", ("net_margin",)),
    (r"Beta|贝塔|防御", ("beta_250d",)),
)
_MOD_VERB = re.compile(
    rf"{_REMOVE.pattern}|{_LOOSEN.pattern}|{_TIGHTEN.pattern}|调整为|调整到|调到|调成|调为|改成|改为|改到|设为|设成"
)


def _clause_categories(clause: str) -> list[tuple[str, set[str]]]:
    """分句里提到的指标类别：(原文用词, 对应指标 id)。类别词优先，其次指标名/别名。"""
    out: list[tuple[str, set[str]]] = []
    covered: set[str] = set()
    for pat, mids in _CATEGORY_WORDS:
        m = re.search(pat, clause, re.IGNORECASE)
        if m:
            out.append((m.group(0), set(mids)))
            covered.update(mids)
    low = clause.lower()
    for md in catalog.METRICS.values():
        if md.id in covered:
            continue
        name = next((n for n in [md.name, *md.aliases] if n and n.lower() in low), None)
        if name:
            out.append((name, {md.id}))
    return out


def missing_category_warnings(query: str, present_metrics: Iterable[str]) -> list[str]:
    """修改句里有删除/调整动词、但提到的类别在 present_metrics 中没有任何条件 → “当前策略中没有××条件，该部分未生效。”

    present_metrics 应为修改前与修改后策略的指标并集：修改时新增了该类条件、或该类条件本来存在（已被删除/调整），都不提示。
    """
    present = set(present_metrics)
    out: list[str] = []
    for clause in _CLAUSE_SPLIT.split(query or ""):
        if not clause.strip() or not _MOD_VERB.search(clause):
            continue
        for word, mids in _clause_categories(clause):
            msg = f"当前策略中没有{word}条件，该部分未生效。"
            if not (mids & present) and msg not in out:
                out.append(msg)
    return out


_ST_EXCLUDE = re.compile(r"(?:剔除|排除|去掉|去除|不要|不含|不包括|过滤掉?)\s*\*?ST股?(?!\s*(?:条件|限制|过滤))", re.IGNORECASE)
_ST_KEEP = re.compile(
    r"(?:保留|包含|包括|不剔除|不排除|允许)\s*\*?ST股?|(?:去掉|删除|删掉|移除|取消)\s*\*?ST\s*(?:条件|限制|过滤)", re.IGNORECASE,
)
_NEW_EXCLUDE = re.compile(r"(?:剔除|排除|去掉|去除|不要|不含|不包括|过滤掉?)\s*次新股?(?!\s*(?:条件|限制|过滤))")
_NEW_KEEP = re.compile(r"(?:保留|包含|包括|不剔除|不排除|允许)\s*次新股?|(?:去掉|删除|删掉|移除|取消)\s*次新\s*(?:条件|限制|过滤)")
_STEP_RATIO = 0.25  # 数值阈值放宽/收紧 25%
_STEP_RANK = 20.0  # 分位放宽/收紧 20 个点


def _scaled(c: Condition, loosen: bool) -> Condition:
    """数值阈值朝放宽（loosen=True）或收紧方向调 25%；分位调 20 个点（限制在 0~100）。"""
    n = c.model_copy(deep=True)

    def step(v: float, up: bool) -> float:
        return round(v + (1 if up else -1) * _STEP_RATIO * abs(v), 4)

    if c.op in catalog.RANK_OPS and c.value is not None:
        widen = (c.op == "pct_rank_lte") == loosen
        n.value = round(min(100.0, max(0.0, c.value + (_STEP_RANK if widen else -_STEP_RANK))), 4)
    elif c.op in ("gt", "gte") and c.value is not None:
        n.value = step(c.value, up=not loosen)
    elif c.op in ("lt", "lte") and c.value is not None:
        n.value = step(c.value, up=loosen)
    elif c.op == "between" and c.value is not None and c.value2 is not None:
        lo, hi = sorted((c.value, c.value2))
        n.value, n.value2 = step(lo, up=not loosen), step(hi, up=loosen)
    return n


class _Modifier:
    """规则修改：逐分句识别 放宽/收紧/去掉/只看/新增，并生成中文变更说明。"""

    def __init__(self, base: Strategy, searcher: UniverseSearcher | None) -> None:
        self.base = normalize(base)
        self.searcher = searcher
        self.conds = [c.model_copy(deep=True) for c in self.base.conditions]
        self.universe = self.base.universe.model_copy(deep=True)
        self.changes: list[str] = []
        self.warnings: list[str] = []
        self.unsupported: list[Unsupported] = []
        self.interps: list[Interpretation] = []

    async def run(self, query: str) -> ParseResult:
        for clause in (c.strip() for c in _CLAUSE_SPLIT.split(query)):
            if clause:
                await self._clause(clause)
        if not self.changes:
            return ParseResult(
                intent="modify", unsupported=self.unsupported, interpretations=self.interps, parser="rule_fallback",
                warnings=self.warnings + [NO_MODIFICATION_HINT],
            )
        new = normalize(self.base.model_copy(update={"conditions": self.conds, "universe": self.universe}, deep=True))
        return ParseResult(
            intent="modify", strategy=new, interpretations=self.interps, conflicts=detect_conflicts(new),
            unsupported=self.unsupported, changes=self.changes, parser="rule_fallback", warnings=self.warnings,
        )

    # ── 分句 ──

    def _next_cid(self) -> str:
        used = {c.id for c in self.conds}
        n = 1
        while f"c{n}" in used:
            n += 1
        return f"c{n}"

    async def _clause(self, clause: str) -> None:
        text = self._risk(clause)
        if not _meaningful(text) or _GENERIC_BODY.match(text):
            return
        fm = FORECAST_METRIC.search(text)
        if fm is not None:
            # “放宽预测市盈率”：策略里不可能有预测类条件，也不能拿市盈率TTM代替 → 整个分句只记 unsupported
            if all(u.phrase != fm.group(0) for u in self.unsupported):
                self.unsupported.append(Unsupported(phrase=fm.group(0), reason=FORECAST_REASON, alternative=FORECAST_ALTERNATIVE))
            return
        before = {c.metric for c in self.conds}
        miss: Unsupported | None = None  # “没有找到要修改的条件”：若是因为该类条件本就不存在，改为 warning
        if _LOOSEN.search(text):
            miss = self._adjust(text, clause, "loosen")
        elif _TIGHTEN.search(text):
            miss = self._adjust(text, clause, "tighten")
        elif _REMOVE.search(text):
            miss = self._remove(text, clause)
        elif _TARGET_VALUE.search(text) and self._targets(text):
            miss = self._adjust(text, clause, "set")
        else:
            mask = _Mask(text)
            switched = await self._switch_universe(text, mask)
            added = self._add(mask)
            if not switched and not added:
                miss = Unsupported(
                    phrase=clause, reason="没有识别出要做的修改",
                    alternative="可以说“放宽/收紧 + 指标”“去掉 + 指标或原短语”“只看 + 股票池”，或直接描述要新增的条件",
                )
        # 要删除/调整的类别在当前策略中不存在（修改前后都没有）→ 明确提示该部分未生效，不静默忽略
        absent = missing_category_warnings(text, before | {c.metric for c in self.conds})
        self.warnings += [m for m in absent if m not in self.warnings]
        if miss is not None and not absent:
            self.unsupported.append(miss)

    def _risk(self, clause: str) -> str:
        """处理“剔除/保留 ST、次新”；返回去掉这些片段后的分句。"""
        text = clause
        for exclude, keep, sys_id in ((_ST_EXCLUDE, _ST_KEEP, "sys_st"), (_NEW_EXCLUDE, _NEW_KEEP, "sys_new")):
            template = next(c for c in catalog.DEFAULT_CONDITIONS if c.id == sys_id)
            m_keep = keep.search(text)
            if m_keep:
                text = text.replace(m_keep.group(0), " ")
                hit = [c for c in self.conds if semantic_key(c) == semantic_key(template)]
                for c in hit:
                    self.conds.remove(c)
                    self.changes.append(f"删除条件：{describe(c)}")
                if not hit:
                    self.warnings.append(f"当前没有「{describe(template)}」条件，无需删除。")
                continue
            m_ex = exclude.search(text)
            if m_ex:
                text = text.replace(m_ex.group(0), " ")
                same = [c for c in self.conds if semantic_key(c) == semantic_key(template)]
                if any(c.enabled for c in same):
                    self.warnings.append(f"已有条件「{describe(template)}」，无需重复添加。")
                elif same:
                    same[0].enabled = True
                    self.changes.append(f"启用条件：{describe(same[0])}")
                else:
                    self.conds.append(template.model_copy(deep=True))
                    self.changes.append(f"新增条件：{describe(template)}")
        return text.strip()

    # ── 目标条件 ──

    def _targets(self, text: str) -> list[int]:
        body = _TARGET_VALUE.sub(" ", _ACTION_WORDS.sub(" ", text))
        by_phrase: set[int] = set()
        entry = lexicon_entry_for(body)
        for i, c in enumerate(self.conds):
            sp = c.source_phrase or ""
            if not sp:
                continue
            if sp in body or (entry is not None and any(re.search(p, sp, re.IGNORECASE) for p in entry["patterns"])):
                by_phrase.add(i)
        if by_phrase:
            return sorted(by_phrase)
        metrics = _metrics_in(body)
        return [i for i, c in enumerate(self.conds) if c.metric in metrics]

    def _generic(self, text: str) -> bool:
        body = _TARGET_VALUE.sub(" ", _ACTION_WORDS.sub(" ", text))
        return bool(_GENERIC_BODY.match(body))

    # ── 动作 ──

    def _adjust(self, text: str, clause: str, mode: Mode) -> Unsupported | None:
        """放宽/收紧/改为某值；找不到目标条件时返回待定的 unsupported（由 _clause 决定是否改为 warning）。"""
        verb = _MODE_VERB[mode]
        targets = self._targets(text)
        if not targets and mode != "set" and self._generic(text):
            targets = [i for i, c in enumerate(self.conds) if c.enabled and c.origin != "system" and op_family(c.op) != "bool"]
        if not targets:
            return Unsupported(
                phrase=clause, reason=f"没有找到要{verb}的条件", alternative=f"请写明指标或原短语，例如“{verb}估值”“{verb}波动率”",
            )
        explicit = _TARGET_VALUE.search(text)
        applied = False
        zero: list[Condition] = []
        for i in targets:
            old = self.conds[i]
            if explicit is not None:
                new = self._explicit(old, explicit, mode)
            elif mode == "set":
                new = None
            else:
                new = _scaled(old, mode == "loosen")
            if new is None:
                continue
            if semantic_key(new) == semantic_key(old):
                if explicit is None and old.value == 0:
                    zero.append(old)
                continue
            self.conds[i] = new
            self.changes += condition_changes(old, new, verb=direction_verb(old, new))
            applied = True
        if applied:
            return None
        if explicit is not None:
            self.unsupported.append(Unsupported(
                phrase=clause, reason="相关条件的口径或方向与给定数值不匹配，未能修改",
                alternative="可在条件编辑器中直接修改阈值或分位口径",
            ))
        for c in zero:
            self.warnings.append(f"「{describe(c)}」阈值为 0，按比例{verb}不改变阈值，可在条件编辑器中直接修改。")
        return None

    def _explicit(self, c: Condition, m: re.Match[str], mode: Mode) -> Condition | None:
        """“放宽到行业70%分位”“收紧到30倍”“改成20%”：按给定数值修改。

        口径不匹配（分位值作用于数值条件等）返回 None；放宽/收紧时只接受朝该方向的修改，
        避免把“PE > 0”这类前提条件改成“PE > 30”。
        """
        num = float(m.group("num"))
        scope = _SCOPE_WORDS.get(m.group("scope") or "")
        rank_mode = "分位" in m.group(0) or scope is not None
        n = c.model_copy(deep=True)
        scope_changed = False
        if c.op in catalog.RANK_OPS:
            if not (rank_mode or m.group("pct")):
                return None
            if not 0 <= num <= 100:
                self.warnings.append(f"分位取值应在 0~100 之间，未按 {fmt_num(num)} 修改「{describe(c)}」。")
                return None
            n.value = num
            if scope is not None and scope != c.rank_scope:
                if scope in catalog.METRICS[c.metric].rank_scopes:
                    n.rank_scope = scope  # type: ignore[assignment]
                    scope_changed = True
                else:
                    self.warnings.append(f"「{catalog.METRICS[c.metric].name}」不支持{catalog.RANK_SCOPE_LABEL[scope]}分位，保留原口径。")
        elif rank_mode or op_family(c.op) != "numeric":
            return None
        elif c.op == "between":
            if c.value is None or c.value2 is None:
                return None
            lo, hi = sorted((c.value, c.value2))
            lower_better = catalog.METRICS[c.metric].higher_is_better is False
            if mode == "loosen":
                lo, hi = (lo, num) if num > hi else (num, hi) if num < lo else (lo, hi)
            elif mode == "tighten" and lo < num < hi:
                lo, hi = (lo, num) if lower_better else (num, hi)
            else:
                return None
            n.value, n.value2 = lo, hi
        else:
            n.value = num
        if mode != "set" and not scope_changed and direction_verb(c, n) != _MODE_VERB[mode]:
            return None
        return n

    def _remove(self, text: str, clause: str) -> Unsupported | None:
        """删除目标条件；找不到时返回待定的 unsupported（由 _clause 决定是否改为 warning）。"""
        targets = self._targets(text)
        if not targets:
            return Unsupported(
                phrase=clause, reason="没有找到要删除的条件", alternative="请写明指标或原短语，例如“去掉回撤条件”“删除估值合理”",
            )
        drop = set(targets)
        for i in sorted(drop):
            self.changes.append(f"删除条件：{describe(self.conds[i])}")
        self.conds = [c for i, c in enumerate(self.conds) if i not in drop]
        return None

    async def _switch_universe(self, text: str, mask: _Mask) -> bool:
        hits = _scan_universe_lexicon(mask)
        boards = _scan_boards(mask)
        new: Universe | None = None
        if boards:
            b = boards[0]
            d = await resolve_board(
                b.variants[-1], _board_phrase(text, b), self.searcher, origin="rule", kind_hint=b.kind_hint, variants=b.variants,
            )
            self.warnings += d.warnings
            self.unsupported += d.unsupported
            if d.mentioned:
                new = d.universe
                if d.choices:
                    others = "、".join(o.name for o in d.choices[1:])
                    self.warnings.append(f"“{d.choice_query}”有多个候选股票池，已使用「{d.choices[0].name}」；其他候选：{others}。")
        if new is None and hits:
            h = hits[0]
            new = preset_universe_draft(h.code, h.name, h.kind, text[h.start : h.end], origin="rule").universe
        if new is None:
            return bool(boards)
        if universe_key(new) != universe_key(self.universe):
            self.changes.append(f"股票池：{universe_label(self.universe)} → {universe_label(new)}")
            self.universe = new
        else:
            self.warnings.append(f"股票池已是「{universe_label(new)}」。")
        return True

    def _add(self, mask: _Mask) -> bool:
        hits = _scan_lexicon(mask)
        for h in hits:
            draft = lexicon_phrase(h.entry, h.phrase)
            ids: list[str] = []
            for c in draft.conditions:
                if any(semantic_key(x) == semantic_key(c) and x.enabled for x in self.conds):
                    self.warnings.append(f"已有条件「{describe(c)}」，未重复添加。")
                    continue
                c.id = self._next_cid()
                ids.append(c.id)
                self.conds.append(c)
                self.changes.append(f"新增条件：{describe(c)}")
            self.interps.append(Interpretation(phrase=h.phrase, meaning=h.entry["meaning"], condition_ids=ids, confidence="medium", origin="rule"))
        return bool(hits)


def _metrics_in(body: str) -> set[str]:
    """句中提到的指标：指标名、别名（不区分大小写）与类别词。"""
    low = body.lower()
    out: set[str] = set()
    for m in catalog.METRICS.values():
        if any(n and n.lower() in low for n in [m.name, *m.aliases]):
            out.add(m.id)
    for pat, mids in _CATEGORY_WORDS:
        if re.search(pat, body, re.IGNORECASE):
            out.update(mids)
    return out


def guard_negative_caps(strategy: Strategy, clarifications: list[Clarification]) -> list[str]:
    """AI/规则生成的“PE < X”类条件：估值倍数为负（亏损等）也会满足上限，自动改为“介于 0 ~ X”并说明。"""
    from ..engine.conditions import describe as _describe

    names: list[str] = []

    def lower_ok(c: Condition) -> bool:
        if c.op in ("gt", "gte") and c.value is not None:
            return c.value >= 0
        return c.op == "between" and c.value is not None and c.value2 is not None and min(c.value, c.value2) >= 0

    def fix(conds: list[Condition]) -> None:
        for c in conds:
            md = catalog.METRICS.get(c.metric)
            if md is None or not md.positive_only_for_rank or c.op not in ("lt", "lte") or c.value is None or c.value <= 0:
                continue
            if c.origin not in ("ai", "rule") or any(lower_ok(o) for o in conds if o is not c and o.metric == c.metric):
                continue
            c.op, c.value2, c.value = "between", float(c.value), 0.0
            c.rationale = ((c.rationale + "；") if c.rationale else "") + "自动补充下限 0：排除亏损（负值）"
            c.label = _describe(c)
            names.append(md.name)

    fix(strategy.conditions)
    for cl in clarifications:
        for o in cl.options:
            fix(o.conditions)
    if not names:
        return []
    return [f"“{'、'.join(dict.fromkeys(names))}”的上限条件已自动改为“介于 0 ~ 上限”，以排除亏损或负值公司；如需包含负值，可在条件表中改回“小于”。"]
