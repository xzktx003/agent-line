# 后台自动字节验收真实探针

入口：`probes/full_workflow/automatic_verification.py`。私有证据：`var/automatic-verification-flow/`。复用现有 QwenPaw 原生 ReMe 父角色及受控 Codex，未修改角色、插件、主配置或主维护门禁。

真实 AuthoringAPI 创建、保存、发布 Start → Agent → 验收条件 → 通过／失败 End，自动发布目录提供绑定。探针自身角色绑定冻结一份受信字节策略；两个运行串行写同一专属文件，分别写正确和错误标记。任务创建、Codex执行、字节验收、结果回传均由现有后台工作线程完成。脚本不调用手工任务登记、验收、回执提交或结果转发方法。

第一个应用通过仅对该 TaskStore 实例的提交方法注入暂拒，制造真实回执已落盘但验收尚未提交的故障窗口。须同时观察：执行成功且真实资源退出、verification pending、原工作区租约仍属于该任务、Sim 未恢复。随后 drain 并关闭探针应用，重建 TaskStore、AutomaticVerifier 与后台线程；重新读取同一证据文件完成提交。证据文件字节哈希及完整回执（含 checkedAt）必须不变，不能重新执行 Codex。

第二个运行故意写错误字节，应原生执行 succeeded 但独立验收 failed，并抵达失败 End；不得将该业务失败描述为业务成功。两次分支以原生 condition.selectedPath 与实际执行 End 数据核对，不能只凭父运行 completed 判断。

重复启动脚本默认只查询原身份，零远端写入。每个外部写操作之前记录私有意图；未知状态不创建新尝试。最终结论以 report.json 为准，failure.json 代表需要保留的未完成现场。

范围仅为声明的字节与大小策略，不代表执行任意测试套件。原生 ReMe 可能随父会话正常自动更新；探针不手工写回或替换记忆系统。

## 已取得的真实证据

`report.json` 为 `passed_live`，两条任务均原生 succeeded，后台独立验收分别 passed、failed。实际执行分支分别为 passEnd、failEnd；失败用例 `businessVerified=false`。

| 用例 | 根运行 | 任务 | 原生完成子运行 |
|---|---|---|---|
| 正确字节／故障恢复 | `3a094983-1f66-5e0b-80b5-ce47f4c632b5` | `task-c98685f9ba3441f29b9aaecfc664b4d5` | `d90516c8-6a92-4d62-b5b0-624bf218528a` |
| 错误字节 | `1ece6f8f-5c14-55cf-96d7-3fe488f55c89` | `task-c9d588af54ea40fda2ce8c7275a7eef2` | `f515f209-f823-4f18-a452-6547de70eb3b` |

首个任务真实回执提交被注入拒绝一次。保存的 faultWindow 证明原任务资源已退出但 verification pending，租约仍属于原任务，原生仍 paused 且没有 resume。重建应用后 `receiptReusedExactly=true`：证据文件 SHA256 和完整回执（包含 checkedAt）逐字节／逐字段一致，只有原先一条 Codex 任务。

最终两父任务 finished，真实容器退出证据已入 TaskStore，工作区租约和 owned active containers 都为零，主维护状态未变。相同触发重放没有新增 native 写入；再次默认运行只读观察原两任务，写入数为零。所有证据 JSON 权限检查均为 0600。

本次仅运行上述两条真实任务，没有更换角色或记忆后端，没有手动验收／回传，没有重启 QwenPaw，也没有扩大主服务准入。
