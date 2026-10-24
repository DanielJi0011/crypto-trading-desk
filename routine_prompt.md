# 每日例行（北京时间 08:05）
0. （必做，只读）用 x 工具 search_posts_all 分别搜索 BTC/ETH/SOL 过去 24 小时公开英文帖（query 例：`(bitcoin OR $BTC) lang:en -is:retweet`，start_time 为 24 小时前 UTC，max_results 20，post.fields=created_at,public_metrics,author_id，expansions=author_id，user.fields=username），原始 JSON 分别存为 `/workspace/ta-desk/state/x_feed/<今天>/raw_<BTC|ETH|SOL>.json`，再运行 `cd /workspace/ta-desk && .venv/bin/python -m desk.x_feed_build <今天>` 生成 md。只读公开内容，不发帖、不点赞、不关注、不做任何互动。
1. 在盒子上运行：`cd /workspace/ta-desk && bash run_daily.sh`（约 5–15 分钟）。执行直连 **OKX 模拟盘**（Demo Trading，禁止实盘）；仓位与护栏按 `config.yaml execution.equity_basis` 的权益基准（默认 initial=state/demo_account.json 的初始权益）× 百分比计算，不写死金额。
2. **交易经理结构化字段自查**（见 `playbooks/trade_manager/` 当前版本）：读今日 `state/decisions.jsonl` 里交易经理结构化输出与模拟盘挂单/成交结果（`state/ledger/demo_events.jsonl`），检查无理由字段、矛盾值、默认/边界值、静默采用；已知口径差异不算问题：交易经理理由里引用的共识是其提示中【不含其本人投票】的 S（决策记录字段 `consensus_pre_tm`），代码最终共识 `consensus` 计入了交易经理这一票，两者不同属正常，只有理由引用值与 `consensus_pre_tm` 也不一致时才记；有问题写入 `state/field_audits/<今天>.md`（原始字段 + 追查结果），并在发给用户的摘要里主动提出；无问题摘要里写一句「交易经理字段自查：无异常」。不改交易逻辑、不下实盘单。
3. 读取 `reports/<今天>.md`（账户/持仓/挂单均来自模拟盘），把"账户概览、今日决策（每币一句）、平仓与复盘、手册/权重变动、LLM 成本、字段自查"用中文摘要发给用户，并附日报全文。
4. 若运行失败或报告"运行错误"，如实告知错误，不要重试实盘相关操作。系统只在 OKX 模拟盘交易（只用 OKX_DEMO_* 密钥 + x-simulated-trading: 1），绝不实盘。可用 `.venv/bin/python -m desk.okx_demo status` 只读核对模拟盘。
5. 周日的运行会额外做每周自研，摘要里附上 `research/report-<周>.md` 的结论。

# 晚场例行（纽约时间 08:30；北京时间夏令时 20:30，冬令时 21:30）
平台定时：`CRON_TZ=America/New_York 30 8 * * *`。流程、角色、讨论、加权、开平减仓、止损收紧与全部护栏与早场完全相同；已有持仓不加仓不反手，无持仓的币可以开新仓；晚场不跑基准对照组。
0. （必做，只读）同早场第 0 步重新抓取 X：时间窗为过去 12 小时，原始 JSON 存为 `/workspace/ta-desk/state/x_feed/<今天>-evening/raw_<BTC|ETH|SOL>.json`，再运行 `cd /workspace/ta-desk && .venv/bin/python -m desk.x_feed_build <今天> evening`。只读公开内容，不做任何互动。
1. 运行：`cd /workspace/ta-desk && bash run_daily.sh --session evening`。窗口为纽约时间 08:25 到 10:30，窗口外自动退出；完成标记 `state/daily_done/<今天>-evening`，日志 `logs/run-<今天>-evening.log`，决策ID `D-<日期>-E-<币>`。
2. 交易经理字段自查同早场第 2 步，只看今日晚场决策（决策ID 含 `-E-`）；结果追加到 `state/field_audits/<今天>.md` 的「晚场」小节。
3. 读取 `reports/<今天>-evening.md`，按早场第 3 步格式发中文摘要并附全文，标题注明「晚场」。
4. 失败处理同早场第 4 步。晚场不做每周自研。
