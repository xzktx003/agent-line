# Coding Kanban只读联动接口核对

2026-10-23只读查看本机 `/data01/home/zukang.xu/workspace/coding_kanban` 的当前源码，未运行其服务、命令或接口，未修改该仓库、配置或数据。结论只针对当前本机实现，不推断远端最新版。

## 已定位的契约

- `apps/web/src/lib/api.ts` 的 `getAgentTaskSummary` 调用 `GET /api/agent-sessions/{id}/task-summary`。
- `packages/shared/src/index.ts` 的 `AgentTaskSummaryResponse` 含 `available`、可选的最后用户/Agent消息摘要和 `updatedAt`，没有返回所查询的会话ID、项目ID、Codex轮次或独立验收结果。
- `apps/server/src/routes/agent-sessions.ts` 的同一路由读取Codex记录后，会在摘要变化时调用 `registry.updateSession`，还会更新缓存。GET不等于无持久状态变化，因此不能直接作为本项目承诺的纯只读结果查询。
- 同文件的 `GET /api/agent-sessions/{id}` 对普通本地记录调用 `registry.getDetail`，对 `remote-tmux-discovered` 则调用 `tmuxAdapter.getDetail`。实际注入类型为 `LocalTmuxAdapter`，不能只根据sourceType名称猜测实现。
- `services/local-tmux-adapter.ts` 的 `getDetail` 在存在paneId时会capturePane，随后调用 `registry.replaceOutputEntries` 替换输出条目；无paneId才直接返回原本地详情。因此通用详情GET也不能对任意会话承诺纯只读。
- `services/agent-session-registry.ts` 的本地 `getDetail` 返回 `{agentSession, outputEntries}`。后者是终端正文，不应透传。`AgentSessionRecord` 包含ID、原生会话ID、工作目录、来源类型、交互/连接状态，也包含SSH目标、命令、传输信息等不应公开的内容。

## 对联动实现的约束

联动必须绑定明确的本项目、Kanban登记ID、原生Codex会话身份和工作区；名称、目录相似或最近一条记录不足以建立归属。摘要响应本身缺少这些字段，不可将摘要文本当作当前任务的已核实结果。

没有已登记的目标地址和关联身份，当前不猜测端口、不扫描服务、不从用户全局凭证取认证，也不新增首页连接配置。后续接入优先使用既有本地会话的最小元数据投影，或显式导入原生快照，并保留用户提供快照与实时核实的区别。只读读取不得调用register、focus、stdin、恢复、启动、重连、停止、终端接管或会话更新接口。

投影仅按需提供对应会话标识、独立观察时间及原连接/交互状态；不得透传终端正文、SSH信息、命令、路径、凭证或不相关会话。Kanban的idle/exited/最后Agent摘要不能映射为本项目执行成功、独立验收通过或记忆提交成功。旧快照必须显示观察范围，不能称实时状态。

初次核对时尚未实现客户端和页面。后续已定位纯目录 `GET /api/agent-sessions` → registry.list，只读Map并排序副本；已据此补明确本地会话/原Codex身份/授权工作区匹配的有界reader、授权任务接口及默认折叠UI，见[实现与范围](kanban-readonly.md)。正式项目仍未登记源，未发送目录请求或建立真实关联，P2仍未验收。不使用上述有副作用的摘要/详情GET，没有执行HTTP、测试、Node、浏览器或模型，不新增验收计数。
