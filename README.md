# ta-desk：多角色加密货币交易台（纸面交易）

基于 TauricResearch/TradingAgents **v0.7.0**（`vendor/TradingAgents`，独立 venv `.venv`），每天由 Grok Bot 例行任务在北京时间 08:05 触发 `run_daily.sh`，对 BTC / ETH / SOL（OKX USDT 永续，模拟盘，私有请求一律带 `x-simulated-trading: 1`）运行一轮。本机没有 cron；`config.yaml` 的 `schedule.run_time` 不被代码读取。`run_daily.sh` 只在北京时间 07:55~12:00 窗口内执行非 mock 运行，窗口外记 `daily_duplicate_trigger`（out_of_window）后退出；补跑需设 `TA_DAILY_IGNORE_WINDOW=1`。

## 流程（`bash run_daily.sh`）
1. **盯市/结算**：用上次以来的已收盘 1H K 线检查止损/止盈（同根 K 线都触及按止损；跳空按开盘价），持有期到期平仓。
2. **打分**：到期（默认 5 天）的角色观点用 OKX 日线打分，更新角色权重。参与权重的是 ROLE_KEYS 去掉组合经理、加上交易经理（初始 1.0，满 20 条打分后按命中率调整）；组合经理只执行、不投票，不在 `state/weights.json` 里。
3. **分析**：TradingAgents 全流程（市场/情绪/新闻分析师 → 多空辩论 → 研究经理 → 交易员 → 激进/保守/中性风控 → 投资组合经理），价格数据全部来自 OKX（替换了 TradingAgents 的 Yahoo 价格源）。每个角色都会读到自己的**角色手册**。
4. **立场**：解析每个角色的【立场】行/结构化输出（方向、目标、止损、置信、周期），缺失才调用 LLM 抽取。
5. **交易经理**（`trade_manager`，勿与 `playbooks/trader` 交易员混淆）：深度模型给出结构化决策 → 代码规则（加权共识等）→ 护栏 → 纸面成交或条件挂单。杠杆等字段以交易经理结构化输出为准（见 `config.yaml` `leverage_max`）。**结构化字段自查**职责写在 `playbooks/trade_manager/`：每日随 08:05 例行复盘无理由/矛盾/边界字段，问题记入 `state/field_audits/`，见 `routine_prompt.md`。
6. **复盘会**：每笔平仓一次 LLM 复盘，逐角色判定对错与教训。
7. **手册修订**：投票角色（`ROLE_KEYS`）自上次修订后 ≥20 条新打分才审阅；每次最多 2 处改动；版本化（`playbooks/<角色>/vNNN.md`）；修订后观察 20 条，命中率比修订前低 5pp 以上自动回滚。**交易经理手册**不在该自动路径，人工版本化于 `playbooks/trade_manager/`。
8. **日报**：`reports/YYYY-MM-DD.md`（持仓、纸面盈亏、今日决策与各角色观点、成绩单、手册/权重变动、LLM 用量与成本）。

## v2 升级（2026-10-20）
1. **按角色分配模型**：`config.yaml` → `role_models`（厂商 deepseek / grok / gpt）。Grok、GPT 走用户中转 `RELAY_BASE_URL`（默认 `https://<LLM_RELAY_HOST>/v1`），grok-* 用 `XAI_API_KEY`，gpt-* 用 `OPENAI_API_KEY`。缺密钥自动回退 DeepSeek，并在日报"模型分配"一节注明。可用 `TA_MODEL_<角色>` 临时覆盖某角色模型。用量按厂商/模型分别记录（中转价格未知，只记 token）。
2. **角色专属数据**：
   - 技术（市场）分析师：新增 `run_backtest` 工具，可在 OKX 日线/4H/1H 历史（仅分析日之前）上回测做多条件，复用 ai-trader/backtest.py，含样本外统计。
   - 新增**资金流分析师**（flows）：OKX 公共接口的资金费率、持仓量、多空账户比、主动买卖比，DefiLlama 稳定币流通；报告只提供给多空研究员、研究经理、交易员、组合经理。ETF 资金流因 Cloudflare 拦截暂未接入。
   - 情绪分析师：Reddit/StockTwits（TradingAgents 自带）+ 可选的 X 讨论摘录 `state/x_feed/<日期>/<币>.md`（盒子上无法免凭据搜索 X，由 Grok Bot 例行任务用 X 搜索写入）。
   - 新闻分析师：TradingAgents 新闻工具。
