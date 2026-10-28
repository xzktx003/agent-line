# 子工作流固定版本的当前源接口缺口

当前补齐路径：原生workflow API历史pin仍缺失，适配层已实现[单层确定性展开及受控保存发布/父预算](subworkflow-admission.md)，不调用原生活动子workflow。尚无嵌套或真实执行验收；下文按时间记录先前仅引用/预览阶段限制，不能据其否定当前源码，也不能据最新代码宣称完整AC33通过。

2026-10-23只读检查当前固定Sim源码，不运行原生调用或测试。`apps/sim/executor/handlers/workflow/handler.ts` 的 executeCore 在 useDeployed 路径调用loadChildWorkflowDeployed(workflowId,origin)，随后从返回值读取deploymentVersionId；loadChildWorkflowDeployed调用readWorkflowDefinitionAsExecutor({origin,workflowId,state:'deployed'})，未接受调用方指定版本。`tools/workflow/types.ts`/executor及tool-runner参数提供workflowId/inputMapping，不是冻结deploymentVersionId。构造历史版本ID本身不能证明真正按该版本执行。

因此不能把当前原生workflow节点直接暴露成已符合AC33的子工作流。需要继续评估正式支持的固定版本扩展点，或在发布时展开已校验的不可变子图并保持来源、输入输出、深度与总预算；后者仍由Sim执行，不能另建推进图的引擎。尚未实施或验收上述方案，当前编译器仍拒绝未支持节点，保持旧版本保护。

JSON模板导入只生成本地可编辑草稿，不等同子工作流调用、递归限制或跨run预算。AC33与完整R17仍未完成。

进一步核对当前v2公开execute契约：lib/api/contracts/v2/workflows.ts的v2WorkflowRunSelectionSchema只接受deployment活动部署或manual已保存草稿；契约明确deployment pinning/source snapshots属于服务端内部选项，不是wire字段。execute路由将async部署运行交给原application operation，没有调用方固定部署版本参数。不能通过在请求中偷偷添加deploymentVersionId来补齐固定子流程。

现已补原项目所有权下版本目录和只读revision快照，详情使用原decode/compile/规范摘要；编辑器可核对旧版本但不执行或恢复。此为后续受控展开的读取基础，不能据此宣称已实现调用固定子流程。见[版本历史](version-history.md)。

2026-10-23补[固定子流程引用核对](subworkflow-reference.md)：精确project/workflow/revision/deployment/hash，父子所有权先验、拒直接自引用；读取指定版本并验证输入/结束输出来源描述，显式executionSupported=false。仅引用读取基础已接通，尚无递归展开、输入输出映射、marker回读或共用预算执行，不提升AC33。

随后实现[单层编译展开预览](subworkflow-expansion.md)：start/end转function入口/返回出口，确定性子节点和输入输出引用、来源marker与完整decode/recompile核验已接通纯源码路径。当前无实际保存/归一化证据，仍拒保存/发布/执行，嵌套深度和共用预算待接；前段“尚无映射/marker”为先前批次历史边界，不能当当前源码或完整执行证明。
