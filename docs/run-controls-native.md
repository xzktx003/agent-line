# Sim 原生运行控制实测

探针：`probes/sim/run_controls_probe.py`。只使用专用无模型流程及隔离 SQLite；不修改主服务准入、QwenPaw 或记忆。通过真实 AuthoringAPI、发布目录、WorkflowGateway、ApprovalDiscovery、ApprovalAPI 和 PublicationVerifier，使用 TestClient，不新增监听服务。

执行方式：

```sh
.venv/bin/python probes/sim/run_controls_probe.py
```

首次运行先持久保存每个操作 payload/intent，再调用 HTTP。收到回执才记录 received。已有 `var/sim-run-controls/state.json` 时默认只查询原运行 ID，绝不重建流程、重投未知请求。凭据从已有私有 Sim 配置读取，不打印；独立审批凭据及全部 JSON 证据使用 0600。

## 已验证结果

流程：Start → Human → condition（实际审批 approved 布尔值）→批准/拒绝 End。原版 Sim 推进图，未注册执行任务，未调用模型。

- workflow：`65b33347-63fb-4201-b2b4-f9f594217d49`，revision 1。
- 继续场景 root：`c8a26180-01e9-5d10-83a1-859c8256f278`。
- 批准恢复 child：`d1e5602f-e50f-45bc-bd35-9a2b7b227dd4`。
- 停止场景 root：`a61e5950-1033-5fee-a47a-ccfe697bf537`。

两个运行均真实执行到 Human pause，核验冻结发布图与实际 run graph 后发现审批。

1. `pause` 使用 expectedVersion 0。随后人工审批 POST 返回 409，审批账本 decision 仍为空，上游写入数为零。
2. 重建 FastAPI app 对象，读取同一账本，暂停状态仍为 paused。
3. 继续场景使用 expectedVersion 1 提交 continue，由服务真实检查原生暂停与发布证明；之后人工批准，原生 child 为 completed，实际 End 输出严格为 `{"decision":"approved"}`。
4. 停止场景提交 stop，journal 记录官方取消回执。回执仍明确 `verifiedStopped:false`；单独轮询 root 看到原生 `cancelled`。
5. 再次重建 app，停止意向仍为 stop_requested；以相同 operationId 和原 expectedVersion 重放 stop，上游没有第二次取消 POST。

首次实际 native 写入共 **7 次**：create/save/publish 3 次、execute 2 次、resume 1 次、cancel 1 次。TaskStore 任务数为 0。默认第二次运行只 GET，实际写入 **0 次**。真实执行 session 43513 已正常退出。

## 证据与边界

私有目录 `var/sim-run-controls/`：`state.json` 保存原始操作意向、回执、暂停与跨 app 查询；`report.json` 保存逐次 native 方法/路径和写入统计；`observation.json` 保存默认重跑查询记录。独立 credentials 与各 SQLite 不应公开。

这证明本地“后继准入暂停”及原生取消，而非任意运行指令即时冻结。跨 app 对象重建证明持久状态重新加载；未声称操作系统重启测试。此流程无 Codex，因此没有模型在途取消或容器退出验收；官方 cancel ack 也不能作为资源退出证据。

验证：语法编译、26 项 run-control/wiring/cancel 定向测试及 flake8 E9/F63/F7/F82 通过。未修改上游源码。

## 停止提交与取消意向之间的崩溃窗口

显式独立测试命令：

```sh
.venv/bin/python probes/sim/run_controls_probe.py --recover-stop-window
```

使用原 workflow 的固定发布版本，仅新增第三个运行。原两个场景和旧报告不重投、不覆盖。独立 `state-stop-recovery.json` 保存各步意向；该文件存在时此 flag 也只读取原身份，未知不重投。

实测先通过 HTTP start 到原生 Human 暂停，核对实际发布证明；无后台 worker 时直接 `RunControl.apply(stop)` 持久化停止意向，明确确认 `run-cancel` 记录不存在、native 仍 paused。随后重建应用接线，启动真实生产 `ApprovalDiscoveryWorker`，不是手动调用 cancel/reconcile：请求事件钩子记录唯一取消 POST 来自 `agent-line-approval-discovery` 线程。最终原生 run 为 cancelled，取消账本 accepted，回执 `verifiedStopped:false` 保留。

本新增场景实际写入 **2 次**（execute 与后台 cancel），没有第二次 create/save/publish，没有任务或模型。默认重跑 flag 为 **0 写入**。worker 已 join，线程不存活，session 24622 正常退出。证据保存在 `state-stop-recovery.json` 与 `stop-recovery-observation.json`；语法/静态检查和更新后的 23 项控制测试通过。

这里模拟的是持久 stop 已提交、取消意向尚未写入的精确恢复窗口，验证应用重建及生产 worker 恢复；未实际杀死操作系统进程，也未声称未知取消会重发。