3. **每周自研**（`desk/research.py`，默认周日随日常运行触发，或 `python -m desk.research --force`）：分析师（市场/情绪/新闻/资金流）按**角色专属特征菜单**（`research.role_features`：市场=涨跌幅/均线偏离/ATR/振幅/btc_ret_5/dow，情绪=rsi_14/dd_high20/up_high20，新闻=仅 vol_z20，资金流=vol_z20/ret_1/vol_dir；vol_dir=vol_z20×sign(ret_1)；含任一菜单外特征的假设在检验前整条拒绝，记入 `research/rejected.jsonl`，不登记、不计入检验数；结构化输出为空重试 1 次，仍空记「本角色无假设」）提出 ≤3 条假设（做多+做空合计，每条带 `side: long|short`，side 缺失（含旧记录）或非法记为 unknown「未知」：不检验、不写手册、报告单列「未知方向，跳过检验」） → 先写入 `research/hypotheses.jsonl` 预注册 → 三币日线约 4 年，按时间前 70% 样本内 / 后 30% 样本外，超额收益 = 交易净收益 − 同方向无条件基准（做多基准 = 漂移 − 双边费；做空基准 = −漂移 − 双边费，即与同期自然跌幅比较，做空止损在入场价上方；费用两边对称抵消） → 同一天入场的多笔合并为一个观测，t 值用 Newey-West（lag = 持有期）；分两阶段：每周探索期（`research.phase: explore`）按单次名义门槛，通过只进 `research/candidates.jsonl`，不算通过；季度确认（`research.confirm_quarter`）样本内 t ≥ Bonferroni 门槛（按**全局**累计实际检验过的假设数，所有角色、多空合并；未知方向跳过的不计）、样本外 t ≥ 1.65 且均值>0、样本量门槛（按观测日数）→ 通过才可写入手册（学习冻结期间只写候选）（版本化、每次 ≤2 条（多空合计）、做空结论写作「历史上随后 N 天做空的超额收益为正」、观察 20 条打分后命中率下降 >5pp 自动回滚；观察期内推迟写入）。每周成本上限 `cost_cap_usd` / `token_cap`。报告 `research/report-<周>.md`。登记表在角色菜单生效前的 8 条越权假设，在 `research/annotations.jsonl` 标注 `menu_violation: true`，仍计入检验数。新闻、情绪、资金流的特征都是价量代理，报告中附说明。

TradingAgents 自带的记忆与反思（`state/ta_memory/trading_memory.md`，到期后自动结算并反思）保持开启。

## FRED 宏观数据（desk/macro.py，2026-10-22）

- 每日从 FRED 官方 API（`fred/series/observations`，file_type=json，timeout=15）拉取 `config.yaml` 的 `macro.fred_series`：DGS10、DGS2、DFF、DTWEXBGS、T10YIE、CPIAUCSL（给同比）、VIXCLS；每项给最新值、日期、较 1 周 / 1 月变化。
- 密钥只从环境变量 `FRED_API_KEY`（`state/secrets.env`，run_daily.sh 自动加载）读取，绝不打印；日志/错误里的 URL 会把 `api_key` 替换为 `***`。
- 按日缓存到 `state/macro_cache.json`；单项失败用缓存旧值（标“旧缓存”），全部失败且无缓存则标注“不可用”，不会让 run_daily 失败。`macro.enabled: false` 关闭。
- 中文摘要只作为新闻分析师（news）的专属简报注入日常讨论；每周自研 `research.role_features.news` 仍只有 `vol_z20`。日报在账户概览后增加「宏观数据（FRED）」表格。
- 手动取数：`.venv/bin/python -c "from desk import settings, macro; print(macro.brief(macro.get(settings.load_config(), force=True)))"`（需先加载 secrets.env）。

