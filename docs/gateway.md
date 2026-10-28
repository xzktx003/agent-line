# QwenPaw 与 Sim 同源网关

实现位于 `deploy/gateway/`，仅包裹原版应用，没有修改上游源码、编辑器 DOM 或安全响应头。统一访问源为 `http://10.30.0.28:3194`，QwenPaw 入口 `/qwenpaw/`，Sim 原版编辑器保留 `/workspace/…`。这是后续 QwenPaw 插件内嵌 Sim 的部署基础；独立打开两页不等价于插件画布已经验收。

## 路由和官方依据

| 外部路径 | 目标 |
| --- | --- |
| `/qwenpaw/api/**` | QwenPaw 原版 `/api/**`，当前端口读取 `var/qwenpaw/agent-line-process.json` |
| `/qwenpaw/**` | 独立官方 console 构建产物及 SPA 回退 |
| `/socket.io/**` | Sim realtime 3192，HTTP polling 与 WebSocket |
| `/extension/v1/**`、`/workbench/**` | 我方适配服务 3193 |
| 其他路径 | Sim 3190，包括 `/_next`、`/api`、`/login`、`/workspace` |

QwenPaw `website/public/docs/console.zh.md:35` 明确支持 `VITE_CONSOLE_BASENAME=/qwenpaw`、`VITE_API_BASE_URL=/qwenpaw`；`console/vite.config.ts:31` 将其用于静态 URL，`src/api/config.ts` 在此基础添加 `/api`。`build-console.sh` 使用这些正式构建变量，显式清空 `TOKEN`，输出独立目录，不替换正在运行的 QwenPaw 静态目录。产物被本目录 `.gitignore` 忽略。构建依赖现有上游 node_modules，不安装新依赖。

Sim 未配置 Next basePath，因此保留根路径，避免对 JS/HTML 做字符串重写。`apps/sim/proxy.ts` 的 `X-Frame-Options: SAMEORIGIN` 与 `lib/core/security/csp.ts` 的 `frame-ancestors 'self'` 完整保留，同源父页满足这些限制。iframe 内仍需 Sim 自己的登录会话；共享 origin 不自动赋予组织成员资格，也不实现单点登录。

`sim-origin.override.yaml` 使用官方 `NEXT_PUBLIC_APP_URL`、`BETTER_AUTH_URL`、`NEXT_PUBLIC_SOCKET_URL`、`TRUSTED_ORIGINS` 与 realtime `ALLOWED_ORIGINS`。最后一项依据 `apps/realtime/src/config/socket.ts:24`。内部 `SOCKET_SERVER_URL` 不变。部署地址改变时要同步这些公开配置，不能只换网关端口。

## 启动及维护

从 agent_line 目录执行：

```bash
deploy/gateway/build-console.sh
docker compose --env-file var/sim/sim.env -f deploy/sim/compose.yaml -f deploy/gateway/sim-origin.override.yaml up -d --no-deps simstudio realtime
deploy/gateway/run.sh
```

数据库、上传、会话仍由原隔离 Sim 部署持久化。网关不保存凭据，关闭访问日志，不信任外来 X-Forwarded-*。不剥除 CSP/XFO；保留重复 Set-Cookie。HTTP 内容流式传递，保留压缩编码；WebSocket 双向传递。未设置额外 Qwen CSP，避免破坏官方插件加载器的 Blob import。网关使用既有 httpx、websockets、FastAPI，不加依赖。

局域网 HTTP 是当前开发验证地址；现有上游 Socket.IO 设置 production Secure cookie，此 cookie 在普通 LAN HTTP 上可能不落盘。HTTP 登录与实际 WebSocket 握手需分别核验，不假称具备 HTTPS 部署。网关与两套 UI 同源后共享浏览器信任边界，插件应只安装可信代码。

## 已知上游边界

Qwen `console/src/utils/chunkServerReadiness.ts:139` 的错误恢复探测硬编码根 `/api/version`，会落到 Sim；正常 basename 页面/API 已验证，但 chunk 更新失败时的自动恢复不能声称完整兼容。`AppCard.tsx` 还包含 `/api/frontend_plugin/` 图标判断，需专项验证。没有为这些边界改上游源码或抢占 Sim 的根 API。

中文工作台独立提供，Sim 原版编辑器仍有英文。该网关不提供全量画布汉化，也不以 DOM 翻译掩盖此差距。插件实际 iframe、保存、重新打开、节点执行与协作恢复需要完整浏览器验收。

## 验证与证据

`probes/gateway/test_gateway.py` 的 4 项测试验证路由隔离、hop-by-hop 清理、SSE mock 内容传递、CSP/XFO 和重复 cookie 保留及缺失静态资源 404；mock 不证明真实 SSE 业务已验收。

