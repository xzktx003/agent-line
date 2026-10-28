# Coding Kanban只读会话联动

任务详情新增默认折叠“Kanban会话摘要（只读）”。明确点击读取后，复用管理员登记的本地Kanban源，只读取其已有会话目录，不启动、注册、恢复、重连、接管或控制任何会话。不增加首页连接表单，不自动轮询，不改变本项目的执行、验收或记忆状态。

## 本轮范围与启用条件

当前正式项目未登记Kanban源，默认返回not_configured，不发送请求。本轮未改正式配置，没有猜测端口、复制凭证、扫描/启动Kanban或发起HTTP验证。实现已提供接口和UI，不代表本机实际联动已完成验收。

已有项目配置可选增加以下片段，端口仅演示格式，必须来自管理员确认的目标监听，不是本机实际地址：

```json
"kanbanReadOnly": {"baseUrl": "http://127.0.0.1:3210"}
```

只允许exact baseUrl字段、http、数字loopback地址与显式端口；禁止DNS名称、嵌入凭证、路径、查询或片段。::1需IPv6括号形式。不转发本项目运行Bearer，不借用用户/浏览器账号，不读取全局配置或环境代理。目标若要求另一个认证机制，本适配当前明确不可用，不尝试绕过。

项目的Codex thread必须已存在于目标Kanban目录，并且登记为明确本地Codex、相同原生会话ID与同一授权工作目录。容器中的Codex任务不一定在Kanban登记；缺少关联时只显示未观察，不自动导入或创建会话。仅支持这个已存在记录的只读联动范围，不承诺导入所有会话、跨项目聚合或当前回合结果。

## 源接口与只读性

本机Kanban `GET /api/agent-sessions` 调用 `registry.list()`，只读取已有Map、排序副本并生成目录时间，不走会更新会话的task-summary或tmux详情路径。契约来自本机源码，不是本轮HTTP验收，也不推断未来版本无需适配。

原目录接口返回全目录，适配器只在有界临时内存中处理，再输出本任务匹配项的白名单；不存原文、记录其他会话或返回原目录。只发固定目录GET，禁重定向、环境代理与重试，显式要求不压缩JSON。异步I/O使用7秒timeout，连接/读取有独立上限；解析与投影受2MiB、2000项限制，并在阶段后检查截止时间，不承诺CPU硬实时终止。拒重复键、非有限数值、非对象条目及未知格式，异常只返回固定原因。

## 归属与响应

`GET /extension/v1/node-tasks/{task_id}/kanban?expectedVersion=…` 沿原运行Bearer和项目范围。版本不符先409；无源/原Codex会话ID时不请求。读取后再次核对任务版本和执行身份，变化则丢弃结果。

只匹配 `agentSessionId == codexThreadId`、`workingDirectory == 授权工作区`、sourceType=local、agentKind=codex、hostId缺省或local，无SSH目标及SSH传输标记。没有匹配为not_observed；多个符合项为ambiguous，不选择最近一项、按名称替换或注册新项。

响应固定来源coding_kanban_readonly，readOnly=true、executionSupported=false、coverage=session_directory_snapshot、taskTurnMatched=null、resultAuthority=unverified_session_summary。只包含原任务/项目/版本/原Codex身份、读取与目录生成时间及选中会话的小投影。未配置not_configured、缺身份identity_unavailable、无匹配、歧义均不包含session正文。

匹配项仅保留登记ID、原连接/交互状态、已有lastAgentMessageSummary、其taskSummaryUpdatedAt及截断标记。排除其他会话、终端/outputPreview、SSH、命令、工作目录和源地址的结构字段。摘要是此授权会话的自由文本，可能提到任务相关文件、命令或其他内容；不能保证全面语义脱敏。当前运行凭证在全文截断前移除，最长显示4000字符，不展示原终端记录。

目录生成时间只代表列表读取时刻，不是摘要或执行的最新时间。摘要时间取原taskSummaryUpdatedAt，缺失保持未知。会话可能有多个turn，匹配thread不等于摘要属于当前任务回合；空闲/退出、摘要正文均不能证明本项目执行成功、独立验收通过或记忆写入成功。

前端核对任务/项目/版本/原Codex身份、来源、状态和结构，切任务/身份/重绘后丢弃迟到回执。没有新服务连接流程、浏览器存储、自动任务或调度器。

## 证据边界

指定gpt-6-luna实现reader并独立审阅，根集成/复核与2个Python AST解析。没有测试、Node、HTTP、浏览器、模型、lint/typecheck/build或实际目录读取。本轮不修改Kanban仓库或正式配置，不把源码、部署或not_configured结果当作实际P2联动验收，完整PRD及AC计数保持未闭环。
