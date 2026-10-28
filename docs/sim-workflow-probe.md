# Sim v2 真实工作流探针

状态：**2026-10-21 已完成真实条件两分支与 Human v2 跨 Sim 服务重启恢复探针**。这是 G0 的局部真实证据，不代表 AC-04 全部条件种类、AC-37 外部 Codex 审批或完整 PRD 已通过。

## 运行范围

`probes/sim/workflow_probe.py` 使用正式 v2 HTTP API，在配置的专用工作区创建两个隔离流程。不会修改上游源码，不实现第二套调度器，不调用模型、不发送通知、不触碰用户代码项目。所有持久执行、分支和 Human 暂停由 Sim 完成。

- 条件图：Start → Condition → 两个 Response 结束节点，同一发布版本分别输入 yes/no。
- 人工图：Start → Human v2 → Response。先退出探针进程，后续进程读取同一原生 run/context 身份恢复。
- 为证明 **Sim 服务重启恢复**，需要在 pause 成功后由部署维护流程重启本次隔离 Sim 服务，再执行 inspect/resume。仅重启探针不能证明 Sim 跨服务恢复。

## 配置和命令

使用私有文件 `var/sim/probe-auth.json`，权限必须为 0600。字段为 `baseUrl`、`apiKey`、`workspaceId`。只通过 Sim 正式 UI/API 创建账号、专用工作区与个人 API key，不读取数据库制造凭据。部署环境文件 `sim.env` 里的加密密钥不是可调用 v2 的用户 API key。

```bash
cd agent_line
.venv/bin/python probes/sim/workflow_probe.py graphs
.venv/bin/python probes/sim/workflow_probe.py branches
.venv/bin/python probes/sim/workflow_probe.py pause
# 由部署负责人在此重启本探针所属的隔离 Sim 服务，记录容器/进程身份和时间。
.venv/bin/python probes/sim/workflow_probe.py inspect
.venv/bin/python probes/sim/workflow_probe.py resume
```

凭据不通过命令行传递，也不输出到日志。探针结果写入 `var/sim/workflow-probe-state.json`，权限 0600。里面包含原生 workflow/run/context 身份与响应，保留用于恢复核对；不提交到版本库。

## 防重复与失败语义

每次修改性 HTTP 请求之前先落盘 intent，回执成功后记录 received。存在未确认 intent 时禁止重发，包括创建、发布、执行和恢复请求。执行前持久化 `X-Run-Id`；该 ID 是上游一次性 claim，不是结果重放键。GET 404 不证明未执行，绝不自动换 run ID。

`inspect` 仅查询已有身份，不重建流程。失败退出不会写入 passed 标记。遇到失败保留原状态和隔离资源，先核对；不要删除状态文件再跑来掩盖不确定执行。探针不自动删除流程或执行记录。

## 固定源码依据

以下相对路径均位于 `upstream/sim/apps/sim/`，实际提交以 `upstreams.lock.json` 为准：

| 接口或配置 | 正式来源 |
| --- | --- |
| 创建流程、完整图替换、发布、执行、运行查询、恢复请求 | `lib/api/contracts/v2/workflows.ts` |
| GET/PUT 图状态与 normalized persistence | `app/api/v2/workflows/[workflowId]/state/route.ts` |
| 图 round-trip fixtures | `lib/api/contracts/v2/workflow-graph.test.ts` |
| Start 的内置 input 字段 | `blocks/blocks/start_trigger.ts` |
| 条件 JSON `{id,title,value}` 与 condition-ID 句柄 | `executor/handlers/condition/handler.test.ts` |
| Response 结束行为与 JSON 编辑配置 | `blocks/blocks/response.ts` |
| Human v2 字段及无通知配置 | `blocks/blocks/human_in_the_loop.ts` |
| 持久 pause context 的结构 | `lib/api/contracts/workflows.ts` |

公开 v2 工作区接口在当前源码中以列举/读取为主；探针要求配置一个通过正式界面创建的专用工作区，避免猜测不存在的创建接口。

## 需要真实执行确认的缺口

1. 镜像与固定源码的版本一致性及这些 v2 路由是否存在。
2. 条件表达式执行依赖是否在自托管环境正常，不用静态 schema 检查代替。
3. 图写入 lint 和发布是否接受所用标准节点/子字段；异常时保留证据调整探针，不能修改上游绕过。
4. 同一发布图两次真实结果是否分别为 `{branch: yes}` 和 `{branch: no}`。
5. Human v2 是否返回持久 context；重启 Sim 后旧 run/context 是否可查询和恢复。
6. 超时、HTTP 200 内带 failed、未知回执均不得误算成功；本探针只在原生 completed 且输出精确匹配时记录通过。
7. 这只是 Sim 的控制节点可行性探针，不证明 QwenPaw、Codex、记忆、中文入口或完整 PRD 已实现。

## 本次真实结果（2026-10-21）

通过正式 `/api/auth/sign-up/email`、`sign-in/email`、`/api/workspaces`、`/api/users/me/api-keys` 创建专用测试身份和工作区。辅助脚本 `probes/sim/bootstrap_probe.py` 保留私有凭据，不输出密钥。未直接写 Sim 数据库。

| 场景 | 原生身份 | 实际结果 |
| --- | --- | --- |
| 条件工作流 | `f34f8d8b-98da-46e2-8f3e-55be83c3864d` | 保存、发布、重读图成功 |
| yes 分支 | `bfe99ea6-e6de-44d0-afae-6b7e887e3173` | completed；output.data 为 `{branch: yes}` |
| no 分支 | `3f0ac2ec-3478-49e6-963d-30ebc44c3962` | 同一发布图；completed；output.data 为 `{branch: no}` |
| Human 工作流 | `d617185b-2e17-49c3-b4e2-56c663347a6c` | Human v2，未配置通知 |
| Human 运行 | `ad77fdf7-e0bc-45db-87b1-ab8204822c9a` | 真实 paused、人类暂停 context 非空；重启 Sim 后查询原运行与 context，再恢复同一 run 至 completed；output.data 为 `{probe: human-resumed}` |

实际执行 `deploy/sim/run.sh restart simstudio`，只重启本项目 `agent-line-simstudio-1`。Docker StartedAt 从 `2026-10-21T10:35:16.798967321Z` 变为 `2026-10-21T10:37:01.902004516Z`，重启后 health HTTP 200；未重建数据库，未创建替代运行。

私有完整响应、请求意图、原生身份和重启记录：`var/sim/workflow-probe-state.json`。凭据与该证据文件均为 0600。已运行结束的 Human 不应再次发送 resume；后续只用 inspect 核对。

实际发现并修正两项探针假设：Response 输出是 `{data,status,headers}` 包装；块 ID 在 Sim 存储中跨工作流唯一。Human 首次保存因 `start` 重名得到明确 409，再 GET 确认仍是初始 seed 图，记录 confirmed_not_applied 后改用 `human-` 前缀。没有把未知提交盲目重发，也没有修改上游。

尚未覆盖：else-if 三路、外部 QwenPaw/Codex 长任务、原生 Codex 审批映射、并行暂停、多租户、跨版本升级和统一中文入口。本次验证只证明当前运行 Sim 的标准条件节点、持久 Human v2 pause/resume 可用。
