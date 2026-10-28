# 恢复必要测试（2026-10-23）

用户明确恢复测试，覆盖之前不自动运行的安排。实现继续优先复用原仓库、官方接口和薄适配；测试按风险和改动范围选择，不默认每次全量，不恢复已删除的冗余测试。

## 本轮实际结果

解释器为本仓库 `.venv/bin/python`，`PYTHONPATH=tests`；临时文件通过 `TMPDIR` 放入仓库私有 `var/testing-resumed-20261023/tmp`。

| 批次 | 模块 | 实际结果 |
| --- | --- | --- |
| 核心接线 | test_task_store、test_sim_journal、test_run_control、test_approvals、test_memory、test_qwenpaw_memory | 94项，1.124秒，OK，退出0 |
| 删除/备份/升级门禁及静态入口 | test_memory_deletion、test_qwenpaw_memory_deletion_client、test_memory_governance_plugin、test_backup、test_upgrade、test_workbench | 60项，13.328秒，OK，退出0 |

执行命令形式为：

```sh
TMPDIR="$PWD/var/testing-resumed-20261023/tmp" PYTHONPATH=tests .venv/bin/python -m unittest test_task_store test_sim_journal test_run_control test_approvals test_memory test_qwenpaw_memory
TMPDIR="$PWD/var/testing-resumed-20261023/tmp" PYTHONPATH=tests .venv/bin/python -m unittest test_memory_deletion test_qwenpaw_memory_deletion_client test_memory_governance_plugin test_backup test_upgrade test_workbench
```

共154项既有测试实际通过。没有新增测试、运行依赖、业务功能或架构层；没有修改上游、正式配置、任务或记忆，没有重启服务。测试使用临时SQLite、内存ASGI客户端、MockTransport/原生动作替身；升级门禁使用临时本地Git fixture，不拉取远程或切换正式版本。插件测试导入已安装QwenPaw，初始化日志不是实际浏览器或模型执行证据。

## 证据范围

以上证明所选当前回归用例通过，不证明全部源码、最近所有增量、视觉界面或完整PRD通过。记忆测试不证明原生ReMe索引已清除或真实跨项目召回隔离；备份测试不证明整套安装恢复；升级测试只验证门禁，不执行真实升级。55项验收总账不因本批单元/本地集成测试而提升。

AGENTS.md、README、requirements与原生复用政策已同步恢复测试约定；旧阻塞审计改为历史记录，连续阻塞计数清零。完整目标为active，仍保留原生隔离、只读执行与安装级升级恢复的未完成范围。后续根据必要的实际验收推进，不继续用“禁止测试”作为阻塞理由。
