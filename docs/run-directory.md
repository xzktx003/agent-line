# 工作流已登记运行目录

工作台的运行目录用于找回已有运行ID。数据来自当前Sim提交账本，选择记录后填入原查询表单，用户再点击“查询运行状态”读取原生状态。不会因浏览目录重新启动、恢复、重试或控制流程。

接口：GET /extension/v1/workflows/{bindingId}/runs，沿用原工作流服务认证。可选limit为1至100（默认50）；after为上一页返回的nextCursor，没有游标时读取最新一页。响应示例：

```json
{
  "bindingId": "binding-demo",
  "projectId": "project-demo",
  "workflowId": "workflow-demo",
  "source": "local_submission_journal",
  "executionStateIncluded": false,
  "runs": [
    {"runId": "run-demo", "workflowId": "workflow-demo", "submissionStatus": "accepted", "updatedAt": 1792713600}
  ],
  "nextCursor": null
}
```

示例只是格式说明，不是实际任务或运行证明。submissionStatus表示本地提交状态；accepted不等于运行中、执行成功或验收通过。updatedAt只表示原根提交记录更新时间，不是实时心跳或最后业务活动。原生执行、恢复子执行和独立验收仍需原详情接口及任务证据。

## 授权与分页

- 先用既有binding_for检查当前身份/项目；查询只包含该原生workflow、空context的根提交记录，排除恢复、审批、配置和控制记录。
- 已发布绑定额外匹配同一binding/project/workflow/publication快照，与原逐运行查询的发布范围一致。其他版本不会被列出，也不会用其runId生成游标。
- 游标必须是同一过滤范围的可见运行ID。读取事务内先定位其根记录rowid再向更旧记录分页；回执晚到只更新状态/时间，不使旧记录跳到前面。新登记运行需刷新第一页。
- 每次最多读取limit+1条投影判断是否还有下一页，不返回输入、完整回执、秘密或原生工具正文，不批量观察原生运行。读目录不会写任务、操作意图或开启执行。

cursor不在当前范围时404，非法分页参数422；坏运行身份/存储不可用时503。错误提示不回显其他项目或版本的运行信息。工作台缺目录接口时保留原手动查询。

## 当前证据

仅源码和Python AST审查。指定gpt-6-luna只读复核后端授权/快照/SQL参数/游标边界未发现实质问题。未新增或运行测试、Node、浏览器、HTTP目录查询、真实模型或探针，也未重载主后端。此目录不是原生全部运行浏览器：不包含未经过本安装提交账本的原生运行，当前不跨所有绑定聚合或展示原生实时执行状态。真实列表、跨设备及完整运行监控仍待验收，AC状态不提升。

前端和后端已完成源码接线，独立静态收尾审查确认行点击只填表、不会自动请求原生状态；凭证/绑定/分页的epoch与generation隔离、游标防循环及旧后端缺接口兼容均保留。新凭证立即清除旧目录，晚到启动回执不覆盖其他查询选择；404只提示目录/绑定或分页位置不可用，不据此推断服务版本。手动查询和原控制操作防重记录保持。
