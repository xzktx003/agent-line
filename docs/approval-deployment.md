# 人工审批部署

服务配置可选 `approvalFile` 指向独立的私有 JSON 文件。未配置时不挂载审批接口；配置后必须同时具有正式 Sim 连接。此入口复用原 Sim client 和持久 SimJournal，Sim 仍负责选择后续分支。

```json
{
  "schemaVersion": 1,
  "ledgerPath": "/absolute/private/approvals.sqlite",
  "credentials": [
    {
      "token": "替换为独立随机生成的至少32字符令牌",
      "subject": "owner",
      "projectScopes": ["project-id"],
      "capabilities": ["approve"],
      "expiresAt": 1800000000
    }
  ]
}
```

配置文件和审批库必须使用当前用户所有的0600普通文件、绝对canonical路径，拒绝符号链接或父目录别名。配置拒绝重复JSON键、额外字段、重复token/subject、未知项目和非有限过期时间。整数协议版本不能用布尔或浮点代替。

首次部署需显式调用 `ApprovalLedger.initialize(path)` 创建新库；正常启动不会创建或覆盖库。批准/拒绝的决定和与原工作流、run、block、context、发布版本、激活及attempt的绑定均持久化，重启不能清空这些身份。

人工凭证与现有服务凭证分开配置，不能重复使用同一个token。凭证由服务端映射到操作者、项目范围、approve权限和到期时间，浏览器只提交decisionId和approve/reject，不提交actor或重新绑定审批。到期/撤销权限在决定持久化及投递阶段再次检查。当前配置文件变更需要重新装载服务；进程内ApprovalAccess提供原子替换/撤权接口，但未向普通浏览器开放管理API。

审批库的绑定由可信工作流发布/运行集成建立；本配置不会扫描任意Sim暂停点，也不会自动把普通长任务结果等待当成人工审批。不得向模型、工作流节点或普通任务服务分发人工审批令牌。

六项运行时装配测试覆盖独立认证路由、配置范围、权限、凭证重合、丢库拒绝与不连接Sim时的启动失败。它们不代替真实Sim审批分支及工作台浏览器验收。

## 当前隔离部署

主服务已启用 `var/adapter/approvals.json`，审批库为 `var/adapter/approvals.sqlite`，保留配置备份 `var/adapter/service-before-approvals.json`。人工凭证仅在0600文件中保管，不在文档展示。主服务PID2480327（会话97176，以实际进程为准），人工token查询列表HTTP200，普通service token查询审批HTTP401，任务准入仍unknown且维护锁保持。

中文页面：`http://10.30.0.28:3194/workbench/approvals.html`。它与原任务工作台分开连接人工审批凭证；页面内存保存，离开页面失效，不写浏览器持久存储。该页面不会把普通服务凭证升级为人工权限。

当前审批绑定仍由可信内部集成/隔离验收脚本建立；任意用户创建的新工作流自动登记审批尚未接通。因此不能仅凭审批页面可用宣称全部审批产品需求完成。

Sim恢复回执修复部署后，主服务更新为PID2572841/session70060；原两条人工决定重启后仍在。历史uncertain未改写，新的真实修复验证见docs/sim-adapter.md。
