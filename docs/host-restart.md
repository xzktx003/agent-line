# 真实在途 QwenPaw 宿主重启探针

`probes/executor/host_restart_probe.py` 使用独立TaskStore、RoleDispatchLedger、新task/session和随机阶段文件。经过正式RoleDispatchGateway父角色召回/委派进入现有固定容器Codex角色；实际观察stage1和native thread/turn后，仅使用本项目manager stop/start重启QwenPaw。其他模型任务已协调暂停，不重启其他服务或Docker daemon，不解除主库maintenance。

manager必须恢复同一个端口。无论停止成功还是探针断言失败，finally都调用manager.start恢复本项目宿主。恢复后重新创建gateway/store实例，只reconcile/observe原task，不执行submit；核对原始父任务ID、完整native身份和外部POST计数。

在原sleep60秒窗口之后检查stage2没有写出，核对受信容器退出receipt。原生终态必须独立观测；如果仅容器退出而原生终态未知，报告requires_reconciliation，不将退出码或HTTP确认伪装为turn_aborted。

私有证据：`var/executor/host-restart/receipt.json`及同目录两个SQLite。脚本重复运行不得新建任务；未知提交/未到重启阶段的旧意图要求核对原任务，不盲重跑。完整主库maintenance在整个过程中保持不变。

## 前期尝试：尚未进入宿主重启阶段

第一轮父任务 `task-7b64fb5a184a` 官方180秒超时，未有native执行身份或stage1。保留原TaskStore/RoleLedger；随后通过正式委派撤销接口持久撤销迟到委派，revoked=true，不把此事实伪装成native终态或资源释放。

第二轮显式新attempt曾错误地建立独立TaskStore，绕开第一轮未知任务仍占用的工作区租约。这是探针设计错误，已停止后续执行并报告主线，不把其视为合格恢复验收。该轮native虽task_complete，但因父指令中的“委派后报告”被传入执行角色，Codex再次委派后提前返回，未生成stage1；容器桥随后退出容器，未执行宿主重启。所有失败证据保留在attempt-2目录。

脚本现已修正：未来新attempt共享原有TaskStore/RoleLedger，不能以新库绕过未知租约；执行指令明确要求Codex亲自shell、禁止再委派并等待命令完成。未解除旧未知资源租约，不再发新任务，待主线协调可证明的旧任务收尾或独立workspace方案。当前QwenPaw宿主未被本探针停止，端口未改变。

主线随后通过受信 `resolve_unstarted_cancellation` 入口处理第一轮：live官方父任务finished，task级撤销true且executionClaimed=false，持久RoleLedger角色/session匹配；原任务现为canceled/resourcesReleased=true，但executionIdentity仍为空，没有伪造native终态。证据 `var/executor/host-restart/unstarted-resolution.json`。

经再次核对上述状态，第3次尝试使用原 `host-restart/tasks.sqlite` 与 `roles.sqlite`，明确workflow attemptId=3；只有探针私有输出放在attempt-3目录，没有创建新执行库。主库maintenance保持不变。

## 第4次尝试的自动收尾实测

第3次任务 `task-89e1b03702644df8b769bc7891528ac3` 已有native task_complete及容器退出证明，但没有stage1，业务验收失败。原因是传给执行层的协调指令导致其寻找不存在的委派工具。此结果未被写成重启成功。启动第4次前，明确核对第3次resourcesReleased=true且共享workspace_leases为空。

第4次任务 `task-8603a783ff9b4617aae7e12c07cf83a1` 使用纯最终shell脚本、明确attemptId=4与同一原数据库；父任务 `task-9a13a2400b35` 官方180秒超时，尚未创建Codex身份，因此没有停止或重启QwenPaw。

主线自动“未启动执行取消”接线完成后，在新进程仅执行原任务request_cancel → Dispatcher.reconcile → RoleDispatchGateway.cancel，读取fresh官方撤销与live父终态，由正式内部证据自动收尾。没有手工注入proof，也没有调用submit。

实测结果：原任务canceled/resourcesReleased=true，executionIdentity仍为 `{}`；原工作区租约已删除，重新打开共享TaskStore后结果保持。证据 `var/executor/host-restart/attempt-4/automatic-unstarted-resolution.json` 含before/after任务、before/after租约与持久RoleLedger。此结果证明自动未启动任务收尾，**不证明在途宿主重启恢复**。

第4次收尾时QwenPaw保持PID2261737、端口52403；本探针当时没有执行manager.stop/start。主库maintenance未解除，所有旧失败证据保留。在途stage1→重启→原identity恢复仍待父模型可用时继续真实验收。

## 父任务超时诊断与第5次受控尝试

只读诊断脚本 `probes/executor/diagnose_parent_timeout.py` 检查第4次持久会话及对应日志窗口：工具结果、thinking记录、usage事件及stream retry warning均为0。证据能定位到首次持久模型/工具输出之前，不能区分限流器等待与提供者请求/首响应延迟，不能据此宣称429或网络故障。

