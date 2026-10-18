"""指标目录、算子、股票池预设、规则词典。

这是“AI 能说什么”的边界：LLM 与规则解析器只能输出这里登记过的指标与算子；
引擎只按这里的口径计算与判定；前端只按这里的名称/单位展示。
"""

from __future__ import annotations

from .schemas import Condition, MetricDef, OpDef, UniverseOption

NUMERIC_OPS = ["gt", "gte", "lt", "lte", "between"]
RANK_OPS = ["pct_rank_lte", "pct_rank_gte"]
BOOL_OPS = ["is_true", "is_false"]

FY = "扶摇"

# 数据集 → (中文名, 接口)
DATASETS: dict[str, tuple[str, str]] = {
    "tickers": ("A股代码表", "/api/meta/tickers/list"),
    "constituents": ("指数/板块成分股", "/api/a-share-index/constituents/ths-stock-list"),
    "industry_map": ("同花顺行业归属（行业指数成分反查）", "/api/a-share-index/catalog/ths-index-list + constituents"),
    "snapshot": ("行情快照", "/api/a-share/prices/snapshot"),
    "valuations": ("估值快照", "/api/a-share/valuations/snapshot"),
    "indicators": ("财务指标（本期）", "/api/a-share/financials/indicators"),
    "indicators_yoy": ("财务指标（上年同期）", "/api/a-share/financials/indicators"),
    "income": ("利润表（季报序列）", "/api/a-share/financials/income-statements"),
    "history": ("历史日K（后复权）", "/api/a-share/prices/historical"),
    "benchmark": ("基准指数日K（沪深300）", "/api/a-share-index/prices/historical"),
    "dividends": ("分红送转事件", "/api/a-share/corporate-actions/adjustment-factors"),
}
# 全市场可廉价批量获取（每次 100 只）的数据集：可用于行业/全市场分位参考组、全A预筛
CHEAP_DATASETS = {"tickers", "snapshot", "valuations"}


def _m(**kw) -> MetricDef:
    return MetricDef(**kw)


