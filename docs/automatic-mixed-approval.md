# 自动执行与人工审批混合流程

入口：`.venv/bin/python probes/full_workflow/mixed_approval.py`。独立数据和私有证据位于 `var/automatic-mixed-approval/`。探针复用正式 QwenPaw 角色、原生 ReMe 和既有受控 Codex 执行器，不修改主配置、角色或记忆配置，不手动写回记忆；原生记忆可能随会话正常更新。

通过真实 AuthoringAPI 创建、保存和发布 Agent → Human → 审批条件 → 批准/拒绝 End 的流程。自动发布目录提供 binding，静态 bindings 为空。TaskFlowRuntime、Dispatcher 和审批发现线程使用同一客户端、恢复日志和运行账本；不手工登记任务、审批绑定或调用回传。两次运行分别创建一个专用文件，均由独立 ArtifactPolicy 验证精确字节。

只有已观察到绑定的人工暂停且文件验收通过后，探针才通过独立审批凭证提交明确 approve/reject。该身份为授权测试的独立操作员，不表示用户亲自点击，也不由模型决定。此前使用服务凭证的决定请求必须为 401 且没有原生写请求。

验收检查原生条件 selectedPath/selectedOption、实际 End、恢复子运行最终输出以及人审 resumeInput 与持久决定完全一致；重复同触发和同决定须保持原身份且原生写请求为零。核对父任务结束、容器退出、租约清零及主维护状态不变。已有 state 的再次运行仅观察原身份，未知结果不得重投。

真实结果以 `report.json` 为准；不能根据脚本存在或模型自述声称通过。

## 已完成的实际证据

本次两案例均 `passed_live`，主探针 PID 1210634 正常 exit 0；审批发现、执行和完成回传线程已随各自生命周期停止。workflow 为 `76c2ad33-59d2-47fe-bbc7-c169a46ad979`，自动发布 binding 为 `published-93a2ff4fd779390f9ccdafed5b982e414821c102a76cb0b5`。

| 决定 | 原运行 | 最终恢复子运行 | 实际结束节点 |
| --- | --- | --- | --- |
| approve | 875f6767-5678-5cce-a0c3-94ec9efdb0ea | a8ed4866-9336-436d-9cf5-dd09266ad6ef | approvedEnd |
| reject | 457465a2-6995-5e97-b154-caf94cb5a3a9 | 390a56f1-1613-493f-9e78-d9475e6bfd2e | rejectedEnd |

批准案例任务 `task-00928950d6224423bad8a2d52aef17d7`、父任务 `task-bc7b9e3f9c7c`；拒绝案例任务 `task-d2f456316fc14d9dafd8b101e14c4e95`、父任务 `task-321765b8fb30`。两任务均独立验收 passed、资源释放且父任务 finished。实际审批身份为 `mixed-probe-independent-operator`，不是模型，也不冒称用户亲自审批。

两个服务凭证审批请求均为 401。两个案例的重复触发及重复决定均复用原记录，新增原生写请求为 0。原生条件输出、选中 End、恢复子运行终态和持久人审载荷均匹配；报告保留冻结发布快照、完整任务证据、原生调用及审批回执。

末尾租约 0、自有活跃容器 0；主维护状态前后完全一致。普通再次运行只读同一两个任务，新增提交 0、原生写请求 0。JSON 私有证据权限为 0600；py_compile 和 flake8 致命规则检查通过。
