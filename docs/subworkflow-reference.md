# 固定版本子流程引用核对

这是子流程编译展开的前置读取接口，尚不能添加或执行子流程节点。完整能力仍需输入输出映射、来源marker无损回读、确定性展开、递归深度和共用运行预算接线；AC-33仍未验证。

当前代码状态：单层子图已接[受控保存/发布与父预算](subworkflow-admission.md)，引用接口本身仍只读且不执行。来源核验不等于运行准入；尚无完整嵌套或真实端到端验收。前述“尚未接线”是原引用批次历史状态。

后续源码已补[单层编译展开预览](subworkflow-expansion.md)，具备入口/返回出口转换、映射和marker重编核对；实际状态保存回读与执行仍未开放，下文“尚未实施”为原引用接口批次的历史说明。

授权只读POST `/extension/v1/authoring/projects/{project}/subworkflows/resolve` 接受：

```json
{
  "parentWorkflowId": "parent_workflow",
  "reference": {
    "projectId": "project",
    "workflowId": "child_workflow",
    "revision": 2,
    "deploymentVersionId": "deployment_id",
    "graphSha256": "替换为版本详情返回的64位小写十六进制摘要"
  }
}
```

reference仅允许上述五个字段，revision为1–9007199254740991严格整数；同项目、父子各自本地创建所有权先核对，父流程自引用拒绝。通过后只读取指定revision，deploymentVersionId和规范图SHA须全部一致，不按名称、活动部署或最新版本替换。版本状态须能被原decode/compile完整识别，规范文档不超过256KiB，节点/边沿原200/1000边界。读取后再次核对编辑身份与项目授权。没有新写日志、发布、执行、角色创建或记忆动作。

返回source=verified_fixed_subworkflow_reference、readOnly=true、executionSupported=false，含原reference、parentWorkflowId、isActive、documentSha256、原document和节点/边计数。isActive只是当前版本活动标记，不授予运行资格。

input描述原唯一start的object/input端口。outputs逐项描述每个end：literal_object只表示固定JSON对象，具体值在原document；result_reference明确sourceNodeId/sourceType及原响应包装字段agentLineResult。若来源为Agent，额外描述其outputSchemaId和可选outputFields；这些是源节点正文契约，不是整个结束响应schema。join来源只描述原joinPolicy，不伪造Agent结构或独立验收通过。

接口不做输入映射、不推断哪一个结束节点会运行、不展开图、不启动任务。POST仅承载只读结构化请求，响应no-store；请求仍使用原Authorization头，凭证不进入引用、URL或快照。它不替代发布前角色/权限/执行能力校验，也不把未来子流程预算当作已接通。

后续方向：在共同compile/decode语义中使用已知Sim function marker保存调用来源和固定文档，确定性重命名内部身份；消去子start并应用输入映射，转换子end并接回父后继，不能直接复制trigger/response导致提前结束。全部展开Agent沿父workflow/run登记，复用原租约、冻结角色和runBudget，不创建子调度器或新的记忆权威。旧图编译保持不变，未知marker拒绝覆盖。该部分尚未实施，不以本说明代替代码。

仅源码/Python AST和指定gpt-6-luna静态审查；未运行测试、Node、浏览器、HTTP、模型探针、lint/typecheck/build。接口实际行为及完整子流程均未验收，PRD与AC计数20/22/13保持。
