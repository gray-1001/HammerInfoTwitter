# 战锤信息日报（InfoTwitter）

每天 12:00 自动采集过去 48 小时的战锤领域情报，经真实性校验与重要性排序后，
推送 4 条通识新闻 + 1 条模型价格快讯（+ 可选的第 6 条中文社区情报）到个人微信。

---

## What is this? (English)

**InfoTwitter** is a self-hosted daily digest for Warhammer news — Warhammer 40,000,
The Horus Heresy, Age of Sigmar and The Old World. It collects from **46 sources**
(official site, Google News, Reddit, Bilibili, Tieba, NGA, YouTube), extracts
structured facts with an LLM, scores every story on a six-dimension rubric, and
pushes a ranked digest to WeChat every day at 12:00 — together with a price board
that compares overseas RRP against Chinese official/trade prices and second-hand listings.

Unlike the RSS scrapers and Discord bots that dominate this space (all <5 stars),
this is an end-to-end unattended pipeline: collection → body fetch → LLM extraction →
claim verification → clustering → scoring → slot allocation → render → push → ledger,
plus crash recovery, deduplication across days, and weekly new-product ingestion.

**Highlights**

- **46 sources across 7 adapter types**, English + Chinese (Bilibili / Tieba / NGA)
- **LLM extracts, the code scores** — transparent six-dimension rubric (0–100), replayable and tunable
- **Claim-level verification** — every extracted claim must appear verbatim in the source text, or it is dropped
- **Two-track pricing** — overseas RRP matched to GW product codes (1,530 verified matches), plus CN official, trade and Xianyu prices
- **387 tests**, including a golden-corpus regression snapshot (same input → same clusters/scores)
- **Operational by design** — launch-time state reconciliation, alert log, `doctor` self-check, weekly and daily scheduled tasks

> 中文说明见下方；需求与设计在 [`docs/00-需求与设计.md`](docs/00-需求与设计.md)，
> 最近一次整改报告见 [`docs/24-P0P1整改与首推准备-2026-11-19.md`](docs/24-P0P1整改与首推准备-2026-11-19.md)。

---


## 快速开始

```powershell
# 1. 依赖（已创建 .venv）
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium

# 2. 配置密钥
Copy-Item .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY 与 PUSHPLUS_TOKEN

# 3. 跑一次（不发送，只打印；产物写到 reports/_dryrun/，不影响已发布日报）
.\.venv\Scripts\python.exe run.py run --dry-run

# 4. 正式跑一次（会推送到微信）
.\.venv\Scripts\python.exe run.py run

# 5. 查看本地日报（局域网内手机也能打开）
.\.venv\Scripts\python.exe run.py serve
```

## 命令

| 命令 | 说明 |
|---|---|
| `run.py collect` | 只跑采集，验证源可达性 |
| `run.py run` | 完整流水线（采集→分析→评分→推送） |
| `run.py run --dry-run` | 完整流水线但不发送，产物落 `reports/_dryrun/`，**不写台账、不清库** |
| `run.py run --no-push` | 生成日报但不推送 |
| `run.py run --force` | 忽略"今日已推送"限制强制重推 |
| `run.py run --no-collect` | 跳过采集，只用库内数据调参 |
| `run.py run --reanalyze` | 清空历史抽取结果后重跑（换模型时用） |
| `run.py --llm none run` | 纯规则模式，零成本 |
| `run.py --llm mock run` | 离线模拟，验证链路 |
| `run.py doctor` | 环境自检：配置/代理/密钥/数据库/日报页/源健康/定时任务；有 FAIL 时退出码非 0 |
| `run.py replay` | 用已有分项重算排名，零成本调权重 |
| `run.py status` | 查看台账与事件状态 |
| `run.py history --csv out.csv` | 历史推送汇总与导出 |
| `run.py serve [--host 0.0.0.0]` | 启动日报服务（含反馈接口） |
| `run.py price` | 价格摘要 |
| `run.py price --import-excel 调价表.xlsx` | 导入 GW 官方调价表 |
| `run.py price --import-csv data/price_snapshots.csv` | 导入人工快照（淘宝/闲鱼观测） |
| `run.py price --fetch-overseas` | [已废弃] 逐商品搜海外价，改用 `scripts/match_catalog.py` 的目录流水线 |
| `run.py price --purge-mismatched` | 清理历史上因宽松匹配写入的错配海外价 |

## 无密钥也能用

不配置 `DEEPSEEK_API_KEY` 时自动降级为**规则模式**（`src/infotwitter/rules.py`）：
用关键词规则判断事件类型、品类、量级、可行动性与传闻标记，
排序依然成立，只是没有中文翻译和摘要。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

**387 个用例**，覆盖：文本清洗、价格提取与换算、聚类与层级升级、多源独立性判定、
评分边界、破平规则、槽位补位阶梯、多样性约束、推送文本与 HTML 排版、推送标题契约、URL 转义、
摘要预算、story_id 稳定性、评分回放、台账 status 保护与幂等、claim 证据校验与跨文档交叉印证、
矛盾检测、中文社区卡、feed 有效性、反馈去重、预期价模型、以及连续多日运行的去重与补位。

其中两组是「规则化」的回归基线：

- `tests/golden_corpus.py` + `tests/golden/snapshot.json`：人工标注的金标准语料，
  断言「同输入必同输出」「打乱顺序结果不变」「只共享泛化实体的文档绝不合并」；
- `tests/test_push_state_machine.py`：推送台账 0/1/2 三态与崩溃恢复的终态收敛。

