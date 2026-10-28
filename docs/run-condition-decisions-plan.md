# 条件实际命中只读记录计划

PRD7.4/13.4要求查看实际分支。固定Sim condition handler返回selectedOption/conditionResult/selectedPath，原运行GET selectedOutputs只能按block选择，不能按输出字段。复用已授权固定publication/run身份与原get_version图hash校验，单次最多20条件、响应流最多1MiB；临时读取有界块响应，只提取决定元数据，不返回/保存其来源正文、selectedTitle或工具数据。

按冻结逻辑nodeId稳定分页，原生id映射并核准确run/workflow。只有ruleId、Boolean结果、原生目标和原图唯一branch边全匹配才matched；缺输出（含权限隐藏）为unobserved，异常/不一致为unverified，不当未执行/false。输出仅当前原生块视图，不声称全部历史激活或时间线，不新建状态库或推进图。

前端原运行图内默认折叠、显式读取，分页/刷新/身份/图/退出代次隔离；核身份和原图边后展示文字，不给箭头染色伪造完整路径。本地自动刷新、原生状态按钮不触发这个新读取。

Luna独立frontend，根backend与另一Luna只读复核；无测试/Node/HTTP/浏览器/模型/构建，仅源码/AST。维护重载仍只验证空闲/备份/进程与摘要，不提升AC。