`probes/gateway/browser_probe.py` 用真实浏览器及私有本地测试账号验证登录、页面及 socket 握手，凭据只存内存。截图/报告在忽略的 `var/gateway/`。测试只读取现有工作区，不运行外部模型或业务。重建容器时产生过一份 Sim 502 报告，必须重跑健康后报告才可作为最终证据。

最终健康后重跑结果：Qwen 登录 200，实际导航至 `/qwenpaw/chat`；Sim 登录 200，导航至现有隔离工作区的原版 Human 流程画布（Start、Approval、Done 三节点），保留 SAMEORIGIN；真实浏览器 Socket.IO WebSocket 初始握手通过。`var/gateway/browser-report.json` 已覆盖维护期间的 502 记录。已人工查看 `sim-root.png` 确认画布实际渲染。这些是顶层页面及真实传输证据，尚不是插件 iframe 或 SSE 业务全链路证据。

当前网关进程 PID `1955299`，执行会话 handle `1583`；启动命令见 `run.sh`，仅关闭此 PID 可停止网关，不影响 QwenPaw/Sim/适配服务。原独立构建会话 `59619` 已成功结束。两份上游 `git status --short` 为空。

## 插件内嵌后续验收

`plugins/agent-line/dist/index.js` 现在使用官方 `window.QwenPaw` React、route、menu 接口注册“工作流工作台”，读取网关 `/agent-line-gateway/config` 的公开 `{simEditorPath:"/workspace"}`。配置不含工作区、流程或秘密。仅允许同源 workspace 路径，iframe 不改上游 DOM，不注入翻译，不放宽安全头。直接访问原 QwenPaw 端口时会明确提示使用统一网关，不错误嵌套 Qwen 首页。

真实 `probes/gateway/plugin_probe.py` 已通过：Qwen 宿主账户菜单 → Settings → 工作流工作台 → 打开原版画布 → iframe 内真实 Sim 邮箱/密码登录 → 原版画布加载 → 点击隔离 condition 流程 → 重载仍登录 → 关闭移除 iframe。Sim 官方 sign-out 后画布回到登录页，Qwen 页面仍登录；Qwen 原生 Logout 后进入 `/qwenpaw/login` 且 iframe 卸载，但 Sim 独立会话仍可访问。探针最后退出自身 Sim 会话。截图 `var/gateway/plugin-sim-editor.png`、`plugin-sim-logged-out.png`，报告 `plugin-report.json`。此次没有编辑/保存图或发起执行，持久化编辑仍需专项测试。

视觉复核采用 visual-verdict，原版顶层 Sim 截图作参照，评分 92，证据位于 `probes/gateway/.omx/state/plugin/ralph-progress.json`。外层中文、原版画布英文，不声称完整中文。

安装注意：当前认证实例中官方 CLI `plugin install --force` 失败仍返回 exit 0，不能以退出码证明成功。已改用官方 `POST /api/plugins/install`，携带合法账户登录取得的 Bearer、参数 `{source:绝对插件目录,force:true}`，HTTP 200 后核对已安装 JS 和源码逐字节相等，再真实浏览器验证。执行桥由主线拥有，此次整体热安装同时加载主线最新桥；安装前已确认无在途任务。无需重启 QwenPaw。

网关新增配置路由后当前 PID **1976564**，执行会话 **5785**（替代前述旧进程）。本地代理测试现为 5 项，新增公开配置无秘密/工作区测试。

## 原版画布创建、编辑持久化与两分支真实执行

2026-10-21 新增 `probes/gateway/canvas_probe.py`。该探针通过 QwenPaw 登录、真实 Settings 菜单和插件 iframe 进入已授权的隔离 Sim 工作区，使用原版文件导入控件创建新流程 **Canvas Browser 8b69c1fa6e**，ID `d917e5c9-3867-46d0-8260-466ad4e565fb`。创建路径是官方 JSON 导入 UI，**不是手动拖出节点和连线，也不是直接调用 API 写图**。没有修改此前 G0 或完整工作流探针图。

原始新图包含 Start、Condition、两个 Response，共 4 节点、3 条边。通过真实节点双击和原版编辑器文本输入，将条件改为 `<start.input> === 7`；离开输入框后自动保存。通过官方只读 state API 等待持久化，再关闭插件 iframe、重新打开并选择此流程，重新双击条件节点读回相同参数；重开前后的完整 blocks 与 edges 相等。随后在原版 UI 点击 Deploy，版本 v1 成为 Live。

执行使用**同一已发布图的官方 v2 API**，不是浏览器 Run 按钮。分别输入 `{input:7}` 与 `{input:8}`，实际结果如下：

