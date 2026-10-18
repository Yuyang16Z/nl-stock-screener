"""输入/输出合规守卫（DESIGN §6.5）。全部为纯函数。

- check_input：识别涨跌预测、荐股/买卖建议、收益承诺三类请求（block），以及“反弹/黑马”等弱预测措辞、
  “有望走强/即将突破”等前瞻措辞、未与涨跌搭配的未来时间词（warn）；
- history_warning：warn 中的前瞻措辞/未来时间词 → 解析 warnings 里的“已按历史走势描述处理，不代表未来”说明；
- convertible_intent：把“会涨的股票/有潜力的股票”这类说法改写为只描述历史数据的句子，供规则解析生成草案；
  含未来时间词或“翻倍/涨停/大涨”的说法不改写（只拦截）；
- sanitize_output：删除模型输出中含买卖建议、目标价、收益承诺、涨跌预测的句子，并返回命中的禁语。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from ..catalog import DISCLAIMER
from ..schemas import ComplianceNotice

__all__ = [
    "DISCLAIMER",
    "UP_TREND_DESCRIPTION",
    "check_input",
    "convertible_intent",
    "history_warning",
    "sanitize_output",
]

Category = str  # "prediction" | "advice" | "return_promise"

_CLAUSE_STOP = "，,。；;！!？?\n"

# ───────────────────────────── 输入侧 ─────────────────────────────

# 指向未来的时间词（“近期/最近/今年”描述的是历史或当期，不在此列；“汽车后市场”不是“后市”）
_FUTURE = (
    r"(?:明天|明日|后天|下周|下星期|下礼拜|下个?月|下半年|明年|接下来|未来|今后|往后|后市(?!场)|短期内|年底前?|月底前?|节后|"
    r"下一?个?交易日)"
)
_FUTURE_RE = re.compile(_FUTURE)
# 价格/收益类走势词：与时间词搭配即为时间限定的涨跌预测（“涨”覆盖涨停、大涨）
_MOVE = r"(?:涨|跌|走势|行情|股价|表现|反弹|收益|赚|翻倍|翻番|走强|走弱|回升|上行|下行|突破|看好)"
# 业绩类词：与时间词搭配时，还要有情态词（会/将/能/要……）才算预测（“未来业绩会增长吗” vs “接下来看业绩增长快的”）
_FUNDAMENTAL = r"(?:业绩|盈利|利润|营收)"
_MODAL = r"(?:会不会|能不能|可不可以|还会|还能|将要|将会|能够|可能|会|将|能|要)"
# 输入侧识别用（含翻倍/翻番：“能翻倍”既是预测也是收益承诺）；改写只用 _REWRITE_UP，不含翻倍/涨停/大涨
_UP_MOVE = r"(?:上涨|暴涨|大涨|拉升|涨停|走强|翻倍|翻番|涨)"
_DOWN_MOVE = r"(?:下跌|暴跌|大跌|跌停|走弱|跌)"
_NC = f"[^{_CLAUSE_STOP}]"  # 同一分句内的任意字符

# 句首口语过渡词（“接下来看……”“然后帮我找……”）：不是时间限定，检查前先屏蔽
_TRANSITION = re.compile(
    r"^\s*(?:接下来|然后)[，,、\s]*"
    r"(?=(?:我们?|咱们?|请|再|要|想|来|就)?(?:看|找|筛|选|帮|给|查|换|改|加|去|把|列|搜|按|只))"
)

# 时间限定的涨跌预测、目标价、“预测股价”：拦截且不做改写（不能把对未来的提问换成历史条件来回答）
_FIRM_PREDICTION: list[re.Pattern[str]] = [
    # “明天/下周……涨跌/翻倍”：只标出时间词本身
    re.compile(rf"{_FUTURE}(?={_NC}{{0,8}}?{_MOVE})"),
    # “明年业绩会……”“下半年会不会盈利……”：时间词 + 业绩词 + 情态词
    re.compile(rf"{_FUTURE}(?={_NC}{{0,8}}?(?:{_MODAL}{_NC}{{0,6}}?{_FUNDAMENTAL}|{_FUNDAMENTAL}{_NC}{{0,4}}?{_MODAL}))"),
    # “未来业绩增长快”“明年盈利改善”：对未来基本面的判断，同样只拦截、不改写成历史条件
    re.compile(rf"{_FUTURE}(?={_NC}{{0,8}}?{_FUNDAMENTAL}{_NC}{{0,4}}?(?:增长|增速|提升|改善|向好|好转|高增|下滑|下降|恶化))"),
    re.compile(r"(?:涨到|能到|会到)\s*(?:多少|\d)|跌到多少|(?:能|会)突破\s*\d"),
    re.compile(r"目标价|目标位|目标股价"),
    # “预测市盈率/一致预期 EPS”是指标名（无一致预期数据，由解析放入 unsupported），不按预测请求拦截
    re.compile(r"预测(?!\s*(?:市盈率|PE|EPS|每股收益|净利润|利润|营收|收入|增速))|预判", re.IGNORECASE),
]
_PREDICTION: list[re.Pattern[str]] = [
    *_FIRM_PREDICTION,
    # “会涨/能翻倍/还会跌”（“有机会/动能/主要”里的“会/能/要”不是情态词）
    re.compile(rf"(?<![机动主]){_MODAL}(?:大|继续|持续|再|一直)?(?:反弹|{_UP_MOVE}|{_DOWN_MOVE})"),
    re.compile(r"(?:走势|股价|行情|价格)(?:会|将|能)"),
    re.compile(r"有潜力|潜力股|潜力大|潜力足|成长潜力|增长潜力|上涨潜力|上涨空间|升值空间"),
]

_ADVICE: list[re.Pattern[str]] = [
    re.compile(r"推荐|荐股"),
    re.compile(
        r"(?:该不该|要不要|能不能|可不可以|值不值得|适不适合|应不应该|是否应该|是否|应该|该|要|能|可以|值得|适合)\s*"
        r"(?:买入|买进|卖出|买|卖|入手|上车|持有|加仓|减仓|清仓|抛售|抛|割肉|止损|补仓|建仓|抄底|布局|出手)"
    ),
    re.compile(r"买(?:什么|哪只|哪支|哪个|哪些|啥)"),
    re.compile(r"抄底|满仓|全仓|梭哈|all\s*in|重仓买入|清仓|割肉|止盈|止损|仓位", re.IGNORECASE),
    re.compile(r"买点|卖点|(?:买入|卖出|入场|进场|出场)时机|什么时候(?:买|卖|入场|进场|出手)"),
    re.compile(r"(?:买入|卖出|买卖|投资|操作)建议"),
]

_RETURN_PROMISE: list[re.Pattern[str]] = [
    re.compile(
        r"稳赚不赔|稳赚|包赚|躺赚|稳赢|必赚|只赚不赔|保本|保底收益|保证收益|收益有保障|保收益|必涨|只涨不跌|稳涨|"
        r"十倍股|百倍股|零风险|没有风险|不会亏|不亏钱"
    ),
    re.compile(r"无风险(?!利率)"),
    re.compile(r"翻倍|翻番"),  # 历史描述（“近一年涨幅翻倍”）除外，见 _is_historical
]

# 弱预测措辞：不拦截，只提示（解析照常进行）
_WARN: list[re.Pattern[str]] = [re.compile(r"反弹|黑马|牛股|妖股|起飞|主升浪|拉升|飙升")]
# 前瞻措辞（与输出侧禁语“有望走强”同口径）：至少 warn，并按历史走势描述处理（“翻倍”另按收益承诺拦截）
_FORWARD: list[re.Pattern[str]] = [
    re.compile(r"(?:有望|即将|大概率|有机会|可望)\s*(?:继续|持续|再度|再次)?\s*(?:上涨|反弹|走强|突破|大涨|涨停|翻倍|回升|上行)"),
    # “看涨/看多”（输出侧禁语）；“看看涨幅”“查看涨跌幅”“看多少只”不算
    re.compile(r"(?<![看查])看(?:涨|跌|多|空)(?![幅跌涨少间])|唱多|唱空"),
]
# 未与涨跌/业绩情态搭配的未来时间词（“未来三年业绩增长快”）：warn，按历史数据处理
_FUTURE_WARN: list[re.Pattern[str]] = [_FUTURE_RE]

_HISTORY_HINT = re.compile(r"近|过去|去年|前年|已经|已|历史|曾经|曾|累计|涨幅|期间|以来")

_CATEGORY_ORDER: tuple[Category, ...] = ("prediction", "advice", "return_promise")
_PATTERNS: dict[Category, list[re.Pattern[str]]] = {
    "prediction": _PREDICTION,
    "advice": _ADVICE,
    "return_promise": _RETURN_PROMISE,
}
_MESSAGES: dict[Category, str] = {
    "prediction": "本产品不预测股价涨跌、目标价或未来表现。",
    "advice": "本产品不推荐个股，也不提供买入、卖出、持仓或仓位等投资建议。",
    "return_promise": "任何投资都有风险，本产品不承诺收益，不存在“稳赚、保本、必涨”之类的筛选条件。",
}
_SUGGESTION = "可以改为描述可观察的数据特征，例如“近60日涨跌幅为正、估值合理、现金流好”，由引擎按条件筛选并给出逐条依据。"
_WARN_MESSAGE = "“反弹、黑马”等说法涉及对未来走势的判断：本产品只按历史与当期数据筛选，不预测涨跌，结果不代表未来表现。"
_FORWARD_MESSAGE = "“有望走强、即将突破”等说法是对未来走势的判断：本产品不预测涨跌，只能按历史走势描述筛选（如近60日涨跌幅），结果不代表未来表现。"
_FUTURE_MESSAGE = "“未来、下半年”等时间词指向未来：本产品只按历史与当期数据筛选，相关说法按最新报告期/历史区间的数据处理，结果不代表未来表现。"
_FORWARD_HINT = "“{words}”涉及对未来走势的判断，已按历史走势描述处理，不代表未来表现。"
_FUTURE_HINT = "“{words}”是对未来的表述，已按历史数据描述处理，不代表未来表现。"


def _clause_prefix(text: str, start: int, width: int = 8) -> str:
    """match 起点之前、同一分句内的最多 width 个字符。"""
    i = start
    while i > 0 and start - i < width and text[i - 1] not in _CLAUSE_STOP:
        i -= 1
    return text[i:start]


def _is_historical(text: str, m: re.Match[str]) -> bool:
    return m.group(0) in ("翻倍", "翻番") and bool(_HISTORY_HINT.search(_clause_prefix(text, m.start())))


def _scan(text: str, patterns: Iterable[re.Pattern[str]]) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for p in patterns:
        for m in p.finditer(text):
            if m.group(0) and not _is_historical(text, m):
                hits.append((m.start(), m.group(0)))
    return hits


def _mask_transition(text: str) -> str:
    """把句首口语过渡词（“接下来看……”）替换为等长的分句符，保持其余位置不变。"""
    m = _TRANSITION.match(text)
    return text if m is None else "，" * m.end() + text[m.end():]


def _ordered_unique(hits: Sequence[tuple[int, str]]) -> list[str]:
    out: list[str] = []
    for _, word in sorted(hits):
        if word not in out:
            out.append(word)
    return out


def check_input(text: str) -> ComplianceNotice | None:
    """输入合规检查：预测/荐股/收益承诺 → block；弱预测措辞、前瞻措辞（有望走强）、单独的未来时间词 → warn；其余 None。

    句首口语过渡词（“接下来看……”）不算时间限定；时间词与业绩/盈利搭配时须有情态词（会/将/能/要）才算预测。
    matched 为原文中的触发片段（按出现顺序去重），前端用于高亮。
    """
    t = _mask_transition(text or "")
    found: dict[Category, list[tuple[int, str]]] = {c: _scan(t, _PATTERNS[c]) for c in _CATEGORY_ORDER}
    cats = [c for c in _CATEGORY_ORDER if found[c]]
    if cats:
        hits = [h for c in cats for h in found[c]]
        message = "".join(_MESSAGES[c] for c in cats) + _SUGGESTION
        return ComplianceNotice(level="block", category=cats[0], message=message, matched=_ordered_unique(hits))  # type: ignore[arg-type]
    forward, warn, future = _scan(t, _FORWARD), _scan(t, _WARN), _scan(t, _FUTURE_WARN)
    if forward or warn or future:
        message = _FORWARD_MESSAGE if forward else _WARN_MESSAGE if warn else _FUTURE_MESSAGE
        return ComplianceNotice(level="warn", category="prediction", message=message, matched=_ordered_unique(forward + warn + future))
    return None


def _outer_words(words: Sequence[str]) -> list[str]:
    """去掉被其他命中片段包含的片段（“有望反弹”“反弹” → “有望反弹”），保持顺序。"""
    return [w for w in words if not any(w != o and w in o for o in words)]


def history_warning(notice: ComplianceNotice | None) -> str | None:
    """warn 提示中的前瞻措辞/未来时间词 → 写进解析 warnings 的说明（“已按历史走势描述处理，不代表未来”）；其余 None。"""
    if notice is None or notice.level != "warn":
        return None
    forward = [w for w in notice.matched if any(p.search(w) for p in _FORWARD)]
    future = [w for w in notice.matched if _FUTURE_RE.search(w) and w not in forward]
    words = _outer_words(forward + future)
    if not words:
        return None
    return (_FORWARD_HINT if forward else _FUTURE_HINT).format(words="、".join(words))


# ───────────────────────────── 可转化意图 ─────────────────────────────

UP_TREND_DESCRIPTION = "近60日趋势向上（仅描述历史，不代表未来）"
_GROWTH_DESCRIPTION = "高成长（按最新报告期的历史增速）"

# 改写：把对未来的期望换成可观察的历史描述（“翻倍/涨停/大涨”不改写，含这些词的句子由 _NO_REWRITE 直接拦截）
_REWRITE_UP = r"(?:上涨|拉升|走强|涨)"
_REWRITES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?:成长|增长)潜力(?:大|足|高)?的?"), f"{_GROWTH_DESCRIPTION}的"),
    (re.compile(r"潜力股"), f"{UP_TREND_DESCRIPTION}的股票"),
    (re.compile(r"(?:有|具有|具备)?(?:上涨)?潜力(?:大|足)?的?|(?:有)?(?:上涨|升值)空间(?:大)?的?"), f"{UP_TREND_DESCRIPTION}的"),
    (re.compile(rf"(?:大概率|很可能|有望|即将|有机会|可望)\s*(?:继续|持续|再度|再次)?\s*(?:{_REWRITE_UP}|上行|回升)的?"),
     f"{UP_TREND_DESCRIPTION}的"),
    (re.compile(rf"(?<![机动主])(?:还会|还能|将要|将会|能够|可能|会|将|能|要)(?:继续|持续|再|一直)?{_REWRITE_UP}的?"), f"{UP_TREND_DESCRIPTION}的"),
]
# 含未来时间词（含句首的“接下来”）或“翻倍/翻番/涨停/大涨”的说法：只拦截，不给可执行草案
# （“下周能翻倍”“明年能翻倍”不能改写成“近60日趋势向上”；历史语境的“近一年涨幅翻倍”也不改写）
_NO_REWRITE: list[re.Pattern[str]] = [
    re.compile(rf"{_FUTURE}|今年内|今年之内"),
    re.compile(r"翻倍|翻番|涨停|大涨|暴涨"),
]
# 删除：荐股、买卖动作与收益承诺的措辞、前瞻副词（剩余部分若仍有可筛选的描述，则交给规则解析）
_REMOVALS: list[re.Pattern[str]] = [
    re.compile(r"大概率|很可能|极可能|有望|即将|有机会|可望"),
    re.compile(r"(?:强烈)?推荐(?:一下|几只|几个|一些|一只|个|只|点)?(?:买入|买进|购买|买)?|荐股"),
    re.compile(
        r"(?:该不该|要不要|能不能|可不可以|值不值得|适不适合|应不应该|是否应该|是否|应该|该|要|能|可以|值得|适合)\s*"
        r"(?:买入|买进|卖出|买|卖|入手|上车|持有|加仓|减仓|清仓|抛售|抛|割肉|止损|补仓|建仓|抄底|布局|出手)的?"
    ),
    re.compile(r"买(?:什么|哪只|哪支|哪个|哪些|啥)"),
    re.compile(r"抄底|满仓|全仓|梭哈|all\s*in|重仓买入|清仓|割肉|止盈|止损|仓位", re.IGNORECASE),
    re.compile(r"买入|买进|卖出"),
    re.compile(
        r"(?:稳赚不赔|稳赚|包赚|躺赚|稳赢|必赚|只赚不赔|保本|保底收益|保证收益|收益有保障|保收益|必涨|只涨不跌|稳涨|"
        r"十倍股|百倍股|零风险|没有风险|不会亏|不亏钱|无风险|翻倍|翻番)的?"
    ),
]
_FILLER = re.compile(
    r"股票|个股|公司|标的|哪些|哪只|哪支|哪个|什么|有没有|有|是|请|帮我|给我|一下|几只|几个|一些|一只|那些|这些|还有|可以|"
    r"[的了吗呢吧啊呀哦么嘛和与及\s，,。.；;！!？?、~～…:：]"
)


def _tidy(text: str) -> str:
    t = re.sub(r"的{2,}", "的", text)
    t = re.sub(r"[，,、；;]{2,}", "，", t)
    t = re.sub(r"\s{2,}", " ", t)
    t = t.strip(" ，,、；;。.！!？?的")
    return t


def convertible_intent(text: str) -> str | None:
    """能转化为可观察条件的意图 → 改写后的句子（供规则解析）；无法转化 → None。

    时间限定的涨跌预测（“明天会涨”“下周能翻倍”）、含未来时间词（明天/下周/明年/今年内/未来/接下来/后市……）
    或“翻倍/翻番/涨停/大涨”的说法、目标价、“预测股价”、看跌预测等不做转化（返回 None，只拦截）。
    """
    t = (text or "").strip()
    if not t or _scan(t, _FIRM_PREDICTION) or any(p.search(t) for p in _NO_REWRITE):
        return None
    m = _TRANSITION.match(t)
    if m is not None:  # 句首口语过渡词（“然后帮我找……”）不留在改写句里
        t = t[m.end():].strip()
    out = t
    for pat, repl in _REWRITES:
        out = pat.sub(repl, out)
    for pat in _REMOVALS:
        out = pat.sub("", out)
    out = _tidy(out)
    if not out or out == t:
        return None
    # 复查用户原有的其余措辞；插入的历史描述本身含“不代表未来”，不参与复查
    probe = out.replace(UP_TREND_DESCRIPTION, "，").replace(_GROWTH_DESCRIPTION, "，")
    notice = check_input(probe)
    if notice is not None and notice.level == "block":
        return None
    if len(_FILLER.sub("", out)) < 2:
        return None
    return out


# ───────────────────────────── 输出侧 ─────────────────────────────

_OUTPUT_BANNED: list[re.Pattern[str]] = [
    re.compile(r"建议\s*(?:逢低买入|逢高卖出|关注买入|买入|买进|卖出|增持|减持|加仓|减仓|持有|清仓|抄底|买|卖)"),
    re.compile(r"(?:买入|买进|卖出|增持|减持|加仓|减仓|买卖|操作)建议"),
    re.compile(r"强烈推荐|推荐(?:买入|购买|关注|持有|增持)|值得推荐"),
    re.compile(r"必将上涨|必涨|稳赚不赔|稳赚|包赚|保本|保证收益|收益保证|保收益|稳赢|零风险|无风险(?!利率)"),
    re.compile(r"目标价|目标位"),
    re.compile(r"预计\s*(?:将|会)?\s*(?:上涨|下跌|反弹|涨|跌)(?:\s*[-−]?\d+(?:\.\d+)?\s*[%％])?"),
    re.compile(r"(?:一定会|肯定会|将会|必然|势必|注定|必将|会)\s*(?:大幅)?(?:上涨|下跌|反弹|走强|走弱|突破|大涨|大跌)"),
    re.compile(r"一定会"),
    re.compile(r"(?:有望|即将)\s*(?:上涨|反弹|走强|突破|大涨|涨停|翻倍|回升)"),
    re.compile(r"(?:上涨|下跌|上升)空间"),
    re.compile(r"看涨|看跌|看多|看空|唱多|唱空"),
    re.compile(r"(?:值得|可以|适合)\s*(?:买入|买进|买|入手|上车|布局|抄底|持有|加仓)"),
    re.compile(r"抄底|满仓|梭哈|上车|低吸|高抛|逢低布局|逢低吸纳"),
    re.compile(r"翻倍|翻番"),
]
# 同一分句内、紧邻禁语之前出现这些词，视为否定表述（如“不构成买卖建议”“并非稳赚”）
_NEGATIONS = ("不构成", "不代表", "不提供", "不是", "并非", "不做", "不作", "不得", "不会", "避免", "禁止", "没有", "不", "非", "无", "勿", "未")
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


def _negated(sentence: str, start: int) -> bool:
    prefix = _clause_prefix(sentence, start, width=6)
    return any(n in prefix for n in _NEGATIONS)


def _violations(sentence: str) -> list[str]:
    hits: list[tuple[int, str]] = []
    for p in _OUTPUT_BANNED:
        for m in p.finditer(sentence):
            if m.group(0) and not _negated(sentence, m.start()):
                hits.append((m.start(), m.group(0).strip()))
    return _ordered_unique(hits)


def sanitize_output(text: str) -> tuple[str, list[str]]:
    """删除含禁语的句子（买卖建议、目标价、收益承诺、涨跌预测等），返回 (清洗后文本, 命中的禁语)。

    否定表述（“不构成投资建议”“不承诺收益”“并非稳赚”）不算违规。
    """
    if not text:
        return "", []
    kept: list[str] = []
    violations: list[str] = []
    for sent in _SENTENCE_SPLIT.split(text):
        if not sent:
            continue
        hits = _violations(sent)
        if hits:
            violations.extend(h for h in hits if h not in violations)
            continue
        kept.append(sent)
    return "".join(kept).strip(), violations
