# 可信产物验证

`agent_line/verification.py` 读取实际工作区产物，按部署提供的 SHA-256、可选精确字节数和读取上限进行校验。它不采用模型返回的 testsPassed、完成文本或预期 hash；预期值必须来自可信测试/部署配置。

## 调用契约

```python
from agent_line.verification import ArtifactPolicy, verify_artifacts

receipt = verify_artifacts(
    task_store,
    task_id,
    workspace=registered_workspace,
    policies=[ArtifactPolicy('proof.txt', expected_sha256, exact_size=expected_size)],
    evidence_dir=extension_artifact_directory,
)
```

必须至少一条 policy，不允许空集合通过。任务从真实 TaskStore 读取，必须 executionStatus=succeeded 且 resourcesReleased=true；绑定 workspace 必须匹配，完整原生 executionIdentity 必须存在。校验后再次读取任务状态和身份，禁止按已变更的身份记录证据。

`verify_artifacts` 只返回并保存 receipt，**不修改 TaskStore verificationStatus**。主线可信验收服务可以调用 `store.record_verification(task_id, receipt)`；不应将该写操作暴露给模型或无验证的 HTTP 请求。

当前已有自动接线：现有 DispatchLoop 调用 AutomaticVerifier，依据服务端冻结的字节策略保存并核对回执，再提交 TaskStore。原工作区租约一直保留到验收提交；提交前应用退出可复用原回执恢复。配置及限制见 [后台自动验收](automatic-verification.md)，真实成功/失败与故障恢复见 [探针记录](automatic-verification-live.md)。下文底层接口和早期手工验证记录保持其原有范围，不代表运行测试套件。

## 路径与读取边界

- 每条路径为严格相对 POSIX 路径，拒绝绝对路径、`.`、`..`、反斜杠、空段与 NUL。
- 根目录与每层子目录通过目录 FD、O_DIRECTORY 和 O_NOFOLLOW 打开；文件也使用 O_NOFOLLOW，拒绝符号链接，不仅检查最终字符串前缀。
- 使用 O_NONBLOCK 避免 FIFO 阻塞，fstat 必须证明是普通文件。
- 按上限逐块读取，不一次性加载任意大文件。
- 比较读取前后 inode/size/mtime/ctime，且重新 stat 当前名称，拒绝读中变化或被替换的路径。
- 缺失、符号链接、目录、超限、hash 或 size 不匹配均产生 failed 检查，不冒充测试通过。

这不约束检查完成后的外部手动修改，也不是完整操作系统沙箱。receipt 证明的是指定时间、指定原生任务关联产物在这组检查中的事实。

## Receipt 与证据文件

结果包含：`kind=verification`、schemaVersion、taskId、完整 executionIdentity、checkedAt、status、checks、scope、evidenceFile。每个 check 提供路径、期望 hash/size、实际 hash/size（能读取时）、状态与原因。receipt 不复制产物正文。

证据保存到显式准备的扩展 artifacts 目录，目录路径本身也拒绝符号链接。临时文件以 O_EXCL、0600 创建，文件 fsync 后原子 rename，再 fsync 目录。落盘失败不会返回一个声称已保存的成功回执。

## 测试与真实结果

`tests/test_verification.py` 共 8 项，先红后绿：真实文件读取及精确 receipt、错误产物不采纳模型声明、文件/父目录 symlink、绝对路径/逃逸/空政策、任务工作区/资源前置条件、缺失/目录/超限、FIFO、证据目录 symlink。测试确认 verifier 本身不改变 TaskStore 状态。

真实隔离任务 `task-b80a131c24f04608ac159a7d6d71e4be` 的 `dispatcher-proof.txt` 按可信期望字节 `AGENT_LINE_DISPATCHER_OK\n`（真实换行）检查 passed。使用持久任务中绑定的正式工作区，读取实际文件，没有从模型报告推断内容。

证据文件：`var/artifacts/task-b80a131c24f04608ac159a7d6d71e4be-26db720268f94e16981a455c30c3abe1.json`。生成当时原任务 verificationStatus 保持 pending，交主线独立集成 record_verification。

本次只证明 dispatcher 示例文件的字节、大小及身份绑定；不证明完整代码正确性、测试套件通过、PRD 全部产物验收或审查通过。后续命令测试应使用服务端登记命令和独立可信退出状态，不将任意模型 shell 字符串直接执行。
