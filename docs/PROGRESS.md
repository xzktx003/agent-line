# 实施进度（持续更新）

## 运行额度并发领取只预留一次（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有运行额度的并发准入：多个领取可以读到同一剩余次数，但不能同时放行。
- 预留发生在原有 `claim` 的 `BEGIN IMMEDIATE` 里。检查通过后写入一条 `submission_intent`，其他领取要等这次提交完成。超过 `maxSubmissions` 的领取失败，任务保持 `registered`，原因是 `run_submission_limit`。额度用尽后的尾任务仍可登记，只在领取时拒绝。
- 读快照里的 `admissionAllowed` 不是预留。token、模型调用次数和费用仍是未知，不记成零，也不做成硬限额。取消确认和其余资源生命周期没有改。结束清理仍只移除已证明容器。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7`、`PRD-11.8` 与 `PRD-11.9` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest 通过 `tests.test_execution_limits` 与 `tests.test_run_budget_overage`，共 22 项。其中新增的并发领取先一起看到剩余 2 次，最终只有 2 条 `submission_intent`。

## 结束清理只移除已证明容器（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有结束流程在移除容器时可能连正式 Agent、长期记忆、项目文件或需审阅工作树一起删掉。
- 结束时只允许 `docker rm` 一个 64 位十六进制容器 ID。`docker kill` 先走同一校验。身份不合格、ID 含工作区路径，或 ID 命中工作区根、`agent.json`、`MEMORY.md`、`review-worktree`，就在删除前拒绝，也不写 `exitReceipt`。删除后若这些当时存在的路径消失，拒绝并报告持久状态被删。
- 预算准入、取消确认、用量未知和已完成退避没有改。没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7`、`PRD-11.8` 与 `PRD-11.9` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest 通过 `tests.test_end_cleanup`，3 项。正常移除保留四类文件；非法容器 ID 不执行 `docker rm`、不写 `exitReceipt`；删除命中保留标记则拒绝。

## 离线、限流与无进展分别退避（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有巡检里离线、限流与无进展没有各自的等待。
- 服务不可用按 3、6、12、24、30 秒退避，不沿用未知的 2、4、8、16、30 秒。限流仍至少等 5 秒，并遵守 1 到 30 秒的 Retry-After；重复限流不自动加长。本地快照持续无进展仍在满 30 秒后按 2、4、5 秒，未满 30 秒仍是 1 秒。三种等待互不推进。换成另一种情况，或本地五项指纹变化，就从自己的第一档重新开始。
- 这只影响观察查询。不调用模型，也不把健康检查变成监控任务。预算准入、未知用量、取消确认和其余资源生命周期没有改。结束流程仍不会自动删除正式 Agent、长期记忆、项目文件或需审阅工作树。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7`、`PRD-11.8` 与 `PRD-11.9` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest 17 项通过，覆盖 `tests.test_poll_backoff` 4 项和 `tests.test_dispatch_loop` 13 项。

## 计量延迟超额显示实际超出次数（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有计量延迟造成提交次数超过上限时，页面仍把额度说成已耗尽，并把剩余次数夹成 0。
- 超额快照的 `remainingSubmissions` 等于上限减已用，可以为负。运行页改为显示实际超出次数，并写明不能据此把活动进程标成已停止。未超额时剩余次数不变；恰好用尽或到时仍用原来的耗尽文案。已用超过上限但剩余被夹成 0 时，页面显示无法核对。
- 新调用仍按原准入拒绝。token、模型调用次数和费用仍是未知，不记成零。巡检、取消确认和资源生命周期没有改。结束流程仍不会自动删除正式 Agent、长期记忆、项目文件或需审阅工作树。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7`、`PRD-11.8` 与 `PRD-11.9` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest 通过 `tests.test_run_budget_overage` 3 项，以及 `tests.test_execution_limits` 18 项。`node --check` 通过 `frontend/app.js`。

## 停止适配服务不向进程组发信号（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有停止适配服务时，执行客户端和launcher处在同一进程组，退出信号可能落到其他 Codex。
- Codex 执行客户端用独立会话启动，因此不在适配服务的进程组里。退出时先按原有容器所有权清理，再只向启动时刻仍匹配的那个 PID 发信号。不调用 `killpg`。启动时刻变化、无法读取或进程已退出时不发信号。
- 结束流程仍不会自动删除正式 Agent、长期记忆、项目文件或需审阅工作树；这一条还没改。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7` 与 `PRD-11.8` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest 通过 `tests.test_adapter_stop_scope`，5 项。

## 关闭浏览器不取消后台任务（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有关闭浏览器会丢掉正在观察的运行，重新打开不能按原运行 ID 恢复观察。
- 关闭页面不发送停止、取消或新的查询，后台任务继续。运行标识写在地址栏的 `binding` 和 `run` 上，不写入 localStorage 或 sessionStorage。重新打开只预填原运行；授权后按该标识重新查询，不启动新运行。从往返缓存回到页面时也不补发停止。
- 结束流程仍不会自动删除正式 Agent、长期记忆、项目文件或需审阅工作树；这一条还没改。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7` 与 `PRD-11.8` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- `node --check` 通过 `frontend/run-observation.js` 和 `frontend/app.js`。`tests.test_browser_close_observation` 1 项通过。

## 原生取消回执不再提前当成已停止（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有停止活动任务时，原生中断确认在没有容器退出证明时被当成已停止。
- 原生取消回执只表示中断已申请。没有匹配的容器退出证明时，活动任务保持 `cancel_requested`，`resourcesReleased` 为 false。父任务停止确认和委派撤销都不能代替 Codex 退出证明。
- 没有撤销时，即使原生报告退出也不释放。撤销成功后，后续观察仍可报告同一原生退出事实；该事实本身不是容器退出证明，任务在证明落地前不是已停止。带有匹配容器退出证明的取消才会结束并释放资源。未知、超时和不匹配身份仍不能确认。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7` 与 `PRD-11.8` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest 162 项通过，覆盖 dispatcher、role dispatch、run control wiring、run control、run control api、dispatch loop、event contract、completion relay、completion worker 和 native approval api。

## 停止后一次扫描移出全部排队任务（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有停止已经提交、但任务尚未逐个扫描时，排队任务没有全部移出队列。
- 调度循环发现某个运行已停止后，同一次扫描会把该运行全部仍是 `registered` 的任务记为 `canceled` 并保留记录。这些任务没有启动，因此按原取消规则释放资源并离开队列。不调用原生 `submit` 或 `cancel`。当前页只看到其中一个排队任务时，同运行的其余排队任务也会取消。其他运行的排队任务保持 `registered`。
- `accepted`、`submitting`、`uncertain` 仍只进入 `cancel_requested`。资源未释放前不是已停止。当前页之外的活动任务不会被提前标成已退出。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7` 与 `PRD-11.8` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest 108 项通过，覆盖 run control wiring、run control、run control api、dispatch loop、event contract、completion relay、completion worker 和 native approval api。

## 已记下的未决审批阻止继续（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有恢复流程时忽略已经记下的未决审批，仍允许后继准入。
- 继续前只读已保存的审批事件，不查询原生审批。`waiting` 且同一 `requestId` 没有决定时拒绝继续，包括排队和已结束任务。`expired`、`unbound`、`decision_recorded` 不阻止。没有审批记录时，原来的继续核对不变。
- 新的 `approval_created` 和 `approval_decided` 载荷带 `requestId`。带绑定的拒绝在原四字段外增加 `requestId`。已确认的权限拒绝仍接受旧的四字段和新的五字段。没有 `requestId` 的旧 waiting 无法配对，损坏记录同样失败关闭。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7` 与 `PRD-11.8` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest 95 项通过，覆盖 event contract、run control、run control api、run control wiring、completion relay、completion worker 和 native approval api。

## 暂停必须选择活动任务继续或中断（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有新的暂停不说明活动 Codex 是继续还是中断。
- 新暂停必须带 `activeExecution`，只能是 `continue` 或 `interrupt`。继续和停止若带这个字段则拒绝。`continue` 不申请取消，活动任务保持原状态。
- `interrupt` 只对当前 `accepted`、`submitting`、`uncertain` 的本运行任务调用既有 `request_cancel`。`registered` 保持排队。不调用 Sim `journal.cancel`，不把资源标成已释放，未确认前不是已停止。相同中断载荷重放时按当前状态补申请：仍是 `registered` 的保持排队，已经变成 `accepted`、`submitting` 或 `uncertain` 的会补上取消申请。
- 旧的三字段暂停仍可读取，状态显示 `activeExecution='unrecorded'`。按原载荷重放不补选择；改成继续或中断会冲突。界面先打开选择框再提交。未记录的暂停不显示为已停止。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7` 与 `PRD-11.8` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest 49 项通过，覆盖 run control、run control api、run control wiring 和 native approval api。`node --check` 通过 `frontend/app.js`。离线探针打开 `/workbench/runs.html`，17 项检查通过，模拟 POST 5 次，真实 HTTP 请求 0。

## 原生同线程重试保持原任务身份（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有同一线程出现第二个 `task_started` 时，观察把原任务打成未知。
- 同一 rollout 按事件顺序只取第一个 `task_started` 的 turn。尚未绑定 turn 时只绑定这一个。已绑定且不一致时保持原身份并返回未知，不改写。
- 原 turn 的状态、输出和证据只从该 turn 读取。后一个 turn 不新建任务、不换线程、不替换已绑定 turn，它的完成也不覆盖原输出，不新增 `submission_intent` 或执行身份。
- 缺少首轮、turn 冲突，以及同一 rollout 的多轮用量仍失败关闭。`codex_usage_snapshot` 仍要求恰好一轮。权限证据里的额外 turn 仍拒绝。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest 83 项通过，覆盖 bridge native retry、interruption、retirement、resources、cancel、native rejection、runtime retirement、launcher exit、plugin contract 和 memory after restart。

## 未受理拒绝仍计入共享预算（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有受理前明确拒绝把任务退回 registered 后，原提交不再被账本当成未知。
- 状态仍是 registered、提交仍 pending、身份和证据为空、资源未退出，并且恰好一次 `submission_intent` 后只跟着拒绝或解除拒绝、最后一条是 `submission_rejected` 时，这一次提交继续计数，起始时间不变。显式修正后的再提交只写解除事件，不新增 `submission_intent`。
- 同一节点的重试或循环继续消耗剩余业务次数。同一运行中的另一个节点可以继续，直到 `run_submission_limit` 或 `run_execution_deadline`。token、模型调用次数和费用仍是未知，不记成零。跨 run 的嵌套总预算仍不做。未知尾部事件或错误原因仍失败关闭。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest 通过 `tests.test_execution_limits`、`tests.test_dispatcher`、`tests.test_dispatcher_limits`、`tests.test_service_limits`，共 49 项。`node --check` 通过 `frontend/app.js`。共享额度文案 5 项断言通过。


## 旧激活的迟到结果不再推进当前轮次（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有旧激活的迟到结果仍可能恢复一个已经进入下一轮的暂停。
- 同一运行已经接受了更高的 `block_loopN` 时，更早一轮的结果不再恢复暂停，原因是 `stale_activation_not_current`。不查询当前暂停，不重发执行，不新建尝试，也不覆盖新尝试的输出。
- 已经记下的旧回执仍只按原记录返回。当前这一轮自己的结果仍可回传。没有更高轮次时，原来的回传规则不变。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest 通过 `tests.test_completion_relay`、`tests.test_completion_worker`、`tests.test_event_contract`、`tests.test_parallel_wait_relay`、`tests.test_loop_task_discovery`，共 82 项。


## 已确认的执行器中断不再先查暂停（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有已确认的执行器中断仍先核对暂停，然后才按失败分支回传。
- 执行状态是 `failed`、资源已退出、记忆策略已满足，并且有唯一配对的 `execution_interrupted` 与 `resource_exit` 时，有失败分支就回传一次原失败状态。不查询暂停，不重发执行，不新建尝试。这不表示收到了 Codex 原生终态。
- 没有失败分支时原因是 `execution_interrupted_needs_human`，原任务和原尝试保持不动。证据不完整、进程仍在运行、取消意图不明或记忆未提交时仍只等待。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest 通过 `tests.test_completion_relay`、`tests.test_completion_worker`、`tests.test_event_contract`、`tests.test_parallel_wait_relay`、`tests.test_loop_task_discovery`，共 81 项。`node --check` 通过 `frontend/task-activity.js`。执行器中断文案 7 项断言通过。