## 目录结构

```
config/
  config.yaml        运行配置（权重、阈值、窗口、通道）
  sources.yaml       源注册表（46 个源，含层级与接入方式）
src/infotwitter/
  adapters/          7 类采集适配器：rss / googlenews / browser / bilibili / tieba / nga / reddit
  pipeline.py        聚类（约束式合并）+ 评分卡 + 槽位分配
  llm.py             DeepSeek 特征抽取（可降级为规则模式）+ claim 证据校验
  rules.py           规则引擎（无 LLM 时的降级抽取）
  content.py         正文抓取（HTTP 优先 + Playwright 兜底 + trafilatura 抽取）
  pricing/           价格模块：商品目录 / 翻译 / 海外目录抓取 / 货号匹配 / 周更新品
  render.py          推送文本 + HTML 日报 + 推送标题
  notify.py          推送通道（PushPlus / 企业微信）
  server.py          日报静态服务（含 /healthz 与反馈接口）
  tokens.py          反馈接口的按天令牌
  runner.py          端到端编排（含启动状态收敛与失败告警）
  cli.py             命令行入口
  doctor.py          环境自检（配置/代理/密钥/日报页/推送状态/定时任务）
scripts/             长期运维脚本：翻译、目录抓取、补货号、匹配、周更编排、GitHub 推送
docs/                需求与设计，以及最近一次整改报告
data/                SQLite 数据库与登录态（不入库）
reports/             每日 HTML 日报（`_dryrun/` 里是 dry-run 产物）
logs/                运行日志 + alert.log（推送失败告警）
```

## 自动化

| 任务 | 时间 | 作用 |
|---|---|---|
| `InfoTwitter-Daily` | 每天 12:00 | 采集 → 分析 → 评分 → 推送 → 发布日报页 |
| `InfoTwitter-Price-Weekly` | 每周一 09:00 | 抓海外目录 → 补货号 → 新品入库 → 翻译 → 匹配写价 |

两个任务互相独立：价格链路跑挂不影响日更。注册方式见
[`scripts/install_task.ps1`](scripts/install_task.ps1) 与
[`scripts/install_weekly_task.ps1`](scripts/install_weekly_task.ps1)。

## 关键设计

**LLM 只抽取，不打分。** 模型输出结构化特征（事件类型、量级、可行动性、摘要、claim），
分数由 `pipeline.py` 按 `config.yaml` 里的权重计算，可回放、可调参、可解释。

**claim 必须能在原文里找到。** 模型给的 `evidence` 会在入库前做原文子串校验，
找不到就整条丢弃——这是"可信"承诺的最后一道闸。

**多源印证按发布域名算。** 同一个内容被多个采集配置重复收录不算独立来源，
否则"多源印证升级"会变成自己印证自己。

**评分卡 importance@v2**（满分 100）：

| 维度 | 权重 |
|---|---|
| 来源权威 | 20 |
| 事件量级 | 25 |
| 传播热度 | 20 |
| 可行动性 | 15 |
| 时效 | 10 |
| 个人相关 | 10 |

再乘以品类系数：40K / 30K = 1.0，AoS / TOW = 0.85，其他 = 0.60。

**两个时间语义不要混：**
- `published_at`（原文发布时间）→ 用于**时效**计分
- `discovered_at`（首次入库时间）→ 用于**24 小时推送窗口**

**降级阶梯**：合格候选不足 4 条时，依次用传闻补位 → 历史 backlog 补位 → 如实少推。

**成本**：`llm.provider: none` 为纯规则零成本模式；DeepSeek 模式下按每天 250-350 篇
入库、规则粗筛后 80-120 篇进模型估算，约 ¥0.5-2/天。分析结果缓存在 `raw_docs.analysis`，
同一文档不会重复计费。

## 已知限制

- **代理依赖**：官网、Goonhammer 等境外源需经系统代理访问。本机代理存在间歇性
  TLS 握手失败，已用重试 + 域名熔断（含冷静期半开）兜住，偶发源缺失属正常。
- **Google News 链接**：聚合条目无法还原原始 URL（Google 加密跳转），
  已被浏览器适配器抓到的同一事件会优先采用真实链接。
- **中文源覆盖不均衡**：中文 Google News（战锤40K / 战锤模型）实测可用并已接入；
  机核/游研社 RSS 可用但 7 天内往往没有战锤条目；贴吧/微博依赖 RSSHub，
  而公共实例被 Cloudflare 拦截（403），需自建实例。槽 6 因此仍偏向 B 站内容。
- **中文战锤新闻本身稀疏**：实测最新一条可能在 5 天前，所以中文源采集窗口放宽到 168h，
  槽 6 单独用 7 天窗口，并标注「N 天前」；通识槽仍严格要求新近发布。
- **价格数据**：国内官店价来自 GW 中国调价表（1745 个商品）；海外价通过目录抓取 + 货号匹配
  覆盖 900+ 个商品（可校验样本 100% 货号一致）。**涂料类**因国内是 6 支装、海外是单支，
  单位口径不同，暂不比价；**圣典**因版本不同（中文版 vs 英文版）也不比价。
- **闲鱼/淘宝**：`price --login` / `--fetch-cn` 已实现，闲鱼可抓（量少），淘宝依赖登录态、
  尚未跑通。合规优先，不考虑自研签名逆向。
- **日报页默认局域网可访问**：`app.report_host` 改成 `127.0.0.1` 可只给本机看。
  反馈接口用按天令牌 + 每日去重挡住刷权重，但没有真正的账号体系。
