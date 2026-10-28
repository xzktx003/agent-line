# 上游升级兼容准入策略

当前包含只读报告器 `agent_line.upgrade` 与执行插件接口不兼容时的运行时降级。报告器不执行升级，不 fetch/pull/reset，不修改上游、配置或数据。默认拒绝；空探针集、未知、失败、过期目标 SHA、脏源码或证据缺失均不会批准兼容性。

这不是完整 PRD 升级实现：正式升级仍需要冻结新准入、核对在途执行、记录版本、取得一致性备份、实际迁移、隔离验收、切换和可验证回退。报告 `compatibilityApproved=true` 只代表给定证据通过格式、版本绑定和源码完整性准入；不能代替真实运行证据，也不授权结束进程或恢复旧库。

## 当前与候选版本

当前版本从 `upstreams.lock.json` 读取，只接受固定的 QwenPaw、Sim 两项完整 SHA。当前目录必须在 `upstream/` 下；候选使用单独 checkout（例如 `var/upgrade-candidates/`），也可指向相同版本用于维护核对。检查复用 `scripts/check_upstreams.py`：实际仓库根、HEAD、origin、已跟踪改动和非忽略的未跟踪文件。

候选 origin 必须与当前锁定 origin 相同。候选 SHA 和目录必须在报告中明确。不得先在正式目录拉取后再假称做过升级前检查。只读报告器不创建候选 checkout；现有独立准备工具见[候选源码准备](upgrade-candidate.md)。该工具只取得指定版本的源码，不安装、运行或切换服务。

目前已补充两个明确固定提交间的[隔离应用语义升级与旧备份恢复](qwenpaw-upgrade-semantics.md)：原生角色、记忆、当前插件包、模板发布版本、已完成历史、绑定和 journal 可读。根代理另行只读核对真实 Sim 历史及三份离线安装。Sim 版本不变且未回滚，不将本切片视为完整安装恢复、模型召回/Codex/审批兼容或九项探针全部通过。

## 执行插件接口不兼容时

配置中的路径、秘密权限、现有数据库、项目和角色绑定先按原规则验证。只有远端执行插件描述接口不兼容或不可用时，`load_app` 捕获明确的 `ExecutionCompatibilityError`，保留认证历史查询，并阻止新的扩展变更操作。接口不兼容显示 `controlledExecution=unsupported`，接口不可用显示 `unknown`；健康接口另提供固定中文 `executionBlockReason`，不输出远端正文或凭据。普通健康响应保持原结构，不凭该状态自动开放执行。

降级时不创建执行 Dispatcher，不启动任务发现、完成回传或审批发现后台；不会替旧任务释放租约、重复认证重试、重新编码或推进 Sim 节点。所有扩展变更请求沿用原服务/独立人工凭证鉴权，然后返回503。记忆删除状态的 GET 实际包含协调写入，因此同样受阻；普通记忆列表、详情和任务/事件/工作流历史仍可读。不能以 HTTP 方法是 GET 推断它一定不写数据。

工作台在原警告区显示中文原因，禁用新启动，保留历史查询。刷新不隐藏原因，生成新触发标识或脚本提交表单也不能绕过阻断；退出清除状态，新登录仍读取实际健康。界面验证为明确的离线响应 fixture，11项交互检查、1440/390px无横向溢出、视觉94/pass；它不冒充真实不兼容的主部署。证据在 `.omx/state/execution-degradation-ui/`。

修复不停止或更改 QwenPaw 原服务、原生角色或记忆。本地配置、秘密/数据库损坏、角色版本权威不符等错误继续严格失败，不创建空库或静默改绑；Sim 客户端在构造失败时关闭。恢复需核对实际插件/宿主接口和原执行身份后重新启动适配服务，不自动重投。

独立审查先发现并复现删除协调GET写旁路和构造失败客户端未关闭，两项均先红后绿修复。真实临时HTTP接口/schema999、原 SQLite 与 MemoryAPI 回归证明：不兼容时历史保留、任务/事件/租约不变、无执行与协调写入；原生 PluginLoader 另有不兼容版本拒载证据。对应 `tests/test_execution_degradation.py` 与 `var/acceptance/execution-degradation-review-final.json`。AC-24按其模拟不兼容接口场景通过，不代表任意未来上游都已兼容。主部署本轮未切换后台配置，正式升级与完整PRD继续待验收。

