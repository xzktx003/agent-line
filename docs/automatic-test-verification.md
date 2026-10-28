# 可信测试命令自动验收

目标是补齐 PRD 的真实测试证据：继续使用现有验收线程、策略冻结和工作区租约，不增加常驻服务、依赖、记忆后端或上游补丁。

实施范围为服务端配置测试 argv，发布→运行→任务冻结；Codex 实际退出后，在固定镜像、无网络、无凭据、只读工作区的临时容器执行。独立保存退出码、有界日志和正资源退出证据，最后与文件检查共同决定 verificationStatus；Sim 仍负责后继调度。旧 schema1 文件策略和回执保持兼容。

执行前保存提交意图，恢复只查询原容器。未知创建/启动、无法观察退出或环境变化时保持阻塞及原工作区租约，不重跑命令、不创建替代执行。完整证据先落盘，再沿用原事务提交验收与释放租约；数据库提交失败后只复用证据。

## 配置

仍使用同一私有服务 JSON，不增加配置文件或常驻进程：

```json
"verification": {
  "evidenceDirectory": "/absolute/private/verification-evidence",
  "testExecutor": {"imageId": "sha256:<已存在的完整64位本地镜像ID>"}
}
```

镜像须提前具备 `/usr/bin/python3`、`/usr/bin/timeout` 和测试需要的工具或依赖；执行器不下载镜像或安装软件。目录需实际存在、所有者权限0700，并与项目目录分离。可用 `DockerTestExecutor(directory, image_id).environment_id` 取得真实环境摘要；上例占位值须替换。

角色绑定中的 `verificationPolicy` 使用 schema2：

```json
{
  "schemaVersion": 2,
  "policyId": "project-unit-tests-v1",
  "kind": "command_and_artifacts",
  "requireTests": true,
  "testEnvironmentId": "docker-test-v1:<运行器返回的64位摘要>",
  "artifacts": [],
  "tests": [{
    "id": "unit-tests",
    "argv": ["python3", "-m", "unittest", "discover", "-s", "tests"],
    "timeoutSeconds": 60,
    "maxOutputBytes": 65536
  }]
}
```

`artifacts` 可声明0–32个文件，规则与旧字节策略相同。`tests` 为1–8个独立ID；每项 argv 为1–64个非空字符串，总字符数最多16384，不经宿主 shell 拼接。单项超时1–300秒，默认60秒，策略总量最多900秒。单项输出上限1–1048576字节，默认65536，总量最多4194304字节；整数不接受布尔值。浏览器和模型不能覆盖这些值。

环境摘要绑定固定镜像、执行UID/GID、网络/挂载/资源限制、超时参数、输出收集器源码摘要和日志配置。环境不符阻止新测试，不将旧流程自动更新到新环境。schema1仍只允许文件字节策略，不通过把其 requireTests 改为true隐式升级。

## 结果与恢复

每项 `test_command` 记录 argv、环境、退出码、有界 stdout/stderr、截断标记，以及绑定原 taskId、Codex身份、策略摘要和测试ID的 Docker 退出证明。只有退出码0、非OOM、输出未超限且实际容器退出并完成清理才判通过；失败或缺少证据不会因模型声称通过而放行。

Python收集器先有界缓冲，最终输出最多 `maxOutputBytes+1` 字节；超限失败。Docker日志另固定8MiB×1。超时在容器内部由GNU timeout约束，容器退出后其PID命名空间内的后代也退出。超时可能损失尚未输出的诊断日志，不因此判成功。

提交意图、启动意图、完成证据和清理状态各自持久化。清理失败或回执未知继续查询原完整containerId；先核对所有权、镜像、参数、挂载和原退出证明，再清理。`removed:true` 后重复读取不调用Docker。第一项未知时不启动下一项，最终回执落盘后数据库失败则复用原回执，不重跑测试或重新读取变化的文件。

## 已取得证据

