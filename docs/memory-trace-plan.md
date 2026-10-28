# 原生召回与委派快照追踪

继续PRD10.5/13.5/R16，复用原RoleDispatchLedger已保存的父任务观察快照，新增原任务授权只读GET与手动展开摘要，不主动GET上游，不写事件/状态，不建记忆正文库或调用模型。

严格核task/role/session/deployment及parent task_id；只解析原生plugin_call/output的memory_search和agent_line_delegate，按call_id配对、拒绝重复/悬空/过大/非法结构，不将模型叙述当工具证据。查询参数/结果/委派正文只出SHA256指纹，不暴露内容/路径/秘密；展示调用顺序与匹配当前upstreamTaskId的委派回执。先召回后委派仅是顺序，memoryInjection始终unverified。snapshot时间原账本未存则null，本地readAt不能冒充抓取时间。

缓存缺失或无法核对保持unknown，计数不当零；缓存子集不证明未执行/全部记忆。任务详情默认折叠，显式读一次，换任务/身份/重查使迟到失效；原生正文与角色记忆继续由QwenPaw管理。root service/frontend，Luna parser/gateway，Luna静态复核。

不改插件/上游/原生数据、执行提示或准入，不添加依赖/调度器。无测试/Node/HTTP/浏览器/模型/lint/build；维护重载保留原数据，不提升AC。