_METRICS: list[MetricDef] = [
    # ── 估值（实时快照）──
    _m(
        id="pe_ttm", name="市盈率TTM", category="valuation", unit="倍",
        description="股价相对最近12个月净利润的倍数，由数据源计算；亏损公司为负值。",
        source=f"{FY} /api/a-share/valuations/snapshot · pe_ttm", datasets=["valuations", "snapshot"],
        period_type="realtime", higher_is_better=False, allowed_ops=NUMERIC_OPS + RANK_OPS,
        rank_scopes=["universe", "industry", "market"], positive_only_for_rank=True,
        plausible_range=(-5000, 5000), sweep_values=[10, 15, 20, 25, 30, 40, 50], backtestable=True,
        caveats=["亏损公司 PE 为负，分位排名时视为“不适用”", "回测中使用 收盘价/滚动四季基本每股收益 近似重建"],
        aliases=["市盈率", "PE", "估值"],
    ),
    _m(
        id="pb_mrq", name="市净率MRQ", category="valuation", unit="倍",
        description="股价相对最近一期每股净资产的倍数，由数据源计算；净资产为负时无常规意义。",
        source=f"{FY} /api/a-share/valuations/snapshot · pb_mrq", datasets=["valuations", "snapshot"],
        period_type="realtime", higher_is_better=False, allowed_ops=NUMERIC_OPS + RANK_OPS,
        rank_scopes=["universe", "industry", "market"], positive_only_for_rank=True,
        plausible_range=(-500, 500), sweep_values=[0.8, 1, 1.5, 2, 3, 5],
        caveats=["适合银行、地产、周期等重资产行业"], aliases=["市净率", "PB", "破净"],
    ),
    _m(
        id="ps_ttm", name="市销率TTM", category="valuation", unit="倍",
        description="股价相对最近12个月营业收入的倍数，由数据源计算。",
        source=f"{FY} /api/a-share/valuations/snapshot · ps_ttm", datasets=["valuations", "snapshot"],
        period_type="realtime", higher_is_better=False, allowed_ops=NUMERIC_OPS + RANK_OPS,
        rank_scopes=["universe", "industry", "market"], positive_only_for_rank=True,
        plausible_range=(0, 2000), sweep_values=[1, 2, 3, 5, 8, 12], aliases=["市销率", "PS"],
    ),
    _m(
        id="pcf_ttm", name="市现率TTM", category="valuation", unit="倍",
        description="股价相对最近12个月经营现金流的倍数，由数据源计算；经营现金流为负时为负值。",
        source=f"{FY} /api/a-share/valuations/snapshot · pcf_ttm", datasets=["valuations", "snapshot"],
        period_type="realtime", higher_is_better=False, allowed_ops=NUMERIC_OPS + RANK_OPS,
        rank_scopes=["universe", "industry", "market"], positive_only_for_rank=True,
        plausible_range=(-5000, 5000), sweep_values=[5, 10, 15, 20, 30], aliases=["市现率", "PCF"],
    ),
    # ── 成长（报告期）──
    _m(
        id="revenue_yoy", name="营业收入同比增长率", category="growth", unit="%",
        description="报告期累计营业收入相对上年同期的增长率（数据源财务指标）。",
        source=f"{FY} /api/a-share/financials/indicators · calculate_operating_income_yoy_growth_ratio",
        datasets=["indicators"], period_type="report", higher_is_better=True,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(-100, 10000),
        sweep_values=[-10, 0, 5, 10, 15, 20, 30, 50], backtestable=True,
        caveats=["回测中由利润表季报自算，口径可能与指标接口略有差异"],
        aliases=["营收增长", "收入增长", "营收同比"],
    ),
    _m(
        id="net_profit_yoy", name="归母净利润同比增长率", category="growth", unit="%",
        description="报告期累计归属于母公司股东的净利润相对上年同期的增长率（数据源财务指标）。",
        source=f"{FY} /api/a-share/financials/indicators · calculate_parent_holder_net_profit_yoy_growth_ratio",
        datasets=["indicators"], period_type="report", higher_is_better=True,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(-100000, 100000),
        sweep_values=[-20, 0, 10, 20, 30, 50], backtestable=True,
        caveats=["上年同期为负或接近 0 时同比增长率失真", "回测中由利润表季报自算"],
        aliases=["利润增长", "净利增长", "业绩增长"],
    ),
    _m(
        id="op_profit_yoy", name="营业利润同比增长率", category="growth", unit="%",
        description="报告期累计营业利润相对上年同期的增长率（数据源财务指标）。",
        source=f"{FY} /api/a-share/financials/indicators · calculate_operating_profit_yoy_growth_ratio",
        datasets=["indicators"], period_type="report", higher_is_better=True,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(-100000, 100000),
        sweep_values=[-20, 0, 10, 20, 30], backtestable=True, aliases=["营业利润增长"],
    ),
    # ── 盈利能力 ──
    _m(
        id="roe", name="净资产收益率（报告期累计）", category="profitability", unit="%",
        description="数据源“净资产收益率”，中报/季报为期内累计值，未年化。",
        source=f"{FY} /api/a-share/financials/indicators · index_weighted_avg_roe",
        datasets=["indicators"], period_type="report", higher_is_better=True,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(-200, 200),
        sweep_values=[0, 3, 5, 8, 10, 15],
        caveats=["中报/季报为期内累计值（未年化），与年报数值不可直接比较"],
    ),
    _m(
        id="roe_annualized", name="净资产收益率（年化估算）", category="profitability", unit="%",
        description="报告期累计 ROE 按季度数线性年化，用于与“ROE≥15%”这类年度口径阈值比较。",
        formula="ROE(报告期累计) × 4 ÷ 报告期季度数", source="派生：财务指标 index_weighted_avg_roe",
        datasets=["indicators"], period_type="report", higher_is_better=True,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(-400, 400),
        sweep_values=[5, 8, 10, 12, 15, 20], derived=True, estimate=True,
        caveats=["线性年化忽略季节性，属估算"], aliases=["ROE", "净资产收益率", "盈利能力"],
    ),
    _m(
        id="roe_change", name="ROE同比变动", category="profitability", unit="百分点",
        description="本期 ROE 减去上年同期 ROE（同口径累计值相减，无需年化）。",
        formula="ROE(本期) − ROE(上年同期)", source="派生：财务指标 index_weighted_avg_roe（本期与上年同期）",
        datasets=["indicators", "indicators_yoy"], period_type="report", higher_is_better=True,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(-200, 200),
        sweep_values=[-2, -1, 0, 0.5, 1, 2], derived=True, aliases=["ROE提升", "盈利改善"],
    ),
    _m(
        id="roe_deducted", name="扣非净资产收益率（报告期累计）", category="profitability", unit="%",
        description="数据源“扣非加权净资产收益率”，期内累计值，未年化。",
        source=f"{FY} /api/a-share/financials/indicators · index_deduct_weighted_avg_roe",
        datasets=["indicators"], period_type="report", higher_is_better=True,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(-200, 200),
        sweep_values=[0, 3, 5, 8, 10], aliases=["扣非ROE"],
    ),
    _m(
        id="gross_margin", name="销售毛利率", category="profitability", unit="%",
        description="报告期销售毛利率（数据源财务指标）。",
        source=f"{FY} /api/a-share/financials/indicators · sale_gross_margin",
        datasets=["indicators"], period_type="report", higher_is_better=True,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(-200, 100),
        sweep_values=[10, 20, 30, 40, 50, 60], backtestable=True,
        caveats=["银行、券商、保险等金融企业通常无毛利率（不适用）", "回测中由利润表自算"],
        aliases=["毛利率", "高毛利"],
    ),
    _m(
        id="gross_margin_change", name="毛利率同比变动", category="profitability", unit="百分点",
        description="本期销售毛利率减去上年同期销售毛利率。",
        formula="毛利率(本期) − 毛利率(上年同期)", source="派生：财务指标 sale_gross_margin（本期与上年同期）",
        datasets=["indicators", "indicators_yoy"], period_type="report", higher_is_better=True,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(-200, 200),
        sweep_values=[-2, -1, 0, 1, 2, 5], derived=True, backtestable=True, aliases=["毛利率提升"],
    ),
    _m(
        id="net_margin", name="销售净利率", category="profitability", unit="%",
        description="报告期销售净利率（数据源财务指标）。",
        source=f"{FY} /api/a-share/financials/indicators · sale_net_interest_ratio",
        datasets=["indicators"], period_type="report", higher_is_better=True,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(-10000, 1000),
        sweep_values=[5, 10, 15, 20, 30], aliases=["净利率"],
    ),
    # ── 盈利质量 / 现金流 ──
    _m(
        id="ocf_to_profit", name="净利润现金含量", category="quality", unit="%",
        description="经营活动现金流净额 ÷ 净利润（数据源财务指标），衡量利润的现金成色。",
        source=f"{FY} /api/a-share/financials/indicators · net_profit_cash_content",
        datasets=["indicators"], period_type="report", higher_is_better=True,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(-100000, 100000),
        sweep_values=[50, 80, 100, 120], caveats=["净利润为负或很小时失真"],
        aliases=["现金流", "盈利质量", "利润含金量"],
    ),
    _m(
        id="ocf_to_revenue", name="销售现金比率", category="quality", unit="%",
        description="经营活动现金流净额 ÷ 营业收入（数据源财务指标）。",
        source=f"{FY} /api/a-share/financials/indicators · operating_cash_flow_net_divide_income",
        datasets=["indicators"], period_type="report", higher_is_better=True,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(-100000, 100000),
        sweep_values=[0, 5, 10, 20], aliases=["销售现金比"],
    ),
    # ── 偿债 ──
    _m(
        id="debt_ratio", name="资产负债率", category="solvency", unit="%",
        description="负债合计 ÷ 资产总计（数据源财务指标）。",
        source=f"{FY} /api/a-share/financials/indicators · assets_debt_ratio",
        datasets=["indicators"], period_type="report", higher_is_better=False,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(0, 300),
        sweep_values=[30, 40, 50, 60, 70, 80],
        caveats=["银行、保险等金融企业资产负债率天然很高，跨行业比较需谨慎"],
        aliases=["负债率", "负债低", "杠杆"],
    ),
    _m(
        id="current_ratio", name="流动比率", category="solvency", unit="倍",
        description="流动资产 ÷ 流动负债（数据源财务指标）。",
        source=f"{FY} /api/a-share/financials/indicators · current_ratio",
        datasets=["indicators"], period_type="report", higher_is_better=True,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(0, 1000),
        sweep_values=[1, 1.5, 2, 3], caveats=["金融企业通常不适用"], aliases=["流动比率"],
    ),
    # ── 价格 / 风险（窗口，后复权日K）──
    _m(
        id="volatility_250d", name="年化波动率（近250日）", category="price", unit="%",
        description="近250个交易日后复权日对数收益率的标准差 × √252。",
        formula="std(ln(Pt/Pt−1)) × √252 × 100", source=f"派生：{FY} /api/a-share/prices/historical · close_price（后复权）",
        datasets=["history"], period_type="window", higher_is_better=False,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(0, 300),
        sweep_values=[15, 20, 25, 30, 35, 40, 50], derived=True, backtestable=True,
        aliases=["波动率", "波动", "稳定", "平稳"],
    ),
    _m(
        id="max_drawdown_250d", name="最大回撤（近250日）", category="price", unit="%",
        description="近250个交易日内，后复权收盘价相对此前最高点的最大跌幅（正数，越小越稳）。",
        formula="max(1 − Pt / max(Ps, s≤t)) × 100", source=f"派生：{FY} /api/a-share/prices/historical · close_price（后复权）",
        datasets=["history"], period_type="window", higher_is_better=False,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(0, 100),
        sweep_values=[10, 15, 20, 25, 30, 40], derived=True, backtestable=True, aliases=["回撤"],
    ),
    _m(
        id="beta_250d", name="Beta（近250日，相对沪深300）", category="price", unit="",
        description="个股日收益对沪深300日收益的回归系数，衡量相对大盘的波动敏感度。",
        formula="cov(ri, rm) ÷ var(rm)", source=f"派生：{FY} 个股与 000300.SH 历史日K",
        datasets=["history", "benchmark"], period_type="window", higher_is_better=None,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(-5, 5),
        sweep_values=[0.5, 0.7, 0.8, 1.0, 1.2], derived=True, backtestable=True,
        aliases=["Beta", "贝塔", "防御"],
    ),
    _m(
        id="return_20d", name="近20日涨跌幅", category="price", unit="%",
        description="近20个交易日后复权收盘价涨跌幅，仅描述历史走势。",
        formula="(P_last ÷ P_last−20 − 1) × 100", source=f"派生：{FY} /api/a-share/prices/historical（后复权）",
        datasets=["history"], period_type="window", higher_is_better=None,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(-100, 1000),
        sweep_values=[-20, -10, -5, 0, 5, 10, 20], derived=True, backtestable=True,
        caveats=["仅描述历史，不代表未来"], aliases=["近一月涨幅", "短期涨幅"],
    ),
    _m(
        id="return_60d", name="近60日涨跌幅", category="price", unit="%",
        description="近60个交易日后复权收盘价涨跌幅，仅描述历史走势。",
        formula="(P_last ÷ P_last−60 − 1) × 100", source=f"派生：{FY} /api/a-share/prices/historical（后复权）",
        datasets=["history"], period_type="window", higher_is_better=None,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(-100, 1000),
        sweep_values=[-30, -20, -10, 0, 10, 20, 30], derived=True, backtestable=True,
        caveats=["仅描述历史，不代表未来"], aliases=["近三月涨幅", "趋势"],
    ),
    _m(
        id="return_250d", name="近250日涨跌幅", category="price", unit="%",
        description="近250个交易日后复权收盘价涨跌幅，仅描述历史走势。",
        formula="(P_last ÷ P_last−250 − 1) × 100", source=f"派生：{FY} /api/a-share/prices/historical（后复权）",
        datasets=["history"], period_type="window", higher_is_better=None,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(-100, 5000),
        sweep_values=[-30, -10, 0, 10, 30, 50], derived=True, backtestable=True,
        caveats=["仅描述历史，不代表未来"], aliases=["近一年涨幅"],
    ),
    _m(
        id="avg_amount_20d", name="近20日日均成交额", category="liquidity", unit="亿元",
        description="近20个交易日成交额均值。",
        formula="mean(turnover) ÷ 1e8", source=f"派生：{FY} /api/a-share/prices/historical · turnover",
        datasets=["history"], period_type="window", higher_is_better=True,
        allowed_ops=NUMERIC_OPS + RANK_OPS, rank_scopes=["universe"], plausible_range=(0, 100000),
        sweep_values=[0.5, 1, 2, 5, 10], derived=True, backtestable=True,
        aliases=["成交额", "流动性", "成交活跃"],
    ),
    _m(
        id="pct_change_today", name="最新涨跌幅", category="price", unit="%",
        description="行情快照相对前收盘价的涨跌幅（盘中为实时，收盘后为当日）。",
        source=f"{FY} /api/a-share/prices/snapshot · price_change_ratio_pct", datasets=["snapshot"],
        period_type="realtime", higher_is_better=None, allowed_ops=NUMERIC_OPS + RANK_OPS,
        rank_scopes=["universe"], plausible_range=(-35, 35), sweep_values=[-5, -2, 0, 2, 5],
        aliases=["今日涨幅", "当日涨跌"],
    ),
    # ── 规模 / 分红（估算）──
    _m(
        id="market_cap_est", name="估算总市值", category="size", unit="亿元",
        description="数据源暂未提供总市值字段：优先按 市盈率TTM × 归母净利润TTM 估算；亏损（PE≤0）时改用 市销率TTM × 营业收入TTM。",
        formula="pe_ttm × 归母净利润TTM ÷ 1e8（PE≤0 时 ps_ttm × 营业收入TTM ÷ 1e8）；TTM = 本期累计 + 上年年报 − 上年同期累计",
        source=f"派生：{FY} 估值快照 pe_ttm/ps_ttm + 利润表 parent_holder_net_profit/operating_income", datasets=["valuations", "income", "snapshot"],
        period_type="realtime", higher_is_better=None, allowed_ops=NUMERIC_OPS + RANK_OPS,
        rank_scopes=["universe"], plausible_range=(0, 300000), sweep_values=[50, 100, 200, 500, 1000, 3000],
        derived=True, estimate=True, caveats=["估算值，可能与实际总市值存在偏差"],
        aliases=["市值", "大盘", "小盘", "规模"],
    ),
    _m(
        id="dividend_yield_ttm", name="股息率（近12个月，估算）", category="dividend", unit="%",
        description="近365天每股现金分红（税前）合计 ÷ 最新价。",
        formula="Σ 近365天 dividend_per_share ÷ last_price × 100",
        source=f"派生：{FY} 分红事件 dividend_per_share + 行情快照 last_price", datasets=["dividends", "snapshot"],
        period_type="realtime", higher_is_better=True, allowed_ops=NUMERIC_OPS + RANK_OPS,
        rank_scopes=["universe"], plausible_range=(0, 100), sweep_values=[1, 2, 3, 4, 5],
        derived=True, estimate=True, caveats=["以除权除息日统计近365天，送转不计入"],
        aliases=["股息率", "高股息", "分红"],
    ),
    _m(
        id="revenue_amount", name="营业收入（报告期累计）", category="growth", unit="亿元",
        description="利润表报告期累计营业收入。",
        source=f"{FY} /api/a-share/financials/income-statements · operating_income", datasets=["income"],
        period_type="report", higher_is_better=True, allowed_ops=NUMERIC_OPS + RANK_OPS,
        rank_scopes=["universe"], plausible_range=(-1000, 1000000), sweep_values=[10, 50, 100, 500],
        aliases=["营收", "营业收入"],
    ),
    _m(
        id="parent_np_amount", name="归母净利润（报告期累计）", category="growth", unit="亿元",
        description="利润表报告期累计归属于母公司股东的净利润。",
        source=f"{FY} /api/a-share/financials/income-statements · parent_holder_net_profit", datasets=["income"],
        period_type="report", higher_is_better=True, allowed_ops=NUMERIC_OPS + RANK_OPS,
        rank_scopes=["universe"], plausible_range=(-100000, 100000), sweep_values=[0, 1, 5, 10, 50],
        aliases=["归母净利润", "净利润"],
    ),
    # ── 基础 ──
    _m(
        id="is_st", name="是否ST", category="basic", unit="", value_type="bool",
        description="证券简称含“ST”（含 *ST）视为风险警示股。",
        source=f"{FY} /api/meta/tickers/list · name", datasets=["tickers"], period_type="static",
        allowed_ops=BOOL_OPS, backtestable=True, caveats=["按当前简称判断；回测中存在偏差"],
        aliases=["ST", "风险警示"],
    ),
    _m(
        id="listing_days", name="上市天数", category="basic", unit="天",
        description="今天距上市日期的自然日天数。",
        formula="今日 − list_date", source=f"{FY} /api/meta/tickers/list · list_date", datasets=["tickers"],
        period_type="static", higher_is_better=None, allowed_ops=NUMERIC_OPS, plausible_range=(0, 20000),
        sweep_values=[180, 365, 730, 1095], derived=True, backtestable=True, aliases=["次新", "上市时间"],
    ),
]