## 已确认的执行失败不再先查暂停（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有已确认的 Codex 执行失败仍先核对暂停，没有失败分支时仍被当成结果未就绪。
- 执行状态是 `failed`、资源已退出、记忆策略已满足，并且有匹配当前执行 ID 的已确认 `execution_terminal` 时，有失败分支就回传一次原失败状态。不查询暂停，不重发执行，不新建尝试。新尝试仍只由 Sim 按节点策略决定。
- 没有失败分支时原因是 `execution_failure_needs_human`，原任务和原尝试保持不动。证据不完整、资源未退出或记忆未提交时仍只等待。执行器中断仍先核对当前暂停。记忆写入失败时，执行失败仍不进入失败分支。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest 通过 `tests.test_completion_relay`、`tests.test_completion_worker`、`tests.test_event_contract`、`tests.test_parallel_wait_relay`、`tests.test_loop_task_discovery`，共 81 项。`node --check` 通过 `frontend/task-activity.js`。执行失败文案 7 项断言通过。


## 受理前明确拒绝不再自动重发（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有提交在受理前被明确拒绝后仍可能自动重发。
- 精确响应头 `X-Agent-Line-Submission: rejected_unaccepted` 且状态是 409 或 422 时，原因记为 `submission_rejected_unaccepted`。任务回到 registered，不保留执行身份，自动循环不再重发。只有显式修正后的一次性证明可以再提交一次。
- 未知结果、5xx、超时、普通 409，以及已经带有执行身份的拒绝，仍是 uncertain，不当成受理，也不自动重提。模型不可用标记仍优先于这条拒绝。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest 通过 `tests.test_qwenpaw_gateway`、`tests.test_dispatcher`、`tests.test_dispatch_loop`、`tests.test_role_dispatch`、`tests.test_task_store`、`tests.test_run_view_submission_block`、`tests.test_event_contract`，共 100 项。`node --check` 通过 `frontend/task-activity.js`。拒绝文案 7 项断言通过。


## 长期记忆写入失败只重试记忆阶段（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有已确认的长期记忆写入失败仍被当成代码结果未就绪，完成回传可能因此恢复或进入新的代码尝试。
- 编码成功、独立验收通过、资源已退出，且绑定要求记忆提交时，记忆状态恰好是 `failed` 的原因是 `memory_write_needs_retry`。不查询暂停，不恢复流程，不调用执行，不新建尝试。原任务、产物和尝试标识保持不变。
- 记忆仍待核对、需要审核或尚未请求时，继续只等待。不要求提交记忆时，原结果仍可回传，并保留失败的记忆状态。执行失败遇到记忆写入失败时，也不进入失败分支。这条完成回传本身不提交记忆写入。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest 通过 `tests.test_completion_relay`、`tests.test_completion_worker`、`tests.test_event_contract`、`tests.test_parallel_wait_relay`、`tests.test_loop_task_discovery`，共 81 项。`node --check` 通过 `frontend/task-activity.js`。记忆失败文案 4 项断言通过。


## 鉴权失败和缺模型不再自动重查（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有认证失败和明确的模型不可用仍被当成可重试的状态查询。
- 提交或查询得到认证失败，或响应带有精确的模型不可用标记时，自动循环不再继续查询。请修复原来的认证或模型后再核对。不会换成其他模型或执行路径，也不会重新提交。
- 短暂的未知、不可用和限流仍按原间隔重试查询。取消请求和执行期限仍核对原任务。手动核对入口仍可查询，不经过这条自动阻塞。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest 通过 `tests.test_completion_relay`、`tests.test_completion_worker`、`tests.test_event_contract`、`tests.test_dispatch_loop`、`tests.test_qwenpaw_gateway`、`tests.test_role_dispatch`、`tests.test_role_revision_plugin`，共 127 项。`node --check` 通过 `frontend/task-reconcile.js`。认证和缺模型文案 2 项断言通过。


## 已确认的权限拒绝不再当成等待或重试（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有已确认的原生权限拒绝仍可能被当成普通等待、传输重试或新的业务尝试。
- 执行已经结束，拒绝决定与当前任务和原生身份一致，且唯一执行策略仍是只读沙箱和 `never` 时，有失败分支就回传一次原状态。没有失败分支时原因是 `permission_denied_needs_human`，原任务、沙箱和审批策略保持不动。不查询暂停，不调用执行，不新建尝试。
- 执行尚未结束、资源未退出、记忆未提交，或控制项已经变化时，仍只等待，不提前判整单失败。只读执行不允许批准扩大权限的限制保持不变。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest 覆盖完成回传、完成循环和事件契约。`node --check` 覆盖 `frontend/task-activity.js`。权限拒绝文案另有断言。


## 已确认的测试失败进入修复分支，不再当成传输重试（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有已确认的测试失败与执行失败共用暂停查询和恢复。
- 执行成功、独立验证失败、证据匹配且至少一项检查失败时，若绑定已有失败分支，回传一次原验证状态。不查询暂停，不重发执行，不新建尝试。没有失败分支时原因是 `verification_failure_needs_human`，原任务保持不动。证据不完整、资源未退出或记忆未提交时仍只等待。
- 执行失败和执行器中断仍先核对当前暂停。完成循环只报告这个转人工原因；其他等待原因继续隐藏。任务中心说明有修复分支才送入，否则转人工。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest 通过 `tests.test_completion_relay`、`tests.test_completion_worker`、`tests.test_output_contract`、`tests.test_automatic_verification`、`tests.test_parallel_wait_relay`、`tests.test_loop_task_discovery`，共 78 项。`node --check` 通过 `frontend/task-activity.js`。验收失败文案 5 项断言通过。


## 无法整理的输出转人工，不再当成传输等待（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有输出结构无法整理时仍返回普通等待，完成循环会把它和传输重试混在一起。
- 已成功执行但声明结果仍不合法时，回传原因是 `declared_output_needs_human`。不查询原暂停，不恢复流程，不新建尝试。同一结果的单一围栏投影仍可继续交付，原文不改。
- 其他等待原因不写入完成循环报告。验收在独立测试前阻断，原因同样是转人工。任务中心对 invalid 显示已转人工，不把 unsupported 说成传输重试。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest 通过 `tests.test_completion_relay`、`tests.test_completion_worker`、`tests.test_output_contract`、`tests.test_automatic_verification`，共 39 项。`node --check` 通过 `frontend/task-activity.js`。转人工文案 5 项断言通过。


## 同一结果的单一围栏可以投影（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有输出结构不合法时完全拒绝整理同一结果。
- 整段正文恰好是一个无语言标记或 `json` 标记的 Markdown 围栏，且内部是原完整 JSON 时，校验原因是 `same_body_fence_unwrapped`。`structuredOutput` 和回传 `result` 使用这份正文。已保存的 `text` 不改写，也不新建尝试。
- 围栏前后有说明、结尾多一个空行、多层围栏、其他语言标记、缺字段、额外字段或重复键，仍是 `business_result_invalid`，转人工核对，不猜测补齐。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境 unittest `tests.test_output_contract` 5 项通过。`node --check` 通过 `frontend/task-activity.js` 与 `frontend/app.js`。`outputShapeNote` 与 `showsBusinessFields` 11 项断言通过。


## 新的结果回调带上激活和尝试身份（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有新的 Sim 结果回调只关联流程和节点，不写明这次激活与尝试。
- 新回执现在包含 `nodeExecutionId` 和 `attemptId`。尝试标识缺失、为空或含空白时，不恢复暂停，也不发起新的原生查询。执行状态未知时仍只等待，不自动做业务重试。
- 已经记下的旧回执如果没有这两项，仍按原记录返回，不改写成新正文，也不再发送一次。回执里的激活或尝试与当前任务不一致时，拒绝把它当成同一次交付。这次没有创建尝试，也没有提交运行。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-11.7` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- 项目虚拟环境的 unittest 通过 `tests.test_completion_relay`、`tests.test_parallel_wait_relay`、`tests.test_completion_worker`、`tests.test_run_control_wiring`、`tests.test_loop_cancellation`、`tests.test_loop_task_discovery`、`tests.test_compiled_parallel_integration`，共 91 项。

## 运行图保留多次尝试和重新运行边界（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有运行图把多次尝试折在标识里，也不说明重新运行会另建运行记录。
- 同一节点有至少两个不同尝试标识时，尝试号直接显示，并说明记录分别保留。其中有执行失败或验收失败时，该尝试的按钮是“查看失败证据”，打开的仍是原任务详情，不覆盖其他尝试。尝试标识相同或无法核对时，不称为新尝试。
- 运行图读取完成和本地状态更新时说明：重新运行会创建新的运行记录，入口是“以原输入准备新运行”，不要把本次运行当成可原地重跑。这次没有创建尝试，也没有提交运行。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-13.4` 仍 unverified。连线命中仍是 unknown，Codex 活动仍无进程证据。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- `node --check` 通过 `frontend/run-view.js`。`attemptRetention` 与 `rerunBoundaryNote` 12 项断言通过。

## 已有阻塞标出类别（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有运行详情和任务行不把已经返回的阻塞标成审批、等待回答、资源不足、输出错误或执行未知。
- `waiting_approval` 标审批，`waiting_input` 标等待回答。已附在任务上的项目名额、工作区占用或角色占用标资源不足。执行已结束且结果结构为 invalid 或 unsupported 时标输出错误。提交中、提交未知、队列原因无法核对，或最近核对未知，标执行未知。
- 准入关闭、串行排队、次数上限、角色版本不符、验收失败和原生暂停都不标成认证失效。没有这些字段时不显示阻塞类别，也不重算 500 条队列。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-13.4` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- `node --check` 通过 `frontend/task-activity.js`、`frontend/app.js`、`frontend/overview.js`、`frontend/run-view.js`。`namedBlock` 21 项断言通过。

## 运行图节点可打开原任务详情（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有运行图节点的“查看任务详情”只填入任务号、不读取原任务详情。
- 已授权且标识合法时，这次点击提交原有“查询任务”，打开同一份详情：角色会话说明、Codex 执行标识、产物、测试证据和记忆追踪入口。未授权或非法标识不查询。首页链接仍只预填。
- 事件正文和记忆快照仍需在详情里再点一次。查看或刷新画布不加载历史工具正文，也不另建任务页。连线命中仍是 unknown，Codex 活动仍无进程证据。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-13.4` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- `node --check` 通过 `frontend/run-view.js` 与 `frontend/app.js`。`nodeDetailAction` 与 `nodeDetailHint` 15 项断言通过。

## 运行图单独说明读取连接状态（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有运行图把最近一次读取结果混在时间里，不单独说明连接状态。
- 尚未读取、读取成功、读取失败并保留固定结构、读取失败且没有可保留的图，分别给出连接状态。成功不表示连线已命中，也不表示 Codex 仍连接。失败不表示 Codex 已断开。自动更新暂停不改连接状态。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-13.4` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- `node --check` 通过 `frontend/run-view.js`。`connectionAnswer` 8 项断言通过。

## 运行图节点显示已有阻塞原因（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有运行图节点任务不显示任务中心已经能解释的阻塞原因。
- 提交中、提交未知和申请取消沿用原状态说明。已登记任务只有回执带合法 `submissionBlock` 或串行等待队列时才显示原因；没有这些字段不编造。验收失败和执行成功仍不是阻塞。
- 运行图只转交任务列表已经算好的四种启动阻塞，不把最多 500 条节点任务再拿去重算队列。资源不足、工作区占用等队列原因仍在任务详情里，图上不另造。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。现场运行图还没有 `submissionBlock`。`PRD-13.4` 仍 unverified。连线命中仍是 unknown，Codex 活动仍无进程证据。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- `node --check` 通过 `frontend/run-view.js`。`runTaskBlockReason` 13 项断言通过。`tests.test_run_view_submission_block` 2 项通过。

## 运行详情显示已核对的项目、固定版本和任务参数（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有运行详情不显示已经核对的项目、固定流程版本和根提交任务参数。
- 读取运行图成功后，项目和固定流程版本只取本次图回执里的 `identity.projectId` 与安全整数 `revision`。缺项目或版本不是安全整数时不显示这三行，也不改用绑定列表上的当前版本。操作者固定写“本次提交回执没有操作者字段”，因为提交回执没有该字段。
- “读取本次任务参数”只请求现有 `GET /workflows/{binding}/runs/{run}/input`。身份、来源和对象结构核对后才展示；含当前凭证则禁止展示；超过 64000 字节不展示正文。文案说明这是本地根提交输入，不是 Codex 提示词或工具正文。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-13.4` 仍 unverified。连线命中仍是 unknown，Codex 活动仍无进程证据。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- `node --check` 通过 `frontend/run-view.js` 与 `frontend/app.js`。`fixedRunContext` 与 `parameterAcceptance` 22 项断言通过。


