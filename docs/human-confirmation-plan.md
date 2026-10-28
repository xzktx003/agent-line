# 人工确认事项随运行冻结计划

人工节点新增可选question，trim后非空、最多2000字符且无NUL，不配置时保持旧图。复用原AL_HUMAN_V1标记，仅question/TTL任一显式配置才添加；发布humanApproval.questionsByBlock，原纯人审/混合run冻结approvalQuestions。不改审批binding schema、Agent记忆或调度器。

原授权审批详情从该审批对应原运行快照读取，核项目/workflow/run/revision及human block；配置存在返回文字，无字段明确未配置，缺发现能力/账本异常显示不可核对。不根据当前草稿、名字或新发布补造旧问题；不在审批目录、匿名首页或URL增加正文。不新增原生请求，不改变原决定/过期/幂等权限。

Luna前端提供可选textarea与独立能力保护，详情textContent脱敏显示；根backend与另一Luna静态复核，无测试/Node/HTTP/浏览器/模型，只源码/AST。按原空闲维护方式重载不提升AC。