## 环境变量
| 变量 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` 或 `TA_LLM_API_KEY` | LLM 密钥（优先 TA_LLM_API_KEY），绝不打印 |
| `TA_LLM_BASE_URL` | 可选，默认 `https://api.deepseek.com` |
| `TA_DEEP_MODEL` / `TA_QUICK_MODEL` | 可选，覆盖 config 里的 `deepseek-reasoner` / `deepseek-chat` |
| `RELAY_BASE_URL` / `XAI_API_KEY` / `OPENAI_API_KEY` | 中转地址与 Grok、GPT 密钥 |
| `TA_DESK_HOME` | 可选，数据目录（默认本目录） |

注意：DeepSeek 目前把 `deepseek-chat` 和 `deepseek-reasoner` 两个旧名都路由到 `deepseek-flash`；想用更强的深度模型可设 `TA_DEEP_MODEL=deepseek-v4-pro`（单价约 4 倍）。

## 安全
- `config.yaml` `mode: paper`；实盘需 `mode: live` **且** 存在 `state/LIVE_APPROVED`，且本版本**没有实盘下单代码**（即使满足也会拒绝）。OKX 只用公开行情接口，不读取 OKX 密钥。
- 紧急停止：`touch state/KILL`（停开仓：禁止现价单/条件单/限价补单并撤开仓类条件单；平仓、减仓、收紧止损、到期平仓照常；run_daily 跳过全部 LLM）。旧路径 `state/ledger/KILL` 过渡期仍生效并提示迁移。
- 全冻结：`touch state/HALT`（除读取、通知外禁止一切自动写操作，含到期平仓、改止损、补单；交易所端已挂止损止盈不动）。删除文件即解除。

## 命令
```bash
bash run_daily.sh                 # 3 个币
bash run_daily.sh --coins BTC     # 只跑 BTC
bash run_daily.sh --mock          # 模拟 LLM 演练（不耗 token）
.venv/bin/python -m pytest tests -q
```

## 目录
- `desk/`：settings、okx_data（OKX 数据接入）、llm（配置/用量成本/模拟 LLM）、roles（角色与手册注入）、analysis、stances、manager、ledger（纸面账本+护栏）、scoring、review、report、run_daily
- `playbooks/`：11 个投票角色手册与版本，另加 `trade_manager/`（交易经理固定职责）；`state/`：账本、决策、观点、打分、权重、用量、字段自查 `field_audits/`、TradingAgents 记忆；`reports/`：日报；`logs/`

## OKX 模拟盘直连（Demo Trading，2026-10-20 起）

> **禁止实盘。** 所有交易所调用都经 `desk/okx_demo.py`：只读 `OKX_DEMO_API_KEY / OKX_DEMO_SECRET_KEY / OKX_DEMO_PASSPHRASE`
> （存于 `state/secrets.env`，权限 600），每个请求硬编码 `x-simulated-trading: 1`，进程启动先调 `/api/v5/account/config` 自检，
> 失败即拒绝下单，绝不回退实盘 `OKX_API_*`。

**交易经理否决/缺席**（`manager.tm_status`，每日流程与 rediscuss 同一套）：
- 否决 = 交易经理方向投票为中性（action=hold 且未填条件入场）、无持仓时 sell/reduce、置信 0、或条件入场 entry_size_pct=0 → 本币不下现价单、不挂条件单，按 `cancel_pending_on_new_decision` 撤旧条件单（受保护的不撤），已有持仓不动，日报写「交易经理否决」。action=hold 但给了看多/看空的条件入场计划**不算否决**，日报标「动作标签与方向投票不一致」。
- 缺席 = 交易经理调用失败：这一票不计入共识，不算否决，本次不开新仓，日报写「交易经理缺席，不开新仓」。
- 日报：条件单表有「触发后限价」列（交易所 ordPx/orderPx，或登记表 order_px；-1=市价）；总名义占比写明分母「占初始权益/实时权益 X 的 y%」（按 `execution.equity_basis`）。重渲染日报（不重跑分析）：`python -m scripts.rerender_report YYYY-MM-DD --usage run-xxx.json:标签 --note INST=文本`。

