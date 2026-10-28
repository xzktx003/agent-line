# AC-01：原版上游与独立扩展边界

当前此项通过。它只对应 PRD AC-01：“安装扩展后检查两个上游源码目录，无业务源码补丁；扩展和数据在独立位置”。不等同于 R-01/R-07 全部完成，不批准新版本升级，也不证明其余功能或完整 PRD 已交付。

当前安装报告：`var/acceptance/ac-01-report.json`（0600），绑定本次 PRD 摘要、固定 QwenPaw/Sim 提交、实际宿主进程身份和插件文件摘要。

## 已核实证据

- `scripts/check_upstreams.py` 在两个实际 checkout 上通过：仓库根、HEAD、origin 均与 `upstreams.lock.json` 相符，没有受版本控制的修改或非忽略的未跟踪文件。
- 已安装宿主包的 1066 个 Python 文件逐一与固定上游 `src/qwenpaw` 比较，缺失与差异均为零。干净 Git 工作树没有掩盖已安装 Python 包的业务补丁。
- 官方认证插件清单显示 `agent-line` 已启用，真实插件 health 返回 200；源码和实际安装插件的五个入口/桥接/治理/前端文件逐字节一致。
- 扩展源码位于独立 `agent_line/`、`frontend/`、`plugins/agent-line/`、`deploy/`，宿主工作区、秘密、适配器、Sim 和执行工作区位于 `var/`，均在两个上游 checkout 之外。
- 实际 `/proc` 进程环境核对 QwenPaw 工作区和秘密目录；正式角色工作区规范路径没有指向上游源码。
- Docker 实际 Sim Postgres、Redis 与上传目录挂载位于独立数据位置，没有将扩展或状态写入上游源码目录。

## 生成产物边界

Git 检查不会审计 ignored 文件。QwenPaw checkout 中存在构建、依赖和生成元数据目录；当前 Console 静态资源使用 `upstream/qwenpaw/console/dist`。报告明确记录了这些情况。

“无业务源码补丁”不表示上游目录没有构建输出，也不等同于生成 bundle 已通过可复现构建比对。升级仍需要固定候选、接口和运行验证、备份与恢复，不能仅凭 AC-01 放行。

只读复核命令：

```bash
python3 scripts/check_upstreams.py
.venv/bin/python deploy/qwenpaw/manage.py status
```

进一步的包摘要、官方插件 inventory、角色路径和实际 Docker mounts 证据保存在该报告内；读取和记录这些证据不重启服务、不修改上游或角色配置。
