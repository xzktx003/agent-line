# 主动核对原执行计划

补 PRD12.2 的 POST /extension/v1/recovery/{task_id}/reconcile 与任务详情中的显式操作。现有查询继续只读本地记录；手动核对复用 Dispatcher.reconcile 的原任务身份、冻结策略、持久状态与资源退出证明，不调用 submit/tick 或新建角色/执行。

原运行凭证按任务项目授权，严格 expectedVersion 防过期操作。固定回执 source=original_execution_reconciliation、taskId、submissionAttempted=false、outcome封闭枚举、task为原public_task结果；gateway缺失或未开始任务不冒充已观察。原后台与手动核对并存，状态冲突固定中文拒绝，不能覆盖终态/身份或释放未知租约。

这是状态核对动作，不是纯只读：原已有取消请求、流程停止或超时策略可能沿原核对路径继续取消/收尾，不授权新编码、独立验收、记忆写入或Sim后继推进。维护/兼容保护保持原边界，不以手动核对绕过新执行准入。前端中文说明靠近按钮，只在可核对的现有任务中提供；请求中禁用，身份/选择/页面变化丢弃迟到结果，失败不自动重发。

仅本仓库service.py、frontend/task-reconcile.js与app.js/runs.html接线和说明，无依赖。根实现，指定gpt-6-luna独立静态复核；不运行测试/Node/HTTP/浏览器/模型/lint/typecheck/build，不提升完整PRD或AC状态。
