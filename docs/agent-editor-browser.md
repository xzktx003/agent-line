# Agent 节点搭建页真实浏览器验收

本轮完成真实 Sim 草稿创建、保存和发布，不启动模型，不创建正式角色，不修改 QwenPaw 原生记忆。此结果不证明流程执行链或完整产品已交付。

## 环境与防重

脚本：`probes/sim/agent_editor_browser.py`。独立目录 `var/agent-editor-browser/` 为 0700，配置、状态与报告为 0600；凭证只从私有文件读取，不打印。使用现有 Sim API 身份及 workspace，固定项目 `native-memory-roundtrip`、绑定 `native-loop-parent`、原生角色 `agent-line-native-loop-parent`。项目工作区从宿主 execution_bridge 配置读取，未将同角色配置到其他项目。

`--prepare` 仅一次显式创建独立 TaskStore、SimJournal 和 authoring 配置，目录已存在时拒绝覆盖。服务通过正式 `agent_line.runtime serve` 加载配置；没有 executionFile 或 taskFlowFile。脚本为每次真实 POST 在私有 state 中先写操作编号和 intent，再允许浏览器发送。已有 state 拒绝重跑创建；不得删除状态以绕过这一限制。

临时服务 `127.0.0.1:3196`（PID 689260）已 SIGTERM 正常关闭，确认 `/proc/689260` 不存在、3196 无监听。运行的浏览器进程已由脚本关闭。

## 实际结果

证据：`var/agent-editor-browser/report.json`、`state.json`。

- Workflow ID：`c18d4504-a8e7-4262-8037-14ea12cb8798`。
- 共 3 次真实写请求：create、save、publish 各一次。
- 浏览器配置开始 → Agent → 结束，选择正式既有角色绑定、任务模板与开始输入；保存读回的 document 与发布 receipt 的 externalTasks 均包含正确角色映射。
- 第二 Agent 的前置 Agent 输出选择仅在本地草稿验证，未保存、未发布、未执行。
- 仅拦截 roles GET，分别模拟空列表和 503：原绑定标识仍保留，显示失效/未核验，保存被禁用；未修改服务端角色配置。
- 桌面 1440×1000、手机 390×844；手机选中 Agent 配置后没有 document 横向溢出。
- localStorage 与 sessionStorage 项数为 0；退出清空凭证、任务模板及角色选择内容。
- 页面异常与 CSP 违规均为 0。`node --check frontend/editor.js` 和探针 Python 编译通过。

截图（同一轮浏览器验收）：

- `var/agent-editor-browser/desktop-page.png`
- `var/agent-editor-browser/desktop-editor.png`
- `var/agent-editor-browser/mobile-page.png`
- `var/agent-editor-browser/mobile-editor.png`

本轮未发现需要修改 editor 的可复现问题。视觉裁决由主线基于以上截图另行完成，此报告不预先宣称视觉评分。

## 调用方式与范围

```bash
# 仅在从未初始化的隔离目录执行一次
.venv/bin/python probes/sim/agent_editor_browser.py --prepare
.venv/bin/python -m agent_line.runtime serve --config var/agent-editor-browser/service.json --host 127.0.0.1 --port 3196
# 另一个终端运行；已有state会拒绝重写
FONTCONFIG_FILE=/tmp/agentteams-browser-fonts/fonts.conf LD_LIBRARY_PATH=/tmp/kanban-browser-libs/usr/lib/x86_64-linux-gnu .venv/bin/python probes/sim/agent_editor_browser.py
```

以上是复核入口说明，不是再次创建现有流程的授权。现有验收应读取原 state、操作回执和 Workflow ID；需要新的独立探针时必须使用另一个明确隔离目录，不能重用已有 mutation 身份。新流程运行绑定、有限循环、人工审批发布和完整 Agent 执行验证不在本探针范围。
