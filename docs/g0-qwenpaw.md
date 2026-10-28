# G0：QwenPaw 无源码修改扩展证据

核查 checkout：`502b59d88464d5c9e8be4a507347282d6a54e0be`。本报告是源码与静态契约核查，不代表真实模型、界面、重启闭环已通过。所有源码路径以下相对 `upstream/qwenpaw/`。本次未访问用户凭据、未启动原有服务、未修改上游。

## 结论与实施边界

可以通过官方插件安装前端入口、后端 API、自定义工具、AgentScope middleware 和记忆后端，保持 QwenPaw Git 工作树原版。不能将现有 `submit_to_agent` 等同于可重启恢复的持久任务服务：后台任务表是进程内字典，重启丢失。应由 Agent Line 持久保存任务身份、节点激活、委派 receipt 和恢复核对状态。

原生 Codex 适配器已有真实 app-server 身份与审批桥接，但其线程恢复失败会退回创建新线程。对于不允许重复副作用的业务任务，外层必须先核对并停在 `reconciliation_required`，不能因恢复接口失败重投执行。

## 已找到的官方扩展面

| 能力 | 源码位置 | 实施含义 |
|---|---|---|
| 插件安装、热安装 | `src/qwenpaw/cli/plugin_commands.py:39`；`website/public/docs/plugins.zh.md:25` | `qwenpaw plugin install /absolute/path`；必须设置独立 `QWENPAW_WORKING_DIR`，否则 CLI 会发现既有 runtime 并热安装到旧服务 |
| 前端菜单、页面、认证 fetch | `website/public/docs/plugins.zh.md:318,505,533` | bundle 调用 `window.QwenPaw.menu.add`、`route.add`；用宿主 SDK，无需改 Console 源码 |
| 后端 HTTP 路由 | `src/qwenpaw/plugins/api.py:600` | `register_http_router(router,prefix='/agent-line')` → `/api/agent-line`；自有路由仍须做角色/项目权限判断 |
| 工具受控边界 | `src/qwenpaw/plugins/api.py:820` | `register_tool` 注册运行时工具和治理元数据；独立 `agent_line_delegate` 工具内执行持久幂等与权限核对 |
| middleware | `src/qwenpaw/plugins/api.py:654`；`plugins/middleware-demo/tracing-middleware/tracing_plugin.py:29` | `on_acting` 可包裹工具调用；用于限制角色执行工具与追踪，不能仅靠提示词防旁路 |
| 记忆插件契约 | `src/qwenpaw/plugins/api.py:347`；`src/qwenpaw/agents/memory/base_memory_manager.py:52` | `MemoryBackendContext` 包含 agent_id/working_dir/backend_config；支持官方记忆后端扩展 |
| Agent 持久身份 | `src/qwenpaw/app/multi_agent_manager.py:170,217` | 根据持久 profiles 加载已有 workspace，不应每个 workflow run 创建新 Agent |

注意：官方 pet 示例包含 monkey patch（`plugins/bundle/qwenpaw-pet/plugin.py:80`），并不应复制为本项目升级兼容方式。只借鉴正式注册 API。

## 后台任务与恢复的事实

- `src/qwenpaw/agents/tools/agent_management.py:767`：`submit_to_agent` 调用 `/console/chat/task`，返回 task 与 session 文本头；`check_agent_task:872` 轮询。
- `src/qwenpaw/app/routers/console.py:58-69`：`_BackgroundTask` 与 `_bg_tasks` 为内存状态。
- 同文件 `post_console_chat_task:987` 每次产生随机 task ID，`1237` 查询不存在返回 404。task ID 丢失不是“执行失败”的证明。
- `src/qwenpaw/app/task_tracker.py:51` 的 tracker 保留当前进程 detach/reconnect，不等于跨进程持久恢复。
- `src/qwenpaw/harnesses/codex/adapter.py:386,416,554,579,585`：真实 thread/start、turn/start，线程映射写盘，thread/resume 失败后移除映射再起新线程。
- `adapter.py:640-711`：处理 commandExecution/fileChange/permissions approval，转 QwenPaw ApprovalService。当前 root_session_id 设为 session_id；跨父角色审批路由不能仅凭此代码假定正确，需真实链路验证。

**必须落地**：委派工具先在 Agent Line 事务内注册激活和 attempt；同一键命中原任务；未获取上游 receipt 的崩溃窗口进入待核对。只有明确未执行或经显式新 attempt 才允许启动新任务。Sim 控制业务重试，适配器仅做相同任务的查询/传输恢复。

## 记忆闭环与权限

`Workspace.memory_manager`（`src/qwenpaw/app/workspace/workspace.py:201`）提供当前 workspace 的管理器。`BaseMemoryManager.memory_search:158` 是搜索契约；`ReMeLightMemoryManager.memory_search:680` 与 `auto_memory:820` 提供实际搜索和对话沉淀。`list_actions/run_action:853/873` 是后端相关维护能力，不能当通用任意 CRUD。

