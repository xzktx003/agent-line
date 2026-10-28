# 停止验收工作台接线

本页继续使用原任务详情、取消确认框和任务取消 POST，不新增服务或页面。

- 只有服务端 `canCancelVerification === true` 才允许对已完成编码的任务显示“停止验收”。确认文字说明不会删除产物或撤销已完成的编码，只停止测试并撤销后续测试。
- `verificationCancellationRequested` 表示请求已持久接收。即使编码执行 `resourcesReleased` 为 true，页面仍显示“编码执行资源已退出，验收停止待确认”；只有 `verificationStatus=canceled` 才显示“已取消验收”。
- 取消请求未取得明确回执时，保留本页非敏感任务标识并禁用重复提交，只通过查询核对。退出、切换任务或修改任务标识会隔离迟到响应。令牌不写入 URL 或浏览器持久存储。
- 不带这些新字段的旧任务继续采用原执行取消逻辑。

## 验证

`node --check frontend/app.js` 通过。离线探针 `probes/sim/verification_cancel_browser.py` 使用全部请求拦截的明确 fixture，12 项检查通过：按钮及确认文字、单次 POST、接收后等待测试退出、GET 确认已取消、未知回执不重投、退出后迟到响应隔离、令牌清空、持久存储为零、无页面错误及手机无横向溢出。共 3 个模拟取消 POST，真实 API 写入为 0。

运行命令：

```sh
FONTCONFIG_FILE=/tmp/agentteams-browser-fonts/fonts.conf LD_LIBRARY_PATH=/tmp/kanban-browser-libs/usr/lib/x86_64-linux-gnu .venv/bin/python probes/sim/verification_cancel_browser.py
```

证据：`.omx/state/verification-cancel/report.json`、`desktop.png`（1440×1000）、`mobile.png`（390×844）、`ralph-progress.json`。沿既有工作台参考进行一次视觉核对，93 分通过，未修改视觉布局。

本轮仅证明前端模拟交互与措辞，不替代后端真实取消、测试容器退出或故障恢复验收；未调用真实模型、停止接口或修改正式配置。
