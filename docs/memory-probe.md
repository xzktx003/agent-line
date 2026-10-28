# QwenPaw 原生记忆真实探针

当前已验证：专用正式原生角色 `agent-line-memory-probe`，通过官方 HTTP 写入记忆后，原生 `memory_search` 工具检索到随机标记及来源，并在回答中使用。跨重启验证已通过：PID 从 1736304 变为 1793937，同一角色、新 session 再次通过真实工具输出和最终回答校验。此局部证据不代表完整 AC-10（尚需 Codex 注入和后续任务行为）。

脚本：`probes/qwenpaw_memory/probe.py`。完整私有证据：`var/qwenpaw/memory-probe.json`（0600）；认证读取隔离实例的 `var/qwenpaw-secrets/local-login.json`，不输出秘密。角色模型使用该隔离实例已确认可用的 `kilo/kilo-auto/free`，没有变更其他角色、全局配置或上游源码。

## 正式接口和路径

| 操作 | 接口 | 实测说明 |
| --- | --- | --- |
| 创建正式角色 | POST `/api/agents` | backend=qwenpaw，固定角色 ID；已有角色则复用 |
| 记忆写回 | PUT `/api/workspace/memory/agent-line-probe.md?section=digest`，body `{content: ...}` | 使用 `X-Agent-Id` 明确选择专属角色；服务器返回 written=true |
| 读回记忆 | GET 同路径 | 内容逐字一致 |
| 显式重建检索 | POST `/api/agents/{agentId}/memory/reindex?scope=bm25` | 本探针避免等待异步索引；不是每次产品任务均应重建 |
| 启动角色新会话 | POST `/api/console/chat/task` | session_id 独立，提示只含检索问题，不含随机答案 |
| 查询任务 | GET `/api/console/chat/task/{task_id}` | 按原回执查询，不盲目重发；保存真实结果事件 |

原生写入路由位于 `upstream/qwenpaw/src/qwenpaw/app/routers/workspace.py`。这属于 QwenPaw 自己的记忆管理 API；探针没有直接写其 Markdown、SQLite 或 ReMe 索引。检索是在正式角色执行时调用 `memory_search`，不是外部服务自行 grep 文件。

公开 `qwenpaw.memory.BaseMemoryManager.memory_search` 和 `auto_memory` 可由正式插件封装；当前上游没有通用 memory_search HTTP 路由。现有 HTTP 提供记忆文件读写、reindex、status、graph。若产品需要无父模型检索，可通过受支持插件路由封装公共 MemoryManager 契约，并验证项目权限，不能猜测一个不存在的 HTTP 搜索接口。

## 验证判定

1. 独立随机 marker 写入专属角色 digest。
2. 新任务提示只询问项目代号 cobalt-orchard 的 marker，没有提供答案。
3. 要求真实 `plugin_call` name=memory_search。
4. 匹配相同 call_id 的 `plugin_call_output`，state=success，正文同时含随机 marker 和 `digest/agent-line-probe.md` 来源。
5. 最终 assistant message 含 marker，任务 result.status=completed。
6. 重启后验证 PID 已变化、相同角色记忆仍可读，再使用另一个 session_id 重复工具证据验证。

字符串包含“memory_search”或模型自述“记住了”不够，脚本 `verify_recall` 检查工具事件、关联身份与最终回答。

## 命令

```bash
cd agent_line
.venv/bin/python probes/qwenpaw_memory/probe.py seed
.venv/bin/python probes/qwenpaw_memory/probe.py recall --session before
# 确认隔离实例无在途任务后由部署维护流程重启 QwenPaw。
.venv/bin/python probes/qwenpaw_memory/probe.py after-restart --session after
```

请求超时且已有 taskId 时使用 `poll --session before`（或 after），不能新建替代会话掩盖未知提交。脚本在提交前先保存 submission_unknown 意图。seed 只用于这个专用探针文件，不可当成覆盖任意用户记忆的工具。

## 已知边界

- 此证据证明正式记忆 API 写入和原生角色检索，不证明模型自动经验提炼、候选审核、删除/更正防复活。
- 本探针使用 BM25；不证明语义 embedding 检索质量。
- 未证明这些记忆已进入 Codex 委派输入；需要执行桥另做实际注入证据。
- 专属角色可避免本探针混用其他项目，但不证明系统所有文件工具和自动记忆路径具有完整隔离能力。
- 首次会话上游 BOOTSTRAP 触发只读 read_file 后才调用 memory_search，未修改文件。产品受控角色需通过工具权限限制；不能把探针角色默认权限当作生产隔离已达标。

## 跨重启真实结果

| 阶段 | 原生会话 | 工具调用 | 结果 |
| --- | --- | --- | --- |
| 重启前 | `memory-probe-4029d9b9f1ef48790caf` | `call-f15146a6-5a09-4eb9-920d-5723e5ada1dd` | memory_search success，来源 digest/agent-line-probe.md，随机 marker 与最终答案一致 |
| 重启后 | `memory-probe-4ed1f5dabc16ac446bb7` | `call-2a0b46eb-4465-436a-9b1c-01aa8ccbc65f` | 不向新提示提供 marker，仍召回同一来源，最终答案一致 |

重启由执行桥负责人统一维护完成，本探针未干扰其在途任务。角色 ID 没有重建，GET 官方记忆 API 在新进程中读回一致。

## 可复用官方 HTTP 记忆适配器

模块：`agent_line/adapters/qwenpaw_memory.py`；测试：`tests/test_qwenpaw_memory.py`。

- 部署构造时固定 `role_agent_id`，每次请求用相同 X-Agent-Id；模型输入不能指定角色、路径或 section。
- 唯一命名规则为 `digest/agent-line-task-{taskId}.md`，task ID 使用受限标识字符，不允许路径穿越。
- `MemoryApproval` 由可信审核服务提供，绑定 task ID、内容 SHA-256 和决定 ID；缺少或不匹配则 needs_review，零网络写入。此对象不是可直接反序列化的模型“批准”声明。
- 写入前先 GET：内容相同则幂等 committed；已有不同正文则 needs_review，绝不覆盖；读状态未知则 pending。
- 最多一次 PUT，然后 GET 正式读回确认；即使 PUT 超时也先读回。只有读回匹配才 committed。
- pending 返回 nextAction=reconcile_only。后续 `reconcile` 只读，不重发 PUT，不重新执行编码任务。
- 状态与 TaskStore 的执行状态独立；此模块不会将任务标成功或重新启动任务。

上游文件 PUT 没有 compare-and-swap。调用方必须在自己的持久审核/提交总账中串行化相同角色和 task 命名空间、提交前保存意图；未知结果仅核对。该适配器不假称能够约束其他直接调用上游 API 的外部写入者，也不能单独提供跨进程恰好一次保证。

真实适配器烟测 `probes/qwenpaw_memory/adapter_smoke.py` 在专用角色写入另一文件 `agent-line-task-memory-adapter-proof.md`，使用明确批准的合成测试内容，官方读回 committed；再次执行只读匹配，不重写。原随机 marker 文件未覆盖。证据保存在 memory-probe.json 的 approvedAdapterSmoke。

该 API 写入明确审核后的正文，不自动把模型输出直接写入正式记忆，不等同于 QwenPaw auto_memory 经验提炼已完成。
