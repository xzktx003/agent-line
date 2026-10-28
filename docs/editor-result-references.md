# 编辑器：Agent 结果引用与运行入口

编辑器和运行页前端已完成下述隔离浏览器真实验收。临时服务已关闭；未启动模型、真实 run 或修改 QwenPaw 角色/记忆。

## 已实现交互

条件规则可选择开始输入或前序 Agent 结果。开始输入仍保存旧格式 `{id,operator,field,value}`，字段为 `input.xxx`；Agent 结果保存包含 `source` 的格式，字段限 `result.verificationStatus` 或 `result.output.xxx`。不会在加载或应用时静默丢失来源。

结束节点可切换固定 JSON 与前置 Agent 结果引用，分别保存互斥的 `{output:...}` 与 `{resultFrom:...}`。原来源失效时保留其标识并提示，不自动选择其他 Agent。候选需可达且位于消费者的所有路径上；图连线变化后重新核对候选，应用/保存时再次检查，后端仍是最终校验权威。

发布回执的 bindingId/revision 或草稿 GET 返回的 publishedBindings 会显示版本运行/查询链接。只传递非秘密 bindingId，运行页预填 `binding` 与 `query-binding`；不会认证、自动查询或启动任务。只有活动版本允许新运行，历史版本用于查询，权限及活动版本由服务端核验。编辑凭证不随链接传递。

原有未知操作编号保留、只查询防重、退出清空、未保存确认、epoch 隔离未改变。运行页链接不代表运行链已经获得新的准入授权。

## 验证与后续隔离浏览器方案

已执行：`node --check frontend/editor.js`、`node --check frontend/app.js`。真实验收应另建明确隔离的私有账本与配置目录，不重用既有 browser state，也不改主服务、原生角色或记忆。

协调后建议单轮验证：

1. 在现有允许项目/角色边界下，仅创建一次开始 → Agent → 条件 → 两个结束节点的草稿。每个实际 POST 前持久化 intent，不能丢回执后重复新建。
2. 条件选择 Agent，保存 `result.verificationStatus == passed`；一个结束使用 resultFrom，一个结束保留固定 JSON。真实保存/重新读取验证 source/resultFrom 不丢失；原 input.xxx 规则另作本地兼容检查。
3. 调整连线形成绕过来源的路径，验证来源失效提示且应用/保存拒绝；恢复有效连线。不得通过放宽后端验证完成测试。
4. 真实发布后检查绑定链接仅含非秘密 ID，运行页两个字段预填、凭证为空、没有新 run POST。重新打开草稿验证 publishedBindings 历史入口。
5. 桌面与手机同轮截图；检查 CSP、页面错误、storage 为零及退出清空。未知回执继续仅查询原 operation。

上述方案只验收编辑、发布与导航，不运行模型。需要执行链验证时另由主线明确安排，不能由点击链接自动触发。


## 实际浏览器验收结果

脚本：`probes/sim/editor_results_browser.py`。独立私有目录 `var/editor-result-browser/`；配置、state、report 为 0600。使用原项目 `native-memory-roundtrip` 和既有绑定 `native-loop-parent`，独立 TaskStore/SimJournal，无 executionFile/taskFlowFile。每个实际 POST 前保存意图；已有 state 禁止重复默认运行。`--observe` 只打开原流程、核对来源与退出清空，拦截所有 POST。

权威行为证据：`var/editor-result-browser/state.json`、`report.json`。Workflow ID 为 `685a2c84-6556-40fd-86a5-a5332b03a822`；create/save/publish 各一次，共 3 次 POST，run POST 为 0。

- 真实 GET 读回与浏览器重新读取均确认条件 `source` 与 `result.verificationStatus`、结束 `resultFrom` 保留；另一结束节点的固定 JSON 不变。
- 发布 receipt 的绑定与 GET publishedBindings 一致；链接仅含绑定 ID。运行页 binding/query-binding 正确预填，凭证为空，没有运行请求。
- 本地改变连线绕过原 Agent 后，来源标识仍保留且显示失效，应用被拒绝。该非法编辑未保存到 Sim。
- 桌面 1440×1000、手机 390×844 同轮截图；手机结束结果选择无整页横向溢出。storage 为零，退出清空来源表单与链接；页面错误及 CSP 违规为零。
- 首轮探针重新连接时重复挂载 dialog 监听，产生一次“已处理的确认框”Python 回调警告；仅修复探针监听去重。后续 `--observe` 零 POST 验证原保存结果与单次确认退出，通过。报告明确保留这项探针警告，未删除意图或重发写请求。

主线已对比原工作台与本轮桌面、手机截图，视觉裁决 92/pass；记录在
`.omx/state/editor-result-references/ralph-progress.json`。`desktop-page.png`、
`desktop-editor.png`、`mobile-page.png`、`mobile-editor.png` 均在上述私有目录。
截图不替代实际 API 读回与行为断言。

临时服务 PID 821623 已正常 SIGTERM 退出，确认进程不存在且 3196 无监听。两个 JavaScript 的 node --check、探针 py_compile 与 diff 检查通过。后续只能用原 Workflow ID 观察，不能删除 state 重新创建；本轮未覆盖模型执行、循环或人工审批发布。

## 数值比较常量修复（2026-10-23）

条件的数字比较值现须有限且绝对值不超过JavaScript安全整数上限，超大业务编号须改用文本比较。后端生成前及前端应用/保存/发布预校验同步，安全范围内生成代码不变。历史浏览器证据不覆盖本增量，仅源码/AST审查，详见[condition-number.md](condition-number.md)。
