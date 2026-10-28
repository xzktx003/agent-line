# 基础模块验证记录

当前实现仅为PRD的一部分。执行命令：`.venv/bin/python -m unittest discover -s tests -v`，2026-10-21 共42个测试通过。`flake8 --select E9,F63,F7,F82` 与上游完整性检查通过。测试结果不能证明真实执行和全量产品完成。

| 测试文件 | 数量 | 行为证据 | 未覆盖的产品环节 |
| --- | --- | --- | --- |
| test_task_store.py | 16 | 并发去重、固定身份、丢库拒绝、父子工作区互斥、准入事务、取消不冒充停止、记忆状态独立、事件分页 | 真实上游进程状态、审批、实际记忆、身份迁移 |
| test_service.py | 7 | 实际HTTP测试客户端认证、项目隔离、合法配置解析、能力未知阻止准入、输入冲突 | 正式插件到服务身份交换、执行worker |
| test_sim_client.py | 9 | 模拟传输超时/冲突/404/错误信封/跳转不能自动重发；HTTP202不算完成 | 真实Sim调用、Human暂停恢复、反向代理 |
| test_upstreams.py | 5 | 实际临时Git仓库版本/远程/源码检查且不改动用户文件 | 升级版本兼容、数据库回滚 |
| test_plugin_contract.py | 1 | 真实QwenPaw Loader注册、认证、卸载及不兼容拒绝 | 完整宿主浏览器体验与执行链路 |
| test_backup.py | 4 | SQLite在线一致快照、校验和、禁止覆盖现有库、恢复保留锁且关闭新执行准入 | QwenPaw/Sim全产品备份、外部代码副作用对账、解除恢复隔离的完整审核流程 |

## 数据库初始化与备份

`TaskStore.initialize(path)` 是显式首次安装；`TaskStore(path)` 正常打开且拒绝丢失/未知版本。当前格式为2。格式1不得自动重建，后续需显式迁移和验证。正常业务不会自动清理数据或秘密。

`create_backup(database, destination)` 使用SQLite backup接口，保存独立目录内的tasks.sqlite和manifest.json，校验和用于损坏检测，不是来源签名。备份含任务输入，目录0700、文件0600。

`restore_backup(bundle, new_path)` 只能写新目标；校验完成后写入持久admission_guard。恢复任务可读，执行领取禁止；旧活动身份和工作区锁继续保留。恢复数据不能回滚项目代码和原生进程，也不证明可以继续执行。尚未提供自动解除隔离入口，必须在后续实现的对账流程完成前保持禁止准入。

终态记录仅由可信执行适配器调用，业务HTTP没有暴露finish/accept/资源退出方法。终态证据不等于测试验证，verificationStatus不会根据模型声明自动通过。

## Sim outbound mutation journal

`agent_line/sim_journal.py` persists a mutation intent before calling the Sim
adapter. A repeated workflow/run/context identity returns the stored state;
changed input is rejected. Neither a lost response nor process restart retries
an intent. Start and each pause context have independent durable identities.

`tests/test_sim_journal.py` covers concurrent duplicate submission, timeout plus
reopen, process interruption after durable intent, changed input, independent
resume contexts, and missing storage. These are local tests with a mocked
transport, not evidence of real Sim workflow execution. Deployment must use one
journal per Sim installation; instance binding, read-side reconciliation, and
integrating this journal into the live workflow entry point remain outstanding.
