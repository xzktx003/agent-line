# 业务结果契约适配后端重载

2026-10-23按原var/adapter/service.json、原命令/目录/环境，优雅替换主适配PID3252739为3510681，新进程持有0.0.0.0:3193监听socket，日志确认Application startup complete。原task-result结构校验、编码/审查业务JSON投影与回传、授权格式目录现已装入主适配进程，中文静态工作台仍由同源挂载提供。没有新增服务、依赖、上游补丁或业务配置。

入口：

- QwenPaw：http://10.30.0.28:3194/qwenpaw/
- 工作台：http://10.30.0.28:3193/workbench/
- 流程搭建：http://10.30.0.28:3193/workbench/editor.html

操作前只读确认原进程身份、19任务全部终态且资源已释放、成功任务待验收0、租约0、准入保护0。生成一致性SQLite备份；使用既有admission.lock和admission_guard建立本次专有临时维护保护，再确认任务/事件和配置未变化后发送SIGTERM。原进程退出后才启动替代进程，避免并行后端。启动与数据保留确认后，只撤除本次相同owner/reason的维护保护，最终准入保护回到0；没有修改执行就绪证据或重新授予执行权限。

前后tasks/events全行摘要、19任务状态（17成功、1失败、1取消）、适配配置和10份原生QwenPaw/Agent配置摘要均一致，活动工作区租约0。原QwenPaw进程/插件未重载，新原生Codex用量采集仍不能声称已加载。

私有记录位于var/backend-rollout-business-20261023-85a1vk_b/：before.json含状态/配置/源码摘要，tasks.sqlite为保护前备份，maintenance.json含本次维护身份，process.json、adapter.log、result.json记录新进程与部署结果。目录0700、文件0600，不公开配置、日志或维护身份值。不自动恢复旧库，也不覆盖项目代码。

本次是部署操作，只核对进程、监听、启动日志和数据保留。没有调用HTTP健康/目录/业务接口、运行测试、Node、浏览器、真实模型、探针、lint或类型检查，没有取得新就绪证据，不将端口监听当作业务条件分支验收通过。AC保持20/22/13，真实工作流验收和完整PRD仍待完成。
