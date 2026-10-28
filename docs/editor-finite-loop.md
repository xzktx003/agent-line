# 有限循环编辑器

本页新增 loop 节点：`config={body:Agent文档ID,maxIterations:1..3}`。仅允许一个循环，body 必须是既有 Agent。循环体和完成连线分别保存 `branch:body` / `branch:done`；body 无普通出线。页面明确要求用户在 Agent 配置中自行启用 `allowFailureBranch`，不静默改变失败策略或角色。

配置表单保留失效的原 body ID 并提示，非法次数、失效 body、多循环、body 外部入线/普通出线或未启用失败分支均阻止保存。画布显示节点及两类连线；选择连线起点时排除已配置的 body。条件/结束结果来源检查使用虚拟 loop→body→after 路径，与后端 dominance 规则一致，画布和保存的文档不增加伪造循环回边。后端仍是校验权威。

Sim 生成的固定 function guard 不显示为可编辑文档节点，也不接受用户代码。页面不调度、不自动运行，不承诺 UI 校验等同真实执行成功。原凭证仅内存、未知操作只查询、退出清空、epoch 和未保存保护保持不变。

## 本轮验证

`node --check frontend/editor.js` 与 diff 空白检查通过。使用 Node VM 读取实际前端函数完成六项无服务断言：合法循环、body 结果支配 End、未启用失败分支拒绝、失效 body 拒绝、body 普通出线拒绝、次数越界拒绝。未创建服务、修改 var 或调用模型。

待主线安排隔离浏览器：用真实 authoring API 保存并重读单 loop+Agent 图，确认 body/maxIterations/两条分支/End resultFrom 精确保留；明确模拟失效 body 的展示及保存拦截；桌面与手机截图、CSP/storage/退出、零 run 请求。真实原生循环执行由主线独立验证，截图不替代执行证据。

## 实际浏览器验收

`probes/sim/editor_loop_browser.py` 已完成独立真实验收。私有目录 `var/editor-loop-browser/` 使用新 TaskStore/SimJournal/authoring 配置；配置项目保持 `native-memory-roundtrip`，角色绑定仍为既有 `native-loop-parent`，无 executionFile/taskFlow、无模型调用。创建前已确认 compiler 固定 guard 引用修正为 `guard.result.needsRepair`。

真实流程 ID：`32d88205-e2e1-4f5e-832e-2f9449bcf98a`。create/save/publish 各一次，所有 POST 前记录 intent，共 3 POST、0 run 请求。`state.json` 保存真实回执及 GET 读回，`report.json` 保存行为断言。默认已有 state 时仅观察原流程，不删除意图、不自动新建或重发未知操作。

- 浏览器人工明确勾选 body Agent 失败策略，默认并未自动开启。
- 保存、GET 和页面重新加载均确认 loop.body、maxIterations=3、body/done 两条边和 End.resultFrom 保留；发布回执含冻结 loopPolicies。
- 删除 body 的本地未保存测试保留原 body ID、显示失效并阻止保存；此非法图未写入 Sim。
- 桌面 1440×1000、手机 390×844 截图在 `desktop-page.png` / `desktop-editor.png` / `mobile-page.png` / `mobile-editor.png`；手机无整页横向溢出。CSP、页面错误均为零，storage 为零，退出清空凭证与循环体表单。
- 发布绑定 URL 仅预填运行页两处 binding 字段，凭证为空，无运行请求。随后 `--observe` 真实只读复核原文档未改变。

临时服务 PID 1051855 已正常 shutdown，exec 93109 terminal，进程不存在且 3196 无监听。探针 py_compile、editor.js nodecheck 通过。主线对照原工作台及桌面/手机截图，视觉裁决 93/pass，记录于 `.omx/state/editor-finite-loop/ralph-progress.json`；此证据只覆盖编辑保存发布和浏览器交互，不替代真实原生循环执行证明。
