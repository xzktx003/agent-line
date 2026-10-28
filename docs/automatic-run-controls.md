# 真实任务运行控制探针

入口为 `probes/full_workflow/run_controls.py`，私有证据位于 `var/automatic-run-controls/`。使用既有 `agent-line-native-loop-parent`、原生 ReMe 和既有受控 Codex 容器，不改主服务、角色、插件或主维护门禁。只重建探针自身的应用生命周期。

探针通过真实 AuthoringAPI 创建、保存、发布 Start → Agent → End，自动目录提供运行绑定。两个用例串行，各最多一个真实 Codex 任务，专用产物和开始标记互不共享。Codex 必须前台写开始标记、等待 20 秒、再写最终产物；只有原 thread/turn 的受控容器仍 Running 且开始标记字节匹配时才发控制，不能用固定延时推断已经开始。

- `pause_continue`：运行中暂停，确认不取消原任务；任务完成后通过独立 ArtifactPolicy 验收；确认 Sim 仍停机器等待、没有 resume 日志。停止并重建探针应用，确认 paused 和任务身份保留，再显式 continue，核对原结果抵达 End，无新 Codex 任务。
- `stop`：运行中持久停止、请求取消原任务和原 Sim 运行；继续观察原身份，必须取得真实容器退出证据。取消 ACK 不充当资源退出。确认没有结果回传到 End。

本图没有第二 Agent；不能据此宣称真实验证了第二 Agent 不登记。该边界由运行控制集成测试覆盖。原生 ReMe 自动记忆可能随父会话正常更新；探针不手工写回记忆或更改后端。

每次外部操作先保存意图。已有 state 时默认仅查询原 IDs，不继续提交或创建新尝试。最终结论以 `report.json` 为准；`failure.json` 表示未完成，不能当作通过。

## 本次真实结果

两个用例均完成，`report.json` 为 `passed_live`：

| 用例 | 原运行 | 任务 | 最终结果 |
|---|---|---|---|
| 暂停／继续 | `2b930c7f-a2a4-5bcf-a11f-244cecdce9e1` | `task-8e615cde996f4a3e888695104b130653` | 原任务 succeeded，独立验收 passed，继续后 child `9986b15b-a609-4623-ae55-92368bbd3a0d` 到达 End |
| 停止 | `2ade4df7-46fb-54ba-a654-a8d53cef5bcb` | `task-a6eb838489574bec890001cf38d1ca73` | canceled，真实资源退出；最终产物不存在，验收保持 pending，没有 resume |

暂停后观察到相同 native thread/turn 仍在执行、资源未退出、没有取消请求。完成后独立字节验收通过，暂停中的后台完成回传被阻止。探针应用重建后仍为 paused；显式 continue 后仅回传原任务，没有再执行 Codex。

停止操作刚返回时任务仍为 cancel_requested、resourcesReleased=false；随后原任务取消并取得 `docker-inspect-before-remove` 正退出证据，未将 ACK 当作停止证明。两父任务均为 finished，所有工作区租约归零，owned active containers 为零，主维护状态前后相同。重复 trigger 和旧控制 operation 没有新增 native POST；默认重新运行只有两条原任务观察，native writes 为零。

首次脚本退出源于 End 查询结果解析错误：Sim `selectedOutputs=<end>.data` 将完整 selector 作为结果键，原脚本误读为嵌套键。真实暂停／继续行为已经完成，`failure.json` 保留该首次错误。修正后显式 `--finish-remaining` 只核对已完成的原 End、父任务与原身份，并执行此前尚无提交意图的 stop 用例；未重新提交暂停任务。该恢复分支遇到其他已提交但未确认的 case 会拒绝，不重发。最终通过报告不会删除早期失败证据。