**流程**：全员讨论照旧 → 加权聚合（方向/仓位/入场/杠杆/止损止盈）→ 组合经理执行（`manager.pm_execute_demo`）：
先过护栏（数据取自模拟盘），然后
- 现价入场：设逐仓杠杆（set-leverage）→ 市价单 + `attachAlgoOrds` 止损止盈；
- 有入场价：OKX `trigger` 条件单 + `attachAlgoOrds` 止损止盈（slOrdPx/tpOrdPx 仍为 -1，即止损止盈触发后市价）；新决策按原规则替换/撤销旧条件单。
  触发后的委托方式由 `execution.entry_limit` 决定：
  - `enabled: true`（默认）：`orderPx` = 限价，做多 = 触发价×(1−`offset_pct`%)，做空 = 触发价×(1+`offset_pct`%)，按 tickSz 取整；
    `desk/demo_watch.py` 每轮调用 `desk/entry_limit.py`：从触发时刻起超过 `timeout_minutes` 分钟限价子单仍未完全成交 → 撤子单；
    `fallback_market: true` 时先过 KILL/当日亏损护栏（不查单币一仓），再用市价同方向补剩余张数：
    一点没成交 → 补单附带相同止损止盈；部分成交 → 补单不附带，先按总持仓张数挂一张新的 OCO（reduceOnly），再撤旧的附带止损止盈，避免裸仓；
    护栏拦截或 `fallback_market: false` 时只撤单，并确保已成交部分有止损止盈。
    状态 `state/ledger/entry_limit.json`（文件锁 + 状态机防重入，补单用固定 clOrdId 重入时先查单），每一步写 `demo_events.jsonl`（`entry_limit_*`）；
    `demo_ws` 收到条件单触发推送时记下触发时间（只记录，不下单）。只处理开启后新挂的条件单，旧条件单不受影响。
  - `enabled: false`：与原来完全一样，`orderPx=-1`（触发后市价成交）。
  （`execution.protected_algo_ids` 里的不撤，当前保护 SOL 空单 4026675931388473344）；
- 已有持仓：清仓 close-position、减仓 reduceOnly 并按剩余张数重挂止损止盈、只收紧止损 amend-algos；交易经理 new_tp 在持有或减仓后改同一张 oco 的止盈（可调近调远，须在现价盈利一侧且距现价至少 manager.tp_min_gap_pct，张数等于剩余仓位）。交易经理提示中的共识 S 不含其本人投票，记录字段 consensus_pre_tm 保存该值，consensus 为计入交易经理后的最终值。
持仓模式 long_short_mode 时自动传 posSide。数量 = 保证金% × 权益基准 × 杠杆 / 价格 / ctVal，按 lotSz 向下取整。

**不再写本地纸面账本和本地 pending。** 账户、持仓、挂单（日报、护栏、交易经理提示词）全部从模拟盘读取。
旧纸面账本已归档到 `state/archive/paper-ledger-20261020`。本地只保留元数据 `state/ledger/demo_orders.json`
（条件单的有效期/来源、持仓的持有期到期时间、止损检测游标），事件日志在 `state/ledger/demo_events.jsonl`。

**权益基准（`config.yaml → execution.equity_basis`）**
- `initial`（默认）：读 `state/demo_account.json` 的 `initial_equity_usdt`（当前 101898.76 USDT，即非 USDT 资产全部卖成 USDT 后的值）；
- `live`：模拟盘实时 USDT 权益。
仓位 `size_pct` 与护栏都以基准 × 百分比计算，代码不写死金额：单笔保证金 ≤ 20%（约 20380）、总名义（含其他币条件单）≤ 180%（`max_total_notional_pct`，不随杠杆上限变化）（约 183418）、
当日亏损 ≥ 5%（约 5095）停止开仓（模拟盘当日已实现盈亏 `positions-history.realizedPnl` / 初始权益）。
最多 3 个仓（含其他币条件单）、每币一仓、杠杆 1~5 倍（2026-10-21 用户批准，由交易经理自主决定，超出夹到 5；止损晚于强平时自动降杠杆）。

