# 中文运行工作台

本切片位于 `frontend/`，无构建步骤，无第三方前端依赖。仅实现当前真实 API 已提供的操作，不代表 PRD 整体前端完成。未修改 QwenPaw 或 Sim 源码。

## 已实现

- 服务凭证连接与断开；凭证仅保存在页面 JavaScript 内存，登录后清空密码框，不写 localStorage、sessionStorage、URL 或日志，刷新后清除。
- 实时读取任务存储、受控执行能力、恢复核对和执行准入状态。
- 读取当前凭证可访问的工作流绑定，以输入补全提供真实选择。
- 填写 JSON 对象输入和触发标识，启动已配置工作流；提交回执不会显示为执行完成。
- 按绑定与运行标识查询真实 Sim 状态、输出和原始证据；独立验收结论缺失时明确展示。
- 任务中心自动读取当前凭证授权的真实任务；支持执行状态筛选、每页 10/25/50 条、游标前后翻页与刷新。选择任务加载原详情；仍可手工输入任务标识。
- 按节点任务标识查询执行、验证、记忆回写的独立状态，提供带中文确认框的取消请求。
- 中文空状态、无权限/失效凭证/未找到/输入错误/网络不明状态；未收到提交回执时保留原触发标识，不自动重发或换标识重试。
- 桌面双栏、窄屏单栏，键盘可见焦点、标签和状态播报。技术标识与原始 JSON 字段保留原文。

底部持续明确说明：完整可视化画布、角色管理与记忆管理尚未交付。没有虚构任务列表、指标或执行记录。

## 同源挂载

主线已在 FastAPI 服务挂载 `/workbench/`。静态资源使用相对路径，所有 API 请求固定访问当前 origin 下的 `/extension/v1`，不支持用户输入任意后端地址，不将凭证发送至其他域。

挂载形式：

```python
from fastapi.staticfiles import StaticFiles

app.mount('/workbench', StaticFiles(directory=frontend_path, html=True), name='workbench')
```

挂载应在 API 路由注册后，`frontend_path` 使用部署计算的绝对路径。页面使用外部 JS/CSS，没有内联事件或脚本，可配合 `script-src 'self'; style-src 'self'; connect-src 'self'`。服务端应保留已实现的认证和 CSP，静态页可公开但业务数据始终需要服务凭证。

本机入口为 `http://127.0.0.1:3193/workbench/`；主线局域网入口为 `http://10.30.0.28:3193/workbench/`。本次浏览器测试来自本机，不能作为另一台局域网设备成功访问的证据。

## 实际浏览器验证

`frontend/browser-check.mjs` 使用现有 Chromium 与 Node 内置 DevTools WebSocket，无新增依赖。运行前必须存在主线创建的 `var/adapter/service.json`；脚本只在内存读取第一个 service token，不输出秘密。它会真实启动一个新的 `condition-check` 测试运行，不能在未经允许的业务绑定上使用。

本机命令：

```bash
FONTCONFIG_FILE=/tmp/agentteams-browser-fonts/fonts.conf \
LD_LIBRARY_PATH=/tmp/kanban-browser-libs/usr/lib/x86_64-linux-gnu \
CHROMIUM_PATH=/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell \
node frontend/browser-check.mjs
```

以上 `/tmp` 路径是本机已有浏览器依赖与中文字体，不是产品运行依赖；普通用户浏览器使用系统中文字体。其他机器可配置其已有 Chromium，无需这些路径。

2026-10-21 实际完成十项检查：未登录禁用执行、真实错误凭证拒绝、真实登录与授权绑定读取、无效 JSON 阻止、启动真实条件图、查询真实完成状态、真实不存在任务 404、390 像素窄屏无横向溢出、断开清除、刷新无凭证恢复且无脚本异常。

证据：

- `frontend/artifacts/browser-report.json`：十项实测结果与真实 runId。
- `frontend/artifacts/desktop-disconnected.png`：桌面未连接。
- `frontend/artifacts/desktop-connected.png`：桌面实际运行结果。
- `frontend/artifacts/mobile-connected.png`：窄屏实际运行结果与任务查询错误。
- `frontend/artifacts/visual-verdict.json`：检查截图后的视觉验收。

初次浏览器检查发现本机缺中文字体和无 submitter 的程序化表单提交问题，已用现有字体配置并修复处理；最终截图中文可读。设计检测器降级为正则模式，不能据其空输出声称完整无障碍/对比度检查通过。

## 尚未验证与未完成

- 当前实际配置仍将受控节点执行标为 unknown，因此未创建真实 Codex 节点任务来证明正向取消与执行中断；界面取消分支已接真实 API，但不能宣称已端到端验证。
- 完整流程画布、节点编辑/发布、运行列表、角色绑定管理、审批台、记忆追踪等仍需后续实现。
- 当前网页按按钮查询，不自动轮询；刷新后凭证与页面状态清空，已有运行需凭原标识查询。
- 跨设备局域网访问、浏览器长期会话、完整无障碍验收尚未验证。

## 任务中心增量验证（2026-10-21）

列表调用 `GET /extension/v1/node-tasks`，仅传递 `limit`、可选 `status` 与服务返回的 `after` 游标；授权边界由服务端验证。切换筛选/每页数量或刷新时重置游标，延迟返回的旧列表不会覆盖新筛选结果，断开连接会清除列表及详情。没有浏览器缓存或模拟记录。当前执行过滤项与服务枚举一致：已登记、提交中、结果待核对、已接收、成功、失败、取消申请、已取消。