| 输入 | 原版 Sim runId | 状态 | 输出 |
| --- | --- | --- | --- |
| 7 | `60bedf31-44e9-49de-95f2-74702946908c` | completed | `{data:{branch:"yes"},status:200}` |
| 8 | `2e422673-e1d0-463b-81b9-c151a71e1f15` | completed | `{data:{branch:"no"},status:200}` |

每次提交前将独立 runId 和 intent 写入 `var/gateway/canvas-executions.json`，后续重跑只查询已有 run，不重发请求。创建也有 `canvas-state.json` 的 import-intent；若创建结果未知则中止，要求核对，避免盲目重复导入。API key 与登录密码只在内存使用，不写报告。

证据：`var/gateway/canvas-report.json`、`canvas-persisted-state.json`、`canvas-executions.json`、`canvas-persisted.png`、`canvas-deployed.png`。`reopenedParameterMatches`、`reopenedGraphMatches`、`deployedViaOriginalUI` 均为 true；两执行均为 completed。已查看截图确认 Qwen 外壳中的原版条件编辑器、参数与两条分支。执行命令：

```bash
FONTCONFIG_FILE=/tmp/agentteams-browser-fonts/fonts.conf \
LD_LIBRARY_PATH=/tmp/kanban-browser-libs/usr/lib/x86_64-linux-gnu \
.venv/bin/python probes/gateway/canvas_probe.py
```

此轮未改网关、上游源码、插件 JS、执行桥，也没有新增依赖。完整画布中文、从零手拖节点连线、浏览器 Run 参数交互尚不在这份证据范围；原版英文仍可见。初次尝试用 textarea 填入带双引号条件时，原版编辑器保存了转义形式，探针明确失败；改用无字符串转义的数值条件完成本次验收，不能据此声称字符串编辑路径已验证。

主线恢复服务后的网关 PID 为 `2086702`（取代前述旧 PID）；Qwen 动态端口始终以 `var/qwenpaw/agent-line-process.json` 为准，本轮未重启服务。

## 会话隔离修复（2026-10-21）

后续审批验收发现共享 `httpx.AsyncClient.build_request` 会合并其 cookie jar，使匿名请求可能携带此前登录的 Sim 会话。此问题已修复：代理使用独立 `httpx.Request`，Cookie 仅来自当前入站请求；共享连接池配置 `NoSharedCookies` 同时拒绝接收及回放 Cookie。没有通过 clear jar 规避，因此没有清空操作的并发竞态。上游重复 Set-Cookie 仍逐个转发给对应客户端。

6 项单测通过，新增预先污染共享 jar 后依次转发 Alice、Bob、匿名请求的回归。真实 `cookie_isolation_probe.py` 使用两个独立合法登录 browser context 与匿名 context，5 轮并发查询证明两个 session ID 不同且各自稳定，匿名 session 始终 null；两登录浏览器实际显示工作区，匿名浏览器实际显示登录页。报告 `var/gateway/cookie-isolation-report.json` 不含凭据或 session ID。此前顶层/iframe截图不能作为跨用户会话隔离证据，应以此修复后测试为准。

仅重启经 `/proc` 核对归属的本项目网关，当前 PID `2321077`、会话 `58794`。未改 Sim/Qwen 服务或上游代码。

## 中文工作台接入（2026-10-22）

插件现以“打开中文工作台”为主入口，直接复用现有 `/workbench/` 页面及其中的流程搭建、任务与人工审核导航；“打开原版画布”继续使用同一个 iframe，不增加另一套页面或调度器。公开配置新增固定 `workbenchPath:/workbench/`，两个入口均采用精确同源路径白名单，不接收 URL 凭据。配置失败或超时可重试；切换、重载和关闭前要求确认丢弃未保存内容和页面内凭证。QwenPaw 退出后 iframe 卸载，重新登录不保留子页面临时凭证。Sim 仍使用独立登录。

官方热安装 HTTP 200 后已逐字节核对安装前端与源码；QwenPaw 未重启，插件后端没有改动。仅重启所属网关，当前 PID **1553996**、exec **33021**，端口仍为3194。部署记录在 `var/gateway/workbench-plugin-deployment/report.json`。匿名业务接口仍拒绝，原 frame-ancestors/self 与响应头完整保留，主任务准入保护不变。

真实桌面与390px浏览器已验证中文入口、内部导航、两个原任务只读查询、退出清空、原版画布独立登录；恶意配置拒绝与配置500重试为单独标注的 fixture。完整记录见 [插件中文工作台验证](qwenpaw-workbench-plugin.md)。本次未从主服务发布或执行新流程，主服务仍 unknown/guarded；此处导航成功不代表正式执行或完整中文宿主已交付。
