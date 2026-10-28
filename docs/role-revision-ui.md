# 角色版本阻断提示：浏览器验证

`probes/sim/role_revision_browser.py` 使用本机 3193 的真实 HTML/CSS/JS；所有 `/extension/v1/` 响应均为 Playwright fixture。**这是模拟响应的前端验证，不是实际角色配置变更或正式任务执行验收。** 凭据是无效测试字符串，未读取真实凭据；所有业务写请求被拦截，实际写请求数为零。

已通过：

- `role_revision_required`、`role_revision_mismatch`、`role_revision_unavailable` 在任务列表和详情均显示对应中文提示。
- registered/pending/resourcesReleased=false 显示“尚未启动，无执行资源”，不显示“执行资源已退出”。
- 查询过程中退出，迟到详情响应不能重新填入任务；列表清空、凭据清空、本地及会话存储为空。
- 无自动重新提交或取消请求，无浏览器脚本错误。
- 1440 桌面与 390 手机宽度均无横向溢出。

复现命令（使用已有浏览器库及字体，无安装）：

```sh
FONTCONFIG_FILE=/tmp/agentteams-browser-fonts/fonts.conf \
LD_LIBRARY_PATH=/tmp/kanban-browser-libs/usr/lib/x86_64-linux-gnu \
.venv/bin/python probes/sim/role_revision_browser.py
```

证据位于 `.omx/state/role-revision-ui/`：`report.json`、桌面/手机全页及任务区截图、`ralph-progress.json`。首次未指定字体环境出现中文方框，其失败判定保存在 `font-environment-verdict.json`；使用已有字体环境后确认中文正常，不修改产品代码。最终 visual-verdict 94/pass，仅覆盖响应式可读性，未声称与独立设计稿像素匹配。

浏览器会话 39369 已退出，没有新增监听服务。前端文件未修改。
