# macOS界面与运行任务目录适配服务重载

2026-10-23按原命令、工作目录、完整环境和var/adapter/service.json，优雅替换主适配PID4189247为233248，新进程持有0.0.0.0:3193监听，并记录Application startup complete。操作脚本退出后再从/proc确认进程与该监听仍在。

最新资源包括[macOS界面](macos-interface.md)及[运行到节点任务目录](run-task-directory.md)。界面沿用同源静态目录，无新前端构建或服务；后端新project/workflow/run筛选与回显源码已装入，不以此宣称业务查询或视觉验收通过。

原入口保持：工作台http://10.30.0.28:3193/workbench/，流程搭建http://10.30.0.28:3193/workbench/editor.html，QwenPaw主入口http://10.30.0.28:3194/qwenpaw/。

操作先核对原PID启动身份/命令/目录/监听及空闲状态，创建一致性SQLite备份；通过已有admission锁和guard设置本轮专有维护保护，核对数据与配置后SIGTERM旧进程，等待退出再启动新进程。完成后仅撤除相同owner/reason的本轮保护，没有授予新执行权限或更新就绪证据。

前后19任务及事件全行摘要一致（17成功/1失败/1取消、资源均释放），成功待验收0、活动0、工作区租约0。8份适配配置与10份QwenPaw/Agent配置摘要保持，本轮源码摘要保持，最终维护保护0。未重启QwenPaw、安装插件、修改原生记忆或上游源码。私有记录var/backend-rollout-macos-20261023/包括before.json、tasks.sqlite备份、maintenance.json、process.json、adapter.log和result.json；目录0700/文件0600。

本轮仅部署与进程/socket/启动日志/数据保留核对，没有主动HTTP健康/业务请求、浏览器、截图、测试、Node、模型、探针、lint或类型检查。当前健康没有重新查询；此前unknown/false不是本轮新结论。原生Codex采集插件未热加载，不能声称已生效。完整PRD和AC计数仍为20/22/13，不能用成功监听替代真实工作流或界面验收。
