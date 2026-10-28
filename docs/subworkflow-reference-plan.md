# 固定子流程引用与接口核对计划

完整AC33仍要求真实固定版本执行、深度和总预算；当前不宣称完成。原Sim workflow节点不能固定历史部署，后续选编译展开，复用原function marker承载来源并在同一父run内执行。先实现可复用的严格版本引用读取边界并接入授权只读API，不新增执行入口、数据库或调度器。

引用精确包含projectId/workflowId/revision/deploymentVersionId/graphSha256，revision严格安全整数；拒未知键/错项目/父流程自引用。先核父子项目所有权，再读取指定revision而非活动部署；核原id/revision/graphhash，解码重编规范图，不把当前isActive当执行资格。返回原引用、固定文档与明确输入/每个结束节点输出来源描述；Agent来源仅描述其真实outputSchemaId/可选outputFields，join只描述原汇合输出，不合成业务通过结论。

子图按现有200节点/1000边编译边界与256KiB规范文档限制核验。接口显式readOnly和executionSupported=false，不能据此新建/保存/发布/启动调用。原版本历史API暂不改动，后续编译器消费同一精确引用校验函数；旧source边界文档更新为引用基础已实现、展开及marker回读尚未接通。

根负责subworkflow_reference.py、authoring_api.py与说明；指定gpt-6-luna只读静态审查。仅AST/源码，不测试/Node/HTTP/浏览器/模型/lint/typecheck/build，完成后按现有维护保护方式加载只读接口，AC及完整PRD不提升。