METRICS: dict[str, MetricDef] = {m.id: m for m in _METRICS}

# 财务指标接口 index_id（2026-10-17 用真实 Key 实测；成长类字段名与官方文档不同，以实测为准）
INDICATOR_FIELDS: dict[str, str] = {
    "revenue_yoy": "calculate_operating_income_yoy_growth_ratio",
    "net_profit_yoy": "calculate_parent_holder_net_profit_yoy_growth_ratio",
    "op_profit_yoy": "calculate_operating_profit_yoy_growth_ratio",
    "roe": "index_weighted_avg_roe",
    "roe_deducted": "index_deduct_weighted_avg_roe",
    "gross_margin": "sale_gross_margin",
    "net_margin": "sale_net_interest_ratio",
    "ocf_to_profit": "net_profit_cash_content",
    "ocf_to_revenue": "operating_cash_flow_net_divide_income",
    "debt_ratio": "assets_debt_ratio",
    "current_ratio": "current_ratio",
}

# 同花顺行业：目录同时含二级（881xxx.TI，约 90 个）与三级（884xxx.TI，约 230 个）。
# 行业分位的同业组用二级行业（平均数十家，统计更稳）；三级成员常不足 10 家。
INDUSTRY_LEVEL2_PREFIX = "881"

# 结果表默认展示的廉价指标（与条件指标求并集后取数）
DISPLAY_METRICS = ["pe_ttm", "pb_mrq"]