## 运行详情明确回答整体流程是否暂停（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有运行详情在没有暂停点时不回答整体流程是否暂停。
- 原生摘要新增“整体流程是否暂停”。已返回暂停记录且状态为 paused 时回答是；状态为 paused 但没有暂停点时说明是，并保留原来的“未提供具体暂停点”。其他已知状态且暂停记录为空时回答否，暂停信息改为“本次原生记录未暂停，因此没有暂停点”。未知状态或状态与暂停记录矛盾时不回答是或否。
- Codex 活动仍只在接口返回 `unknown` 时显示未知，并写明不根据流程是否暂停推断。资源尚未退出、原生未暂停和原生已暂停都不当成 Codex 仍在运行。没有重载适配器 `870621`，没有提交新任务，也没有调用现场健康检查。`PRD-13.4` 与 `PRD-13.7` 仍 unverified。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- `node --check` 通过 `frontend/run-view.js`。`workflowPausedAnswer` 与 `codexActivityAnswer` 12 项断言通过。

## 任务中心显示已保存的负责人 Agent（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有任务中心不显示已经保存的负责人 Agent。
- 授权任务列表把 `roleAgentId` 从折叠标识改到可见行。合法的 1 到 256 字符身份原样显示。键在但为空、超长、带空白或不是文本时显示“原记录未提供”，不从角色绑定名称补造。没有这个键的旧响应不新增行。
- 未授权首页的源码现在也返回同一套核对后的 `roleAgentId`。适配器 `870621` 没有重载，现场首页还是旧响应，所以这次不会凭空多出这一行。静态的授权列表下次打开即可使用现进程已经返回的身份。
- 没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-13.7` 仍 unverified。原版中文覆盖仍未完成。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- `tests.test_service` 22 项通过。`node --check` 通过三个前端文件。`responsibleAgent` 6 项断言通过。

## 任务列表显示已保存的固定流程版本（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有任务列表不显示已经返回的流程版本。
- 首页和授权任务列表在 `workflow.revision` 这个键存在时显示“固定流程版本”。合法的 1 到 256 字符版本原样显示。键在但为空、超长、带空白或不是文本时显示“尚无记录”，不从流程号、运行号或角色版本补造。没有这个键的旧响应不新增行。任务详情原有的“发布版本”行保持不变。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。静态页下次打开生效。`PRD-13.7` 仍 unverified。原版中文覆盖仍未完成。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- `node --check` 通过 `frontend/task-activity.js`、`frontend/app.js`、`frontend/overview.js`。`workflowRevision` 8 项断言通过。

## 授权任务列表显示已保存的业务目标（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有授权任务列表不显示已经返回的业务目标。
- 授权目录在响应带有 `workItem` 时，沿用原来的核对规则显示业务任务、固定目标和验收条件。缺这个键的旧响应不新增行。对象在但无法核对时，只说明请查询原登记，不补造目标。未授权首页仍只显示业务任务号，不把目标放进公开摘要。
- 没有重载适配器 `870621`，也没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。静态页下次打开生效。`PRD-13.7` 仍 unverified。原版中文覆盖仍未完成。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- `node --check` 通过 `frontend/task-activity.js`、`frontend/app.js`、`frontend/overview.js`。`businessLines` 6 项断言通过：无对象不显示，合法目标显示三条，项目不一致、空目标和空验收条件都只提示无法核对。

## 任务中心单独显示等待原因（2026-10-28）

- 执行模型仍是 `gpt-6-luna`。`10.40.0.2` 没有这个模型，不作为执行路径。本轮放开的限制只有任务中心把等待原因混在“需要关注”里。
- `waitingReason()` 只在这些情况返回原因：已登记且已有 `submissionBlock`，或已有合法的串行等待队列；`submitting` / `uncertain`；`cancel_requested`。没有队列载荷的已登记任务返回 `null`，不编造。`accepted`、验收失败和记忆失败仍只显示“需要关注”。原因文字相同时不再重复“需要关注”。
- 首页、运行管理列表和任务详情都显示“等待原因”。不新增接口、不补写事件、不重载适配器 `870621`。静态页由进程现读，下次打开页面生效。现场 Python 仍是旧代码，但队列和启动阻塞字段已由现进程返回。
- 没有放开 `kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。没有提交新任务，也没有调用现场健康检查。`PRD-9.2`、`PRD-12.5`、`PRD-13.7` 仍 unverified。完整业务任务展示和原版中文覆盖仍未完成。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- `node --check` 通过 `frontend/task-activity.js`、`frontend/app.js`、`frontend/overview.js`。同一解释函数的 11 项行为断言通过：无队列的已登记任务不产生等待原因；已知队列原因只显示“等待原因”；验收失败仍只显示“需要关注”；`accepted` 不叫等待。

## 任务中心显示真实角色启动时间（2026-10-28）

- 放开的限制只有任务读取不提供执行开始时间。`activity.startedAt` 只取已保存的 `role_started`，用最早的 `occurred_at`；旧库没有该列时才用这条事件的 `created_at`。没有这条事件时返回 `null`。登记时间、受理、委派开始和最近事件都不当作开始时间，也不补写事件。
- 任务中心只在响应已经带 `startedAt` 时显示“开始时间”。没有这个键的旧响应不新增这一行。空值或非法时间显示“尚无记录”。
- 没有重载适配器 `870621`，也没有放开 `10.40.0.2`、`kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。QwenPaw `3297344`、网关 `1918031` 未动。执行模型仍是 `gpt-6-luna`，这轮没有提交新任务，也没有调用现场健康检查。`PRD-9.2`、`PRD-12.5` 和 `PRD-13.7` 仍不标 passed。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- `.venv/bin/python -m unittest tests.test_service tests.test_service_limits tests.test_recovery_records tests.test_execution_degradation tests.test_event_contract` 52 项通过。`node --check` 通过 `frontend/task-activity.js` 和 `frontend/app.js`。任务中心旧夹具仍打开 `/workbench/`，首页已经没有 `#token`，登录框在 `runs.html`，所以这次没有把它当成回归通过。


## 能力检查说明未写入的原生身份（2026-10-28）

