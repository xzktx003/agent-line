# 恢复子运行的真实状态

Sim 的 resume 回执会给出新的 `resumedRunId`。实际条件异常时，父运行可以仍显示
`paused`，而该子运行为 `failed`；因此只查询最初运行会漏掉真实失败。
本次修改复用现有出站日志与 Sim 查询接口，不修改上游、流程调度或记忆。

## API 与身份

`GET /extension/v1/workflows/{binding}/runs/{rootRun}` 保留原 `execution`，有恢复记录时增加：

- `resumeSubmission`：最新恢复提交的 context、提交状态及已核实子 ID，`safeToResubmit=false`。
- `resumption`：最近已接受恢复的 context、子 ID、`submissionStatus` 和该子运行的实际 `execution`。

最新不明意图和最近已接受子身份按日志意图插入顺序分别读取；不能使用会被迟到网络回执
更新的时间排序。正常日志不删除重建，SQLite 备份保留该记录顺序。较新 intent/uncertain/rejected 不会抹掉
先前已知身份。execute、authoring 和 run-publication 元数据不属于恢复记录。
旧 accepted 回执若没有子 ID，仍保留 accepted 语义，但子执行显示 unknown。
回执中的工作流、原运行或 context 冲突时，不跟随其中的子 ID。

先核验项目权限、已知原运行和发布绑定，再从可信日志取子身份，使用相同工作流地址查询。
SimClient 对显式工作流/运行身份冲突、错误信封或无效响应返回 unknown，不泄露该错误
响应中的内容。子查询失败保留可读的父状态及原身份，绝不补发 resume。
本接口只增加至多一次子 GET；不改变数据库、不登记新任务，也不将子状态覆写成父状态。

## 工作台

工作台分别显示原运行状态、最近恢复提交、恢复执行状态、恢复运行标识及输出。
若最新恢复提交结果不明，未知提示优先，旧子记录明确为“此前”记录；没有自动重试操作。
恢复子运行失败时使用中文失败提示，即使父运行仍“等待恢复”。执行结果与独立验收仍分开。

## 验证

`tests/test_workflow_resumption.py` 的 16 项测试覆盖父子状态、未知回执、重开与维护只读、
元数据排除、旧回执、项目和身份冲突、403/404 及 GET 防重；两个并发回归以事件控制
旧请求迟回，不让其覆盖较新未知意图或已接受身份。真实浏览器结果见
[界面验收](workflow-resumption-ui.md)。

临时探针服务 `probes/sim/resumption_ui_service.py` 将上一轮真实 received 的 execute/resume
回执明确导入独立测试日志，来源与哈希保存在 `var/resumption-browser/receipt-import.json`。
导入前真实 GET 确认父 paused/子 failed；没有新 execute/resume，也没有生成新的业务身份。
该导入是历史证据装载，不能作为正常恢复、自动故障核对或新提交验收的替代。
`prepare` 不允许覆盖已有导入；浏览器只允许 GET/HEAD。临时服务验证后停止。

本轮尚不证明完整恢复链遍历、循环激活接线或暂停/取消策略。父子查询不会让 Python
接管 Sim 调度，也不会凭一个子结果声称整个复杂流程完成。