官方实现中 `providers/retry_chat_model.py` 先等待限流器和inner模型调用，再包装返回流的首内容watchdog；当前运行配置的acquire timeout为300秒，而旧探针外层console task限时180秒。单次官方模型live test成功且耗时2.355秒，只代表当前简单请求可用，不解释此前延迟。私有证据为 `attempt-4/timeout-diagnosis.json` 与 `provider-latency-check.json`。

本次仅将探针自身父任务限时设为420秒，不修改上游、角色模型或全局提供者配置。第5次启动前核对第4次已canceled/resourcesReleased、共享工作区租约为空；继续使用同一tasks.sqlite与roles.sqlite，没有新库绕过。启动命令为 `.venv/bin/python probes/executor/host_restart_probe.py --attempt 5 --parent-timeout 420`。脚本失败时通过正式request_cancel与reconcile收尾，不手工注入proof，也不自动创建下一个attempt。第5次结果以 `var/executor/host-restart/attempt-5/receipt.json` 为准。

## 第5次：首次观察发现原生终态缺失

任务 `task-0713175281df4b80bf38d9e34ad732d9` 在420秒限时内实际经过父角色进入容器Codex，写出stage1。父任务 `task-378dfd90b336`；原生task `task-387ebfbce3f4`、thread `01a15e62-adcd-7181-9955-6b82e4fd4e30`、turn `01a15e62-bea0-7f33-a005-d8604e0be5ff` 已在停止宿主前落盘。

manager.stop成功；第一次manager.start返回1，其stderr未留存，原因不能确定。立即重新调用同一manager.start恢复PID2551933、同端口52403，然后复用同一attempt的恢复分支，仅observe/reconcile，没有重新提交。此异常与恢复过程另存 `attempt-5/manager-recovery.json`，避免恢复时返回码覆盖首次失败事实。

真实结果：父任务及原生身份完全不变；恢复后父提交次数为0；独占容器 `4bd2783cc1baaba16758351579ec8b1bdab6719ebc2c7db6f16550fa26c7c54c` 具有匹配owner/thread/turn的Docker退出receipt（running=false、pid=0、status=exited、exitCode=137），超过原sleep60秒窗口后stage2仍不存在。

未通过的验收：原生没有留下turn_aborted或task_complete，receipt的nativeTerminal=false。插件仍返回accepted/terminalConfirmed=false，主线TaskStore仍保留resourcesReleased=false及原工作区租约。`verified=false`、`phase=requires_reconciliation`，不能把受信资源退出证明替换成原生终态。本次证明了宿主退出后容器清理、身份持久化和不重复提交；尚未证明完整任务终结恢复。没有创建第6次任务，没有解除主库maintenance。

后续应由主线明确增加“宿主丢失且受信executor已终止”的恢复语义，独立记录native终态未知与资源确已退出，保留原身份和退出证据；不应伪造上游turn_aborted或把任务重新发给模型。manager首次启动失败的详细错误记录与受控重试也应补齐。

故障收尾已对第5次原任务仅执行request_cancel→reconcile一次，结果cancel_requested、resourcesReleased=false、原identity不变、共享lease仍为1；证据 `attempt-5/cancel-reconciliation.json`。这是持久取消意图，不是原生取消完成。恢复后真实HTTP认证/健康检查通过。

## 中断证据实现（部署与真实验证见下一节）

执行桥新增独立字段 `executorInterrupted=true`、`nativeStatus=unknown`、`reason=executor_exit_without_native_terminal`。仅在既有角色/工作区/原生thread/turn绑定成立、control observe给出严格匹配的owned退出正证据时，才同时返回resourcesReleased=true及完整resourceExitReceipt；terminalConfirmed仍为false，status不伪造成原生failed/canceled。未知、缺失、活容器或篡改证据均不认中断。此观察不调用control stop，也不创建或恢复Codex执行；原生终态路径保留原有受控stop与退出校验。取消仅追加持久意图，不能用stop ack覆盖或制造资源证明。

新增回归覆盖原生404但容器退出证据存在、缺失/活容器/错身份/畸形证明、重复观察、绑定变化清旧证明、取消保留证据与正常native terminal路径。11个桥测试、真实插件加载契约测试及基础lint通过。本切片未热安装、未重启宿主、未收尾第5次任务；正式状态收敛由主线整合后验证。探针的manager stop/start现在将returnCode、stdout/stderr、时间和timeout信息追加保存到0600私有receipt.managerActions，避免下一次覆盖失败原因；未对本次缺失stderr追溯猜测。

竞态修复：第一次读取rollout尚无终态时，control.observe等待期间任务可能正常完成。桥现在在取得退出正证据后重新读取完整绑定及最终rollout，若出现task_complete/turn_aborted则走原生终态路径；matching receipt identity中的nativeTerminal=true（或缺失）不能被当作中断。确定性时序回归在control返回proof前实际追加task_complete，并检查结果为succeeded而非interrupted；另验证第二次读取中metadata或turn被改动时拒绝中断。未靠sleep规避竞态。

