# 连续 Codex 会话收尾

QwenPaw 仍是主程序，Agent、会话映射和原生 ReMe 继续保留。此修复仅在现有执行插件中调用官方公开的 `HarnessRuntime.stop()`，释放已完成任务使用的 Codex adapter；下一任务由官方 Runtime 创建新的 adapter/client。不修改上游、不删除会话、不重置记忆，也不新增运行服务、依赖或数据库表。

## 问题与证据边界

较早的隔离审批探针第二个任务在 thread/turn 创建前收到 `Codex app-server exited`。第一容器退出与第二调用发生在相邻一秒内，且仅有第一份 launcher 回执；该现场符合退出中的客户端被复用，但缺少完整进程/reader 时间线，不能断言唯一根因。

固定官方源码的零模型回归另确定性复现了一个窗口：旧进程已退出，客户端创建新进程，但旧 stdout reader 延迟进入 `finally`，其共享 pending 清理使新请求收到同一错误。原 Docker/launcher 退出证明不能单独消除旧 reader 的收尾影响。

## 最小处理

1. 沿原任务身份确认真实 native terminal、Docker exit 和 launcher exit。
2. 使用官方 `get_loaded_agent()` 读取原固定目标，不惰性启动已完成角色；核对目标 ID、工作目录、当前配置及无活动任务。
3. 目标仍加载时核对原生任务 finished；重启导致旧临时句柄 404 时，仅允许已经验证的原 native terminal 和正向退出证据覆盖该缺口。其他错误、活动任务或另一未释放执行均不收尾、不释放租约。
4. 调用目标的公开 `workspace.harness_runtime.stop()`；它清空 adapter 缓存，旧 reader 仍属于旧 client，不能触及下一 client 的 pending。
5. 在现有 receipt JSON 持久保存精确的 `runtimeRetirement`，然后才允许资源释放。事实包含原 task、target、workspace、完整执行身份和原 owner/container。旧任务再次查询只核对原事实，不停止当前 Runtime。

桥接提交与收尾共用一个进程内锁；重复提交的观察在锁外执行，避免重入。保存收尾事实失败时仍保持未释放。宿主重启后的 GET 从请求恢复应用引用，已持久事实不依赖原临时任务句柄。

## 使用边界

固定 Codex target 专用于 Agent Line，并通过原工作区租约顺序执行。原生 UI 直接并发调用该 target 不受桥接锁保护；事后活动检查不能撤销已发生的停止。因此不能将本修复宣称为任意原生 UI 并发的原子保护。普通用户 Agent、会话与记忆不参与该 Runtime 收尾。

原 `var/native-permissions-attempt2/` 的失败任务和租约保持，不重投、不改旧证据，也不将新修复追认成旧审批拒绝通过。

## 当前验证

- `tests/test_codex_client_retirement.py` 直接加载固定官方源码，复现旧 reader 窗口，并证明官方 Runtime 收尾后新 client 不受影响；无模型或真实进程。
- `tests/test_bridge_runtime_retirement.py` 验证身份、活动任务、停止/落盘失败、重复旧查询、重开账本和锁边界。
- `tests/test_bridge_retirement_routes.py` 通过真实双认证 HTTP、原 `observe` 和原 SQLite 接线验证重启后的 404 收尾与旧任务查询；外部进程 I/O 明确为模拟。
- 本项目 QwenPaw 插件经备份、官方 CLI 安装和原端口重启；九份配置摘要、原 35 条执行/31 条绑定/7 条撤销逐行保持，无 schema 迁移，主准入仍为 unknown/guarded。部署记录位于 `var/plugin-upgrades/runtime-retirement-v1/`。

真实双分支执行已通过，根代理另核对两独立 Codex 身份、原容器与 launcher 退出，以及两条精确绑定的 `runtimeRetirement=stopped`；详见[真实双分支记录](real-parallel-role-workflow.md)。默认重复0原生写，原配置/守卫不变。最终全量1122项测试通过；这不代表整个原生审批或完整 PRD 已验收。


## 已核实中断且旧原生句柄丢失

主服务在途重启实测另补一个严格内部路径：桥已重新读取完整原绑定与退出证明，
且唯一匹配身份明确 nativeTerminal=false、原生状态 unknown、容器确已退出时，
才允许覆盖旧句柄404并调用上述官方 Runtime.stop。普通404没有此权限；当前账本
出现相反终态、身份变化、活动任务、其他未退出回执、stop或落盘失败均保留未释放。
不建立新状态表，不把资源退出改成原生任务成功。11项确定性回归及主服务真实
中断任务收尾、随后独立Codex/三测试成功的证据见[宿主重启](host-restart.md)。