**命令**
```bash
cd /workspace/ta-desk && set -a && . state/secrets.env && set +a
.venv/bin/python -m desk.okx_demo status          # 只读：自检、USDT 权益、posMode、持仓、挂单数
bash run_daily.sh                                  # 每日流程（直连模拟盘执行）
bash run_demo_watch.sh                             # 轻量轮询一次（不调 LLM）
setsid nohup bash run_demo_watch.sh --loop 300 >/dev/null 2>&1 < /dev/null &   # 常驻（需要时手动启动）
.venv/bin/python scripts/demo_smoke.py        # 冒烟测试：最小张数条件单挂/撤 + 市价开/平，检查残留
```
`desk/demo_watch.py`（`run_demo_watch.sh`）每轮：限价入场超时检查（见上，`execution.entry_limit`）；条件单成交登记持有期 / 过期撤单（受保护的除外）；持有期到期平仓；
从模拟盘算法单历史检测止损或止盈（actualSide=sl/tp）→ 后台启动该币全角色再讨论（`desk.rediscuss --reason sl|tp`，冷却 `pending.rediscuss_cooldown_hours`=6h）。止盈只触发再讨论，不记止损同向冷却；讨论提示会告知角色触发原因，止盈后提醒避免追单、优先回抽入场条件单。止盈后再讨论执行时：若共识选了现价开仓，而交易经理投的是挂条件单（或未投现价单），则不现价开仓，改按交易经理给的入场价、触发条件、止损、止盈走原条件单流程与护栏挂单；交易经理点位不完整或方向与共识相反则观望不下单（动作记为 hold(止盈后不追单)，原因写进 notes）。止损后再讨论与早晚场不受影响。再讨论的决策记录 session 记为 rediscuss。每轮回扫最近 24 小时已成交的止损止盈单，按 algoId 去重（registry watch.handled_algos），不只依赖水位。demo_ws 重连补查以子进程运行 `python -m desk.demo_watch --once`；demo_ws 启动记录代码版本，落后于 HEAD 时告警 stale_code（每小时至多一次）。

**本地纸面流程怎么关/开**：`execution.venue: okx_demo`（当前）即关闭纸面开仓与本地 pending；改回 `paper` 恢复旧流程。
`run_monitor.sh`（旧纸面盯盘）已按用户要求彻底停用，不要再启动。

## 模拟盘 WebSocket 常驻监听（desk/demo_ws.py）

> 只监听、不下单；只用 `OKX_DEMO_*`，只连模拟盘 `wss://wspap.okx.com/ws/v5/{private,business}`（硬编码）；不记录密钥。

- private：`orders`(SWAP) / `positions`(SWAP) / `balance_and_position`；business：`orders-algo`(SWAP) / `algo-advance`(SWAP)（按官方文档算法单频道在 business）。
- 登录签名 `Base64(HmacSHA256(ts+'GET'+'/users/self/verify'))`，每 25s 发 `'ping'`，收到 `pong` 写 `state/demo_ws_heartbeat`。
- 断线指数退避重连（上限 60s）；private 重连后调用一次 `demo_watch.run_once` 补查；连续断线 >10 分钟发一次告警。
- 事件（成交/部分成交、条件单触发、止损、止盈、持仓新增/归零、强平）去重后写 `state/ledger/demo_ws_events.jsonl`（去重记录 `state/ledger/demo_ws_seen.json`）。
- 止损或止盈 → `monitor.schedule_rediscuss`，trade_id 统一 `DEMO-<algoId>`，与 demo_watch 共用 `rediscuss.json`（加了文件锁），同一笔止损或止盈只再讨论一次，6h 冷却共用；触发原因写进事件、记录与通知。演练合约跳过，HALT/KILL 照旧拦截。
- 通知：`TA_NOTIFY_WEBHOOK_URL` / `TA_NOTIFY_WEBHOOK_KEY` 写进 `state/secrets.env` 即生效（每次发送时读取，无需重启）；密钥同时放 `Authorization: Bearer` 与 `X-Webhook-Key`；未设置只记日志。