官方 `/api/workspace/memory/{path}?section=digest` 支持通过 `X-Agent-Id` 选择角色进行记忆文件读写；随后可调用 `/api/agents/{id}/memory/reindex?scope=bm25`。本轮已通过这些公开接口写入，并由真实原生角色的 `memory_search` 工具召回（见下方运行证据）。原生自动沉淀仍需独立验收。

`workspace.py:425-448` 中非 qwenpaw backend 走 harness_runtime 路径；直接 Codex backend 并不证明执行了原生 ReMe 生命周期。因此保持 native 角色召回 → 受控工具委派 → 原生记忆提交，保存召回条目、实际注入载荷摘要、证据和写回状态。项目隔离由固定 project-role workspace 和请求权限共同落实，不能只依赖 query filter。

## 可执行静态验证

从 `agent_line/` 运行：

```bash
python probes/qwenpaw/source_contract.py
git -C upstream/qwenpaw diff --exit-code
```

当前静态探针 8 项通过。探针明确打印 `runtime_verified: false`。升级时探针失败应停止切换，而非自动修补上游。它只能检查接口形状与已知限制，不证明语义兼容。

## 隔离运行验收清单（执行进展见下文）

1. 在 `agent_line/.venv-qwenpaw` 建立 Python 3.11–3.13 环境，安装当前固定 checkout 的依赖。持久 `QWENPAW_WORKING_DIR` 指向 `agent_line/data/qwenpaw`，不可默认指向用户现有目录。
2. 构建自己的 plugin frontend bundle，生成清单 `entry.backend`/`entry.frontend` 与窄版本范围。使用该环境的 CLI 执行 `qwenpaw plugin install <agent_line/plugins/...>`；以安装版本 `qwenpaw app --help` 核对启动参数，再监听独立空闲端口。
3. 浏览器真实进入宿主，确认中文菜单、路由、认证请求；禁用/卸载后路由与监听器清理，再安装成功。记录 Playwright 截图和接口证据。
4. 创建一次正式 project-role，记录 ID；运行一条只读模型任务，重启隔离 QwenPaw 后再次调用原 ID，确认无需创建。
5. 提交同 activation/attempt 两次，确认受控工具只产生一个真实 Codex turn；停在启动 receipt 崩溃窗口后重启，确认进入待核对而非新建 turn。
6. 做一个 sandbox 内文件改动任务，收集实际 threadId、turnId、tool execution、diff、测试退出码。要求审批的操作确认拒绝后没有执行。
7. 写入独特测试记忆，下一轮通过真实 memory manager 召回并在 Codex 请求载荷证实；另一个项目不可召回。失败的记忆写回只重试记忆，不重跑编码。
8. 同时运行两任务与一次升级演练，确认 workspace 写租约、旧 attempt 回调隔离、插件兼容失败阻止切换、可恢复角色/记忆/任务。仅以上真实验收完成才可将 QwenPaw G0 标为通过。

## 2026-10-21 实际运行补充（最新证据）

已从静态核查推进到真实服务和原生执行，仍不代表完整 G0 通过：

- 隔离实例的 Kilo `kilo-auto/free` 通过官方 `/api/models/kilo/models/test`，返回 `success:true,status:available,verification:live`。未配置 API key 也存在可用原生协调模型。其他 provider 没有配置凭据，不推断可用。
- 授权仅复制已有 Codex auth.json 到 `var/codex-g0/auth.json`（0600），不复制全局 config、history、skills 或 sessions，也不修改原凭据。隔离 QwenPaw 进程以此 `CODEX_HOME` 启动。原生 Codex status 返回 authenticated:true；model/list 返回包括 gpt-6.1-sol 的实际账号可用模型。
- 正式角色 `agent-line-g0-parent`（qwenpaw）、`agent-line-g0-codex`（codex）通过官方 POST /api/agents 创建。跨服务 PID 变化后同 ID/backend 保留，证据 `var/qwenpaw/g0-role-persistence*.json`。
- 原生 Codex 首轮确实启动执行，但主机 bwrap 报 RTM_NEWADDR Operation not permitted，文件未写成。证据保留在 `g0-codex-smoke.json`，没有盲目重投。
- 显式第二尝试使用该角色 workspace 与 `danger-full-access`，真实模型 gpt-6.1-sol 调用 exec 写文件并运行 shell 校验；外部独立读取精确内容通过。证据 `g0-codex-smoke-host-v2.json` 与原生 rollout。**此成功不证明 OS 沙箱隔离**，生产受控工作目录/权限仍需解决。

### 工作目录事实

