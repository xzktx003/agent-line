# 中文搭建 HTTP 与重启验收

探针入口为 `probes/sim/authoring_http_probe.py`。它通过真实 `load_app` 配置与 FastAPI TestClient 请求 AuthoringAPI，再使用真实 HTTPX 连接已有隔离 Sim；没有新建监听服务，没有调用模型、QwenPaw、记忆或工作流执行接口。

## 独立数据与凭据

所有新数据位于忽略目录 `var/sim-authoring-http/`：独立 TaskStore、SimJournal、project 工作区、service.json、authoring.json、state.json 和报告。JSON 文件为 0600，目录为 0700。Sim 凭据复用现有私有 probe-auth.json 路径，不输出；编辑凭据独立随机生成，包含 edit_workflows/publish_workflows，服务凭据另行生成。没有修改主适配服务准入状态。

探针在每次 API 写请求前保存完整路径、原 operationId 和原 payload 并 fsync。未知回执通过 GET operation 查询；不会改 operationId 重试。已有 accepted 回执允许以原 payload 回放，由真实 journal 负责去重。统计使用真实 HTTPX request hook，不替换或伪造 Sim 响应。

## 最终真实证据

首次运行真实创建成功，workflowId 为 `59ffcdd1-d5d1-46da-8558-9d983543ae86`，归属只来自本项目 author-create accepted 记录，没有接管既有探针。

首次发现并已修复的阻断：Sim create 原生会生成默认 Start 节点，并非真正空图。初版 GET 返回 document=null、draftEmpty=false、editable=false，因此初版不能进入安全保存。主线接入 is_initial_state 严格默认图判定后，沿用同一 workflow 和原 create operation，完成保存、发布、重开与回放，没有重新创建。该 helper 只接受全空图或精确 pinned 默认 Start，未知字段、输入非空、输出/名称/位置变化、多节点、变量均拒绝。所有权和 expectedStateSha256 检查仍生效。

最终 report.json.status 为 passed_live：

- revision=1，deploymentVersionId=`f41fe8c9-718a-4490-aef1-64d756dcb386`，服务端确切发布版本图核对通过。
- 初次 create 1 次；修复后该次运行只有 save/deploy 两次原生写，重开阶段 0 次。
- 随后独立重跑整个探针：第一会话 9 个真实原生 GET、重开会话 4 个 GET，全过程原生写 0 次。
- document 中文节点和位置完全保持；原 operationId/payload 三类回放回执一致。
- allTransportsClosed=true；所有 JSON 配置/证据权限实查 0600。
- 当前脚本另按 invocation 生成不可覆盖的 report-*.json；report.json 指向最近一次结果。首次失败 failure.json 与原请求/回执 state.json 保留。
- 18 项编译/客户端/默认图测试和定向 flake8 通过。

## 已验证链路与响应形状

链路为独立凭据 create → GET 初始图 → 保存中文条件文档 → GET 相同文档 → publish 并核对确切版本 → 关闭 TestClient → 重新 load_app → GET 同图 → 用原 operationId/payload 回放 create/save/publish → 查询原操作。最终报告已证明重开阶段 native 写请求为 0、所有 transport 关闭。

API 路径前缀 `/extension/v1/authoring`：

- GET `/projects`：projects、nodeTypes、canPublish。
- POST `/projects/{project}/workflows`：operationId/name/description；202 返回 projectId/operationId/kind/status/safeToResubmit/receipt。
- GET `/projects/{project}/workflows/{id}`：workflowId/name/description/document/draftEmpty/editable/stateSha256/isDeployed。
- POST `.../{id}/save`：operationId/expectedStateSha256/document；receipt.outcome 必须 saved，不能把 accepted 单独视为保存成功。
- POST `.../{id}/publish`：operationId/expectedStateSha256；receipt.outcome 必须 published，含 revision/deploymentVersionId；服务端核对确切发布版本图。
- GET `/projects/{project}/operations/{operationId}?kind=create|save|publish`：原持久操作状态与 receipt。

这不是浏览器 UI 验收，也不证明 execute 固定发布版本或跨编辑器 CAS。初始失败和最终结果均保留私有证据，不把接口 202 或部分链路当成完成。
