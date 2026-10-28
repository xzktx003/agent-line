# 经批准记忆的持久提交

实现：`agent_line/memory.py`，使用 `MemoryCommitStore` 保存独立记忆提交总账，`MemoryCoordinator` 调用现有 `QwenPawMemoryClient` 的正式 HTTP 记忆接口。它不读写 TaskStore，不改变编码任务 execution_status，不在写回失败时重新编码。

## 身份和批准

每个数据库对应一个 QwenPaw 安装，键为 `(role_id, task_id)`。角色来自部署构造的客户端，不接受模型选择。正文仅允许经可信审核服务批准的内容；批准包含 task ID、正文 SHA-256 和 decision ID。`approve` 是内部可信调用，没有为模型注册工具或对外暴露 HTTP 路由。

批准对象和源码类型本身不是身份认证。上层审批服务负责验证操作者/来源证据，不能把模型返回的同形 JSON 转成有效批准。

批准持久化后，正文 hash 和 decision ID 不可改变。同内容、同决定返回现有记录；不同内容或决定返回 needs_review，并登记 approval_conflict 事件，不覆盖原批准或原记忆。原批准没有被自动撤销；需要撤销/更正时应另建明确治理流程，不能把新决定偷换进正在提交的记录。

## 状态与崩溃窗口

```text
未登记 → 可信批准 → approved
                         ↓ 原子事务，网络请求前落盘
                       intent
                       ├─ 官方读回一致 → committed
                       ├─ 不同正文 → needs_review
                       └─ 超时/未知/缺失 → pending → 只读 reconcile
```

- 只有 `approved → intent` 的唯一事务赢家能调用一次 `write_approved`。
- 任何已有 intent/pending 的处理都只能调用 `client.reconcile`，包括服务重启、异常、重复请求和并发观察。
- intent 后、发网络之前崩溃，也不会猜测“还没写”并重发。若上游一直缺失，保持 pending，由人工核对；保守阻塞优于重复或覆盖。
- 上游已写但本地记录结果失败时，intent 保留；重启后 GET 核对并提交 committed。
- 结果按版本 compare-and-set 落盘，迟到 pending 不能覆盖更新的 committed。并发 GET 可能使结果暂时停在 pending，下一次只读核对可收敛，不重写。
- committed 与 needs_review 不再自动发网络写。已提交记录后续更正/删除由记忆治理层管理，不能把提交总账当成无限期不变的正文证明。

`MemoryCoordinator.reconcile(task_id)` 是已有提交的显式只读核对入口：在同一角色记忆锁内，只有 intent/pending 进入原协调器的读回路径；approved 和其他状态原样返回。它不批准新内容，不取得首次写入机会，不启动编码任务。已删除内容的原有墓碑检查继续生效；不能用此方法复活已清空的提交正文。`commit` 的首次提交规则保持不变。

## 初始化与调用

数据库只能显式初始化，已有文件不可覆盖。正常打开和每次连接使用 SQLite mode=rw；丢失数据文件明确报错，不创建空库冒充恢复。总账文件为 0600，启用 WAL。

```python
from agent_line.memory import MemoryCommitStore, MemoryCoordinator
from agent_line.adapters.qwenpaw_memory import MemoryApproval, QwenPawMemoryClient

# 仅首次初始化部署时使用；正常启动使用 MemoryCommitStore(path)。
store = MemoryCommitStore.initialize(path)
client = QwenPawMemoryClient(origin, token, role_agent_id=registered_role)
coordinator = MemoryCoordinator(store, client)

# decision 由已认证的审核服务生成，content_sha256 与经批准正文绑定。
record = coordinator.approve(task_id, approved_content, decision)
if record['status'] != 'needs_review':
    outcome = coordinator.commit(task_id)
```

启动时不要自动 approve 模型输出。`commit(task_id)` 没有批准记录时抛 KeyError，零上游写入。不同角色不能拿同一个 task ID 读取或提交另一角色的批准。

## 验证

`tests/test_memory.py` 先失败再实现转绿，共 11 项：显式初始化/数据库丢失、拒绝无批准内容、真实适配器契约、16 次并发仅一次 PUT、intent 后崩溃、上游已写但回执异常、SQLite 结果保存失败、重启只核对、批准冲突、迟到状态防回退、错误角色回执拒绝、角色隔离。其中多个断言合并在同一测试方法中。

契约测试使用真实 `QwenPawMemoryClient` + HTTP MockTransport，不以假的 coordinator 返回值替代 HTTP 行为。

真实烟测另外连接正在运行的隔离 QwenPaw：

- role：`agent-line-memory-probe`
- task：`memory-coordinator-proof`
- 单独正式记忆文件：`digest/agent-line-task-memory-coordinator-proof.md`
- 持久批准 → intent → 官方 PUT/GET → committed。
- 重新打开同一个 SQLite、构建新 coordinator，复用 committed；再通过官方 GET 核对正文一致。
- 结果：`var/qwenpaw/memory-coordinator-probe.json`，数据库 `var/qwenpaw/memory-coordinator-probe.db`，均 0600。

这条真实烟测证明协调器接上正式适配接口，不证明完整模型经验提炼、候选 UI 或 Codex 后任务自动沉淀已交付。

## 边界

QwenPaw 原生文件 API 没有 compare-and-swap。本总账保证使用同一 SQLite 的受控提交者不重复 PUT，不能防止其他进程绕过协调器直接调用上游覆盖文件。每个安装必须使用唯一权威提交总账；不得为每个 worker 建立不同库。备份恢复仍需先冻结写入并核对备份后的外部状态，不可因旧库是 approved 就自动重新提交。

该实现不具有内置证据签名、正文脱敏、内容纠错、索引失效和删除防复活功能，相关完整 PRD 项仍需独立实现和验收。