`src/qwenpaw/app/workspace/workspace.py:451` 传给 native harness 的 cwd 为 `self.workspace_dir.resolve()`。本轮 `request_context.session_project_dir` 未改变原生 Codex cwd，因此不能将 session 项目配置当作 native 执行目录绑定。

官方 POST `/api/agents` 支持 `workspace_dir`（`routers/agents.py:838`），在创建时可将专属 Codex 角色持久工作区绑定指定目录。但这会同时放置 QwenPaw 角色文件/记忆等数据，不等于纯代码 worktree。PUT 已有 Agent 显式保留 canonical workspace（同文件 `update_agent:1151`），不能靠 PUT 移动。产品应选择创建时绑定的专属执行角色目录或由外部 native 执行适配器传入 cwd，并验证工作树隔离。

### 插件真实执行桥接

`plugins/agent-line/execution_bridge.py` 提供 POST/GET `/api/agent-line/executions`，除宿主登录外要求 `X-Agent-Line-Service-Token`。目标角色/目录/CODEX_HOME 为服务端配置；模型不传。窄 receipt SQLite 仅存原生提交身份和核对结果，主线 TaskStore 仍为任务权威。

- 先持久 claim taskId 与规范化载荷，再调用官方 `post_console_chat_task`。
- 相同 taskId/载荷复用；不同载荷 409；丢响应保留 unknown，禁止自动重投。
- taskId 必须映射固定 `roleSessionId=agent-line-<taskId>`。
- 返回 identity 从 upstreamTaskId 逐步扩充真实 codexThreadId/codexTurnId。
- 原生 terminal 与资源释放分开。只有原生事件完成且同进程 upstream task finished 才 `resourcesReleased=true`；重启丢内存 task 不假定资源已退出。
- **观测竞态已实测并修复**：独立新 app-server 的 thread/read 曾将仍在执行的 persisted turn 显示 interrupted，但原执行稍后 task_complete。当前桥接不再起观察 app-server，只读隔离 native rollout：校验 session_meta thread/cwd、唯一 task_started turn，以及相同 turn 的 task_complete/turn_aborted 事件。

真实 HTTP 探针 `probes/qwenpaw/execution_bridge_smoke.py --task-id g0-bridge-native-002` 已通过双认证、重复提交复用、冲突409、真实 thread/turn、native terminal、资源退出和独立文件内容校验。证据 `var/qwenpaw/g0-bridge-native-002.json`。输出“succeeded”只表示 native turn 完成，业务测试与记忆验收仍由主线分别判定。

新增可复跑探针：`runtime_preflight.py`（仅脱敏输出）、`persistent_roles.py create/check`、`native_codex_smoke.py`、`configure_execution_bridge.py`（显式初始化）、`execution_bridge_smoke.py`。带已存 receipt 的脚本只查询原身份，不重新提交。

### 原生父角色召回 → 受控委派 → Codex（已真实贯通）

`probes/qwenpaw/parent_delegate_smoke.py` 使用正式 qwenpaw 父角色与可用 Kilo 模型。通过官方 memory PUT 写入随机 marker，重建 BM25 索引；父角色提示只含任务代号，不含 marker。实际结构化事件依次证明：

1. `memory_search` 调用及同 call_id 的成功结果，来源为 `digest/agent-line-execution-plan.md`，包含 marker。
2. 后续 `agent_line_delegate` 的唯一 instruction 参数含该 marker；同 call_id 的成功输出返回固定 task 与 upstreamTaskId。
3. 桥接观察到相同 upstreamTaskId 的 native thread/turn 完成且资源释放；独立文件读取精确等于 marker 加换行。

证据：`var/qwenpaw/g0-parent-delegate.json`（含 parentChain 调用关联）和 `g0-parent-delegate-parent-result.json`（原生完整事件）。原生 task 为 `task-ced2f0dd1c98`，thread 为 `01a15e18-b57a-7472-9c4a-97d8f716db68`，turn 为 `01a15e18-b5d9-7d31-a4f2-9b96a2478937`。复跑脚本仅查询已保存任务，没有重新提交。

可信绑定 API：`POST /api/agent-line/delegations/bind`，请求 `{taskId,roleAgentId,roleSessionId}`，要求宿主登录及服务 token；session 固定为 `agent-line-<taskId>`，角色必须在服务配置的 allowlist 且正式 backend 为 qwenpaw。SQLite schema 2 持久唯一绑定；同绑定幂等，不同绑定409。模型工具只能读取当前 ContextVar 对应绑定，不能创建绑定或选择 target/workspace。服务重启后须可信重绑以提供当前应用上下文，不改已有持久绑定。

这证明召回信息实际传递到 Codex 执行。仍未证明自动结果记忆写回、生产 OS 沙箱与真实审批拒绝、执行中取消和跨重启资源释放；因此不把完整受控执行 capability 标为 confirmed。父角色最终文本如实报告提交时仍 accepted；后续验收由真实原生证据判定。
