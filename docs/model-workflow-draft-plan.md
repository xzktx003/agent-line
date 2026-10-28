# 自然语言流程草稿交付计划

复用现有执行链：用户从原模板库创建“自然语言流程草稿”流程，选择已有只读规划角色；启动时以自然语言提供需求。QwenPaw 角色继续负责原生记忆和固定 Codex 委派，产出 workflow-draft-v1 格式的可移植模板，不增加模型服务或原生角色。原只读沙箱、发布准入、预算和独立验收保持，不因生成草稿豁免；当前缺少只读运行组件时仍阻止发布。

新增输出契约仅接受原portable template，并由原编译器验证。禁用生成的子流程固定引用和静态batchItem，不能输出任意原生代码、真实角色绑定或自动发布；角色只使用slot_1等占位。结果最多64k UTF8，模板仍受原节点/边/配置/字段/位置限制。

原任务详情对该声明格式的成功且资源已退出任务提供“核对并下载流程草稿”。显式POST读取当前version并再次validate，返回固定来源/身份/规模/模板摘要及模板；不发模型请求、不写Sim。结构通过不代表独立验收或人工批准，结果仍是候选。用户完整核对后通过现有JSON导入，手工选择角色，再保存/发布；不用URL、浏览器存储或新增跨页总线传正文。

根负责output_contract.py/workflow_template.py/新workflow_draft.py/service.py；Luna A负责workflow-templates.js/editor.js；Luna B负责新task-workflow-draft.js/app.js/runs.html；独立Luna静态复核。源码互不覆盖，无新依赖/状态库/上游补丁。只读源码/Python AST，无测试/Node/HTTP/浏览器/模型/lint/typecheck/build，不宣称生成成功或完整P2验收。
