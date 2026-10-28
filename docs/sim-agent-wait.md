# Sim 原生机器等待输入实测

UI Agent 可以优先编译成原生 Human 机器等待，可信服务从公开 API 读取其持久解析输入后登记任务；无需图内 HTTP 提交块或服务令牌。本轮只验证输入/恢复的数据通道，不登记 Agent/Codex 任务，不更改主准入、QwenPaw 或记忆。

## 接口

`SimWaitClient(sim_client).read_input(workflow_id,run_id,block_id)` 只执行 GET：

`/api/v2/workflows/{workflow_id}/runs/{run_id}?selectedOutputs={block_id}.taskInput`

成功返回：

```json
{"input":{"nested":{"value":7}},"encoding":"json-object-string","run":{"workflowId":"...","runId":"...","status":"paused","blockId":"...","contextId":"...","pauseKind":"human","pausePointCount":2,"resumedCount":1}}
```

encoding 为 object 或 json-object-string。错误 `WaitInputUnavailable.code` 是固定诊断，不返回上游正文。校验 run/workflow、状态 paused、当前 block/context 相同、human 类型、整数计数且 `pausePointCount - resumedCount == 1`。两个计数实际属于整条暂停执行：不能强制 resumedCount=0 或 pausePointCount=1，否则第二顺序 Agent 被错误拒绝。

**上层仍必须验证实际发布图、机器等待用途、无循环/并行、单次节点激活和固定角色授权。** 此适配器不证明发布版本，不授权任何恢复，也不消除取值后状态变化；绑定/恢复前需要再次核对身份。现有 CompletionRelay 的 pausePointCount==1 限制已报告主线，本切片没有改它；不能声称完整多 Agent 自动执行闭环已完成。

拒绝缺失/隐藏输出、非对象、重复 JSON key、非有限数字、深度超过 16、超过 64 KiB、原生模板与脱敏/运行时占位符、内部大值引用、截断标记。保守字符筛选可能拒绝本来合法的包含模板语法或保留键名的业务文本；当前不自动放宽或从其他字段补值。

## 源码和真实差异

- Human `inputFormat` 使用 `{id:"taskInput",name:"taskInput",type:"object",value:"<开始.input>"}`。
- `executor/variables/resolver.ts` 递归解析字段；`variables/resolvers/block.ts` 的 formatValueForBlock 将非 function/condition 场景对象 JSON.stringify，因此真实 selectedOutputs 返回 **JSON 对象字符串**，不能假称原生 dict。适配器只对该持久解析结果做严格 JSON object 解码，不读取 trace/template 猜输入。
- `handlers/human-in-the-loop/handler.ts` normalizeInputFormat 保留 field.value，并将其铺到 block output 顶层。
- `lib/logs/execution/trace-store.ts` 从持久 executionState.blockStates[blockId].output 经秘密安全投影读取；不是静态 workflowState。
- `lib/workflows/executor/execution-status.ts` 使用所有 pausePoints.length 和全局 resumedCount。真实第二节点为 2/1。
- `human-in-the-loop-manager.ts` 将恢复对象写到 resumeInput。Response JSON 中对象引用必须不加引号：`{"result": <后续等待.resumeInput.output>}`。首次测试加了引号，原生保留字面模板；失败记录已保留，修复后新版本、新 run 验证，不重投旧 run。

## 真实证据

脚本：`probes/sim/agent_wait_probe.py`。私有证据：`var/sim-agent-wait/state.json`（首次失败）、`state-v2.json`（最终运行）、`report.json` 与各 invocation 的 report-*.json，均通过 0600 原子写/fsync。凭据不写报告。

最终原生图：Start(runMetadata=true) → Human 执行等待 → Human 后续等待 → Response。

- workflowId：`b43ccd0d-ad3a-439c-8008-6b709508e2c8`
- 最终 runId：`67824191-c554-442f-b0d1-c53d0780e0e2`
- 第一 context：`agentwait_22a9e67985824b309734467ae5c43292_wait`
- 第二 context：`agentwait_22a9e67985824b309734467ae5c43292_wait2`
- 第一 taskInput 与输入完全一致，包含中文、多层对象、数组、布尔、null 和数字。
- 第二 taskInput 与第一次恢复的 output 完全一致，resumedCount=1。
- 两次恢复均是预先持久化的固定无模型结果；最终 completed，output.data.result 为 `{"done":true,"来自":"第二节点","nested":{"count":2}}`。
- 再次独立运行报告 passed_live，nativeWritesThisInvocation=0，只观察原 run。

运行入口为仓库根目录 `.venv/bin/python probes/sim/agent_wait_probe.py`。create/save/publish/execute/resume 意图先持久保存，未知写不重发；重复运行不新建图/任务。探针为单操作者使用，不应并行执行。没有恢复其他 Human 暂停。

7 项 MockTransport 测试覆盖固定路径、身份/上下文拒绝、隐藏输出、类型/截断/脱敏、大值引用、JSON 字符串及重复 key、重定向/错误不泄密和全局计数。定向 flake8 通过。