```bash
setsid nohup bash run_demo_ws.sh >/dev/null 2>&1 < /dev/null &   # 启动（单实例锁 state/demo_ws.lock）
pgrep -af desk.demo_ws; cat state/demo_ws_heartbeat; tail -f logs/demo-ws-$(date +%F).log
.venv/bin/python -m pytest -q tests/test_demo_ws.py
```

## 第三、四批整改新增（2026-10-23，分支 wip/batch34，未合并）

- **对照组**（`desk/baselines.py`，`config.yaml` `baselines`）：买入持有、MA50 趋势、随机方向，以及按已记录立场与权重重算的消融组（仅交易经理、仅分析师、仅风控、全体、逐一去掉某角色）。纸面记账，不调 LLM；止损与仓位按 R1/R2/R4 规则，持有期 = `scoring.horizon_days`，同根 K 线止损止盈都触及按止损，只按市价入场（简化）。日报新增「对照组」小节。
- **链路告警**（`desk/chain_health.py`，`config.yaml` `alerts`）：kill/halt 开关变化、日报缺失（09:30 后）、链路异常（配置校验、止损覆盖、demo_ws 心跳超 30 分钟、对账待处理超 30 分钟）、healthcheck 自身过期；离机心跳 `alerts.heartbeat_hours` 默认 0（关闭），原因见配置注释。
- **状态备份**（`scripts/backup_state.sh`）：打包 state，排除 secrets.env、*.env 与锁文件，保留 14 份；离机目的地 `BACKUP_OFFHOST_DEST` 目前只提示，不传输。
- **外部内容隔离**（`desk/untrusted.py`）：新闻、全球新闻、Reddit、StockTwits、X 摘录统一加不可信内容边界与前言；日报模型表对经第三方中转站的角色加注。
- **打分记录**新增 `rules_cited`：观点理由中引用的研究规则编号。

## 晚场（2026-10-23）
- `bash run_daily.sh --session evening`：纽约时间 08:30 开场（北京夏令时 20:30，冬令时 21:30），窗口纽约 08:25 到 10:30，平台定时 `CRON_TZ=America/New_York 30 8 * * *`。
- 流程与护栏同早场；产物：`state/daily_done/<日期>-evening`、`reports/<日期>-evening.md`、`logs/run-<日期>-evening.log`、决策ID `D-<日期>-E-<币>`、X 数据 `state/x_feed/<日期>-evening/`。
- 晚场观点为独立打分样本，持有期从晚场决策时刻起算（1H K线）；不跑基准对照组；健康检查 `evening_missing` 在开场后 60 分钟仍未完成时告警一次。
- 每日下单数上限已取消（`max_orders_per_day: 0` = 不限）。

## 部署前配置占位符

仓库中的私有地址已替换为占位符，部署前请改成你自己的值（或通过环境变量覆盖）：

- `<LLM_RELAY_HOST>`：LLM 中转站主机名，见 `config.yaml` 的 `providers.grok/gpt.default_base_url`；也可设置环境变量 `RELAY_BASE_URL` 覆盖。
- `<BACKUP_HOST>`：离机备份 sftp 主机，见 `config.yaml` → `offsite.sftp_host`。
- 密钥与心跳 URL 不入库，参考 `secrets.env.example`。

克隆后请执行 `git submodule update --init` 拉取 `vendor/TradingAgents`。

## 运行环境

> ⚠️ 本项目**只连 OKX 模拟盘**（Demo Trading，`OKX_DEMO_*` 密钥 + `x-simulated-trading: 1`），绝不实盘。不要配置任何 `OKX_API_*` 等实盘变量，入口脚本也会主动清除这类变量。

### 当前生产环境