真实 Chromium 检查通过 10 项：未认证禁用、错误凭证拒绝、授权连接与无浏览器存储、自动列表、成功筛选、点选详情及终态取消隐藏、末页禁用下一页、390 像素无横向溢出、切换筛选重置、断开清除。使用现有真实任务 `task-b80a131c24f04608ac159a7d6d71e4be`，没有为前端验收伪造或发起节点任务；其验证通过只覆盖后端已记录的产物哈希检查。

证据为 `frontend/artifacts/task-list-browser-report.json`、`task-list-desktop.png`、`task-list-mobile.png` 和 `task-list-visual-verdict.json`。可用上方相同环境变量执行 `node frontend/artifacts/task-list-browser-check.mjs` 复测，只读业务数据、不启动流程。凭证始终仅从私有配置读入进程内存。

当前只有 1 条授权真实任务，因此未实测有非空游标时的跨页往返；已实现游标栈并验证真实末页禁用。桌面全页截图中固定侧栏位置受 Chromium 滚动截图影响，任务内容本身可读。视觉检查延续原设计，无页面横向溢出；检测器仍降级为正则模式，未证明完整无障碍合规。

### 执行结束与资源退出分别展示

任务详情新增“执行进度”“资源退出”两行。服务返回 `resourcesReleased === true` 时显示“执行资源已退出”；明确 false 显示“执行资源退出待确认”；缺失或非布尔值显示“执行资源退出未确认”。终态执行可以显示“执行已结束”，同时资源退出仍待确认。取消回执不再以 canceled 状态暗示资源已经停止，说明文字明确“取消请求不等于执行资源已退出”。界面不增加容器标识或内部路径。

`frontend/resource-browser-check.py` 使用真实 Chromium 加 mock API 验证六项：false、true、缺失、字符串 true、恶意 HTML 作为文本而不执行、取消回执不冒充退出。报告 `var/gateway/resources-browser-report.json`，截图 `resources-pending.png`、`resources-exited.png`。这些是 UI 状态映射证据，不是实际执行资源退出证据；后者由服务端另行验收。JS 语法检查通过。visual-verdict 评分94，记录在 `frontend/.omx/state/resource-status/ralph-progress.json`。

### 中文人工审批页面

入口 `/workbench/approvals.html`，运行工作台导航已添加“人工审批”。实现文件为 `frontend/approvals.html`、`approvals.css`、`approvals.js`，沿用现有界面配色与布局，无新依赖。页面独立输入人工审批 Bearer，不能从运行页服务凭证推导权限；凭证仅内存、输入后清空，不进入 URL/localStorage/sessionStorage。

列表使用真实 `/extension/v1/approvals?limit=20&after=…`，项目范围由服务端过滤，前端没有虚构不存在的 projectId 筛选参数。详情展示原 workflow/run/block/context、版本、激活与尝试标识、有效期、决定人及决定时间。只有 waiting、服务明确观察到绑定暂停点、没有既有决定且本页未提交过时开放按钮。批准与拒绝均先确认原运行及节点。

一次决定在提交前生成唯一 decisionId，并在当前页面的内存 Map 保留；页面内查询、断开后重连不会产生新 ID 或重发 POST。网络失败和投递 uncertain 都只提供查询。刷新会清空凭证和页面内存，因此未知结果提示保留本页；重新登录后的最终去重与权限边界仍由服务端不可变账本保证。没有自动重试、自动恢复或本地执行调度。

恢复投递 rejected 表示服务拒绝投递，与人工 decision=reject 区分；人工拒绝也可以正常送达。决定已记录、恢复请求已接收、运行已结束是分开的状态。未知投递保持琥珀提示，即使原运行已完成，也不把送达回执改写成确认。

验证分两层：

- `frontend/approval-browser-check.py` 使用真实 Chromium 与 mock API，9 项通过：独立凭证、分页、批准、拒绝、断线未知后固定 ID、服务投递 uncertain、暂停点/过期门禁、XSS与凭证脱敏、移动端无溢出。该探针不修改真实审批。
- `frontend/approval-live-browser.py` 使用已授权隔离图和可信绑定，在真实中文 UI 点击两个决定，再只读查询原 Sim 运行。批准记录 `ui-approve-45bd181031a7`、拒绝记录 `ui-reject-c83499351ddc`，对应运行 `c1269b8d-8626-477c-9f9b-f6ef38c41867` 与 `578ecc36-86e7-4f1e-bcb4-d72ba5e08eef`，均 completed 且分别输出 approved/rejected。重跑探针只查询已收回执，不重投。

**真实回执边界：** 此轮服务 transport 对 Sim 返回的子 executionId 与父 runId 差异进行了保守未知处理，两个真实记录的 `deliveryStatusAtReceipt` 均为 **uncertain**。界面真实保留“提交结果未知，需核对”，同时原生运行状态独立显示“执行已结束”。本验证证明中文UI真实操作和原生分支完成，不能作为这两条记录投递已确认的证据；旧未知记录没有被修改。后续 transport 修复由主线负责。

证据位于忽略的 `var/gateway/approval-browser-report.json`、`approval-live-browser-report.json`、`approval-live-browser-state.json`，截图 `approval-desktop-unknown.png`、`approval-mobile.png`、`approval-live-approve.png`、`approval-live-reject.png`。真实状态文件0600，无凭据正文。visual-verdict93记录 `frontend/.omx/state/approvals/ralph-progress.json`。桌面双栏、移动端单栏均复核通过。