OPS: list[OpDef] = [
    OpDef(op="gt", label="大于", symbol=">"),
    OpDef(op="gte", label="不低于", symbol="≥"),
    OpDef(op="lt", label="小于", symbol="<"),
    OpDef(op="lte", label="不高于", symbol="≤"),
    OpDef(op="between", label="介于", symbol="∈", needs_value2=True),
    OpDef(op="pct_rank_lte", label="分位不高于", symbol="分位≤", needs_scope=True),
    OpDef(op="pct_rank_gte", label="分位不低于", symbol="分位≥", needs_scope=True),
    OpDef(op="is_true", label="是", symbol="是"),
    OpDef(op="is_false", label="否", symbol="否"),
]

RANK_SCOPE_LABEL = {"universe": "股票池内", "industry": "所属行业内", "market": "全A股内"}

UNIVERSE_PRESETS: list[UniverseOption] = [
    UniverseOption(kind="index", code="000300.SH", name="沪深300", group="常用指数", size_hint=300),
    UniverseOption(kind="index", code="000905.SH", name="中证500", group="常用指数", size_hint=500),
    UniverseOption(kind="index", code="000016.SH", name="上证50", group="常用指数", size_hint=50),
    UniverseOption(kind="index", code="000852.SH", name="中证1000", group="常用指数", size_hint=1000),
    UniverseOption(kind="index", code="000906.SH", name="中证800", group="常用指数", size_hint=800),
    UniverseOption(kind="index", code="399006.SZ", name="创业板指", group="常用指数", size_hint=100),
    UniverseOption(kind="index", code="000688.SH", name="科创50", group="常用指数", size_hint=50),
    UniverseOption(kind="all_a", code=None, name="全部A股（先按估值/行情条件预筛）", group="全市场", size_hint=5400),
]

