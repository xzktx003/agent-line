# QwenPaw 插件中的中文工作台验证

探针：`probes/gateway/workbench_plugin_probe.py`。只操作独立浏览器会话，不提交流程、任务、审批或记忆写入。官方 QwenPaw 登录和退出属于会话操作；所有 `/extension/v1/` 非 GET 请求由浏览器路由保护拒绝。

运行环境复用已有 Chromium 与中文字体配置：

```sh
FONTCONFIG_FILE=/tmp/agentteams-browser-fonts/fonts.conf \
LD_LIBRARY_PATH=/tmp/kanban-browser-libs/usr/lib/x86_64-linux-gnu \
.venv/bin/python probes/gateway/workbench_plugin_probe.py
```

真实检查包括官方插件菜单、中文工作台 iframe、流程搭建导航及返回、主服务 health 和两个既有任务查询。主服务 `readyForExecution` 必须保持 false。桌面与 390px 视口分别截图并检查工作台及编辑器横向溢出。

切换模式、重载、关闭的确认流程单独检查；QwenPaw 退出应卸载 iframe，重登录后工作台临时凭证应为空。原版 Sim 保留独立登录边界，QwenPaw 登录不会替它登录。

额外路由 fixture 只模拟公开入口配置：HTTP 500 后重试、恶意跨源路径拒绝。报告将 `live` 与 `fixtures` 分开，不能将 fixture 结果当真实服务证明。

截图和私有报告位于 `var/gateway/workbench-plugin/`。最终视觉结论应结合桌面／手机截图和 visual-verdict 记录；脚本执行完毕不自动表示所有视觉问题都已通过。

## 本次检查结果

真实浏览器检查完成，`report.json` 中的所有行为断言为 true，`extensionMutationAttempts=[]`。

- 官方 QwenPaw 登录后，从设置菜单打开中文工作台，再进入流程搭建并返回。
- 主服务真实 health：`controlledExecution=unknown`、`reconciliationRequired=true`、`readyForExecution=false`。只查询原任务 `task-b80a131c24f04608ac159a7d6d71e4be` 和 `task-cbf78536a36c427c9ab6cf38a3720aee`，两者均 succeeded／passed。
- 切换模式取消保留原 iframe；确认重载清空临时凭证；确认关闭卸载 iframe。QwenPaw 退出卸载 iframe，重新登录后的工作台凭证为空。
- 原版 Sim iframe 仍要求独立登录，未用 QwenPaw 凭证越过其边界。本轮未登录 Sim，因此不声称再次验证了两系统各自登出对对方登录态的影响。
- 独立 fixture 中，配置500可重试恢复，跨源 workbenchPath 被拒且不加载 iframe；这些不是现场上游故障证明。
- 稳定桌面 iframe 宽度／scrollWidth 为1116／1116；390px宿主下，工作台与流程搭建均为300／300，宿主为390／390，无横向溢出。

首轮手机截图在 QwenPaw 侧栏动画期间捕获78px临时iframe宽度。探针增加布局稳定等待后复核通过，没有修改产品UI。另一次探针把只读任务路径误写为 `/tasks`，已根据现有页面改正为 `/node-tasks`，未发生任务写入。

最终 visual-verdict 为92／pass，记录于 `verdict-final.json`，初轮诊断保存在 `verdict-initial.json`。截图：`desktop-workbench.png`、`desktop-task-proof.png`、`desktop-editor.png`、`mobile-workbench.png`、`mobile-editor.png`。手机顶部说明区较高是可改进项，不影响本次只读操作与权限边界验收。QwenPaw 原生宿主部分英文不属于本轮插件页面汉化范围。

主服务尚未装配 authoringFile/taskFlowFile；本次只核验搭建页导航与读取已有任务，没有从该正式入口创建、发布或启动新流程。隔离环境真实发布/执行证据仍需与这份入口证据分开看待。