- `probes/executor/test_commands.py --attempt 2`：真实Docker的正常通过、exit7、超时含独立setsid子进程、只读目录拒写、输出超限五项通过；五个原容器均确认清理，已保存结果重读0 Docker调用。上游执行身份明确为synthetic，此探针不能替代真实Codex闭环。证据：`var/test-executor-native-2/report.json`。attempt1为日志/清理修复前证据，全部已结束，不挪作最终版本结果。
- `probes/full_workflow/pinned_roles.py --test-commands --reuse-known-template`：原模板revision2→原生ReMe角色→实际Codex实现add函数→独立三条unittest→自动验收→Sim passEnd。测试exit0且真实日志`Ran 3 tests`/`OK`，测试容器removed，父任务finished，租约与自有活动容器0。根代理从实际账本、文件和原生接口独立复核，见 `var/pinned-test-flow-known-template/root-check.json`。默认重跑仅观察，未再提交。
- 首次 `--test-commands` 创建新图时原生请求回执uncertain，未启动模型或测试。原意图保留在 `var/pinned-test-flow/`；未重投，未换ID重新创建。后续验证明确复用已有且已核验静止的原模板，旧journal只读备份保留所有权凭证，发布新版本；没有接管未知新图。
- `--test-commands --reuse-known-template --test-failure` 已通过真实失败路径：模板revision3，Codex成功写入指定减法实现且字节检查通过，但独立加法测试exit1，验收failed，Sim实际选择failEnd，businessVerified=false。根代理独立核对实际账本、原生接口、测试日志与分支，见 `var/pinned-test-failure-known-template/root-check.json`。虚假 `testsPassed=true` 声明由单元测试覆盖；本次真实模型不额外声称曾作该虚假声明。

## 测试阶段取消

任务取消接口现可持久申请停止待验收的schema2任务，包括原生编码已成功但退出证明仍未到达的窗口。编码状态与产物保持原样；`verificationStatus=canceled` 独立表示验收阶段已取消，不把编码的succeeded改成canceled。

尚未运行的测试先保存撤销记录，重启也不会新建或启动。活动测试只按原完整容器ID、所有权与参数终止，取得实际退出及清理证据后再收尾；未知创建、启动、终止或清理保持pending与原租约。已经完成的测试保留原check。取消汇总必须覆盖冻结策略中的所有测试，未启动项不伪造退出码或进程证明。

同任务私有锁将取消提交与一次验收轮询串行化；锁后才进入数据库，Docker调用不持SQLite写事务。取消先持久提交时，正常验收事务也拒绝旧passed回执覆盖它；取消回执先私有落盘，再与验收canceled状态及原租约释放同事务提交。已有取消回执恢复只读，不重跑测试；精确重放不删除后继任务租约。

节点详情使用服务端 `canCancelVerification` 显示“停止验收”，`verificationCancellationRequested` 只表示等待收尾；即使编码资源已退出，也不会把它当成测试已经退出。中文交互及明确fixture验证见[停止验收界面](verification-cancel-ui.md)。

真实Docker＋当前ASGI探针已证明活动测试跨应用重建、HTTP取消、exit137正退出、第二测试从未启动、容器清理及lease0，保留文件字节不变，重复0 Docker调用；见[真实取消与重建](test-cancellation-native.md)。根代理另查账本/回执/实际原容器，复核位于 `var/test-cancellation-native/root-check.json`。该探针前置Codex执行元数据明确为synthetic；不把它当作一次实际Codex执行后再停止Sim全流程的证据。

实际 Codex→活动测试→停止 Sim 的独立组合也已通过：原模板 revision4，原生角色/Codex 正常完成后，确认第一测试 Running/PID/started 标记，再对原 run 提交一次固定 stop。测试实际 exit137/removed，第二测试从未创建；编码 succeeded 与产物保留，验收 canceled、Sim cancelled，无 resume。根代理独立核对身份、原生父 finished、五处角色版本、策略、文件、租约和容器均符合；见 `var/pinned-test-cancel-known-template/root-check.json` 及[冻结角色流程](pinned-role-workflow.md)。保持原生 ReMe 和记忆配置，未增加服务或上游业务补丁。

运行摘要复用既有任务查询单独统计 schema2 测试阶段：`verificationPending` 为编码 succeeded 后验收 pending 的任务数，`verificationCancelRequested` 为其中已持久申请取消的任务数，`verificationCanceled` 为验收 canceled 的任务数。旧 `active` 和 `resourcesUnreleased` 仍描述编码状态，不代表测试进程数。旧任务或schema1不虚构测试计数；UI缺失/非法字段明确提示而不补零。API回归覆盖pending→申请取消→实际取消，以及原生退出证明未到和其他流程隔离。

## 当前边界

主服务尚未启用此配置或执行准入；本项不证明完整PRD完成。只支持无网络、只读工作区的测试；需要项目目录写入、其他挂载、网络或缺少镜像内依赖的测试会失败，需要部署预先准备适合的环境。临时 `/tmp` 可写且有128MiB上限；镜像原生测试工具的其他限制仍适用。

节点测试取消已接入原任务控制，实际Codex组合现场已取得；全部故障恢复和正式准入仍需继续验收。单容器日志限制不等于全局磁盘配额。已取得的成功/失败/取消单流程证据不能替代其余PRD场景。