## 强制探针集合

每次必须包含以下九项，清单由代码固定，manifest 不能自行减项：

1. `plugin_routes_auth`：插件入口、受保护接口、登录和权限。
2. `sim_graph_wait_resume`：原发布流程、外部长任务等待及原身份恢复。
3. `role_identity_persistence`：原 Agent ID、配置、绑定、历史保留。
4. `codex_delegation_identity`：实际 Codex 委派、执行身份和本轮证据。
5. `approval_mapping`：有效审批映射和旧请求拒绝。
6. `memory_recall_injection_writeback`：记忆召回、实际注入、回写及新任务复用。
7. `project_isolation`：角色/工具/记忆的项目权限边界。
8. `inflight_reconciliation`：旧任务状态未知不重发、写租约保留。
9. `data_migration_restore`：对应版本迁移与备份恢复证据，含备份后真实执行核对。

## Manifest 与证据格式

manifest 示例结构（用实际固定 SHA 和已取得证据替换占位值；此示例不能通过）：

```json
{
  "schemaVersion": 1,
  "candidates": {
    "qwenpaw": {"path": "var/upgrade-candidates/qwenpaw", "remote": "https://github.com/agentscope-ai/QwenPaw.git", "commit": "候选完整40位SHA"},
    "sim": {"path": "var/upgrade-candidates/sim", "remote": "https://github.com/simstudioai/sim.git", "commit": "候选完整40位SHA"}
  },
  "probes": {
    "plugin_routes_auth": {"evidencePath": "var/upgrade-evidence/plugin_routes_auth.json", "sha256": "该文件字节SHA256"}
  }
}
```

每个证据 JSON 必须包含 `probeId`、`status: passed`、`mode: live`、`observedAt`、`details`，以及完整的 `currentCommits` 和 `candidateCommits` 两个映射（各含 qwenpaw/sim）。结果绑定的是两组件版本组合，不仅是本次改变的单组件。路径经过真实路径解析，不能引用安装根外文件。报告不会输出 details 正文；该正文应记录命令、部署、结果和可复核证据位置，不含秘密。

```bash
cd agent_line
.venv/bin/python -m agent_line.upgrade var/upgrade-manifest.json
```

退出码 0 表示提供的兼容证据满足准入；1 表示拒绝或未知。`upgradeExecuted` 永远为 false。

## 证据可信度边界

哈希能检测内容变化，不能证明证据真实。报告器不执行 manifest 指定命令，不把任意文件标为受信签名，也不能识别人为伪造的 `mode: live`。运维流程必须从受控探针产出证据；人工填写“passed”不是可接受的产品验收。单元测试使用明确标注的合成记录，仅验证拒绝逻辑，不能登记为真实升级通过。

旧版本测试结果不能挪用给新 SHA。候选代码有任何变化，需要重新固定和取得相应版本组合证据。本阶段尚未实施完整运行时配置指纹、证据签名或统一年龄政策，因此不把报告扩展为任意未来部署环境的兼容保证。

## 节点任务维护锁

`TaskStore.enter_maintenance()` 原子登记持久维护锁，返回仅供维护控制器保管的
owner 标识。之后新任务登记被拒绝（HTTP 503），已登记任务的幂等重放和只读查询
仍可使用，新的执行 claim 被阻止；已有执行可以核对终态和释放资源。

`maintenance_status()` 返回队列数量、未释放执行数量及 `executionQuiescent`。
只有维护锁存在且所有已启动执行确认释放时，后者才为 true。它只证明该任务库的
执行静止，不证明 Sim、QwenPaw 自动任务、记忆写回或整个安装已停止写入。
完整升级控制器还必须分别排空这些写入方，不能仅凭这个字段切换版本。

`leave_maintenance(owner)` 只移除同一个 owner 创建的普通维护锁，不能覆盖或
清除其他维护者的锁，也不能解除备份恢复时的外部状态核对锁。维护锁跨进程重启
保留。当前未对普通浏览器暴露管理接口。

