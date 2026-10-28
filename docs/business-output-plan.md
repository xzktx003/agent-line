# 业务结果进入原生条件分支

范围：在既有task-result桥契约上增加可选coding-result-v1和review-result-v1，复用现有outputSchemaId发布/运行/任务冻结字段，不另建Schema平台。编码格式为summary、status（completed/blocked/failed）、changedFiles相对路径数组；审查格式为summary、approved布尔、issues字符串数组。只接收完整JSON对象正文，拒绝重复键、额外字段、坏类型、非有限值、混杂说明、超限正文或危险路径，不猜测修复。整段正文恰好是单一 Markdown 围栏且内部为原 JSON 时，只投影该正文，不改已保存原文。

原Codex output保持不变。验证有效后派生结构化投影，向Sim回传的output增加result字段，条件直接读result.output.result.status或approved；已有task-result输出不变化。冻结回执匹配按相同确定性投影比较，不破坏历史重放。派发前附加明确格式要求，但不增加模型调用或工具权限；字段值只是业务/Agent意见，仍需原独立验收通过才成功回传，不把approved当人工批准或status当测试通过。

主实现负责agent_line/output_contract.py、authoring_api.py、dispatcher.py、service.py、completion_relay.py及运行说明；前端独立切片仅负责editor.js/editor.html/PRODUCT.md/DESIGN.md。双方不回退对方编辑。角色目录只声明后端实际支持格式；旧后端仅展示原格式，新编辑器不默认改变已有节点。沿用原生选择框和既有视觉，不新增服务/依赖/数据库或上游补丁。

只读源码、Python AST和指定gpt-6-luna静态审查，不新增或运行测试、Node、浏览器、HTTP、真实模型或探针；源码完成不提升验收状态，任意用户Schema编辑仍待后续实现。

## 源码完成记录（2026-10-23）

上述后端投影/回传/冻结比对、格式请求、授权角色目录、编辑器选择及条件标量字段建议已接线。默认task-result保持，失败执行保持原输出，非registered任务提交重放不重新构造格式提示，结果正文Unicode再次编码核对以拒绝孤立代理字符。独立Luna后端静态审查未发现实质JSON/权限/历史回执问题；前端负责人和根分别阅读缓存隔离/格式保留/字段建议，根修正通用格式提示不应引用新格式才有的result字段。

五个变更Python文件仅AST解析。未新增测试或执行Node/浏览器/业务模型/HTTP/探针/lint，没有正式配置或后端/插件重载。当前部署不会提前声明新格式；源码接线不等于真实Sim条件路径已验收。详见[business-output.md](business-output.md)。
