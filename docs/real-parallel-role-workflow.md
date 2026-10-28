# 原生角色与 Codex 的静态双分支流程

已完成一个真实正例：中文编译图通过原 AuthoringAPI 发布，Sim 同时产生两个暂停节点，后台自动登记任务，经同一个正式 QwenPaw 角色分别委派 Codex，独立字节验收后回传各自的原 context，Sim 完成 `all_success` 汇合和 End。没有手工登记任务、填验收结果或调用 relay。

QwenPaw 原角色 `agent-line-native-loop-parent`、原生 ReMe、原 Codex 目标和模型继续使用。扩展仅连接原插件、现有适配服务与 Sim，没有增加记忆引擎、调度器或服务。正式主适配服务仍保持 unknown/guarded，本次运行使用独立探针账本与进程内测试入口。

## 实测范围

- 图：Start → 静态分发 → left/right 两个 Agent → all_success 汇合 → End。
- Sim workflow：`85d9d8c4-3cc1-4de7-9985-195e6011ec22`。
- 原运行：`302ca9ca-698a-504a-a35f-73ab3c52289c`；最终恢复子运行：`1ff35955-7781-4925-be4f-12caea0123aa`，实际 completed。
- 两套独立节点任务、父任务、会话、Codex thread/turn、launcher owner 与 container。两个原生 context 均 resumed；原恢复日志冻结的结果与实际汇合输出逐字段相同。
- 每分支的发布、运行、任务、父投递和插件绑定五处角色修订一致；验收策略在发布、运行与任务冻结。各自唯一文件与 nonce 没有串用，实际字节及 SHA256 均通过。
- 两节点共享一个代码目录，原工作区租约让 Codex 顺序写入。第一节点退出及验收完成后才提交第二节点；这证明静态分支编排和共享目录互斥，不证明两个 Codex 同时写入。
- 两次原容器退出、launcher 消失和插件持久 `runtimeRetirement=stopped` 均独立核对。父任务与 Codex 原生后台任务均 finished/completed，最终租约和活动 owned 容器为 0。
- 九份主配置摘要及主维护守卫保持。默认重复调用只读观察，两原任务不重建、原生执行写请求为 0。

根代理独立读取实际 Sim API、原生任务 API、SQLite、产物、固定控制器和插件回执，结果为 `var/pinned-parallel-role-flow/root-check.json` 的 `passed_real_static_parallel_read_only`。探针自身报告为同目录 `report.json`。

## 复核与运行入口

```bash
# 默认无 state 时仅做只读就绪检查；已有 state 始终只读原身份
.venv/bin/python probes/full_workflow/parallel_roles.py
# 仅全新现场明确允许一次新流程；已有 state 仍不重投
.venv/bin/python probes/full_workflow/parallel_roles.py --execute
```

已安装插件文件必须与本仓库源文件相同，宿主须在安装之后启动；主环境无在途执行、原角色与 ReMe 不变，才允许进入新流程。create/save/publish/start 每项先持久 intent，未知回执不会换 ID 重投。

## 尚未证明

本轮只验证文件字节，没有运行独立测试命令；测试命令证据见另一[真实编码验收](automatic-test-verification.md)。本轮保留原生记忆实现，但不代替独立的记忆召回/回写验收。没有覆盖在途宿主重启、多工作区同时编码、动态批量、子流程或全部故障矩阵；R-17/AC-31 仍为进行中。

AC-32 已经独立审计与根复核记为通过：原文允许“隔离工作区或拒绝并发”，此现场覆盖共享目录拒绝并发写入的路径。两分支先登记，第一执行资源释放且验收通过后第二才提交；两实际文件与原输出、证据均保留。审计见 `var/pinned-parallel-role-flow/acceptance-review-AC32.json`，不将此条提升为整个 R-17 完成。

连续会话的插件修复与固定 target 专用边界见[Codex 运行实例收尾](codex-runtime-retirement.md)。原隔离审批失败任务及其租约保持，没有通过此新流程重投，也没有将其拒绝审批记为通过。