- 系统：Debian GNU/Linux 13 (trixie)
- Python：3.13.5（仓库内 `.venv`）
- 主要依赖：TradingAgents（`vendor/TradingAgents` 子模块，可编辑安装）、langgraph / langchain-core / langchain-openai / langchain-anthropic / langchain-google-genai、openai、anthropic、pandas、numpy、yfinance、stockstats、requests、websockets、PyYAML、pydantic、pytest。完整锁定版本见 `requirements.txt`（由生产 `.venv` 的 `pip freeze` 生成）。
- 常驻进程：`run_demo_ws.sh`（模拟盘 WS 监听）、`run_demo_watch.sh --loop 300`（模拟盘轮询）、`scripts/healthcheck_loop.sh`（每 10 分钟健康检查）。
- 早晚场触发：生产里由外部助手的例行任务定时调用。早场在北京时间 08:05 执行 `bash run_daily.sh`；晚场在纽约时间 08:30 执行 `bash run_daily.sh --session evening`（北京时间夏令时 20:30，冬令时 21:30）。
- **早晚报摘要（以及抓取 X 舆情到 `state/x_feed/`）是外部助手完成的，不属于本仓库。Docker 里只跑交易台本身**：常驻服务和定时的 `run_daily.sh`。没有 x_feed 时，日流程按缺省情况处理。

### 内置依赖 ai_trader/

`ai_trader/` 目录收录了交易台用到的 4 个模块：`store.py`、`guardrails.py`、`backtest.py` 与原版相同；`okx_client.py` 是**发布裁剪版**，只保留公共行情接口，删除了凭据读取、签名、下单、撤单、设杠杆以及全部私有接口，任何私有或非 GET 请求都会被拒绝。`desk/settings.py` 默认从仓库内的 `ai_trader/` 导入这些模块（也可以用环境变量 `AI_TRADER_SRC` 指向别的目录），克隆下来就能用，不需要外部目录。模拟盘交易只走 `desk/okx_demo.py`。

### 推荐：Docker

- Windows：安装 Docker Desktop（基于 WSL2 后端）
- macOS：安装 Docker Desktop
- Linux：安装 Docker Engine 和 Compose 插件
- 也可以在 Linux 或 macOS 上原生运行：用 Python 3.13 建 `.venv`，执行 `pip install -r requirements.txt && pip install --no-deps -e vendor/TradingAgents`

### 从零开始的完整步骤

```bash
# 1. 克隆并拉取子模块
git clone https://github.com/DanielJi0011/ta-desk.git
cd ta-desk
git submodule update --init

# 2. 准备数据目录和密钥（只填模拟盘密钥 OKX_DEMO_*，以及 LLM、通知 webhook 等）
mkdir -p state reports logs
cp secrets.env.example state/secrets.env
chmod 600 state/secrets.env
# 编辑 state/secrets.env

# 3. 填占位符：编辑 config.yaml
#    <LLM_RELAY_HOST> 改成你的 LLM 中转站（或在 secrets.env 里设 RELAY_BASE_URL）
#    <BACKUP_HOST> 改成离机备份主机（不用离机备份就关掉 offsite）
#    heartbeat_url_file 等绝对路径按你的环境调整

# 4. 构建并启动
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f demo_ws
```

- `docker-compose.yml` 包含 4 个服务：`demo_ws`、`demo_watch`、`healthcheck`、`scheduler`。前三个是常驻服务，`scheduler` 用 supercronic 按 `docker/crontab` 定时运行早晚场。所有服务都设了 `restart: unless-stopped`，`TZ=Asia/Shanghai`。
- `state/`、`reports/`、`logs/` 以卷的形式挂载到宿主机。密钥通过 `env_file: ./state/secrets.env` 注入，`.dockerignore` 已排除 state、reports、logs、.venv 和各类 secrets，密钥不会打进镜像。
- 容器里跑测试：`docker compose run --rm --no-deps -e TA_NOTIFY_DISABLED=1 demo_ws .venv/bin/python -m pytest -q`（测试使用临时数据目录，不发 webhook）。
- 手动补跑（窗口外需要显式设置）：`docker compose run --rm -e TA_DAILY_IGNORE_WINDOW=1 scheduler bash run_daily.sh`

### 入口脚本返回码

`run_daily.sh`：0 = 完成，或正常跳过（已在运行、已完成、不在窗口内）；5 = Python 解释器缺失；6 = 窗口判断调用失败。5 和 6 都会经通知 webhook 发中文告警；通道不可用时，至少写入日志和 stderr。其余非 0 值为日流程本身的返回码。