# 股票池关键词（规则解析用；行业/概念名交给 Repository.search_universes 模糊匹配）
UNIVERSE_LEXICON: list[tuple[str, str | None, str, str]] = [
    # (正则, code, name, kind)
    (r"沪深\s*300|HS300|沪深三百", "000300.SH", "沪深300", "index"),
    (r"中证\s*500|中证五百", "000905.SH", "中证500", "index"),
    (r"中证\s*1000|中证一千", "000852.SH", "中证1000", "index"),
    (r"中证\s*800|中证八百", "000906.SH", "中证800", "index"),
    (r"上证\s*50|上证五十", "000016.SH", "上证50", "index"),
    (r"创业板", "399006.SZ", "创业板指", "index"),
    (r"科创\s*50|科创板", "000688.SH", "科创50", "index"),
    (r"全\s*A|全部\s*A\s*股|全市场|所有(A股|股票)", None, "全部A股", "all_a"),
]


def C(metric: str, op: str, value: float | None = None, value2: float | None = None,
      scope: str | None = None, rationale: str | None = None, hard: bool = True) -> dict:
    """词典里用的条件简写（dict，供解析器实例化为 Condition）。"""
    d = {"metric": metric, "op": op, "value": value, "value2": value2, "rank_scope": scope, "hard": hard}
    if rationale:
        d["rationale"] = rationale
    return d


