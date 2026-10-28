# 隔离 QwenPaw 部署记录

本部署仅验证 QwenPaw 原版 + 官方 Agent Line 插件接入，不代表 PRD 完整功能交付。

## 隔离布局

- Python：项目 `.venv`，安装 QwenPaw `2.3.0b1`。
- 上游 checkout：`upstream/qwenpaw`，tracked 源码保持原版。
- 工作区：`var/qwenpaw`（0700）。
- 秘密：`var/qwenpaw-secrets`（0700）；随机登录凭据 `local-login.json`（0600），不写入日志或文档。
- 认证：`QWENPAW_AUTH_ENABLED=true`，配置 `security.allow_no_auth_hosts=[]`，包括 loopback 请求也必须认证。
- 正式插件：`plugins/agent-line`，通过官方 CLI 安装至独立工作区。
- Console：官方 `console` 执行 `npm ci --no-audit --no-fund`、`npm run build`，由 `QWENPAW_CONSOLE_STATIC_DIR` 指向构建输出；无需修改上游源码。构建的 TypeScript、Monaco CSS、压缩与初始 bundle 校验全部完成。

## 管理命令

从 `agent_line` 执行：

```bash
.venv/bin/python deploy/qwenpaw/manage.py init
.venv/bin/python deploy/qwenpaw/manage.py start
.venv/bin/python deploy/qwenpaw/manage.py status
.venv/bin/python deploy/qwenpaw/manage.py verify
.venv/bin/python deploy/qwenpaw/manage.py stop
```

`init` 仅供首次安装：数据与秘密目录不存在或为空时，调用官方 CLI 初始化并安装插件。任一目录已有内容即拒绝，不用它修复已有安装、重新生成凭据或更新插件。已有实例更新插件需按对应升级文档操作。

`start` 先只读检查当前官方配置、每个已配置角色的工作区与 `agent.json` 身份、原账户/密钥及配置中的扩展持久状态文件；缺失、权限异常或存在 `RESTORE-DO-NOT-START.json` 时拒绝启动，不创建替代角色或密钥。检查不读取记忆正文，不冻结模型或角色配置，也不替代官方加密、数据库 schema 与记忆实现。它不能证明任意记忆子文件完整或密钥可解全部历史密文。

`start` 优先复用进程记录中的原 loopback 端口；没有记录时选择空闲端口。PID/端口/进程出生时间存于 `var/qwenpaw/agent-line-process.json`（0600）；状态文件是当前地址的权威来源。`stop` 验证 `/proc` 出生时间与 CLI 命令行后仅停止对应 PID，等待至进程退出。没有设置系统开机自启。

日志 `var/qwenpaw/service.log`。当前实例仅绑定 127.0.0.1；后续 LAN 统一入口应由认证网关代理，而不是直接扩大后端监听范围。

## 已验证

- 真实匿名 HTTP 请求 `/api/agent-line/health` 返回 401。
- 真实 `/api/auth/login` 登录后 token 请求健康端点成功。
- capabilities 对未验收 Codex/记忆/Sim 保持 unknown。
- 服务在官方 Console 构建完成后进行了受控停止/启动，原角色工作区复用；未调用模型，也未证明长期任务恢复。
- 官方 general 插件在 workspace 惰性启动后挂载，因此首次登录后短暂 404 可能出现；验证器仅对只读健康请求做 30 秒有界轮询。
- Loader 隔离集成测试、JS 语法检查完成。未改变其他已有服务。

## 浏览器探针

```bash
FONTCONFIG_FILE=/tmp/agentteams-browser-fonts/fonts.conf \
AGENT_LINE_CHROMIUM=/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell \
LD_LIBRARY_PATH=/tmp/codex-playwright-libs/root/usr/lib/x86_64-linux-gnu \
.venv/bin/python deploy/qwenpaw/browser_probe.py
```

脚本读取私有凭据但不打印；真实填写登录表单，点击中文工作台菜单与健康检查按钮，截图仅保存登录后的工作台。使用现有浏览器及已存在的动态库目录，不安装系统组件。截图路径为 `var/qwenpaw/plugin-browser.png`；是否完成以探针输出为准。

浏览器验收已完成：真实登录、中文菜单与路由、认证健康按钮均通过；截图已人工查看，中文字体正常。首轮发现 SDK 自动加 `/api`，已将插件请求修正为 `/agent-line/health`。浏览器语言与展开菜单通过宿主现有 localStorage 偏好设置。当前验证进程 PID `1669609`，地址 `http://127.0.0.1:60329`（后续以状态文件为准）。
