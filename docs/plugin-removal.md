# 插件卸载维护

放开的限制只有适配服务里的卸载门禁。没有放开 `10.40.0.2`、taskField 或宿主 user namespace，也没有重启 QwenPaw、适配器或网关。执行模型仍是 `gpt-6-luna`。不交付运行时热更新。

热卸载和热换版始终拒绝。`GET /extension/v1/maintenance/plugin-removal` 只读返回决定；`POST` 同一路径不会卸载插件，也不会释放写租约。有在途任务、未知执行或写租约时返回 409，原因是 `inflight_uninstall_forbidden`。维护锁还没加上时返回 409，原因是 `admission_still_open`。只有新准入已停止且没有在途任务和写租约时，才返回冷维护步骤，`uninstallPerformed` 仍是 false。

冷维护步骤：

1. 进入维护，停止新准入。
2. 核对每个在途任务的身份。未知执行保持原写租约。
3. 在途任务和写租约都清空后，停止宿主。
4. 宿主停止后再删除插件文件。不要在宿主运行时热卸载或热换版。

宿主自己的 `unload_plugin` 捕获钩子异常后仍会继续，本服务不能靠插件钩子把它拦住，也不修改上游。因此热更新不算交付，AC-71 记为不适用，不是通过。

2026-10-27 只重载了正在监听的适配器，`1237908` 换成 `3460260`，绑定仍是 `0.0.0.0:3193`。QwenPaw `1916664` 和网关 `1918031` 没有重启。网关转发后的 GET 是 200，POST 是 409，原因仍是 `inflight_uninstall_forbidden`；`uninstallPerformed` 和 `leasesReleased` 都是 false，租约仍是 0。尾任务 `task-d61d558f568f49ecaf15d583005b2013` 仍是 registered、版本 6，没有 `submission_intent`。新进程的执行准入仍是未知，`readyForExecution` 为 false，所以这次重载没有打开新执行。证据是 `var/adapter-reload-20261027/report.json`。
