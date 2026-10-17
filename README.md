# ScreenLens · 自然语言智能选股与策略解释器

> 把“经营改善、估值合理、走势相对稳定”这类模糊说法，变成**用户可检查、可修改、可执行**的数据条件；在扶摇（同花顺金融数据 API）的真实数据上由**确定性引擎**执行；对每只股票给出入选 / 排除 / 数据缺失的逐条依据，并量化**条件变化的影响**。

- **在线体验**：https://screenlens-fkko.onrender.com （Render 免费实例；北京时间 9:00–23:00 保活，其余时段首次打开需约 1 分钟唤醒）
- **源代码**：https://github.com/Yuyang16Z/nl-stock-screener
- **设计规格**：[docs/DESIGN.md](docs/DESIGN.md) · **数据口径与实测核验**：[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) · **AI 使用与验证记录**：[docs/AI_USAGE.md](docs/AI_USAGE.md) · **测试说明**：[docs/TESTING.md](docs/TESTING.md)

## 1. 用户怎么用

1. 输入一句话，例如“经营改善、估值合理、走势相对稳定”，点「解析意图」。
2. 左侧「AI 理解」：原句中每个短语映射成哪些条件、为什么；模糊词给出 2–3 种口径供选择（默认项标“AI 默认”）；无法用数据表达的说法（如“政策受益”）会明确说不支持并给替代做法。
3. 右侧「筛选条件」：每条条件都能改（指标、算子、阈值、分位口径、硬/软条件、启用开关），股票池、报告期、缺失数据策略可选；条件冲突（如 PE<10 且 PE>20）实时提示，有矛盾时禁止执行。也可以用一句话修改，例如“估值放宽到行业 70% 分位，去掉回撤条件”，系统先列出变更清单，确认后才生效。
4. 「执行筛选」后看结果：
   - **入选 / 排除 / 数据缺失**三个列表，每个条件的数值、阈值、分位（参考组规模）、报告期或价格窗口、来源接口与字段、request_id、数据时点都能展开；
   - **条件影响**：漏斗、每个条件单独卡掉多少只、去掉某条件后会多出几只、阈值扫描曲线、差一点入选的股票；输入新阈值即可看到“新增 N 只 / 移除 M 只”及逐只原因；
   - **数据口径与时点**：每个数据集的接口、状态、覆盖率、数据时点与错误样例；本次用到的指标口径表。
5. 结果可以继续：**比较**（2–5 只并排 + 归一化走势）、**保存策略**、**回测**（时点一致、含成本与基准）、**转为监控任务**（定期重跑并与上次结果做 diff）。
6. 个股详情可看 iFinD 资讯背景（标注“仅背景参考，不参与筛选与打分”）。

## 2. 职责边界

| 角色 | 负责 | 不做 |
|---|---|---|
| **AI（DeepSeek，OpenAI 兼容）** | 自然语言 → 目录内指标的条件草案（含来源短语与理由）；模糊词澄清选项；语义冲突提示；用引擎证据写解释；自然语言修改条件（输出补丁，需用户确认） | 不产生任何个股数值，不决定入选，不直接调数据接口，不预测涨跌、不给买卖建议 |
| **确定性筛选引擎（Python）** | 条件校验与冲突检测；派生指标按公开公式计算；逐股逐条件判定 pass / fail / missing / not_applicable；漏斗、单独卡点、去条件影响、阈值扫描、差一点入选、结果 diff；回测 | 不猜缺失值；缺失、失败、过期一律不判为通过 |
| **金融数据工具** | 扶摇 REST（代码表、指数/行业成分、行情、估值、财务指标、利润表、日K、分红、交易日历）；iFinD MCP（资讯） | 数据层只透传并记录来源，错误码映射为显式状态，不编造 |

AI 不可用（无 Key、超时、输出不合法）时自动降级为规则词典解析，界面标明“规则解析（AI 不可用：原因）”，主链路照常可用。

## 3. 数据来源与口径（摘要）

- 全部个股数值来自 **扶摇** REST：`/api/meta/tickers/list`、`/api/a-share-index/constituents/ths-stock-list`、`/api/a-share-index/catalog/ths-index-list`、`/api/a-share/prices/snapshot`、`/api/a-share/valuations/snapshot`、`/api/a-share/financials/indicators`、`/api/a-share/financials/income-statements`、`/api/a-share/prices/historical`、`/api/a-share-index/prices/historical`、`/api/a-share/corporate-actions/adjustment-factors`、`/api/a-share/calendar/trading-days`。
- **iFinD MCP**（`search_news`）只提供资讯背景；另用 `get_security_indicators` 做过估算市值的跨源核验。
- 报告期默认取“法定披露截止日已过的最近一期”（2026-10-17 时为 2026 年中报）；价格类指标用后复权日K，截至最近交易日；行情/估值时点取最近交易日收盘，接口返回时间单列。
- 用真实 Key 实测后修正了 6 处与官方文档不一致的地方（字段名、披露日语义、行业层级、时点、市值公式、未披露期错误码），见 [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) §3。

## 4. 本地运行

