# Agent Line 官方插件接入探针

此插件是 G0 接入原型，不是完整工作流界面。无需修改 QwenPaw 源码，无新增依赖，不使用 monkey patch。清单允许已测试的 `>=2.2.3b1,<2.3.0b2`。2.2.3b1 到 2.3.0b1 已在隔离环境升级并恢复旧备份；再扩大范围前必须重新验证。

## 已实现

- 使用官方 `PluginApi.register_http_router` 挂载 `/api/agent-line/health` 和 `/api/agent-line/capabilities`。
- 路由必须有宿主认证中间件设置的 `request.state.user`；不信任客户端用户名或未经验证的 Authorization 头。宿主关闭认证或本地白名单绕过认证时也会返回 401，需要部署时启用认证并取消对应绕过。
- 前端通过官方 `window.QwenPaw.route.add/menu.add` 注册中文页面；无构建依赖，`dist/index.js` 就是维护的原生 ES module。使用宿主 React 与认证 fetch。
- 中文工作台主入口复用同源 `/workbench/`，原版 Sim 画布为可选入口；配置失败可重试，切换、重载和关闭先确认清除未保存内容及页面内凭证。宿主退出卸载 iframe。服务、编辑、审批与记忆审核凭证仍分别授权，不将 QwenPaw 登录冒充自动取得业务权限。真实浏览器边界见 `../../docs/qwenpaw-workbench-plugin.md`。
- 尚未验证的 Codex、记忆、恢复、Sim 能力显式返回 `unknown`；健康成功仅指插件接口工作。

## 已执行测试

```bash
# 从 agent_line 目录运行
.venv/bin/python -m unittest discover -s tests -p test_plugin_contract.py
node --check plugins/agent-line/dist/index.js
```

先运行测试时 manifest 不存在，测试失败；随后实现插件，测试通过。测试在独立子进程和临时 `QWENPAW_WORKING_DIR` 中使用已安装的真实 QwenPaw 2.3.0b1 Loader/PluginApi/Registry，覆盖：加载、真实签名 token 校验、缺少/伪造认证的 401、已认证响应、能力 unknown、卸载后 404、不兼容宿主版本拒绝注册。没有读取全局凭据，没有启动监听服务，没有修改上游。

身份单元测试使用真实 QwenPaw `create_token/verify_token` 及测试身份中间件。完整隔离宿主的未认证401、认证200、真实浏览器中文菜单和插件路由也已单独验证；详见 `../../docs/qwenpaw-deployment.md`。

## 受控委派与执行桥接

官方注册 `agent_line_delegate(instruction: str)` 和 `agent_line_execution_status()`，默认禁用。模型不能指定 agent、workspace、session 或任务身份。服务端配置固定 `managed_role_ids`、`codex_target_agent_id`、执行工作目录、隔离 CODEX_HOME、私有服务 token 路径和显式初始化 receipt ledger 路径。未配置时失败关闭。

- 服务端通过双认证 `POST /api/agent-line/delegations/bind` 登记 `{taskId,roleAgentId,roleSessionId,roleRevision}`；session 必须等于 `agent-line-<taskId>`，父角色必须为允许名单内的正式 qwenpaw 角色。同绑定复用，不同绑定409。
- 工具只从可信 ContextVar 查持久绑定，读取正式目标配置验证 codex backend 与固定工作目录，再通过官方 console 后台入口执行。
- `POST /api/agent-line/executions` 接收 `{taskId,roleSessionId,instruction}`；GET 同路径加 taskId 观察。宿主登录及 `X-Agent-Line-Service-Token` 均必需。
- SQLite schema 4 先持久 claim，再调用 native；相同载荷复用，不同载荷409，未知提交不重投。schema3需显式迁移，旧绑定不自动补当前角色版本；已有身份仍可观察和取消。这不是 DAG 或主线 TaskStore 的替代品。
- 新 bind 请求必须携带从可信运行冻结的 `roleRevision`；描述接口提供角色摘要，现有 middleware 和执行桥在新推理/委派前核对。角色名和原生记忆正文不进入摘要。配置或库不可用时保留拒绝新执行的保护，不因加载失败移除 middleware。项目实例现已受控停机迁移、官方安装后重启，并通过一个真实冻结角色/Codex正例；主适配服务执行准入保持未知，详见 `../../docs/role-revisions.md`。
- 读取固定版本 native session mapping 和原生 rollout 验证真实 thread/turn 与终态；资源释放须另有原生后台任务 finished 证据。重启丢失内存任务不推断资源已释放。
- 受管角色 middleware 只允许 memory_search、agent_line_delegate、agent_line_execution_status；直接 shell、文件写入、其他代理委派与未知工具被阻止。

实际链路已由 `../../probes/qwenpaw/parent_delegate_smoke.py` 验证：父角色真实 memory_search 的来源/内容/call_id → delegate 参数和回执 → 相同 native task/thread/turn → 独立文件内容。详见 `../../docs/g0-qwenpaw.md`。原生执行使用隔离 G0 的 danger-full-access，未证明生产沙箱/审批、取消恢复、自动记忆写回，因此能力接口仍保守 unknown。

隔离安装使用官方插件安装 API 或 CLI，工作目录必须指定本项目实例；不要让 CLI 自动发现用户其他服务。现有验证环境已安装，当前源变更应经测试后用官方 API 热安装。
