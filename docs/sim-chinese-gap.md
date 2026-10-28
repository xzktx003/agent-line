# 原版 Sim 中文差距（2026-10-27）

AC-36 要求把差距写清楚。英文画布外面套一层中文壳，不能算完整中文交付。

## 现场

- 原版 Sim 在 `127.0.0.1:3190`，镜像 `ghcr.io/simstudioai/simstudio`。已登录专用探针工作区 `729e9898-4a8b-4dab-9305-6e45508813ea`。页面 `lang=en`。
- 登录页文案是英文：Sign in、Enter your details、Email、Password、Forgot password?、Enter your email、Enter your password、Don't have an account?、Sign up、Terms of Service、Privacy Policy。截图 `var/ac36-sim-zh-20261027/shots/login.png`。
- 已登录工作区和画布的产品文案也是英文：New workflow、Integrations、Workspace、Tables、Files、Knowledge bases、Logs、Workflows、Toolbar、Editor、Live、Run、Triggers、Start、Schedule、Webhook Trigger。节点类型和说明包括 Start、Human、End、Response、Function、Loop、Agent，以及 “Pause execution until a human responds”“On error”“No logs yet”。右侧块目录从 Airtable 起按英文产品名列出。截图 `workspace.png`、`canvas.png`。
- 这两页里的中文只来自本项目写下的流程标题，例如“升级语义隔离验收”“结果引用验收”。那不是 Sim 自己的界面文案。
- 上游块定义里抽到 325 个英文硬编码块名，见 `var/ac36-sim-zh-20261027/inventory.json`。
- `frontend/editor.html` 有另一套中文壳，节点写成“开始、条件分支、人工确认、Agent 任务”。它不是原版 Sim 画布。

## 还要开发的范围

原版画布要达到简体中文，需要覆盖登录、导航、工具栏、约 325 个块名、节点说明、日志和配置面板。当前镜像没有这个语言层，也不是配置开关。在这层完成之前，不能宣称原版 Sim 已完整中文交付。

## 这次放开了什么

只在一次性容器 `ac36-browser` 里安装浏览器库，并用 `--no-sandbox` 截图。没有改宿主 user namespace、AppArmor、taskField、沙箱策略、ReMe、执行模型或 `10.40.0.2`。没有保存或运行流程，也没有改产品代码。截图完成后容器已删除。
