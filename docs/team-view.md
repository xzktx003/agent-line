# 项目团队关系图

AC-65 已完成：在“Agent 团队”查看项目配置中的角色、组织连线和当前活动任务，可修改连线并在服务重启后继续使用。入口：`http://10.30.0.28:3193/workbench/team.html`。使用已有独立流程编辑凭证，其项目范围和 `edit_workflows` 能力继续生效；凭证仅存于当前页面内存，退出连接会清除页面数据。

## 行为与边界

角色来自项目中已登记的正式角色 ID，不创建或复制 Agent。配置中的启用状态不是原生在线状态；已停用角色仍可作为组织关系的端点，编辑关系不会启用角色。当前主项目只有一个已配置角色，因此不会展示虚构的三角色团队。组织关系表示协作方向，不影响权限、工作流连线、执行顺序、模型、会话或 QwenPaw 原生记忆。

页面以 SVG 和完整文字列表展示关系，支持桌面及手机。添加或移除后先保留本地草稿，点击保存才提交；最多 100 个配置角色、200 条唯一有向关系，禁止自连和新增不存在的端点。旧连线中的已删除角色可保留原关系或主动移除。当前任务读取既有 TaskStore，最多 500 条并标记截断，只显示本项目的标识、状态与允许公开的原生身份摘要，不显示输出、证据、路径或凭证。

## 接口和持久化

沿用既有 `/extension/v1/authoring/projects/{project}` 认证和维护守卫：

- `GET /team`：角色、当前 revision、关系及活动任务快照。
- `POST /team`：`operationId`、`expectedRevision`、完整 `edges`；每条关系包含 `sourceAgentId` 与 `targetAgentId`。
- `GET /team/operations/{operationId}`：读取原操作回执。

本地关系复用 SimJournal.operations，以独立项目命名空间和 team-view context 记录。一次事务保存关系、版本和操作回执，不变更 schema，不调用 Sim 服务。写入重查身份、项目权限和版本；同身份同操作同正文重放返回原回执，冲突拒绝。历史版本链损坏时拒绝读取与保存，不静默重建。

保存响应丢失后页面锁定提交，只读取原 operationId；不会自动重投。回执的项目、精确下一版本与完整关系均须匹配。版本冲突保留草稿，成功重新读取后才能继续操作；失败读取不解锁。退出和切换连接通过请求代次防止迟到响应恢复旧数据。

## 已验证

- 9 项团队后端测试，批次完整 1729 项测试通过（95.425 秒）；JavaScript 语法、错误级静态检查和两个上游原版检查通过。
- 21 项拦截 API 浏览器场景通过，覆盖冲突、响应丢失、非法回执、角色变动、退出迟到、文本安全与活动任务；桌面及手机布局最终视觉复核 92/pass。
- 独立真实 loopback Uvicorn、浏览器和 SQLite：复用已有 default、luna-proxy-parent、luna-proxy-codex 配置 ID，保存 revision 1，完整停止服务后重开同一 journal，再修改并保存 revision 2；只有两条团队元数据操作，TaskStore 为空，原生配置前后摘要一致。这里的项目注册表为隔离测试元数据，不代表三角色业务执行或新授权。
- 主适配服务已按原命令及环境重载，备份 9 配置、9 SQLite；19 个任务及真实事件表、9 个原生 Agent 和配置保持。实际本机 LAN 地址 GET 返回 200，匿名 401，三个前端文件与部署内容一致；未对主项目写入团队关系。
- 所有本批验证均无模型、原生 Agent 或 Sim 调用；不修改上游源码、系统文件或原生记忆。主执行准入仍为 controlledExecution=unknown、readyForExecution=false。

私有证据位于 `var/team-view-20261022/`：ui/report.json、native/report.json、main-preservation.json、main-rollout/report.json、main-readonly.json 和 full-regression.log。组织关系的重启持久化已有实际 HTTP/浏览器证据；完整三角色执行、完整 PRD、异机 LAN 和长时间运行仍需单独验收。
