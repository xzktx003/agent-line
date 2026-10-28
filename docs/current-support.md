# 当前支持范围（2026-10-28）

这是当前运行现场的版本、限制、地址和恢复入口。它不把未通过的条款改成通过。

## 支持版本

- QwenPaw 锁定提交 `502b59d88464d5c9e8be4a507347282d6a54e0be`，路径 `upstream/qwenpaw`。插件声明的宿主范围是 `>=2.2.3b1,<2.3.0b2`。隔离升级记录把 `502b59d` 标为 2.3.0b1。
- Sim 锁定提交 `27b53561def7db4eaa9ad2f12c04ea1a0eaaf91c`，路径 `upstream/sim`。运行镜像是 `ghcr.io/simstudioai/simstudio`。
- 插件 `agent-line` 版本 `0.1.0`。
- 执行模型保持 `gpt-6-luna`。基线 A 未启用 B/C 和 DSH。

版本来源是 `upstreams.lock.json`、`plugins/agent-line/plugin.json` 和 `docs/qwenpaw-upgrade-ac23.md`。

## 运行地址

- 局域网入口是网关 `http://10.30.0.28:3194`。2026-10-27 从 `10.30.0.21` 实际访问过。QwenPaw 只监听 `127.0.0.1:52403`，不直接对局域网开放。
- 适配器监听 `0.0.0.0:3193`，当前进程 `870621`，不带准入开关。网关把 `/extension/v1/` 和 `/workbench/` 转到本机 3193。
- QwenPaw 当前进程 `3297344`。网关当前进程 `1918031`。
- 同场只重启了 QwenPaw、Sim 和扩展适配器。数据库、Redis 和网关未动。第3、第6、第7步已在宿主 `3297344` 证明，证据是 `var/prd1010-joint-20261028/`。准入已重写，健康检查 `controlledExecution=confirmed`、`readyForExecution=true`。这次没有把 `nativeSupport` 改成 true。删除正文未复活、审计引用不恢复正文，PRD-10.9 已通过。同场七步的 rollout 已核对，PRD-10.10 已通过。

## 已知限制

- 原版 Sim 画布、登录和块目录仍是英文。中文工作台不把这层算成完整中文交付。见 `docs/sim-chinese-gap.md`。
- QwenPaw 外壳和登录页不在约定的中文工作台范围内。
- 不交付插件热更新。在途任务存在时卸载返回 409，不释放写租约。尾任务 `task-d61d558f568f49ecaf15d583005b2013` 已取消且资源已释放，当前没有 registered 任务，租约为 0。这不表示可以热删除插件。
- 嵌入 Sim 的流式连接已有通过记录：`var/gateway/iframe-stream-20261028/report.json`。同源加载 `/workspace`，刷新后会话仍在，两次套接字握手成功，安全策略仍是 `SAMEORIGIN` 和 `frame-ancestors 'self'`。Sim 仍保持自己的登录边界：退出后重新加载会回到登录框，编辑器不再可见，见 `var/gateway/iframe-logout-20261028/report.json`。
- 现场 `var/adapter/tasks.sqlite` 已显式从版本 2 迁到版本 5，适配器 `185194` 装入了这段代码。版本 3、4、5 可以写人工裁决；版本 4 和 5 保存事件的运行、来源、发生/接收时间和受限载荷；只有版本 5 保存事件类别，并按发生时间而不是插入顺序认定最新终态。版本 5 还会在提交、记忆追踪和原生审批里写入角色启动、记忆召回、委派开始、审批创建和审批处理；注释失败不重试原生提交。事件接口对未记录的契约字段返回 null，不编造运行 ID 或事件 ID。旧事件只补了已知类别，事件 ID 仍是 null。控制台快照可以不回显 `task_id`，缺省时沿用已绑定的父任务号，另一个 `task_id` 仍是 unknown。`task-a9ed501688e04cc688c8b0d167d74550` 的完成快照已核对为 observed，并写入一条记忆召回和一条委派开始。当前适配器是 `870621`，健康检查是 `controlledExecution=confirmed`、`readyForExecution=true`。`role_started` 有 5 条。审批创建和审批处理各 1 条，恢复记录 1 条且不允许新尝试。
- 完整纵向流程已有一次通过记录，但仍有三条限制：规划、实现和审查复用同一个既有正式角色；人工验收由独立探针操作员批准，不是具名最终用户；失败时的修复出口保留，这次通过路径没有进入修复循环。见 `var/vertical-delivery-20261027/report.json`。

## 恢复时先看这些步骤

- 回执未知或宿主重启后，只核对原任务、原线程和原轮次，不自动重发。见 `docs/host-restart.md`。
- 升级兼容报告器只读，`upgradeExecuted` 永远是 false。九项探针缺一即拒绝。见 `docs/upgrade-policy.md`。
- 热卸载不可用。先停新准入，核对在途身份，写租约清空并停止宿主后，才能冷删除插件文件。见 `docs/plugin-removal.md`。
- 数据目录缺失或数据库不可写时要明确阻塞，不能建空团队冒充恢复。见 `docs/installation-backup.md` 和 AC-29 证据。