- 放开的限制只有 `GET /extension/v1/capabilities` 不说明适配版本不暴露 `roleSessionId` 和 `executorAgentId`。响应增加只读 `nativeExecutionIdentity`：两项都是 `not_written`，并写明可核对任务上的角色 ID，上游任务号只在已记录时有效。没有把这两个字段写入存储的 `executionIdentity`，也没有补造会话号或执行者 ID。
- 任务中心只在已返回 `executionIdentityAvailability` 时显示“角色会话说明”和“执行者说明”。没有这份说明的旧响应仍显示原来的上下文行。缺省、脏值和已存值都用读取结果里的原因，不从 `agent-line-<taskId>` 猜测。能力页仍接受没有该字段的旧响应，避免现场旧进程把摘要判成契约失败。
- 没有重载适配器 `870621`，也没有放开 `10.40.0.2`、`kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。QwenPaw `3297344`、网关 `1918031` 未动。执行模型仍是 `gpt-6-luna`，这轮没有提交新任务。现场进程仍是改前代码，所以没有把 `PRD-9.2` 或 `PRD-12.5` 标成 passed。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- `.venv/bin/python -m unittest tests.test_service tests.test_service_limits tests.test_recovery_records tests.test_execution_degradation` 40 项通过。`node --check` 通过 `frontend/service-capabilities.js` 和 `frontend/app.js`。没有调用现场健康检查。

## 执行身份缺失说明（2026-10-28）

- 放开的限制只有公开任务读取不说明未写入的原生身份。`roleSessionId` 和 `executorAgentId` 仍不写入存储的 `executionIdentity`。读取时在旁边返回 `executionIdentityAvailability`：缺省是 `not_written` 并给出原因；已存值标 `recorded`，不能当证明的脏值标 `unavailable`。角色 ID 仍在任务字段，上游任务号只在已记录时标 `recorded`。没有补造会话号或执行者 ID，也没有改审批、验证和取消使用的整份身份。
- 没有重载适配器 `870621`，也没有放开 `10.40.0.2`、`kilo-auto/free`、taskField、user namespace、`approval_policy=untrusted`、CSP 或 `X-Frame-Options`。QwenPaw `3297344`、网关 `1918031` 未动。现场进程仍是改前代码，所以没有把 `PRD-9.2` 或 `PRD-12.5` 标成 passed。条款计数仍是 passed 12 / in_progress 4 / unverified 86。G0-08 与 PRD-18.1 不改。
- `.venv/bin/python -m unittest tests.test_service` 20 项通过。`tests.test_service_limits tests.test_recovery_records tests.test_execution_degradation` 20 项通过。没有调用现场健康检查，避免用新源码指纹把当前准入标成失效。


## 嵌入原版画布的流式连接（2026-10-28）

- 没有新放开限制，也没有改上游、放宽 CSP 或 `X-Frame-Options`。执行模型仍是 `gpt-6-luna`。适配器 `870621`、QwenPaw `3297344`、网关 `1918031` 未换。健康检查仍是 `controlledExecution=confirmed`、`readyForExecution=true`。
- 同源网关嵌入原版画布已通过。文档状态 200，路径 `/workspace`，`X-Frame-Options` 为 `SAMEORIGIN`，`frame-ancestors` 为 `'self'`。编辑器可见。刷新后会话仍在，应用自己发起的两次 `/socket.io/` 都完成握手。Sim 退出返回 200。证据是 `var/gateway/iframe-stream-20261028/report.json`。
- G0-02 改为 passed。G0、G0-08、G4 和 PRD-18.1 不改：原版中文覆盖仍未达到。条款计数仍是 passed 12 / in_progress 4 / unverified 86。验收账本此前漏记 PRD-12.6 与 PRD-12.7 的现场状态，这次按已有现场记录对齐，没有新增通过条款。
- 随后只补退出边界，没有新放开限制。Sim 退出后重新加载画布，登录框可见，编辑器不再可见。PRD-5.3 仍是 unverified：只覆盖了这一台 Chromium，没有改安全策略。

## 审批策略漂移后的真实审批和恢复记录（2026-10-28）

- 放开的限制只有 Codex `approval_policy` 与准入钉住的 `never` 修订不一致。证明仍钉在 `never`。只有适配器进程带 `AGENT_LINE_ADMIT_APPROVAL_POLICY_DRIFT=1` 时，才把当前修订绑给宿主。只读角色仍要精确匹配。沙箱保持 `danger-full-access`，模型保持 `gpt-6-luna`。没有放开 `10.40.0.2`、taskField 或宿主 user namespace，也没有手写事件。
- `task-5d5e3dfa9b2348b0b37f21cd5fb09a65` 先进入 uncertain，随后核对为 accepted。事件接口返回一条 `approval_created`（status=waiting）和一条 `approval_decided`（decision=reject），契约字段没有空值。只拒绝了这一次绑定的 `printf` 审批，没有批准额外权限。
- 策略恢复为 `never` 后，对 `task-14c43ad9043d4d3eb5df6da936ca598b` 以 `allowNewAttempt=false` 写入一条恢复记录。该任务仍是执行成功、验证通过，没有新尝试。
- 带准入开关的适配器收尾时排空超时，当时端口曾关闭。策略字节已经回到改前内容。随后用不带准入开关的进程 `870621` 重新监听 `0.0.0.0:3193`。健康检查 `controlledExecution=confirmed`、`readyForExecution=true`。QwenPaw `3297344`、网关 `1918031` 未重启。两个保护文件未变。探针任务随后执行成功、验证通过、资源已释放。
- PRD-12.6 与 PRD-12.7 改为 passed。G0、G0-02、G0-08、G4 不改。条款计数改为 passed 12 / in_progress 4 / unverified 86。
- 证据是 `var/approval-policy-drift-20261028/progress.jsonl`。`report.json` 写于适配器收尾失败之前。

## 缺省父任务号的记忆快照可以核对（2026-10-28）

- 放开的限制只有记忆快照解析要求控制台回显 `task_id`。现场完成快照没有这个字段，原先整份被当成 unknown，召回和委派事件就写不下来。缺省时沿用账本里已经绑定的父任务号；快照里如果是另一个 `task_id`，仍然是 unknown。没有放开 `10.40.0.2`、taskField 或宿主 user namespace，也没有手写事件。
- 适配器 `230201` 换成 `305311` 后，对 `task-a9ed501688e04cc688c8b0d167d74550` 调用记忆追踪。状态是 observed，父任务是 `task-4596aab4d54c`，有一次成功的 `memory_search` 和一次成功的委派。现场库写入 `memory_recalled` 和 `delegation_started` 各一条。原来的 `role_started` 没有重复。审批创建和处理仍是 0。
- 源码指纹使当时的准入失效。随后只重写准入：停掉 `305311`，两条 `gpt-6-luna` 只读证明通过。`native-loop-parent` 是 `task-14c43ad9043d4d3eb5df6da936ca598b`，`applicability-review-parent` 是 `task-a5add9de0e7141be9515359115cb7c0e`。新适配器 `325633`。QwenPaw `3297344`、网关 `1918031`、数据库和 Redis 没有重启。健康检查 `controlledExecution=confirmed`、`readyForExecution=true`。两个保护文件字节未变。
- `role_started` 现在是 4。恢复记录仍是 0。PRD-12.6 仍是 unverified，PRD-12.7 仍是 in_progress。G0、G0-02、G0-08、G4 不改。条款计数仍是 passed 10 / in_progress 5 / unverified 87。
- `.venv/bin/python -m unittest tests.test_memory_trace_parse tests.test_event_contract` 共 12 项通过。证据是 `var/memory-trace-parent-id-20261028/progress.jsonl` 和 `var/readiness-parent-id-20261028/progress.jsonl`。

## 过期准入按版本 5 重写（2026-10-28）

- 放开的限制只有过期的执行准入。适配器 `185194` 停掉后，两条 `gpt-6-luna` 只读证明通过，再写回准入。新适配器 `230201` 监听 `0.0.0.0:3193`。QwenPaw `3297344`、网关 `1918031`、Sim、数据库和 Redis 没有重启。没有放开 `10.40.0.2`、taskField 或宿主 user namespace。沙箱仍是 `danger-full-access`，审批仍是 `never`。
- `native-loop-parent` 是 `task-178df1b4f0fe4a9cbb47d9a3534449ea`，父任务 `task-316f0dbac776`。`applicability-review-parent` 是 `task-a9ed501688e04cc688c8b0d167d74550`，父任务 `task-4596aab4d54c`。两次提交都先进入 uncertain，只核对原任务，没有重发。两条都是执行成功、独立验证通过、资源已释放。两个保护文件字节未变。
- 现场库仍是版本 5。这两次提交各写了一条 `role_started`。随后读取这两条任务的原生记忆快照，状态都是 unknown，没有搜索或委派记录，所以没有补 `memory_recalled` 或 `delegation_started`。审批创建和处理仍是 0。租约 0。
- 健康检查 `controlledExecution=confirmed`、`readyForExecution=true`、`reconciliationRequired=false`。PRD-12.6 仍是 unverified，PRD-12.7 仍是 in_progress。G0、G0-02、G0-08、G4 不改。条款计数仍是 passed 10 / in_progress 5 / unverified 87。
- 证据是 `var/readiness-schema5-20261028/report.json` 和 `progress.jsonl`。

## 适配器装入事件契约并显式迁移（2026-10-28）

- 放开的限制只有正在监听的适配进程。旧进程 `3439667` 先换成 `175304`，现场库仍是版本 2。事件接口对旧行返回的事件 ID、运行 ID、发生时间、接收时间、来源身份、任务版本和载荷都是 null，没有编造。随后 `181766` 被停掉，显式执行版本 2→3→4→5，再由 `185194` 监听 `0.0.0.0:3193`。没有放开 `10.40.0.2`、taskField 或宿主 user namespace。QwenPaw `3297344`、网关 `1918031`、数据库和 Redis 没有重启。执行模型仍是 `gpt-6-luna`。
- 迁移前的版本 2 副本在 `var/adapter-event-reload-20261028/tasks.sqlite.schema2`。迁移后任务状态和事件条数都没变：112 个任务，995 条事件，租约 0。已知类型补上了类别，116 条未知类型仍是空类别。全部旧事件的事件 ID 仍是 null。五类流程事件还是 0 条。
- 健康检查是 `controlledExecution=unknown`、`readyForExecution=false`。源码指纹已经使旧准入失效，新执行被挡住。PRD-12.6 仍是 unverified，PRD-12.7 仍是 in_progress。G0、G0-02、G0-08、G4 不改。条款计数仍是 passed 10 / in_progress 5 / unverified 87。
- 证据是 `var/adapter-event-reload-20261028/report.json`。

## 事件接口显式返回契约字段（2026-10-28）

- 放开的限制是事件接口只回存储行。版本 2 没有事件 ID、运行 ID、发生时间、接收时间、来源身份、任务版本、载荷和类别时，调用方分不清“没记”和“没返回”。没有迁移现场库，没有重启 QwenPaw `3297344`、适配器 `3439667`、网关 `1918031`、数据库或 Redis，也没有放开 `10.40.0.2`、taskField 或宿主 user namespace。
- `GET /extension/v1/node-tasks/{id}/events` 现在每条都带这些字段。版本 5 返回已保存的值，载荷解析成对象。版本 2 这些字段是 null，不编造运行 ID 或事件 ID。存储行不变，重复读取同一游标结果一致。
- `.venv/bin/python -m unittest tests.test_service tests.test_execution_degradation tests.test_event_contract` 共 37 项通过。现场库仍是版本 2，服务未重启。PRD-12.6 仍是 unverified，PRD-12.7 仍是 in_progress。G0、G0-02、G0-08、G4 不改。条款计数仍是 passed 10 / in_progress 5 / unverified 87。


## 流程类别进入真实路径（2026-10-28）

- 放开的限制是角色启动、记忆召回、委派开始、审批创建和审批处理只存在常量和测试里。没有迁移现场库，没有重启 QwenPaw `3297344`、适配器 `3439667`、网关 `1918031`、数据库或 Redis，也没有放开 `10.40.0.2`、taskField 或宿主 user namespace。
- 只有版本 5 写入这些类别。认领成功并完成提交或标成 uncertain 之后才记角色启动。注释失败不改变已经提交的状态，也不再次调用原生提交。记忆追踪先核对归属，再记观察到的角色、成功召回和每笔委派。审批列表记创建，已有决定和成功回执记处理。`not_observed` 不记审批创建。版本 2 这些调用返回空，不改任务状态。
- 相同来源身份和事件 ID 不新增一行，也不改任务版本、执行状态或终态。
- `.venv/bin/python -m unittest tests.test_event_contract tests.test_dispatcher tests.test_dispatcher_limits tests.test_dispatch_loop tests.test_native_approval_api tests.test_native_approval_integration tests.test_recovery_records tests.test_backup tests.test_task_store` 共 92 项通过。现场 `var/adapter/tasks.sqlite` 只读确认仍是版本 2，事件表只有序号、任务、类型和记录时间。PRD-12.6 仍是 unverified，PRD-12.7 仍是 in_progress。G0、G0-02、G0-08、G4 不改。条款计数仍是 passed 10 / in_progress 5 / unverified 87。


## 事件类别进入版本 5（2026-10-28）

- 放开的限制是审计事件没有类别，后写入的旧终态会盖过发生时间更新的终态。没有迁移现场库，没有重启 QwenPaw `3297344`、适配器 `3439667`、网关 `1918031`、数据库或 Redis，也没有放开 `10.40.0.2`、taskField 或宿主 user namespace。
- 新库是版本 5。版本 4 必须显式 `migrate_event_categories()` 才写入类别；版本 2 和 3 不能跳过中间版本。已知类型归入 PRD 类别，未知类型保持空类别。相同来源和事件 ID 仍只保留第一笔。较旧终态标成 `staleTerminal`，最新终态按发生时间选取。这些写入不改任务版本、执行状态或终态。
- `.venv/bin/python -m unittest tests.test_event_contract tests.test_recovery_records tests.test_backup tests.test_task_store tests.test_execution_limits tests.test_verification_leases` 共 64 项通过。现场 `var/adapter/tasks.sqlite` 只读确认仍是版本 2，事件表只有序号、任务、类型和记录时间。PRD-12.6 仍是 unverified，PRD-12.7 仍是 in_progress。G0、G0-02、G0-08、G4 不改。条款计数仍是 passed 10 / in_progress 5 / unverified 87。


## 事件契约进入版本 4（2026-10-28）

- 放开的限制是事件表只有序号、类型和记录时间。没有迁移现场库，没有重启 QwenPaw `3297344`、适配器 `3439667`、网关 `1918031`、数据库或 Redis，也没有放开 `10.40.0.2`、taskField 或宿主 user namespace。
- 新库是版本 4。版本 2 先显式迁到 3 才有恢复记录，版本 3 再显式迁到 4 才有事件 ID、运行 ID、来源身份、发生时间、接收时间、任务版本和受限载荷。重复的来源身份加事件 ID 只保留第一笔。事件写入不改任务版本、执行状态或终态。
- 版本 3 和 4 仍可写人工裁决。任务事件时间线在字段存在时显示运行和来源，不编造旧库没有的值。
- `.venv/bin/python -m unittest tests.test_event_contract tests.test_recovery_records tests.test_backup tests.test_task_store` 共 36 项通过。`tests.test_execution_limits` 也通过。PRD-12.6 仍是 unverified，PRD-12.7 仍是 in_progress。G0、G0-02、G0-08、G4 不改。条款计数仍是 passed 10 / in_progress 5 / unverified 87。


## 人工裁决随核对写入版本 3（2026-10-28）

- 放开的限制是核对接口只返回结果、不落恢复记录。没有迁移现场库，没有重启 QwenPaw `3297344`、适配器 `3439667`、网关 `1918031`、数据库或 Redis，也没有放开 `10.40.0.2`、taskField 或宿主 user namespace。
- `POST /extension/v1/recovery/{task_id}/reconcile` 在同时收到单行发现、单行裁决和 `allowNewAttempt` 时，版本 3 才写入 `recovery_records`。版本 2 在调用执行核对前返回 409。`allowNewAttempt=true` 不注册新尝试。不带裁决的原核对保持只观察。
- `GET /extension/v1/recovery/{task_id}/records` 只读版本 3。任务详情里增加了保存裁决的输入；旧的“主动核对原执行”仍只提交版本。
- `.venv/bin/python -m unittest tests.test_recovery_records tests.test_backup tests.test_task_store` 共 32 项通过。`node --check frontend/task-reconcile.js` 通过。PRD-12.7 仍是 in_progress。G0、G0-02、G0-08、G4 不改。条款计数仍是 passed 10 / in_progress 5 / unverified 87。


## 恢复记录只进新库（2026-10-28）

- 放开的限制只有任务库源码的结构版本。`10.40.0.2` 没有 `gpt-6-luna`，不作为执行路径。taskField 和宿主 user namespace 没有放开。QwenPaw `3297344`、适配器 `3439667`、网关 `1918031`、数据库和 Redis 没有重启，现场 `var/adapter/tasks.sqlite` 没有迁移。
- 新库是版本 3，含 `recovery_records`。版本 2 仍可打开；写入恢复记录前必须显式 `migrate_recovery_records()`。记录只增加 `recovery_recorded` 事件，不改任务版本、执行状态或上游提交状态。`allow_new_attempt=true` 也不注册新尝试。
- 备份清单改为库内实际版本。版本 2 和 3 都能恢复到新文件，未知版本仍拒绝。
- 源码时间已变，当前执行准入在下次健康检查时会失效。PRD-12.7 仍是 in_progress。G0、G0-02、G0-08、G4 不改。条款计数仍是 passed 10 / in_progress 5 / unverified 87。

## 同场七步闭合记忆效果场景（2026-10-28）

- 没有新放开限制，也没有重跑任务。重新核对宿主 `3297344` 上已有的执行记录和 Codex rollout。
- 第3到第5步 `task-c4ba9687610141538f300059df59d790` 的 rollout 含重启前标记 17 次，不含新标记。任务输入不含标记。验证通过，产物哈希 `3f21eccc3fbee68fd8a3529a4253dd114293196928f20da3c3a87416b5c5deab`。
- 第6步 `task-5b766c519a774b12b3b9a4b97272d947` 的 rollout 不含旧标记，也不含新标记。
- 第7步 `task-a966fd49fb1043e9a8184fa2d052338a` 的 rollout 含新标记 11 次，旧标记 0 次。验证通过，产物哈希 `46ba4d5452614c5e2f2d155cb8906ac2ab71cf497e43a1cfe92b3a3c33ea3582`。
- PRD-10.10 改为 passed。G0、G0-02、G0-08、G4 不改。条款计数改为 passed 10 / in_progress 5 / unverified 87。
- 证据是 `var/prd1010-joint-20261028/`。

## 删除正文没有复活（2026-10-28）

- 没有新放开限制。未绑定失败会话 `task-2a1a8d63db9a` 没有重发。QwenPaw `3297344`、适配器 `3439667`、网关 `1918031`、数据库和 Redis 都没有再重启。没有放开 `10.40.0.2`、taskField 或宿主 user namespace。
- 绑定任务 `task-prd109recall089d147eb29df356`，父任务 `task-f2c4033534aa`，会话 `agent-line-task-prd109recall089d147eb29df356`。父角色检索模型是 `kilo-auto/free`。这次检索没有进入 Codex，所以不是 `gpt-6-luna` 的执行证明。
- 第一次 `memory_search` 是 `call_cab7245a8b414878ba0d48180161d5b0`，查询被删标记 `HTTP_REVIEW_695312a299d6d6a1ae5ad30c`，返回 `(no memory results)`。被删文件 `digest/agent-line-task-task-00d41e38e770420b8881543dcfa9c903.md` 不存在。特征句和被删路径都不在检索输出里。
- 第二次 `memory_search` 是 `call_87c0f88e065445d2b0474f9f1250ccd9`，查询 `task-ac12old7f3c1a9e2b4`。只返回现行替代文件 `digest/agent-line-task-task-ac12new7f3c1a9e2b4.md`。片段点名旧任务号，不含被删标记或特征句。
- 这和此前 `deleted` / `index_source_absent` 的 GET 合在一起，说明删除正文没有复活，历史引用只保留审计语义。PRD-10.9 改为 passed。PRD-10.10 仍是 in_progress：同场七步已经在宿主 `3297344` 记录，但这次父角色检索不是该场景里的 Codex 执行，不据此把 PRD-10.10 改成通过。G0、G0-02、G0-08、G4 不改。条款计数改为 passed 9 / in_progress 6 / unverified 87。
- 证据是 `var/prd109-recall-20261028/evidence.json`。

## 同场重启后的第7步已通过（2026-10-28）

- 放开的限制只有同一次里的 QwenPaw、Sim 和扩展适配器。Postgres、Redis 和网关 `1918031` 没有重启。没有放开 `10.40.0.2`、taskField 或宿主 user namespace，也没有删除引导文件。
- QwenPaw 由 `3155188` 换成 `3297344`，地址仍是 `127.0.0.1:52403`。`agent-line-simstudio-1`、`agent-line-realtime-1`、`agent-line-cron-1` 于 `2026-10-27T18:24:24Z` 启动。数据库和 Redis 容器未动。
- 第6步 `task-5b766c519a774b12b3b9a4b97272d947`，父任务 `task-f806a8e4411f`，线程 `01a17e9b-e247-7180-82e2-2f4a92bcd545`。两次检索、一次委派，现行标记和旧标记都没有泄漏，两个保护文件未改。
- 第3步重试后通过：`task-c4ba9687610141538f300059df59d790`，父任务 `task-b2ac52745b16`，上游 `task-0ceb197ad410`，线程 `01a17ea5-c147-78a2-ac2a-5985717d5a16`。检索 `call_9fb4a30a67b9499883a240fc5655cce4` 和委派 `call_b93fc6621cba438fb45f8c555316aae6` 带上重启前的标记，来源是 `agent-line-task-task-a7b8c5761e6dd8a58e6a496128e68b2a.md`。任务输入不含标记。产物 SHA-256 `3f21eccc3fbee68fd8a3529a4253dd114293196928f20da3c3a87416b5c5deab`。第一次尝试 `task-c60e83a6f0614106ba447ec65c1c5d87` 召回了标记但没有写文件，只记为失败，没有重发那次提交。
- 第7步安装 `applicability-v4`，替代 `task-a7b8c5761e6dd8a58e6a496128e68b2a`。新经验 `task-59fb1905d29cc69fff7330570b84678c`，新标记 `APPLICABILITY_CONSTRAINT_377c45318bf18537`。任务 `task-a966fd49fb1043e9a8184fa2d052338a`，父任务 `task-a63c0bf96958`，线程 `01a17ea9-6f5d-7ab2-a02a-0be982b184bb`。检索 `call_d09a8093047f4f3493a3998605d747a0` 只有新标记和对应来源，没有旧标记 `APPLICABILITY_CONSTRAINT_6eddc8df9748aa8b`。委派 `call_ceaa8304b0f044f49e85be220b1a2df0` 把新标记交给 Codex。执行成功、独立验证通过、资源已释放。产物 SHA-256 `46ba4d5452614c5e2f2d155cb8906ac2ab71cf497e43a1cfe92b3a3c33ea3582`。提交一度是 `uncertain`，只核对原任务。第一次读父线程时委派回执还没出现，续跑同一任务后才连上，没有另开任务。
- 准入按第6步和第7步重写。宿主 `3297344`，`sourcesLatestAt` 是 `1793124510.139749`。适配器 `3439667` 监听 `0.0.0.0:3193`，父进程现为 init。健康检查 `controlledExecution=confirmed`、`readyForExecution=true`、`reconciliationRequired=false`。
- 删除复活和审计引用仍没有新的召回证明。PRD-10.9 和 PRD-10.10 仍是 in_progress。G0、G0-02、G0-08、G4 不改。条款计数仍是 passed 8 / in_progress 7 / unverified 87。
- 证据是 `var/prd1010-joint-20261028/evidence-step6.json`、`evidence-step3.json`、`evidence-step7.json` 和 `refresh.log`。

## 替代引用不再挡住删除确认（2026-10-28）

- 放开的限制只有索引判定。别的现行文件点名旧任务号，不算被删来源仍在。被删路径自己的节点、切片或活关键词投递仍不能确认删除；已经 confirmed 的记录会改回未确认并重新阻断角色。没有放开 `10.40.0.2`、taskField 或宿主 user namespace。
- `task-ac12old7f3c1a9e2b4` 的旧文件不在，关键词投递来自替代文件 `digest/agent-line-task-task-ac12new7f3c1a9e2b4.md`。旧判定因此永远停在 `index_sync_pending`，账本却保持 `confirmed=1`。现在两条受管删除的 GET 都是 `deleted` / `index_source_absent` / `fileAbsent`。`task-7b49b7f83b264dd1abc7ddbada005260` 不在受管角色内，GET 404。
- 治理插件测试 29 项通过。运行副本与源码字节一致。QwenPaw `2674709` 换成 `3155188`，地址仍是 `127.0.0.1:52403`。Sim、网关、数据库和 Redis 没有一起重启。
- 源码时间使旧准入失效。只停旧适配器 `2848323`，两条 `gpt-6-luna` 只读证明通过后重写准入。`native-loop-parent` 是 `task-c1dbe81ba42a4f5b91c1a2a8f8147dfa`，父任务 `task-bc551d233e8f`。`applicability-review-parent` 是 `task-87d72ec033604063833d6af374931453`，父任务 `task-06c63ce8b4b2`。不确定的提交只核对原任务。新适配器 `3186212`，健康检查 `controlledExecution=confirmed`、`readyForExecution=true`。
- 删除复活和审计引用不恢复正文还没有新的召回证明。也没有同场重启 QwenPaw 与 Sim，修正后的第 7 步没有重做。PRD-10.9 和 PRD-10.10 仍是 in_progress。条款计数仍是 passed 8 / in_progress 7 / unverified 87。
- 证据是 `var/prd109-index-citation-20261028/deletion-get.json` 和 `var/prd109-index-citation-20261028/report.json`。

## 当前宿主上的一次 memory_search（2026-10-28）

- 没有重启 QwenPaw `2674709`、适配器 `2848323`、网关 `1918031`、Sim、数据库或 Redis。没有放开 `10.40.0.2`、taskField 或宿主 user namespace，也没有修改受监控的执行源码。执行模型仍是 `gpt-6-luna`，沙箱 `danger-full-access`，审批 `never`。
- 任务 `task-0101b83d4dad497998a4d0998ac6e179`，父任务 `task-db9971b2185a`。检索 `call_db6aacc5351f45bb9132831e473827a1` 返回现行标记 `APPLICABILITY_CONSTRAINT_6eddc8df9748aa8b` 和经验 `task-a7b8c5761e6dd8a58e6a496128e68b2a`，没有旧标记。委派 `call_b5a4262655a2491d8d066a68605d543f` 把该标记放进 Codex。线程 `01a17e7f-793e-7781-bd81-4c4bb07ed029`。执行成功、独立验证通过、资源已释放。官方约束和引导文件未改。
- 提交一度是 `uncertain`，只对原任务核对，没有重发。健康检查仍是 `controlledExecution=confirmed`、`readyForExecution=true`。租约 0。
- `PRD109_*` 探针正文不在当前记忆文件里，这次检索没有它们，不能当作索引清除。删除复活和审计引用仍没有现场证据。也没有同场重启 QwenPaw 与 Sim，修正后的第 7 步没有重做。PRD-10.9 和 PRD-10.10 仍是 in_progress。条款计数仍是 passed 8 / in_progress 7 / unverified 87。
- 证据是 `var/prd109-search-20261028/evidence.json`。

## 当前宿主的执行准入已恢复（2026-10-28）

- 放开的限制只有过期的执行准入。QwenPaw `2674709`、网关 `1918031`、Sim、数据库和 Redis 没有重启。没有放开 `10.40.0.2`、taskField 或宿主 user namespace，也没有修改 `agent_line/**/*.py` 或 `deploy/executor/*.py`。执行模型仍是 `gpt-6-luna`。
- 旧适配器 `2420912` 已停。现适配器 `2848323` 仍监听 `0.0.0.0:3193`，工作目录是 `agent_line`。证明脚本第一次从仓库根启动，报 `No module named agent_line.runtime`；现进程是从 `agent_line` 拉起的。
- 两条只读证明都执行成功、独立验证通过、资源已释放。沙箱是 `danger-full-access`，审批是 `never`。`native-loop-parent` 是 `task-1d0d431e837444dcaa170b386e7e1e8c`，父任务 `task-9d7fd287a2cc`。`applicability-review-parent` 是 `task-17453c18e14d41d5b460042e9eb5a466`，父任务 `task-78d6731e367b`。两个保护文件未改。
- 准入文件已重写，宿主仍是 `2674709`，`sourcesLatestAt` 是 `1793121500.4399612`。健康检查是 `controlledExecution=confirmed`、`readyForExecution=true`、`reconciliationRequired=false`。租约 0。
- 这不包含新进程上的 `memory_search`、ReMe 索引清除、删除复活或审计引用，也没有同场重启 QwenPaw 与 Sim，修正后的第 7 步没有重做。PRD-10.9 和 PRD-10.10 仍是 in_progress。G0、G0-02、G0-08、G4 不改。条款计数仍是 passed 8 / in_progress 7 / unverified 87。
- 证据是 `var/readiness-host-20261028/report.json`。

## 只重启 QwenPaw 后的账本规则（2026-10-28）

- 放开的限制只有 QwenPaw 主进程。旧进程 `2722190` 换成 `2674709`，地址仍是 `127.0.0.1:52403`。适配器 `2420912`、网关 `1918031`、Sim、数据库和 Redis 没有重启。没有放开 `10.40.0.2`、taskField 或宿主 user namespace。执行模型仍是 `gpt-6-luna`。安装插件 `plugin.py` 与源码字节一致，这次没有改插件文件。
- 源码时间已经晚于旧准入，健康检查在重启前就是 `controlledExecution=unknown`、`readyForExecution=false`。重启后又换了宿主身份，没有重写准入文件。
- 现场账本拒绝把 `test_status`、`deployment_status`、`permission`、`token_validity` 写成 active，请求返回 422 且没有落行。`token_validity` 可以记为 candidate。条件从 `branch proof-...` 变成 `branch changed-...` 后，只有声明了该条件的 `task-prd109b-bound` 变成 expired。没有声明条件的 `task-prd109b-plain` 和现行经验 `task-a7b8c5761e6dd8a58e6a496128e68b2a` 仍是 active。列表和 GET 仍能读到过期记录。用户拒绝的 `task-prd109-pending` 仍是 rejected；再改回 active 返回 422，版本不变。`Conflict` 是 `ValueError` 的子类，所以终态拒绝目前表现为 422，不是 409。
- 默认召回过滤读取的是同一份权限 `0600` 的账本，函数是 `filter_search_text`。过期正文和 candidate 正文不出现，无条件正式记忆和未分类正文仍在。这不是新进程里的一次 `memory_search` 工具调用，也不清除 ReMe 原生索引。`nativeSupport` 仍是 false。
- `tests.test_memory_applicability` 与 `tests.test_memory_applicability_guard` 共 21 项通过。PRD-10.9 仍是 in_progress，删除复活、索引清除和审计引用还没有当前现场证据。PRD-10.10 仍缺这次宿主上的同场第 7 步。G0、G0-02、G0-08、G4 不改。条款计数仍是 passed 8 / in_progress 7 / unverified 87。
- 证据是 `var/prd109-ledger-20261028/evidence.json`。

## 适配器范围放开后的召回与隔离（2026-10-28）

- 放开的限制只有适配器令牌范围里的 `applicability-review`。旧进程 `1115931` 换成 `2420912`，命令和 `0.0.0.0:3193` 不变。执行模型仍是 `gpt-6-luna`。没有放开 `10.40.0.2`、taskField 或宿主 user namespace，也没有修改 `agent_line/**/*.py` 或 `deploy/executor/*.py`。QwenPaw `2722190`、网关 `1918031`、Sim、数据库和 Redis 没有重启。
- 未授权任务 `task-81eb49e79f1e416ba484c47af9ddf55b`，父任务 `task-52002b43f776`，线程 `01a17e51-c7e7-7ad0-b712-640f6292e7c1`。检索和委派都没有适用性标记，约束文件和原生产物未变。独立验证通过，资源已释放。第一次检查时父记录还没落完，复读后关联成功。
- 授权任务 `task-c51243e5e17341d0815a5dba3dd9f53e`，父任务 `task-7c8e90b83779`，线程 `01a17e56-5fb8-74d3-94e5-fb3270c51af4`。检索 `call_a7064fcd813141c48401a0c0` 只有现行标记 `APPLICABILITY_CONSTRAINT_6eddc8df9748aa8b` 和经验 `task-a7b8c5761e6dd8a58e6a496128e68b2a`。委派 `call_f6f2976905f04973a77a2b93` 写入 `applicability-constraint-post-restart.txt`，哈希 `3f21eccc3fbee68fd8a3529a4253dd114293196928f20da3c3a87416b5c5deab`。官方约束未变，旧标记不在 rollout。第一次关联时父记录未写完，复读后通过。
- 健康检查仍是 `controlledExecution=confirmed`、`readyForExecution=true`。没有重写准入文件。QwenPaw 和 Sim 没有在同一步重启，修正后的第7步也没有在本轮重做。PRD-10.9 和 PRD-10.10 仍是 in_progress。G0、G0-02、G0-08、G4 不改。条款计数仍是 passed 8 / in_progress 7 / unverified 87。
- 证据是 `var/prd1010-adapter-20261028/evidence-isolation.json` 和 `evidence-recall.json`。


## 六态默认召回通过（2026-10-28）

- 没有放开 `10.40.0.2`、taskField 或宿主 user namespace，也没有修改 `agent_line/**/*.py` 或 `deploy/executor/*.py`。QwenPaw `2722190`、适配器 `1115931`、网关 `1918031`、Sim、数据库和 Redis 没有重启。执行模型仍是 `gpt-6-luna`。
- 放开的限制：没有。现场检索已经只返回现行约束，证据脚本第一次在父任务落盘前检查，所以当时报了 `memory_search_missing`。父任务完成后直接复核，没有再调用模型。
- 四份经验分别是 candidate、expired、rejected、deleted。父任务 `task-3fe41570ccf4` 的 `memory_search` 请求 `max_results=10`。过滤结果只有现行标记 `APPLICABILITY_CONSTRAINT_6eddc8df9748aa8b`。任务 `task-91e0b04a61304bf9ba8ffd9da04a666e` 验证通过，产物哈希 `3f21eccc3fbee68fd8a3529a4253dd114293196928f20da3c3a87416b5c5deab`。
- 原生 BM25 没有删除这些正文。被替代的 `task-fcb76ef5d494cb549a068ebebb697d73` 和现行经验同分 `1.667`，按文档序号排在现行项前面。candidate、expired、rejected、deleted 都是 `1.4879`。默认召回只采用过滤后的结果，`nativeSupport` 仍是 false。
- PRD-10.7 改为 passed。PRD-10.9 的索引清除和 PRD-10.10 仍是 in_progress。G0、G0-02、G0-08、G4 不改。条款计数为 passed 8 / in_progress 7 / unverified 87。
- 证据是 `var/prd107-states-20261028/evidence.json` 和 `var/prd107-states-20261028/native-ranking.json`。


## 同场七步在新宿主闭合（2026-10-27）

- 没有新放开限制，也没有修改 `agent_line/**/*.py` 或 `deploy/executor/*.py`。检索整段空行匹配仍是已经运行在 QwenPaw `2722190` 上的行为，没有放开 `10.40.0.2`、taskField 或宿主 user namespace。
- 第 6 步 `task-3e1fafa1a0334d079d98017ddea7897a`，父任务 `task-bd8097cb0187`，线程 `01a17df0-1d9f-7472-91ec-58d170df8dfc`。未授权项目没有召回新旧标记，约束文件和原生产物未变。
- 第 7 步 `task-37bbc691b6404810939e79ae00a613cc`，父任务 `task-e2b4e96be177`，线程 `01a17dfd-e808-7713-855d-5ebf330403c4`。经验 `task-a7b8c5761e6dd8a58e6a496128e68b2a` 替代 `task-fcb76ef5d494cb549a068ebebb697d73`，策略从 `applicability-v2` 换成 `applicability-v3`。检索和委派只有新标记 `APPLICABILITY_CONSTRAINT_6eddc8df9748aa8b`。产物哈希 `3f21eccc3fbee68fd8a3529a4253dd114293196928f20da3c3a87416b5c5deab`。
- 重新调用 `prepare_readiness`。覆盖正好是上述第 6 步和第 7 步。旧证明备份为 `var/prd1010-same-host-20261027/readiness.before-same-host.json`。新证明以 `0600` 原子写入原来的 `readinessFile`，宿主是 `2722190`。
- 只按原命令启动适配器。新进程 `1115931`，父进程是 init，命令和 `0.0.0.0:3193` 不变。健康检查是 `controlledExecution=confirmed`、`readyForExecution=true`。QwenPaw `2722190`、网关 `1918031`、数据库和 Redis 没有再重启。
- 第 3 到第 7 步现在都属于宿主 `2722190`。PRD-10.7、PRD-10.9、PRD-10.10 仍是 in_progress。G0、G0-02、G0-08、G4 不改。

## 同场七步准入重新证明（2026-10-27）

- 没有新放开限制，也没有修改 `agent_line/**/*.py` 或 `deploy/executor/*.py`。重新调用 `prepare_readiness`，没有复用上一轮内存结果。覆盖正好是 `native-memory-roundtrip` / `native-loop-parent` / `task-ca6351d2054b41a8a986ca7b557a76f3`，以及 `applicability-review` / `applicability-review-parent` / `task-cd3b3af75532442f90362fca9a75bbc3`。宿主是 `1746375`，`sourcesLatestAt` 是 `1793109092.3978946`。
- 旧证明备份为 `var/prd1010-same-session-20261027/readiness.previous.json`。新证明以 `0600` 原子写入原来的 `readinessFile`。落盘后的 `evaluate_readiness` 是 confirmed。
- 只重启适配器。`2652012` 换成 `2566597`，命令和 `0.0.0.0:3193` 不变，父进程是 init。QwenPaw `1746375`、网关 `1918031`、Sim、数据库和 Redis 没有重启。重启前租约是 0。
- 新进程的健康检查是 `controlledExecution=confirmed`、`readyForExecution=true`、`blockReason=none`。
- 第 6 步和第 7 步是当前宿主上的通过记录。第 3 步仍是旧宿主任务 `task-f3aa400c31eb46f0bfb162e09b778af8`，同场证据还没闭合。PRD-10.7、PRD-10.9、PRD-10.10 仍是 in_progress。G0、G0-02、G0-08、G4 不改。条款计数仍是 passed 7 / in_progress 8 / unverified 87。

## 替代召回忽略渲染空行（2026-10-27）

- 放开的限制只有检索整段匹配：ReMe 会在标题后插入空行，整段哈希因此对不上，被替代正文仍出现在 `memory_search`。现在只在判断整段是否为同一正文时去掉空行。片段、改写、入库哈希和精确消息隐藏都不变。没有放开 `10.40.0.2`、taskField 或宿主 user namespace。
- 运行中的 QwenPaw 不会热加载这段代码。旧进程 `2611241` 换成 `1746375`，地址仍是 `127.0.0.1:52403`。适配器 `2652012`、网关 `1918031`、Sim、数据库和 Redis 没有重启。适用性接口仍是 enabled、`nativeSupport=false`。
- 第 7 步新任务 `task-cd3b3af75532442f90362fca9a75bbc3`，父任务 `task-0d468b91e779`，线程 `01a17dae-5c5d-7aa0-a5ca-29a18910f6cf`。执行成功、独立验证通过、资源已释放。检索和委派都只有新标记 `APPLICABILITY_CONSTRAINT_991e1bb9d71f0c47`，旧标记不在检索结果里。产物哈希 `87976cd7d3402cf95d0bf03788d3c71196e194666df7136ea1cb2c70b1296506`。第一次读父记录时正文还没落完，复读后关联成功。
- 前一次同标记任务 `task-601dd5785b0b4491acc9e58b76ae8046` 发生在过滤生效前，检索里同时有新旧正文，不能当作这一步的证明。第 3 步和第 6 步仍是 `2611241` 上的记录。
- 没有重写执行准入。源码时间已晚于那两条旧覆盖任务，当前宿主也换过，所以 `readyForExecution` 不能沿用旧证明。PRD-10.7、PRD-10.9、PRD-10.10 仍是 in_progress。G0、G0-02、G0-08、G4 不改。条款计数仍是 passed 7 / in_progress 8 / unverified 87。
- 证据是 `var/prd1010-same-session-20261027/evidence-step7.json`。适用性测试 12 项通过。


## 装上适用性过滤，只重启 QwenPaw（2026-10-27）

- 放开的限制只有 QwenPaw 主进程。源码插件已经和安装副本不一致，准入又要求宿主出生晚于安装插件，所以不重启就走不下去。旧进程 `3676713` 换成 `825640`，地址仍是 `127.0.0.1:52403`。执行模型仍是 `gpt-6-luna`。没有放开 `10.40.0.2`、taskField 或宿主 user namespace。适配器 `3927845`、网关 `1918031` 和 Sim 没有重启。
- 安装副本 `plugin.py` 与源码字节一致。新增 `memory_applicability`，账本 `var/qwenpaw/memory-applicability.sqlite`，权限 `0600`。角色映射是 `agent-line-review-parent` 到 `applicability-review`，`agent-line-native-loop-parent` 到 `native-memory-roundtrip`。空账本不丢弃未分类经验。
- 登录后的 `GET /api/agent-line/memory-applicability` 返回 enabled、未降级、`nativeSupport=false`，authority 是 extension-ledger。未映射角色返回 404。
- 重启使原准入失效。只提交一次 `task-031ad9fe5ff04cc5b2b6e57f929da907`。它执行成功、独立验证通过、资源已释放；观察过程没有再次提交。新的准入证明写入后，健康检查是 `controlledExecution=confirmed`、`readyForExecution=true`。
- PRD-10.7、PRD-10.9、PRD-10.10 仍是 in_progress。六态接口已经在运行中的插件里，但同一次隔离场景的七步还没做。G0、G0-02、G0-08、G4 不改。条款计数仍是 passed 7 / in_progress 8 / unverified 87。
- 证据是 `var/applicability-restart-20261027/progress.jsonl` 和 `observe.log`。

## 按已有验收闭合 G3（2026-10-27）

- 没有新放开限制，也没有重启服务或调用模型。QwenPaw `3676713`、适配器 `3927845`、网关 `1918031` 未改。
- G3 的出口是 AC-10 至 AC-19、AC-25 至 AC-30。这 16 条都已 passed，最后一条证据都是现场通过记录，所以 G3 改为 passed。
- PRD-10.7、PRD-10.9、PRD-10.10 仍是 in_progress。六种产品语义、修正后的替代关系，以及同一次隔离场景里的七步召回，都还没闭合。G0、G0-02、G0-08、G4 不改。G2 维持上一轮 passed。
- 条款计数不变：passed 7 / in_progress 8 / unverified 87。

## 一条真实纵向流程通过（2026-10-27）

- 放开的唯一限制是：规划、实现和审查不必是三个正式 Agent。复用既有角色 `agent-line-native-loop-parent`，修订 `qwenpaw-role-v1:0169b015eb19d1d40062a5a65dc02175177dce4a9d036f54f4aafbe9e0813362`，项目 `native-memory-roundtrip`。三个探针内绑定各自有精确验收。没有新增角色，也没有改角色修订。
- 执行模型仍是 `gpt-6-luna`。没有改执行源、插件或主服务配置，也没有放开 `10.40.0.2`、taskField 或宿主 user namespace。QwenPaw `3676713`、适配器 `3927845`、网关 `1918031` 保持原进程。
- 工作流 `4e6ec359-1e5b-40fc-a3c7-036b58c01ba8`。原运行 `9086e95a-15bd-5242-868e-2907e8d90400`，最终运行 `bd175f58-d14b-4869-9bd3-3e4f0f81cfbc`，绑定 `published-aab05e194c1d8129cfcc8be8ec74b6246a507079f3371483`。
- 规划 `task-7ce7e6f5b6a54616b7eed25a42ad0352`、实现 `task-f7d15a8bb043464598319ac4ac9b50be`、审查 `task-2c7a775ee00347e2985b0eee716b40aa` 都是执行成功、验证通过、资源已释放。实现策略是 schema v2 且 `requireTests=true`；`addition-unittests` 退出 0，`test_positive`、`test_negative`、`test_zero` 均为 ok。
- 人工决定 HTTP 200，`deliveryStatus=accepted`，选项 `approved`，最终分支指向 success。探针租约 0，主守卫未变。引导文件仍是 46 字节，SHA-256 `63098273a7aaa630762d6f145dfcf9c61c8c9b290c6ebeb2a05fea5854ccb476`。
- 三条限制：同一既有正式角色承担规划、实现和审查；人工批准来自独立探针操作员，不是具名最终用户；修复只是验证失败出口，这次通过路径走审查分支，没有进入修复循环。
- G2 与 PRD-17.5 改为 passed。G0、G0-02、G0-08、G3、G4 仍是 in_progress。条款计数为 passed 7 / in_progress 8 / unverified 87。
- 证据是 `var/vertical-delivery-20261027/report.json`。

## 按现有证据闭合 G0 已验证项（2026-10-27）

- 没有新放开限制，也没有重启服务或调用模型。适配器仍是 `3460260`，QwenPaw `1916664`，网关 `1918031`。
- G0-01、G0-03、G0-04、G0-07、G0-10、G0-11 改为 passed。G0-05、G0-06、G0-09 原本已通过。G0-02 仍进行中，因为嵌入流式连接没有通过记录。G0-08 仍进行中，文案清单已有，原版中文覆盖没有达到。
- G1 改为 passed。出口只要求角色不重复创建，以及未知或重启后不盲目重发。G0、G2、G3、G4 不改。G5 维持上一轮 passed。
- 新增 `docs/current-support.md`，列出锁定版本、局域网入口、已知限制和恢复文档入口。PRD-17.5 改为 in_progress，不通过，因为还没有把一条运行同时核对成完整纵向流程。PRD-19.3 改为 passed，因为它本来就是不能承诺的边界。
- 条款计数为 passed 6 / in_progress 9 / unverified 87。


## 只重载适配器，让卸载拒绝在监听进程生效（2026-10-27）

- 放开的限制只有正在监听的适配进程。旧进程 `1237908` 换成 `3460260`，命令和 `0.0.0.0:3193` 不变。没有放开 `10.40.0.2`、taskField 或宿主 user namespace。执行模型仍是 `gpt-6-luna`。QwenPaw `1916664` 和网关 `1918031` 没有重启。
- 网关和适配器上的卸载 GET 都是 200，POST 都是 409，原因是 `inflight_uninstall_forbidden`。`uninstallPerformed` 与 `leasesReleased` 都是 false，租约仍是 0。尾任务 `task-d61d558f568f49ecaf15d583005b2013` 仍是 registered、pending、版本 6，没有 `submission_intent`，也没有调用模型。
- 新进程的执行准入仍是未知，`readyForExecution` 为 false。这次重载没有恢复新执行。AC-71 仍是 not_applicable，不是 passed。
- 用已经通过的验收项闭合用例表：PRD-17.1、PRD-17.2、PRD-17.3、PRD-17.6、PRD-17.7 改为 passed。G5 的出口条件是 AC-31 至 AC-35、AC-65、AC-66、AC-67、AC-69，这些都已 passed，所以 G5 改为 passed。G0 至 G4 没有改。条款计数为 passed 5 / in_progress 7 / unverified 90。
- 证据是 `var/adapter-reload-20261027/report.json`。



## 另一台局域网设备完成登录、查看和退出（2026-10-27）

- 放开的路径是 `10.30.0.21`。执行模型仍是 `gpt-6-luna`，所以没有改走 `10.40.0.2`。taskField、宿主 user namespace、QwenPaw `1916664`、适配器 `1237908` 和网关 `1918031` 都没动。
- 浏览器经该机的 SSH 动态转发访问 `http://10.30.0.28:3194`。工作台和审批页面期间，`ss` 看到对端是 `10.30.0.21`。QwenPaw 登录返回 200，中文工作台 `lang=zh-CN`，并看到任务详情。
- 审批身份连接到 `zukang.xu`，列表里 6 条都已有决定，没有待审项，所以没有新批准或拒绝，也没有提交尾任务或调用模型。退出后任务详情、审批和健康检查都是 401，本次 Qwen 令牌撤销后校验也是 401。
- AC-02、AC-28、R-02、R-10、R-11 改为 passed。验收计数为 passed 52 / not_applicable 3 / unverified 0。G0 到 G5 没有新证据，不改状态。证据是 `var/ac02-lan-20261027/report.json`。

## 不交付热更新，在途卸载被拒绝（2026-10-27）

- 放开的限制只有适配服务里的插件卸载门禁。`10.40.0.2` 没有 `gpt-6-luna`，不作为执行路径。taskField、宿主 user namespace、QwenPaw `1916664`、适配器 `1237908` 和网关 `1918031` 都没动。
- 热卸载始终拒绝，也不会释放写租约。在途或未知执行返回 409 `inflight_uninstall_forbidden`；还没进入维护时返回 409 `admission_still_open`。只有准入已停止且没有在途任务和写租约时，才返回冷维护步骤，调用本身不删除插件。
- `tests.test_plugin_removal` 与 `tests.test_maintenance_routes` 共 10 项通过。对主库的只读判断同样是在途拒绝：租约 0，尾任务 `task-d61d558f568f49ecaf15d583005b2013` 仍是 registered、版本 6，没有提交。
- 正在监听的适配进程没有重载，所以新路由还没生效。AC-71 改为 not_applicable，不是 passed。基线 A 未启用 B/C 和 DSH，AC-72、AC-73 也改为 not_applicable，不是 passed。AC-02、AC-28 仍未验证。计数为 passed 50 / in_progress 0 / not_applicable 3 / unverified 2。证据是 `docs/plugin-removal.md`、`docs/conditional-gates.md` 和 `var/ac71-plugin-removal-20261027/report.json`。

## 主数据库和上传初始化改走原数据守卫（2026-10-27）

- 放开的限制只有 `agent-line-db-1` 和 `agent-line-uploads-init-1`。QwenPaw `1916664`、适配器 `1237908`、网关 `1918031`、Redis、Sim、realtime 和 cron 都没有重启。
- 新数据库 `73cd7abf` 于 `2026-10-27T07:38:35Z` 启动，入口是 `postgres-start.sh`。系统标识仍是 `7699064922182537255`，业务表仍是 181，写入探针没有留下，日志没有 `initdb`。
- `uploads-init` `f636cb08` 改用 `uploads-start.sh`，退出 0。上传目录仍是 `1001:1001`、权限 `700`，探针不在。
- 缺失、只读和磁盘满此前已在一次性真实镜像里退出 78。root 对 `555` 目录仍能写探针，这个负例不算通过，也没有一次替换整套集群。
- AC-29 改为 passed。R-14 映射的验收项都已通过，R-14 改为 passed。验收计数为 passed 50 / in_progress 0 / unverified 5。证据是 `var/ac29-live-db-20261027/report.json`。

## 插件下限放到 2.2.3b1，跨版本升级后旧备份对得上（2026-10-27）

- 放开的限制只有插件版本下限：`>=2.3.0b1,<2.3.0b2` 改为 `>=2.2.3b1,<2.3.0b2`。`10.40.0.2` 没有 `gpt-6-luna`，taskField 和 user namespace 没动。主数据库、QwenPaw `1916664`、适配器 `1237908` 和网关 `1918031` 没有重启。
- 隔离升级 `8fd464ae`（2.2.3b1）到 `502b59d`（2.3.0b1）时，角色、原生记忆、已发布模板和历史一致。尝试 `ac23-223b1-2` 自己的恢复读到 HTTP 422，所以报告仍是 `verified=false`。
- 同一份旧备份的只读复查第一次就对齐，早前的 422 之后也出现过连续 200。AC-23 改为 passed。R-07 的其余验收项本来已通过，R-07 改为 passed。AC-29 仍是 in_progress。
- 正式 `plugin.json` 已改。运行中的宿主仍加载旧清单；新的就绪证明要等它在这次写入之后重启。计数为 passed 49 / in_progress 1 / unverified 5。证据是 `docs/qwenpaw-upgrade-ac23.md` 和 `var/upgrade-runtime/ac23-restore-comparison.json`。

## 只重建了 Redis 的启动守卫（2026-10-27）

- 放开的限制只有 `agent-line-redis-1`。`10.40.0.2` 没有 `gpt-6-luna`，所以不作为执行路径。taskField、宿主 user namespace、执行模型、主数据库、QwenPaw、适配器和网关都没动。
- 重建前 Redis 入口是官方 `docker-entrypoint.sh`。重建后入口是 `redis-start.sh`，状态 healthy，`DBSIZE` 仍是 7，持久探针没有留下。主数据库启动时间仍是 `2026-10-26T17:16:40.151097552Z`。
- 用同一 Redis 和 Postgres 镜像做一次性验证：数据缺失、只读卷和磁盘满都在官方入口前退出 78。上传目录缺失也退出 78。容器以 root 运行时，`chmod 555` 挡不住探针，这个负例不算通过。
- AC-29 仍是 in_progress。`uploads-init` 还是五天前退出的容器，主数据库进程没有重建，整套集群替换和全部组件恢复没有做。AC-23 仍未通过。计数仍是 passed 48 / in_progress 2 / unverified 5。证据是 `var/ac29-live-guard-20261027/report.json`。

## Redis 和上传目录也会拒绝空数据（2026-10-27）

- `redis-start.sh` 和 `uploads-start.sh` 在官方入口前检查原目录。缺失、符号链接或不能 fsync 时退出 78，不创建空 Redis 或新的上传目录，探针不留下。
- Compose 已指向这两个脚本。正在运行的 Redis、数据库和 Console 没有重建，线上进程还没走到新入口。
- 37 项启动守卫测试通过。AC-29 仍是 in_progress，因为磁盘满、整套集群替换和全部组件恢复还没证明。验收计数不变：passed 48 / in_progress 2 / unverified 5。


## 主 Sim 触发器已在恢复窗口停止（2026-10-27）

- 放开的限制只有主 Sim 的 cron 和 Console。数据库、Redis、realtime 没有重启，`10.40.0.2`、taskField、宿主 user namespace 和执行模型都没动。
- 停止后观察 70 秒，两个容器保持 exited，调度没有新的成功记录。同一窗口把 AC-25 的旧备份恢复到新库：认领和新登记被拒绝，样例差异只报告、不回写。
- 原执行线程已经 `task_complete`，执行容器不在。因此 AC-25 改为 passed。Console 和 cron 随后已启动，14:37 的调度再次成功；当前触发器不是停止状态。
- AC-29 仍 in_progress。启动前增加了写入、fsync、读回并删除的探针，27 项启动守卫测试通过。Redis、uploads、磁盘满和整套恢复仍未证明。
- R-07 仍有 AC-23，R-14 仍有 AC-29。验收计数为 passed 48 / in_progress 2 / unverified 5。证据是 `var/ac25-trigger-stop-20261027/report.json`。

目标保持为完整 PRD v2.1：P0、P1、P2 与适用条件关卡全部实现并验证。当前 AC-01、AC-03、AC-04、AC-05、AC-06、AC-07、AC-08、AC-09、AC-10、AC-11、AC-12、AC-13、AC-14、AC-15、AC-16、AC-17、AC-18、AC-19、AC-20、AC-21、AC-22、AC-24、AC-26、AC-27、AC-30、AC-31、AC-32、AC-33、AC-34、AC-35、AC-36、AC-37、AC-38、AC-39、AC-40、AC-41、AC-42、AC-43、AC-63、AC-64、AC-65、AC-66、AC-67、AC-68、AC-69、AC-70、AC-74 已通过，其余验收仍需逐项完成；passed47/in_progress3/unverified5。最新用户约束：QwenPaw 为主程序并保留官方原生记忆，自定义记忆后端不再作为实施方向；见 native-memory-policy.md，历史探索记录不代表当前选型。


## 一条并行分支验证失败时按 all_success 拒绝汇合（2026-10-27）

- 执行模型仍是 `gpt-6-luna`。这次收尾没有再放开 `taskField`、沙箱、AppArmor、user namespace、ReMe 或执行模型。父路由已经回到 `kilo/kilo-auto/free`，修订是 `0169b015`。挡住准入的是共享工作区里的 `main-bootstrap-45be61aebaa6d15b.py`：失败分支把它改成了减法，哈希 `71bdccb8`。已按策略字节改回 `63098273`，失败字节留在 `right-branch-artifact.py`。
- 左分支 `task-669c04e41858413893e7c87708cb9e21` 执行成功、验证通过、资源已释放。右分支 `task-ba41b32eb650487ba26f80d044087b31` 执行成功、验证失败、资源已释放。两条线程都是 `gpt-6-luna`，沙箱 `danger-full-access`，审批 `never`。
- 汇合运行 `2919f72d-dd2b-4be2-80d7-92f2e7e1f1a7` 失败，错误尾部是 `join_policy_unsatisfied`。祖先运行 `c5c1d31d` 和 `c8f53afa` 仍是 paused，没有 completed。租约 0，非终态任务 0。
- 只重启了适配器，现为 `3370557`，准入 `confirmed/ready`。QwenPaw `1916664` 和网关 `1918031` 没有重启。R-17 仍有 AC-33，不改为 passed。验收计数为 passed 40 / in_progress 8 / unverified 7。证据是 `var/ac31-join-failure-20261027/report.json`。

## 等待审批时重启，原节点恢复且适配器不自调后继（2026-10-27）

- 执行模型保持 `gpt-6-luna`。`danger-full-access` 不会留下可观察审批，所以只重开已经验证过的审批路径：`workspace-write`、`approval_policy=on-request`、宿主 user namespace，以及容器 `seccomp=unconfined` 和 `apparmor=unconfined`。`taskField`、AppArmor profile、ReMe 和执行模型没有放开。父角色临时改到 `ac18parent/grok-4.7`，因为 `kilo-auto/free` 的促销限额会挡住委派。QwenPaw `1916664` 和网关 `1918031` 没有重启。
- 运行 `d56c632a-ed5e-503b-bc40-17a1eb0c3789` 只有原节点 `task-9475c7dfe3814076bed4ac9cc9be3828` 在等待唯一审批。适配器从 `2989397` 重启为 `3001601` 期间，任务数仍是 1，后继文件不存在，Sim 运行号不变。
- 重启后批准原请求 `7a2f9a0c-9538-437d-b1e1-f5fcba12c029`。原任务在 `1793066518.365` 成功并释放，后继 `task-106919f9c254419f97309cf8f50de433` 在 `1793066524.078` 才创建。两条都执行成功、验证通过、资源已释放，线程模型都是 `gpt-6-luna`。
- 这满足 AC-37：原 Sim 节点在适配器重启后恢复，适配器没有自行调度后继。沙箱回到 `danger-full-access/never`，修订回到 `0169b015`，sysctl 回到 1，启动器标记已去掉且仍是 `755`。父路由回到 `kilo/kilo-auto/free`。适配器现为 `3028599`，准入 `confirmed/ready`。租约 0。R-02 仍有 AC-02、AC-36、AC-68，R-14 仍有 AC-25、AC-29、AC-69，两个需求都不改为 passed。验收计数为 passed 39 / in_progress 9 / unverified 7。证据是 `var/ac37-approval-restart-20261027/report.json`。



## 暂停不放后继，停止未确认前不算已停止（2026-10-27）

- 执行模型是 `gpt-6-luna`。`danger-full-access` 下命令不会停在可观察的审批窗口，所以这次只临时打开 `workspace-write` 和 `approval_policy=on-request`。宿主 `kernel.apparmor_restrict_unprivileged_userns` 临时为 0，Codex 容器临时加上 `seccomp=unconfined` 与 `apparmor=unconfined`，否则 `workspace-write` 的 bwrap 无法建 namespace。`taskField`、AppArmor profile、ReMe 和角色模型没动。父角色临时改到 `ac18parent/grok-4.7`，因为 `kilo-auto/free` 促销限额会挡住后继委派。QwenPaw `1916664` 和网关 `1918031` 没有重启。
- AC-18：运行 `6e9f3c83-ce7d-5771-8ce1-ef236cb6b298` 暂停后，界面是“后继任务准入已暂停”，活动任务继续。继续之后才有后继。`task-7a84148bf573487d8ce3342548d46965` 与 `task-d1b32271c3e84560b9c89e7f54ac17cf` 都成功、验证通过、资源已释放，线程模型都是 `gpt-6-luna`。
- AC-19：目标 `task-90830a027fd74d10863bce2e5ecac8b1` 在页面显示“已申请取消，待确认停止”和“执行尚未确认结束”时取消，没有先写成已停止。排队任务 `task-1f434ae6e4c94afea753a0e71c280b63` 随后成功并通过验证。
- 收尾时恢复文件把 `launcher.py` 的修改时间抬过了旧准入证明，脚本先报 `restriction_not_restored`。把启动器时间戳对回原证明后，准入重新为 `confirmed/ready`。沙箱回到 `danger-full-access/never`，修订 `0169b015`，sysctl 回到 1，启动器标记已去掉且仍是 `755`。父路由回到 `kilo/kilo-auto/free`，临时 provider 已不在。适配器现为 `2730112`。租约 0。R-13 改为 passed。验收计数为 passed 38 / in_progress 10 / unverified 7。证据是 `var/ac18-pause-successor-20261027/report.json`。

## 原生审批只接受当前请求（2026-10-27）

- 模型保持 `gpt-6-luna`。只临时把执行目标 `agent-line-container-codex` 的沙箱从 `danger-full-access` 改为 `workspace-write`，并把 `approval_policy` 从 `never` 改为 `on-request`。`taskField`、AppArmor、user namespace、ReMe 和角色 ID 都没改。QwenPaw 与网关没有重启。
- 只改审批策略时，任务 `task-20a19627bb3f47798a7b50ea67535834` 在审批出现前就结束了。改成 `workspace-write` 后，任务 `task-e262027e38a84dd18314dc8802d23a35` 出现一条绑定的原生审批，请求 `7ba306ac-bd71-4dc0-bc02-92f72c618098`，线程 `01a17a05-1761-7143-bb08-f48b4fe9164b`，轮次 `01a17a05-2414-7532-baf2-df1ac16115e6`。
- 页面加载和刷新各查询了一次当前执行权限。旧请求返回 404，旧执行身份返回 409。人工页面批准当前请求返回 200；把同一决定再提交一次仍是 200，没有第二条 shell 命令。唯一的 `require_escalated` 命令退出码 0，标准输出里有一次 nonce。随后的 `wait` 只读取这次命令的输出。
- 任务执行成功、验证通过、资源已释放，租约 0。两个配置文件已按原字节恢复，修订回到 `0169b015`。适配器 pid `4067716`，`controlledExecution=confirmed`、`readyForExecution=true`。启动时 bubblewrap 提示 `workspace-write` 需要 user namespace，但批准后的命令仍然只执行了一次。
- 这满足 AC-17，也满足 AC-64 里的重复决定和旧请求拒绝。没有另开过期请求或新的 attempt，也不覆盖暂停和停止。R-13 仍有 AC-18、AC-19，不能整体通过。验收计数为 passed 36 / in_progress 12 / unverified 7。证据是 `var/ac17-native-approval-20261027-ws/report.json`。


## 进度正文恢复边界（2026-10-27）

- 写入本页新节时，误把未跟踪的 `docs/PROGRESS.md` 覆盖成占位符。Git 没有原文件。上面第 2 节到固定验收段，来自当天 15:20 UTC 会话里 `sed -n 1,120p` 的记录。再往下四节来自 03:27 UTC 的快照，而且这四节不在那 120 行里。
- `同一角色的两个运行按串行队列执行` 在那份快照里是截断的，没有补写。120 行之后如果还有别的正文，没有干净副本，没有编造。

