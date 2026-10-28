# 原生执行权限审批界面

现有 `approvals.html` 分为“流程节点审批”和“执行权限审批”，共用页面内存中的独立人工审批凭证。没有新增登录、服务、原生记忆后端或流程恢复行为。

执行权限区按节点任务查询；`#execution-task=<taskId>` 仅预填非秘密任务标识，不自动发出决定。界面核对 GET 的 taskId、完整 upstreamTaskId/thread/turn 与每条记录身份。只有本次查询的 waiting、bound=true、未过期、尚无决定且本页未提交过的记录可操作。批准与拒绝都需要确认，POST 仅发送 decisionId、decision、expectedIdentity。

发 POST 前在页面内存保存 task/request/identity 对应的固定决定标识。网络失败、冲突或未知回执后只查询，原决定不重投；已有 intent/uncertain 决定亦不可重新发送。空列表、404、过期或归属未核实均不代表已批准。页面明确提示：批准只回应当前 Codex 权限请求，不代表执行、测试或流程成功。

切换任务、查询新快照、选择其他请求或断开连接都会隔离迟到响应；断开清空凭证、内容、任务输入，只保留页面内防重键与决定标识。内容以 textContent 渲染，不使用浏览器持久存储或在 URL 携带凭证。刷新页面清除页面内存，跨刷新持久防重由服务端原生决定账本负责。

## 离线验证

`node --check frontend/approvals.js` 通过。`probes/sim/native_approvals_offline.py` 全部请求拦截为明确 fixture，无真实 HTTP、审批、模型或服务操作。覆盖共享登录、hash 预填、批准/拒绝、项目 404、unbound/expired/not_observed、历史 intent、空列表不推断批准、未知不重投、重新查询轮次、旧轮次 409、任务切换和断开后的迟到响应、凭证/内容清空、storage0、手机无溢出。19 组检查、6 次模拟 POST、0 真实写入、0 页面/CSP 错误。

```sh
FONTCONFIG_FILE=/tmp/agentteams-browser-fonts/fonts.conf LD_LIBRARY_PATH=/tmp/kanban-browser-libs/usr/lib/x86_64-linux-gnu .venv/bin/python probes/sim/native_approvals_offline.py
```

证据在 `.omx/state/native-approvals/`：report.json、desktop.png（1440×1000）、mobile.png（390×844）、ralph-progress.json。沿既有样式两次视觉核对均 93/pass（第二次补服务端权威上下文）；本轮未改 CSS。截图不替代真实原生审批投递/退出验收。

详情与确认框优先展示服务端派生的项目、原运行、节点及本次尝试；详情另显示发布版本、父角色和节点执行标识。缺少字段显示“尚无记录”，不从浏览器猜测或回传替换这些绑定。

## 局域网 HTTP 与任务入口补测

原生决定标识在 `crypto.randomUUID` 不可用时复用 `crypto.getRandomValues` 的 16 字节十六进制生成方式，不使用 Math.random。离线浏览器显式覆盖 randomUUID 为 undefined，验证成功提交及未知请求使用 32 位随机标识，查询后保留原标识且不重投。

任务详情添加同源 `./approvals.html#execution-task=<taskId>` 文字链接。浏览器依次查询两个任务，核对只有当前任务链接、URL 不含凭据、退出清除链接。链接只是入口；正式实例的原生审批插件是否已安装、请求是否可操作仍以服务端查询为准。

最新实际报告为 `.omx/state/native-approvals/report.json`（不是 offline-report.json）。本轮截图 `task-link-desktop.png` / `task-link-mobile.png`，追加视觉核对 93/pass。19 组覆盖、6 次模拟 POST、真实写入 0、页面/CSP 错误 0；未访问真实服务。
