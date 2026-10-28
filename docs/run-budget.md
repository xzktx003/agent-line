# 工作流运行执行额度

本轮已实现代码接线，仅做源码和AST静态审查；按用户要求没有新增或运行测试、浏览器、真实模型或探针，也未重载主服务/修改正式配置。AC-66仍为in_progress，不能把本说明当成实际运行通过证据。

## 配置

在已有service.json的项目记录中可选配置：

```json
{
  "workspace": "/absolute/project/workspace",
  "runBudget": {
    "maxSubmissions": 20,
    "maxExecutionSeconds": 7200
  }
}
```

沿用既有私有0600服务配置。maxSubmissions须为1–10000的严格整数，maxExecutionSeconds须为1–604800的严格整数；未知键、布尔、字符串和显式null均拒绝。省略runBudget保留原行为。配置值由服务端提供，节点HTTP请求不能自行增加或替换runBudget。

## 范围和冻结

同一projectId、workflow.id、workflow.runId共用一份额度；节点、角色、发布revision和attempt变化都不新开额度。原WorkflowRunLedger在流程启动登记时将项目策略复制到不可变snapshot，包括未配置的情况；已登记但尚未到达首个Agent节点的运行也使用原快照。TaskDiscovery从该运行快照继承，直接node-task POST如果能找到同一运行记录，也必须沿用其快照并校验项目/流程/run身份。

没有Sim运行记录的直接节点任务，以首个任务登记冻结项目默认策略。后续任务继承原TaskStore声明；从有预算改成无预算、反向混入和其他策略差异均不能静默发生。TaskStore注册事务再次核对冻结值，避免并发读取策略后分别建立不同额度。已有无预算运行保持无预算，不回填或重写历史。

## 原子消耗和时限

节点的单独limits继续生效，runBudget叠加在其上。claim原事务同时检查两类限制、FIFO和租约，并原子写入submission_intent；后续节点、重试、循环和并行分支都永久消耗同一运行额度。失败、提交结果未知、取消后均不退款；确实在提交前取消才不消耗。额度读数是节点执行提交次数，不是父角色或Codex实际模型调用次数。

受理前明确拒绝、任务回到 registered 且尚未受理时，原来的 `submission_intent` 仍计一次，起始时间不改。显式修正后的再提交只解除这次拒绝，不新写 `submission_intent`。同一运行中的重试、循环和嵌套节点继续消耗剩余次数与时限。用量字段仍保持未知，不能填零。跨 run 的嵌套总预算仍不在本次范围内。

执行阶段时限从本运行首次submission_intent开始计算，后续节点不重置；不包含首个受控任务提交之前的原生图运行和等待时间，因此不是整个Sim图的TTL。已到期时拒绝新任务，原巡检先观察正在执行的任务，再请求取消；未知取消不标已停止，不释放租约。已确认终态的产物保留。轮询和原生传输可能带来取消延迟，不保证截止秒内进程已经退出。

历史事件缺失、乱序、重复或时间戳非法时返回未知并阻止claim，不能当作已用零次。只有两类额度账本都明确且任务未启动、无租约时，确定耗尽或到期的队头才可让出排队位置；其他未知、暂停和角色版本阻断仍按既有策略保留。实现复用原schema2的payload/events，没有新表、状态库、依赖或调度器。

## 中文展示与用量边界

授权任务详情增加runBudget投影：当前作用域、submissionsUsed、maxSubmissions、maxExecutionSeconds、deadlineAt和admissionAllowed。admissionAllowed只表示本运行额度检查，不代表全部执行准入已开放。工作台沿用原详情列表，显示运行额度及耗尽/到期原因；投影作用域不匹配或非法时显示待核对，旧后端缺字段时隐藏。

usage.tokens、modelCalls、cost保持null。固定QwenPaw的token-usage详情按日期/provider/model/agent聚合，缺少可靠task/session/thread/turn关联，不能据此给单任务填写精确用量或费用。父角色准备/总结和Codex的可信逐任务计量仍待接入；订阅CLI无可靠逐次账单时不估造成实际费用。本功能尚未完成token/金额硬预算、子工作流跨run总预算或完整AC-66验收。

## 当前交付状态

已静态核对服务配置、工作流运行快照、任务发现、直接任务入口、TaskStore原子准入、截止取消及中文展示的接线；独立Luna源码审查补齐直接POST快照继承和未知账本队头不得跳过的边界。六个Python文件仅做AST解析，JavaScript仅阅读；没有执行用例或请求真实接口。当前主进程仍为上批部署，未启用runBudget，原任务和QwenPaw记忆未变更。后续需要用户明确要求才运行测试，完整PRD继续追踪。

## 后续运行级展示（2026-10-23）

已有运行查询新增冻结预算摘要，复用当前原额度账本，不用项目最新配置重写历史。详见 [运行额度摘要](run-budget-summary.md)。本节不改变前述历史验证状态；当前仅源码/AST与静态复核，未执行测试或业务请求。

后续单层子流程采用[同父run编译展开](subworkflow-admission.md)，要求发布/新运行有明确项目预算，depth和预算一起冻结；展开节点计入同一额度，恢复沿旧ledger，不新建子run或声称解决任意原生跨run计量。当前只有源码接线与维护加载，未提交真实子流程，前文未实现/未加载为对应历史批次记录。

## 计量延迟超额（2026-10-28）

运行额度摘要在提交次数超过冻结上限时不再把剩余次数夹成 0。`remainingSubmissions` 等于上限减已用，可以为负；页面显示实际超出次数，并说明不能据此把活动进程标成已停止。未超额的剩余次数保持上限减已用。详见 [运行额度摘要](run-budget-summary.md)。新调用的准入拒绝、未知用量和取消确认规则不变。

## 并发预留（2026-10-28）

多个领取可以同时读到同一剩余次数。`admissionAllowed` 只是这个读数，不是放行。真正预留仍在 `claim` 的 `BEGIN IMMEDIATE`：检查通过后写入一条 `submission_intent`，事务提交后其他领取才继续。因此同一运行不会一起超过 `maxSubmissions`。没领到的任务保持 registered，队列原因是 `run_submission_limit`。额度用尽后的新登记仍然成功，只在领取时拒绝。token、模型调用次数和费用仍未知，不设成硬限额。取消确认和其余资源生命周期没有改。回归见 `tests.test_execution_limits` 的并发运行额度用例。适配器未重载，PRD-11.9 仍 unverified。
