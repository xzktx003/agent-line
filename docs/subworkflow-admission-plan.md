# 固定子流程保存发布与父运行预算计划

接通已实现单层展开图的受控保存/发布，而非继续只返回预览。层级上限明确1，nested/batchItem/多end仍拒绝；不是完整AC33交付。保存/发布前在原authoring锁内核所有引用同项目/父子_owned、指定版本/hash/来源文档一致、原author-publish回执存在；当前角色策略须与子版本原冻结agentPolicies规范相等，角色变化不静默替换。要求项目明确runBudget，不改正式配置。

新externalTasks可选subworkflowDepthLimit=1，含子图时必须至少有Agent，编译图/来源marker保留为唯一行为定义。运行注册冻结同flag与父runBudget，预算缺失拒启动；所有展开Agent沿父workflow/run登记，原提交额度/截止/租约共用，不增加子run、调度器或记忆权威。

publication校验增加显式子流程分支，decode/compile全图、所有wait白名单、loop/staticParallel声明准确、深度1及预算flag；原无子流程路径明确拒wrapper绕过。verify_run准确核原发布及实际日志图，保留原归一化范围，只新flag开启此compiler子集，不放宽任意JS。旧运行snapshot字段省略不变；重放核新flag/原预算不混用。

根负责subworkflow_admission.py与authoring_api.py；Luna负责publication.py/workflows.py/approval_discovery.py原运行链；另一Luna静态复核。模板跨安装仍拒子图wrapper，前端集成另行继续，不修改当前实例预算/就绪/上游/记忆。只源码/AST，不运行测试/Node/HTTP/浏览器/模型/lint/build；维护加载不提升AC或声称真实执行通过。