## 工作流提交与维护门禁

运行服务将同一 TaskStore 的维护/恢复保护接入全部共享 SimJournal。
新流程启动、通用恢复、人工审批恢复以及经该 journal 的完成回传都必须先取得
workflow_admission。已有出站回执的同输入重放继续返回原结果，不重新投递；
输入冲突仍报409，未知或intent仍不重投。

任务库旁的 .admission.lock 是0600的常规单链接文件。每次请求独立打开并取得
共享 flock，先检查任务库持久 admission_guard，然后关闭任务库读事务。共享锁
覆盖发布预检、运行元数据登记、Sim意图落账、HTTP投递及回执记录。
enter_maintenance 先取得独占锁，再写入持久维护保护；返回时，本服务此前
已进入的提交调用已经结束。锁顺序始终先flock，再数据库事务，不持journal写
事务等待flock。进程退出由内核释放锁；不得在活跃服务期间删除或替换锁文件。

人工审批在同一可重入mutation_scope内记录决定并恢复，因此维护拒绝不会留下
一条已决定但从未获准投递的新审批。嵌套SimJournal调用复用本线程的既有门禁，
不对同一调用链再次打开锁。新workflow启动也先取得门禁，维护拒绝不会留下
孤立的审批运行元数据。普通浏览器无解除维护锁接口。

此门禁覆盖通过该运行服务和共享journal提交的调用。Sim原生定时器、原生Console、
独立工具或其他服务的直连调用不受此锁控制；后台审批发现仍可能写登记账本。
所以它不证明全系统已停止写入。安装级备份和正式切换仍需停止所有相应写入方，
并按安装备份门禁核验外部执行与原生触发器。

真实Sim探针：
` .venv/bin/python probes/upgrade/workflow_maintenance.py --attempt real-sim-1 `
使用全新私有运行目录及已有无模型图。先正常启动到人工暂停，再进入维护；
新启动和新审批503、无新投递意图且决定未写入，原回执仍202可读；正确owner
解除后原审批完成。证据var/upgrade-maintenance/real-sim-1/report.json。

主服务已加载门禁修复并保留原维护保护；新启动503，已有已完成运行重放202，
journal新增操作0。证据var/upgrade-maintenance/main-service-report.json。
跨进程与进程退出释放、API接线、重启保护等回归包含于当前353项全量测试。

## 恢复后的工作区核对（2026-10-27）

任务库恢复仍只写新的数据库文件，并留下 `restored_snapshot_requires_external_reconciliation`。
`reconcile_restored_workspace` 只比较备份时和当前的工作区指纹：它不回写代码、不重新提交、
也不解除恢复锁。执行观察或 Sim 原生触发器静止状态未知时，新的任务登记继续被拒绝。
扩展触发器的新定时/回调事件同样返回 503；相同事件只重读原回执。主 Sim Console 未停止时，
原生触发器仍记为 unknown，不能把整项验收标成通过。

同日隔离复测：只在执行线程禁用 `codex_apps`，没有放开 `10.40.0.2`、taskField 或宿主 user namespace。容器里没有 `python`，`gpt-6-luna` 用 `sed` 改了挂载样例；恢复后的登记和认领仍被拒绝，差异只报告不回写。主 Sim 原生触发器仍是 unknown，AC-25 不改为 passed。证据 `var/ac25-restore-retry4-20261027/report.json`。
2026-10-27 主 Sim 窗口：cron 与 Console 停止 70 秒且数据库未重启，隔离备份恢复后的触发器记为 stopped、执行记为 observed，AC-25 改为 passed。服务随后已启动，不能把当前运行中的触发器再说成停止。证据 `var/ac25-trigger-stop-20261027/report.json`。


## 当前输入校验增量（2026-10-23）

只读工具已补1MiB普通文件读取、严格JSON与证据时间/详情类型校验，保留现有九项真实证据要求。observedAt须带时区ISO或有限正Unix秒，details为非空文本或对象，版本号不接受布尔值。详见[升级证据输入](upgrade-evidence-input.md)。本轮仅源码/AST/Luna，历史升级与单元测试证据不覆盖此增量，没有执行工具、Git检查或升级。
