# 编译器原生有限循环与 Function guard 真实探针

入口：`probes/sim/finite_loop_guard_probe.py`。独立私有证据位于 `var/sim-finite-loop-guard/`，复用已有私有 Sim API 配置，不打印凭据。只创建本探针工作流，不连接执行器、模型、QwenPaw、角色或记忆。

探针使用生产 `compile_document` → `SimAuthoringClient.save_state/deploy/get_version` → `decode_document` 后重编译精确对比。`PublicationVerifier.check_definition` 和每次暂停时 `verify_run` 都显式传入 `loop_policies`。输入读取使用 `SimWaitClient.read_input(..., context_id=当前_loopN)`，要求同一次输出投影的原生 context 对应当前暂停，并核对 input 为 `{input:{},iteration:N}`。

图是一个原生 doWhile 容器，包含同一个 Agent Human 和编译器确定性 Function guard，循环外一个动态 End。Agent 七字段元数据完整保存，但角色名是未绑定探针占位，不会生成模型任务。手工提供机器结果只用于验证 Sim 运算与公开 API。

实际已验证用例：持续 succeeded/verificationStatus=failed 三轮完成；第二轮 passed 提前退出；缺 executionStatus、缺 verificationStatus、verificationStatus 为布尔值各自必须在 guard 导致恢复子 run failed，且 output=null。不能将失败父 run 的 paused 状态当作成功或重新恢复理由。

## 写入守卫与证据

所有 create/save/publish/execute/resume 都先持久 payload/intent，收到回执后再记录 received。已有 state 默认只 GET 原父子身份，零新写；`--continue-known` 仅复用相同 payload 的已确认回执，未知意图不重投。若写函数的本地前置检查失败，可仅在完整 transport 记录证明没有发出写请求后标记 `proven_no_write`，沿相同 operation 继续；这不适用于响应丢失或上游 5xx。

首次 save 后，deploy 的前置 GET/decode 被官方 loop.data 的空 `collection`、`whileCondition` 默认值阻止。源码 `upstream/sim/packages/workflow-persistence/src/load.ts:211–212` 与真实 native-state.json 一致，编译器已仅规范化这两个精确空值并加入正反例；非空值仍拒绝。该阶段 transport 只有 create/save 两个写，未发 deploy POST，保留失败证据并沿原流程继续。

失败历史与修复版结果分别保留，不能删除或替换成虚构成功。


## 修复版本 2 的实际结果

工作流 `fcbb8596-9030-402b-b295-225fd9a49a74`，version 2，deploymentVersionId `2022e46b-94e9-449f-9f33-030d1fb0463f`。显式发布定义证明为 true；八次暂停的原生 run 版本/图证明均为 true。

| 用例 | 原父 run | 实际迭代 | 最后恢复子 run 状态 |
| --- | --- | --- | --- |
| 连续验证失败 | 92523349-e1e0-4915-9c15-90123cbc6ebe | 0、1、2 | completed，最终结果 verificationStatus 仍为 failed |
| 第二轮通过 | 69dda6e2-e23d-4dfb-9ce7-f79413bc9d2f | 0、1 | completed，最终结果 passed |
| 缺 executionStatus | 1fe17348-f859-4038-b964-2f8db291cdb3 | 0 | failed：loop_execution_invalid |
| 缺 verificationStatus | 7dffedfb-3f6a-4d49-a4ce-a10d5e7b25a4 | 0 | failed：loop_verification_invalid |
| verificationStatus 为布尔值 | 3f83db03-c94b-4e3d-9ae5-ecaa73a1d9ca | 0 | failed：loop_verification_invalid |

三个错误消息均由确定性 `ALLoopGuard...` 原生 Function 产生，恢复子 run output=null；没有被 loop 的错误处理转为 completed。连续失败的流程正常有界结束不等于任务验证通过，动态 End 保留最终 failed 状态。

首版 version 1 暴露本次编译缺陷：Function 原生输出形状是 `{result:{result:原机器结果,needsRepair:true},stdout:"",files:[]}`，循环条件误用了 `.output.needsRepair`，导致第一次恢复后提前完成。`state.json`、`first-resume-observation.json`、`guard-output.json` 保留该真实错误，旧 run 不重投、不宣称成功。修复使用 `.result.needsRepair`，同时增加原生输出形状的 Node 回归。root 明确授权在同工作流发布 version 2，使用独立 `state-revision2.json` 和新用例身份；这是确定性缺陷修复后的版本验收，不是未知请求的换 ID 重试。

运行日志的 workflowState 投影省略 variables，并给 edges 补 null sourceHandle/targetHandle；发布验证器对照正式发布图证明空 variables 后，才在只读投影上补空及去 null，仍完整比较原生编译图。这个差异由对应模块维护者修复，探针没有跳过 publication proof。

version 1 共 5 次实际写（create/save/deploy/execute/resume），其错误证据保留。version 2 共 **15 次实际写**（save/deploy、5 次 execute、8 次 resume）；报告中的累计 acknowledged steps 为 16，因包含复用的旧 create 回执，不能误算为该次 16 次网络写。两版累计 20 次实际写。默认再次执行已验证 **13 次 GET、0 次写**，观察五个父 run 和八个子 run；全部私有证据文件权限 0600。

```bash
.venv/bin/python probes/sim/finite_loop_guard_probe.py
.venv/bin/python -m unittest tests.test_sim_authoring tests.test_authoring_api tests.test_agent_authoring_api -q
.venv/bin/python -m flake8 --select E9,F63,F7,F82 agent_line/sim_authoring.py tests/test_sim_authoring.py probes/sim/finite_loop_guard_probe.py
```

没有进行真实 Agent/Codex 修复、网络不确定提交恢复或服务重启验收；不能据此宣称产品有限循环全量交付。运行计数、原生错误与父子身份见私有 state-revision2/report/observation 记录。
