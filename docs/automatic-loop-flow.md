# 原生循环自动任务探针

入口：`.venv/bin/python probes/full_workflow/loop.py`。首次执行须已完成原生循环守卫现场验证。本探针通过真实 AuthoringAPI 创建、保存和发布一个原生循环；WorkflowGateway 使用自动发布目录，静态 bindings 为空。所有数据库与证据仅放在 `var/automatic-loop-flow/`，不修改主服务、维护状态、正式角色或记忆配置，不手动写回记忆。原生 ReMe 可能随真实父角色会话正常自动更新，不能据此声称记忆正文绝无变化。

同一原生 ReMe 角色与受控 Codex 执行器串行运行两个案例：

- `early_success`：第 0 轮按明确测试指令写错误标记，独立文件校验失败；第 1 轮依据真实前轮验收反馈改成正确标记，校验通过后提前结束，预期 2 个任务。
- `exhausted`：3 轮均按明确测试指令写错误标记，独立文件校验始终失败；达到上限后结束，End 返回最后一轮真实失败结果。流程完成不等于业务成功。

任务登记、每轮暂停绑定、父角色委派和完成回传均由现有后台 worker 执行，探针不手工调用这些步骤。探针仅在原生执行成功且容器释放后运行 ArtifactPolicy，并记录独立验收。每轮保留 context、attempt、repair.previousResult、原生恢复回执、Codex 身份及父任务终态；结束核对原生守卫、动态 End 与最终输出。

已有 state 时默认只观察原 workflow/run，不创建新任务或重发操作。任何意向不确定、模型超时或证据缺失均保留原身份与失败现场，不通过换 ID 绕过。JSON 证据私有 0600。

两组真实模型执行已经通过，完整私有证据见 `var/automatic-loop-flow/report.json`，原始状态与观测分别在 `state.json`、`observation.json`。


## 实际结果

正式发布 workflow：`22fdefa4-08fd-4ba5-bee6-53189ada57a2`，自动目录 binding：`published-a67c5dc63aa30c85130812e8b975d5c061968e56f4432e8d`。

| 案例 | 原运行 | 最终已接受子运行 | 实际轮次 | 独立验收 |
| --- | --- | --- | --- | --- |
| early_success | ac36312b-3bac-580f-a3ae-1e665ff1f648 | bc33ec1d-e3f3-471b-911d-eec4f9be1446 | 2 | failed → passed |
| exhausted | b42d2025-b70a-5c19-aa36-eaaa371c5823 | 49568b08-0566-4e13-ae73-14a358092427 | 3 | failed → failed → failed |

原生 guard 的 `result.needsRepair` 分别为 false 与 true；两组最终动态 End 均精确返回最后一轮任务回传结果。exhausted 的 `businessVerified=false`，仅证明上限控制与失败结果传导正确。

五个原生任务均完成、独立验收均有 ArtifactPolicy 凭据、五个父角色任务均 finished，容器退出证据完整；末尾工作区租约和自有活跃容器均为 0。三个非第 0 轮同时核验：

1. 持久任务输入包含对应前轮 taskId 和失败验收证据，原生恢复回执绑定同一上下文。
2. 官方父任务详情中实际 agent_line_delegate 调用及成功工具回执匹配本轮 taskId 和 native task identity。
3. 委派 instruction 和实际 Codex thread rollout 的 user input 均含前轮 taskId、`sha256_mismatch` 原因及失败文件的 actualSha256。

因此反馈传递证据不依赖模型自述，也不是只根据最终文件内容作推断。全部原生身份、调用与输入证据保存在私有报告中。

主探针进程正常 exit 0。普通再次运行已实测 observation_only，看到同一 5 个任务，新增提交 0、原生写请求 0。JSON 文件权限核对为 0600，py_compile 与 flake8 致命规则检查通过。

首次启动曾在任何状态持久化或远端操作前遇到本地 Path 下标拼写错误；修正为路径拼接后才开始上述唯一两案例执行，没有重投模型任务。

## 三轮真实测试命令持续失败

后续 `probes/full_workflow/test_loop.py` 补齐 AC-20 的真实测试命令场景；上述历史两组是字节校验，不能替代本次证据。根代理先只读核对原模板、两根运行和七个已接受恢复链，确认旧任务已退出及无未知新运行；再复制原专属 journal，复用同一模板发布 revision2。没有修改旧状态或重投旧任务，没有创建新角色、服务、记忆引擎或图调度器。

新运行 `5546dea0-8990-55f8-afb0-a6876d70f05e`，最终子运行 `e3ca7eba-7b15-423d-a983-420863599d7a` 的实际 ID 以 `var/pinned-test-loop-exhaustion/root-check.json` 为准。三次激活、父会话、Codex thread/turn 与执行资源身份均独立。受信策略固定减法文件的字节，并用独立无网络、只读测试容器检查加法：每轮三条 unittest，两失败一通过，实际 exit1。减法是明确授权的故障注入，不是模型修复成功。

| 轮次（从0开始） | Codex 实际行为 | 独立验收 |
| --- | --- | --- |
| 0 | 写入减法文件；后续部分辅助工具不可用，出现exit127 | 文件字节通过，三项测试exit1 |
| 1 | 仅枚举工具，没有项目命令或文件修改；沿用原减法文件 | 文件字节通过，三项测试exit1 |
| 2 | 实际printf写入并用shell输出字节，工具exit0 | 文件字节通过，三项测试exit1 |

两次后续真实 Codex 输入均含上轮任务ID、testId、exitCode=1与实际失败日志行；父委派调用和成功回执、原 rollout 及 SQLite 可核对。发布、运行、任务、父投递、插件绑定五处角色版本一致，测试策略冻结。三个测试容器实际退出并删除，原执行/launcher/Runtime收尾可查，租约0；后台自动发现、验收及回传，无手工登记任务或调用resume。

第三轮后原生 guard 的 `needsRepair=true`，End精确保留最后 `verificationStatus=failed` 结果；没有第四任务。图的 completed 表示循环结束，业务失败是从该真实验证状态派生，不能向原生结果添加不存在的 `businessVerified` 字段。原根日志的部署版本与发布一致；原生恢复子运行的日志端点404，不用它证明终态。原始 selectedOutputs guard/End 已保存供复核。

独立审查与根代理另查实际原生结果、父任务、原数据库、测试回执及原始日志，AC-20已通过。完整证据：`var/pinned-test-loop-exhaustion/{state,report,root-check,root-native-guard,acceptance-review-AC20}.json`。已有状态即使加 `--execute` 也只观察原身份，实际重复0新提交/0原生写入。

这证明三次真实调用与独立测试失败预算截止，不证明每轮都有代码修改、自动修复质量或整个PRD完成。QwenPaw主程序、原角色配置和原生ReMe保持，主配置/守卫未变；原生ReMe仍可随正式会话正常积累记忆。
