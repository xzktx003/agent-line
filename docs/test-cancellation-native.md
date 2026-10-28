# 真实测试阶段取消与应用重建

2026-10-22，本机运行 `probes/executor/cancel_test_commands.py` 已通过真实 Docker + 当前 ASGI API 验证。没有新监听端口，没有调用模型、修改角色或原生记忆，也没有修改主服务配置。

必须区分证据范围：前置 TaskStore 的原生执行成功和资源退出元数据是**明确标注的 synthetic fixture**，不是实际 Codex 执行。本次真实部分是测试容器、后台 AutomaticVerifier、HTTP 取消、应用重建及持久资源证据。

## 实际结果

- 原任务：`task-8e5bbc0d3bb04f8f8b5dfdcf1806ad65`。
- 唯一测试容器：`579e2f3993fecdbaa41d3996a36a2d31b35b345b6826f4d50d1261f58100654e`。
- 第一条测试启动 Python，派生调用 `setsid()` 并等待 90 秒的子程序，自身写 `/tmp/started` 后等待 60 秒。探针核对完整容器身份、owner、实际 Running/PID，并通过只读 `docker exec cat /tmp/started` 确认脚本已运行。没有独立采集该子程序的 setsid 完成状态，因此不额外声称已逐进程验证 session ID。
- 关闭第一份 TestClient/后台线程后，原容器仍 Running。随后重新构造 TaskStore、DockerTestExecutor、AutomaticVerifier 和 ASGI app，查询同一原任务。
- HTTP POST 原任务 `/cancel` 返回原生 `succeeded`、验收 `pending` 与取消意向；没有把已有 native resourcesReleased 当成测试退出。
- 后台实际 `kill` 原 full ID，再通过 inspect 得到退出码 `137`、`running=false`、`pid=0` 的退出证据，保存原测试 check 后清理容器。
- 第二条测试 disposition 为 `not_started`，持久记录 `createIntent=false` 且没有 `containerId`。没有创建第二个测试容器。
- 最终 `verificationStatus=canceled`，原生状态仍 `succeeded`，原工作区租约数为 0。专属 `keep.txt` 字节未变化，原容器精确查询为空。
- 重复 HTTP 取消和再次应用重建共新增 Docker 调用 0；验收及测试回执文件字节稳定。再运行脚本进入 observation-only，新增写入 0。

完整 Docker 操作序列：create → inspect → start → inspect → inspect → kill → inspect → logs → inspect → rm。没有重建、重复启动或第二次终止。

## 证据及复查

私有目录 `var/test-cancellation-native/`，目录 0700、JSON 0600：

- `state.json`：操作意向、原任务及容器身份、实际取消响应。
- `docker-calls.json`：执行器调用操作及时间，不含凭据。
- `report.json`：真实退出 check、取消总回执、重建及幂等验证。
- `evidence/`：逐测试持久意向、撤销、退出与汇总取消证据。
- `tasks.sqlite`：独立探针任务及工作区租约状态。

执行命令：`.venv/bin/python probes/executor/cancel_test_commands.py`。已有 state 时只观察，不以新身份重投。此次进程 PID 2097722 正常退出，退出码 0。

根代理另以SQLite mode=ro核对唯一原任务、取消意图与完成事件、两项测试证明、总回执字节、原租约归零与保留文件，并通过Docker精确查询确认原fullID已清理；复核为 `var/test-cancellation-native/root-check.json`。此前原生执行元数据仍仅为synthetic，不扩大证据范围。