## 最终中断收尾验证

主线通过官方认证插件安装接口热装扩展，安装后的execution_bridge.py与独立扩展源文件字节一致。没有修改上游源码或重新启动业务任务。

`.venv/bin/python probes/executor/reconcile_interruption.py --attempt 5` 从原tasks.sqlite/roles.sqlite打开原任务，使用正式RoleDispatchGateway与Dispatcher重新观察并处理原取消意图。结果为failed/resourcesReleased=true；原thread/turn/upstreamTaskId全部保持，nativeStatus仍unknown、terminalConfirmed仍false，保存独立execution_interrupted与resource_exit证据。取消意图为true，独立验证状态不变，原租约与结果在同事务收尾。

该探针在两个独立进程各运行一次，均verified=true、newSubmissions=0，重开TaskStore状态一致。私有证据为`var/executor/host-restart/attempt-5/interruption-reconciliation.json`，保留两次invocations与完整前后记录。没有手工注入proof或把中断改成成功。

本结果证明已观察到的宿主重启中断可以安全识别、无重复执行并回收租约；原任务的stage2未执行，不是业务任务成功或透明续跑的证明，也不替代所有故障/恢复验收。

## 主适配服务的在途重启与最近核对状态（2026-10-22）

`probes/executor/inflight_adapter_restart.py` 复用原主 TaskStore、角色绑定与执行器。
仅新目录的首次 `--execute` 可登记并提交一条任务；已有状态的所有调用只读原库和
公开 HTTP，不重投或重启。运行证据在 `var/inflight-adapter-restart-20261022/runtime/`。

本轮原任务为 `task-b0f350afee664b8ea71c4a9ed93dd7bf`。实测写出 stage1、取得
同一 native task/thread/turn 与正在运行的 owned 容器证据后，适配服务真实停起；
原身份和单次 claim 保持。随后 QwenPaw 停止期间，在线适配器公开返回独立的
`executionObservation.status=unknown`，执行事实仍为 accepted，租约保留。

执行观察与执行结论分离：现有事件表仅在 observed/unknown 变化时追加事件。
`executionObservation.updatedAt` 是该状态的变更时间，不是每次轮询时间。
查询失败、无效回执或父句柄不可查询时，不把旧的 running 缓存当作当前观察；
原父记录仍保留供追溯。已取得原执行记录也不等于任务成功或资源已经退出。
已结束且资源退出的旧任务不补写首次观察；只有已有 unknown 可在完整正向核对后
清除一次。重复坏回执不会先写 observed 再退回 unknown。

实测发现管理脚本首次重启两次因 EADDRINUSE 失败，原失败记录保留；核实端口
释放后已恢复同端口，见 `runtime/manager-recovery.json`。修复仅让端口探测使用
与服务一致的 SO_REUSEADDR；真实临时 TIME_WAIT 能复用，已有 live listener
仍拒绝，不随机换端口或增加重试状态机。

另发现 target 已加载时，旧句柄404会阻止已验证的中断收尾。执行桥只对已核对的
单一 nativeTerminal=false 退出证明开放内部中断收尾分支；仍需原角色/工作区、
完整相同身份、无活动任务、无其他未退出回执、官方 Runtime.stop 及持久退休事实。
普通404、未知退出或活动任务不获得该例外。新实现不伪造原生终态，不修改上游。

最终通过官方 CLI 停机安装原插件并同端口启动，安装副本与扩展源文件一致。
首次即时 verify 因启动尚未就绪失败，保留原记录；等待同一宿主就绪后 verify 成功，
没有据此反复重启。原任务沿同一身份收敛为 failed/resourcesReleased=true，原生
terminal 仍 unknown；证据只有 execution_interrupted/resource_exit，不伪造成功。
原容器 exit137/pid0/nonrunning、launcher 消失、runtimeRetirement=stopped，租约0；
超过180秒仍无 stage2。独立只读审计26项通过，AC-16按“原身份核对或明确未知”单项通过。

另登记一次独立的启动后验收任务 `task-e8dded60c25a40909768b1c625bddea2`，
复用原角色/ReMe/绑定，实际 Codex 与三个独立 unittest 通过、原父任务 finished、
资源退出，原14条任务全部数据库行保持。刷新原配置指定的执行证据后重载主适配服务；
原 service.json、执行配置和角色保持，当前 confirmed/ready、lease0，匿名401，
同机局域网3193健康与3194主入口200。未声称另一台设备已验收。

证据：`AC16-review.json`、`AC16-main-GET.json`、`browser-native/`及
`post-restart-revalidation/{bootstrap-state,restored-main-report}.json`（均在本轮私有目录）。
已有状态再带 --execute 仍只观察；原失败 phase 和两次 EADDRINUSE 记录保持，
不将探针旧输出改写成首次重启全成功。全量1570项测试通过（70.985秒）、UI62项
明确fixture检查通过，实际无mock页面另核对失败/未知/资源退出分离显示。
本结果不证明中断透明续跑、所有故障窗口、8小时运行或完整PRD通过。
