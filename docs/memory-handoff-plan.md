# 任务详情到记忆审核页交接计划

## 目标

为已确认结束且执行资源已释放的任务提供同源记忆审核入口；审核页通过当前用户独立提供的记忆凭证，严格解析非授权用途的 URL fragment，并重新读取唯一匹配的来源任务和原审核记录。审核页可把经核验的任务元数据整理成待人工补全的经验候选草稿，不自动提交或授予技能权限。

## 接口与身份约定

- 任务详情链接使用 `./memories.html#source-project=<id>&source-binding=<roleBindingId>&source-task=<taskId>`。fragment 仅含这三个唯一键，ID 按服务端路径段格式校验；任务 ID 使用 `task-[0-9a-f]{32}`。
- `GET /extension/v1/memories/bindings` 返回 `bindings` 数组，条目包含 `bindingId`、`projectId`、`roleBindingId`、`roleAgentId`。必须按 `projectId + roleBindingId` 找到且仅找到一项，再使用其 `bindingId` 读取来源。
- 来源详情由 `GET /extension/v1/memories/sources/{bindingId}/{taskId}` 读取，既有审核记录由 `GET /extension/v1/memories/{bindingId}/{taskId}` 读取；两者均须核对响应身份与目标项目/角色/任务一致。任何缺失、多义、无权限或响应不符都停止跳转读取，不回退到默认角色。
- 来源详情的 `source.evidenceSha256` 必须为 64 位小写十六进制字符串，才能生成经验候选；缺失或无效时保留原手写审核能力。

## 实施范围

1. 在 `frontend/app.js` 中，仅当 `executionStatus` 为 `succeeded`、`failed` 或 `canceled` 且 `resourcesReleased === true` 时添加同源审核链接。根据 `memoryStatus` 是否已提交选择文案，URL 不携带凭证、输出、正文、证据或路径。
2. 在 `frontend/memories.js` 中读取并严格校验 fragment，首次读取后立即用 `history.replaceState` 清除；连接后精确解析角色绑定并调用现有 `select(binding, taskId, {})` 强制读取来源与历史。目标失败时显示固定中文提示并清空该跳转选择，绝不使用别的绑定。
3. 延续现有 epoch、AbortController、原正文清除、tombstone 与未知 POST 结果保护；登出清理 URL、所选记录及草稿，不使用 session/local storage。
4. 接入 `#memory-build-experience` 与 `#memory-experience-help`：只有任务来源已核验、可审核、未忙且证据哈希合法时可生成草稿。生成仅填充 textarea，先确认覆盖未提交正文，清除 consent，不发请求。候选包含原项目、角色、任务标识与经验结论、适用条件、失效条件、适用版本、操作边界字段，并以独特待填写占位提示人工补全。未补完占位时按钮与 submit handler 均拒绝提交；提交仍要求显式 consent。

## 静态检查点

后端增量：来源详情返回顶层 `evidenceSha256`，审核请求可带 `expectedEvidenceSha256`。重新读取、摘要核对及审核快照登记共用既有任务验收锁，登记后释放再访问原生记忆；已有审核重放沿用原快照。仅在本仓库修改 API，不改 QwenPaw。

- 仅改动本计划与获分配的 `frontend/app.js`、`frontend/memories.js`；不改 HTML/API/其它文件。
- 审阅链接条件、fragment 键集合与唯一性、ID 格式、绑定唯一映射、响应身份核对、迟到响应隔离、退出清理、草稿字段与提交守卫。
- 按仓库当前要求只做源码阅读和 AST 静态审查；不运行测试、Node/browser、lint、模型或 probe。