# 规则词典：模糊短语 → 解释与备选（AI 不可用时的降级解析；也作为提示词中的参考口径）
LEXICON: list[dict] = [
    {
        "key": "operating_improve",
        "patterns": [r"经营(状况|情况)?(改善|好转|向好|回暖)", r"基本面(改善|好转|向好)", r"业绩(改善|好转|回暖|拐点)"],
        "meaning": "经营改善：看最新报告期的收入、利润是否同比增长，盈利能力是否同比提升",
        "question": "“经营改善”你更想看哪种口径？",
        "default": "b",
        "options": [
            {"id": "a", "label": "营收、净利润同比都在增长", "conditions": [
                C("revenue_yoy", "gt", 0, rationale="收入同比为正"), C("net_profit_yoy", "gt", 0, rationale="利润同比为正")]},
            {"id": "b", "label": "营收、净利润同比增长，且 ROE 同比提升", "conditions": [
                C("revenue_yoy", "gt", 0, rationale="收入同比为正"), C("net_profit_yoy", "gt", 0, rationale="利润同比为正"),
                C("roe_change", "gt", 0, rationale="盈利能力同比改善")]},
            {"id": "c", "label": "利润显著增长（净利润同比 > 20%）", "conditions": [
                C("revenue_yoy", "gt", 0), C("net_profit_yoy", "gt", 20, rationale="利润同比明显改善")]},
        ],
    },
    {
        "key": "high_growth",
        "patterns": [r"高成长", r"高增长", r"成长(性)?(好|强|高|快)", r"快速增长", r"业绩高增"],
        "meaning": "高成长：营收与净利润同比增速较高",
        "question": "“高成长”的门槛定多高？",
        "default": "a",
        "options": [
            {"id": "a", "label": "营收、净利润同比均 ≥ 20%", "conditions": [C("revenue_yoy", "gte", 20), C("net_profit_yoy", "gte", 20)]},
            {"id": "b", "label": "营收、净利润同比均 ≥ 30%（更严）", "conditions": [C("revenue_yoy", "gte", 30), C("net_profit_yoy", "gte", 30)]},
            {"id": "c", "label": "营收、净利润增速都在股票池前 30%", "conditions": [
                C("revenue_yoy", "pct_rank_gte", 70, scope="universe"), C("net_profit_yoy", "pct_rank_gte", 70, scope="universe")]},
        ],
    },
    {
        "key": "profit_growth",
        "patterns": [r"(业绩|利润|盈利|净利)(在)?增长", r"赚钱(越来越多|增加)"],
        "meaning": "业绩增长：净利润同比为正",
        "question": "“业绩增长”要求多大增速？",
        "default": "a",
        "options": [
            {"id": "a", "label": "净利润同比 > 0", "conditions": [C("net_profit_yoy", "gt", 0)]},
            {"id": "b", "label": "净利润同比 ≥ 10%", "conditions": [C("net_profit_yoy", "gte", 10)]},
        ],
    },
    {
        "key": "fair_valuation",
        "patterns": [r"估值(合理|适中|不高|不贵|正常|适当)", r"价格(合理|不贵)", r"性价比(高|好)?", r"不(算)?贵"],
        "meaning": "估值合理：盈利为前提，市盈率不高于同行业多数公司",
        "question": "“估值合理”按哪种口径判断？",
        "default": "a",
        "options": [
            {"id": "a", "label": "盈利且 PE(TTM) 不高于所属行业中位数", "description": "相对估值，跨行业可比", "conditions": [
                C("pe_ttm", "gt", 0, rationale="亏损公司 PE 无估值意义，盈利为前提"),
                C("pe_ttm", "pct_rank_lte", 50, scope="industry", rationale="PE 处于所属行业后 50%")]},
            {"id": "b", "label": "PE(TTM) 介于 0~25 倍", "description": "绝对阈值，简单直观", "conditions": [C("pe_ttm", "between", 0, 25)]},
            {"id": "c", "label": "PB(MRQ) 不高于所属行业中位数", "description": "适合银行、周期等重资产行业", "conditions": [
                C("pb_mrq", "gt", 0), C("pb_mrq", "pct_rank_lte", 50, scope="industry")]},
        ],
    },
    {
        "key": "low_valuation",
        "patterns": [r"低估值", r"估值(低|偏低|便宜|洼地)", r"便宜", r"低估", r"低\s*PE", r"低市盈率", r"破净"],
        "meaning": "低估值：盈利为前提，市盈率处于所属行业较低水平",
        "question": "“低估值”按哪种口径？",
        "default": "a",
        "options": [
            {"id": "a", "label": "盈利且 PE(TTM) 处于所属行业后 30%", "conditions": [
                C("pe_ttm", "gt", 0), C("pe_ttm", "pct_rank_lte", 30, scope="industry")]},
            {"id": "b", "label": "PE(TTM) 介于 0~15 倍", "conditions": [C("pe_ttm", "between", 0, 15)]},
            {"id": "c", "label": "破净：PB(MRQ) < 1", "conditions": [C("pb_mrq", "gt", 0), C("pb_mrq", "lt", 1)]},
        ],
    },
    {
        "key": "stable_trend",
        "patterns": [r"(走势|股价|价格|表现)?(相对|比较|较为)?(稳定|平稳|稳健)", r"波动(小|低|较小|不大)", r"低波(动)?", r"不(怎么)?折腾"],
        "meaning": "走势相对稳定：近一年波动率在股票池中偏低",
        "question": "“走势相对稳定”用哪种口径？",
        "default": "a",
        "options": [
            {"id": "a", "label": "近250日年化波动率处于股票池后 50%", "description": "“相对”稳定：与同池股票比较", "conditions": [
                C("volatility_250d", "pct_rank_lte", 50, scope="universe", rationale="波动率低于股票池中位数")]},
            {"id": "b", "label": "年化波动率 ≤ 30% 且近250日最大回撤 ≤ 25%", "description": "绝对阈值", "conditions": [
                C("volatility_250d", "lte", 30), C("max_drawdown_250d", "lte", 25)]},
            {"id": "c", "label": "波动率与最大回撤都处于股票池后 50%", "description": "更严格", "conditions": [
                C("volatility_250d", "pct_rank_lte", 50, scope="universe"), C("max_drawdown_250d", "pct_rank_lte", 50, scope="universe")]},
        ],
    },
    {
        "key": "small_drawdown",
        "patterns": [r"回撤(小|低|不大|可控)", r"抗跌"],
        "meaning": "回撤小：近250日最大回撤较小",
        "question": "“回撤小”的门槛？",
        "default": "a",
        "options": [
            {"id": "a", "label": "近250日最大回撤 ≤ 25%", "conditions": [C("max_drawdown_250d", "lte", 25)]},
            {"id": "b", "label": "最大回撤处于股票池后 30%", "conditions": [C("max_drawdown_250d", "pct_rank_lte", 30, scope="universe")]},
        ],
    },
    {
        "key": "uptrend",
        "patterns": [r"趋势向上", r"上升趋势", r"走势向上", r"走强", r"(最近|近期)?(趋势|走势)(好|不错|强)", r"多头"],
        "meaning": "趋势向上：仅描述历史区间涨跌，不代表未来",
        "question": "“趋势向上”看哪个时间窗口？",
        "default": "a",
        "options": [
            {"id": "a", "label": "近60日涨跌幅 > 0", "conditions": [C("return_60d", "gt", 0, rationale="近三个月区间上涨（历史描述）")]},
            {"id": "b", "label": "近20日与近250日涨跌幅都 > 0", "conditions": [C("return_20d", "gt", 0), C("return_250d", "gt", 0)]},
        ],
    },
    {
        "key": "oversold",
        "patterns": [r"超跌", r"跌(得)?(多|狠|深)", r"回调(充分|到位)"],
        "meaning": "超跌：历史区间跌幅较大（不代表会反弹）",
        "question": "“超跌”的口径？",
        "default": "a",
        "options": [
            {"id": "a", "label": "近60日跌幅 ≥ 20%", "conditions": [C("return_60d", "lte", -20)]},
            {"id": "b", "label": "近250日跌幅 ≥ 30%", "conditions": [C("return_250d", "lte", -30)]},
        ],
    },
    {
        "key": "high_roe",
        "patterns": [r"盈利能力(强|好|突出|高)", r"高\s*ROE", r"ROE\s*(高|较高)", r"赚钱能力(强|好)", r"净资产收益率高"],
        "meaning": "盈利能力强：年化 ROE 较高",
        "question": "“盈利能力强”的门槛？",
        "default": "a",
        "options": [
            {"id": "a", "label": "年化 ROE ≥ 15%", "conditions": [C("roe_annualized", "gte", 15, rationale="年化估算后与年度口径可比")]},
            {"id": "b", "label": "年化 ROE ≥ 10%", "conditions": [C("roe_annualized", "gte", 10)]},
            {"id": "c", "label": "年化 ROE 处于股票池前 30%", "conditions": [C("roe_annualized", "pct_rank_gte", 70, scope="universe")]},
        ],
    },
    {
        "key": "good_cashflow",
        "patterns": [r"现金流(好|充裕|健康|稳健|充沛|不错)", r"盈利质量(高|好)", r"利润含金量(高)?"],
        "meaning": "现金流好：利润有经营现金流支撑",
        "question": "“现金流好”的门槛？",
        "default": "a",
        "options": [
            {"id": "a", "label": "净利润现金含量 ≥ 80%", "conditions": [C("ocf_to_profit", "gte", 80)]},
            {"id": "b", "label": "净利润现金含量 ≥ 100%", "conditions": [C("ocf_to_profit", "gte", 100)]},
        ],
    },
    {
        "key": "low_debt",
        "patterns": [r"负债(率)?(低|少|不高)", r"财务(稳健|健康|安全)", r"低杠杆"],
        "meaning": "负债低：资产负债率不高",
        "question": "“负债低/财务稳健”的门槛？",
        "default": "a",
        "options": [
            {"id": "a", "label": "资产负债率 ≤ 60%", "conditions": [C("debt_ratio", "lte", 60)]},
            {"id": "b", "label": "资产负债率 ≤ 40%", "conditions": [C("debt_ratio", "lte", 40)]},
            {"id": "c", "label": "资产负债率 ≤ 60% 且流动比率 ≥ 1.5", "conditions": [C("debt_ratio", "lte", 60), C("current_ratio", "gte", 1.5)]},
        ],
    },
    {
        "key": "high_gross_margin",
        "patterns": [r"毛利(率)?高", r"高毛利"],
        "meaning": "毛利率高",
        "question": "“毛利率高”的门槛？",
        "default": "a",
        "options": [
            {"id": "a", "label": "销售毛利率 ≥ 40%", "conditions": [C("gross_margin", "gte", 40)]},
            {"id": "b", "label": "销售毛利率处于股票池前 30%", "conditions": [C("gross_margin", "pct_rank_gte", 70, scope="universe")]},
        ],
    },
    {
        "key": "large_cap",
        "patterns": [r"大盘(股)?", r"大市值", r"蓝筹", r"权重股", r"龙头"],
        "meaning": "大盘/龙头：规模较大（数据源无总市值字段，使用估算市值）",
        "question": "“大盘/龙头”按估算市值的门槛？",
        "default": "a",
        "options": [
            {"id": "a", "label": "估算总市值 ≥ 500 亿元", "conditions": [C("market_cap_est", "gte", 500)]},
            {"id": "b", "label": "估算总市值 ≥ 1000 亿元", "conditions": [C("market_cap_est", "gte", 1000)]},
            {"id": "c", "label": "估算总市值处于股票池前 20%", "conditions": [C("market_cap_est", "pct_rank_gte", 80, scope="universe")]},
        ],
    },
    {
        "key": "small_cap",
        "patterns": [r"小盘(股)?", r"小市值"],
        "meaning": "小盘：规模较小（估算市值）",
        "question": "“小盘”按估算市值的门槛？",
        "default": "a",
        "options": [
            {"id": "a", "label": "估算总市值 ≤ 100 亿元", "conditions": [C("market_cap_est", "lte", 100)]},
            {"id": "b", "label": "估算总市值 ≤ 50 亿元", "conditions": [C("market_cap_est", "lte", 50)]},
        ],
    },
    {
        "key": "liquid",
        "patterns": [r"流动性(好|充足|强)", r"(成交|交易)活跃", r"成交额(大|高)"],
        "meaning": "流动性好：日均成交额较高",
        "question": "“流动性好”的门槛？",
        "default": "a",
        "options": [
            {"id": "a", "label": "近20日日均成交额 ≥ 1 亿元", "conditions": [C("avg_amount_20d", "gte", 1)]},
            {"id": "b", "label": "近20日日均成交额 ≥ 5 亿元", "conditions": [C("avg_amount_20d", "gte", 5)]},
        ],
    },
    {
        "key": "high_dividend",
        "patterns": [r"高股息", r"高分红", r"股息率?(高|不错)", r"分红(多|高|稳定|大方)"],
        "meaning": "高股息：近12个月股息率较高（估算）",
        "question": "“高股息”的门槛？",
        "default": "a",
        "options": [
            {"id": "a", "label": "近12个月股息率 ≥ 3%", "conditions": [C("dividend_yield_ttm", "gte", 3)]},
            {"id": "b", "label": "近12个月股息率 ≥ 5%", "conditions": [C("dividend_yield_ttm", "gte", 5)]},
        ],
    },
    {
        "key": "low_beta",
        "patterns": [r"低\s*beta", r"低贝塔", r"防御(性)?"],
        "meaning": "防御：相对大盘波动敏感度低",
        "question": "“防御/低 Beta”的门槛？",
        "default": "a",
        "options": [
            {"id": "a", "label": "Beta ≤ 0.8", "conditions": [C("beta_250d", "lte", 0.8)]},
            {"id": "b", "label": "Beta ≤ 0.6", "conditions": [C("beta_250d", "lte", 0.6)]},
        ],
    },
]