依赖：Python 3.11+（[uv](https://docs.astral.sh/uv/)）、Node 20+（pnpm）。

```bash
cp .env.example .env   # 填 FUYAO_API_KEY / IFIND_MCP_TOKEN / LLM_API_KEY
cd backend && uv sync && uv run uvicorn app.main:app --reload --port 8000
```

```bash
cd frontend && pnpm install && pnpm dev   # http://localhost:5173 ，/api 代理到 8000
```

Docker（与线上一致，前端构建后由后端托管）：

```bash
docker build -t screenlens . && docker run --env-file .env -p 10000:10000 screenlens
```

### 环境变量

| 变量 | 必需 | 说明 |
|---|---|---|
| `FUYAO_API_KEY` | 是 | 扶摇 API Key（https://fuyao.aicubes.cn/admin） |
| `IFIND_MCP_TOKEN` | 否 | iFinD MCP 密钥（官网个人中心 → 密钥管理）；不填则资讯显示“未启用” |
| `IFIND_MCP_URL` | 否 | 默认 `https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-mcp` |
| `LLM_API_KEY` | 否 | DeepSeek Key；不填则全程规则解析 |
| `LLM_BASE_URL` / `LLM_MODEL` | 否 | 默认 `https://api.deepseek.com` / `deepseek-chat` |
| `WARM_UNIVERSES` | 否 | 启动后预热的股票池，默认 `000300.SH` |
| `DEMO_FAULTS_ENABLED` | 否 | 是否允许界面“异常演示”（模拟接口失败），默认 `true` |
| `MAX_DEEP_EVAL` | 否 | 单次逐股深度评估上限，默认 800 |
| `DATA_DIR` | 否 | SQLite 缓存与保存数据目录，默认 `./data` |

### 部署（Render）

`render.yaml` 为 Blueprint；也可用 `python3 scripts/deploy_render.py`（读取本地 `.env` 中的 `RENDER_API_KEY`，创建/更新服务并写入环境变量）。`.github/workflows/keepalive.yml` 在北京时间白天每 10 分钟访问 `/api/health`（需在仓库变量设置 `APP_URL`）。

## 5. 项目结构

```
backend/app/
  schemas.py   catalog.py          # 数据契约；指标目录（32 个指标）+ 规则词典（18 类模糊说法）
  data/        # 扶摇客户端（重试/限流/缓存/故障注入/来源记录）、iFinD MCP、报告期规则、特征计算（带证据链）
  engine/      # 条件描述、冲突检测、判定与分桶、敏感性、diff、回测（ops.py 为引擎与回测共用的算子实现）
  ai/          # LLM 客户端、提示词、解析与澄清、规则降级、解释与数字校验、合规守卫
  services/    # 筛选任务编排与快照复用、保存与监控（SQLite）、回测任务
  api/routes.py, main.py
frontend/src/  # React + Ant Design + ECharts
scripts/       # 实测探测、可追溯性核验、部署
```

## 6. 产品选择与取舍

- **AI 只做“翻译与解释”**：筛选规则写在封闭的指标目录里，AI 只能从中选；判定交给引擎，保证同输入同输出、每个数字都能回到原始字段。
- **相对口径优先**：“估值合理”“走势相对稳定”这类说法默认用行业/股票池内分位而不是拍一个绝对阈值；绝对阈值作为备选，用户一键切换。
- **缺失显式化**：缺失、不适用（如银行无毛利率）、接口失败与“未通过”分开统计；默认缺失即不入选，也可改为“保留并标注”。
- **估算要标注**：数据源无总市值字段，市值按 PE × 归母净利润TTM 估算并标“估算”（已用 iFinD 总市值核验）。
- **默认股票池沪深300**：全 A 股需要逐股取财务与日K，免费实例上过慢；全 A 模式先用估值/行情条件预筛，剩余超过上限会明确提示，不截断。

## 7. 已知边界与未做事项

- **免费实例**：休眠后首次访问需约 1 分钟，启动后自动预热沪深300（约 1 分钟，顶栏显示进度）；磁盘为临时盘，重新部署或重启后已保存的策略与监控记录会清空；筛选运行记录保存在内存中，实例重启后打开旧结果的个股详情会提示“运行记录不存在，请重新执行筛选”；监控任务只在实例运行时触发。
- **数据**：数据源无总市值与历史估值序列（市值为估算；回测中的 PE 用不复权收盘价 / 滚动四季 EPS 近似重建）；指数/行业成分只有当前时点（回测存在幸存者偏差，已在回测页提示）；2026 三季报在 10-17 时尚不可取，默认报告期为 2026 中报；行业同业组为同花顺二级行业。
- **AI**：解析结果依赖模型，复杂长句可能拆分不全；所有 AI 输出都需用户确认或经过校验，不会直接改变结果。
- **审查发现但未修完的问题**（详见 docs/AI_USAGE.md §3.6）：“下周能翻倍”这类时间限定预测会被改写成历史动量草案；不分红公司（接口 3002）被判为“数据缺失”而非股息率 0%；停牌股估值未标注过期；“有望走强”无合规提示。
- **已知小问题**：全A预筛模式下，因廉价条件“数据缺失”而未进入深度评估的股票，其深度条件原因也显示为“已被前置条件排除”；请求体格式错误时 FastAPI 返回的 422 校验信息为英文。
- **未做**：用户账户与多用户隔离、监控结果推送（邮件/IM）、成分股历史时点、分钟级行情、演示视频。

## 8. 合规声明

本产品基于公开数据做条件筛选与历史统计，结果仅供研究学习，不构成任何投资建议；不预测股价涨跌、不承诺收益；历史回测不代表未来表现。