# 无法映射为数据条件的常见说法（规则解析与提示词共用）
UNSUPPORTED_HINTS: list[tuple[str, str, str | None]] = [
    (r"政策(利好|受益|支持)|受益于|风口|题材|热点", "政策/题材属于资讯判断，数据源无可直接筛选的结构化字段",
     "可把对应同花顺概念板块设为股票池，或在个股详情查看 iFinD 资讯背景"),
    (r"管理层|治理|护城河|竞争力|品牌", "定性判断，暂无结构化数据可筛选", None),
    (r"机构(看好|调研|重仓)|北向|主力(资金)?", "资金/机构类数据当前未在可调用接口中开放", None),
]

DEFAULT_CONDITIONS: list[Condition] = [
    Condition(id="sys_st", metric="is_st", op="is_false", origin="system", source_phrase="默认风控",
              rationale="剔除风险警示股（可删除）"),
    Condition(id="sys_new", metric="listing_days", op="gte", value=365, origin="system", source_phrase="默认风控",
              rationale="剔除上市不满一年的次新股（可删除）"),
]

DISCLAIMER = (
    "本产品基于扶摇（同花顺金融数据 API）等数据源做条件筛选与历史统计，结果仅供研究学习，不构成任何投资建议；"
    "不预测股价涨跌、不承诺收益；历史回测不代表未来表现。投资有风险，决策请独立判断。"
)

EXAMPLES = [
    "经营改善、估值合理、走势相对稳定",
    "沪深300里高成长但估值不贵的公司",
    "中证500中现金流好、负债低、近一年回撤小的股票",
    "盈利能力强、股息率高、流动性好的大盘股",
    "小盘高成长，最近趋势向上",
    "明天哪只股票会涨？",
]


def metric(mid: str) -> MetricDef:
    return METRICS[mid]


def datasets_for(metric_ids: set[str] | list[str]) -> set[str]:
    out: set[str] = set()
    for mid in metric_ids:
        if mid in METRICS:
            out.update(METRICS[mid].datasets)
    return out
