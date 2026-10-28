# 冻结角色修订的真实工作流正例

`probes/full_workflow/pinned_roles.py` 只运行一个真实成功用例，复用
`native-memory-roundtrip` 项目、`native-loop-parent` 绑定及既有
`agent-line-native-loop-parent` 原生 ReMe 角色。没有新建角色或记忆后端。

运行前必须由主线确认插件账本 schema 4 已迁移、运行插件包含 roleRevision 接口、
Codex 显式模型已配置。本探针不迁移、不热安装、不修改正式配置或主准入。

```sh
.venv/bin/python -m py_compile probes/full_workflow/pinned_roles.py
.venv/bin/python probes/full_workflow/pinned_roles.py
```

## 真实路径及证据

探针使用独立私有目录 `var/pinned-role-flow`，从认证插件 descriptor 读取实际角色
修订号，在本次运维验收中明确采纳为测试绑定的可信 pin。这是显式测试配置，
不是运行时自动接受最新角色配置的生产策略。

当前 AuthoringAPI 创建、保存和发布 Start→Agent→Condition→End；发布产生自动
运行目录绑定。TaskFlow 根据真实 Sim pause 登记任务，Dispatcher 通过正式父角色
委派 Codex 写入唯一专属相对文件，AutomaticVerifier 校验预先配置的 SHA256/大小，
CompletionLoop 回传结果，Sim 条件选择通过分支并完成 End。没有手工 register、
record_verification 或 relay.deliver。

最终检查 publication、WorkflowRunLedger、TaskStore payload、父投递 ledger、
插件 `role_bindings` 中的 roleRevision 全部相同。插件账本仅通过 SQLite mode=ro
查询对应 task；还核对 role/session 身份。保留原生 thread/turn、父任务 finished、
容器资源退出证据、工作区租约归零以及实际 Sim selectedPath/End 输出。

所有外发创建/保存/发布和 start 均先保存 intent。目录已存在 state 时只 GET 原身份，
不启动后台 worker、不发新任务；未知请求不得换 ID 重投。成功后的同幂等键请求只
验证已有 receipt 回放没有新增 Sim 写入，不产生第二次编码。

## 范围

仅一个正例；不重复故障矩阵。文件字节验收不等于通用测试套件通过。原生 ReMe
在正常模型调用中可能自然更新；探针不手工修改其正文/配置。主服务维护状态必须
与运行前一致。未成功取得真实报告前，不得将静态检查记为现场通过。

## 2026-10-22 真实结果

主线完成正式插件 schema 3→4 的显式迁移、校验及重启后，执行本探针。
`var/pinned-role-flow/report.json` 已记录 `status=passed_live`；exec 99609 正常退出 0。

- workflow：`fc628016-3198-4921-ae23-ad3e35c3fd51`
- 发布绑定：`published-572ce328c15b2adad033e6aa974a904c596b3558bfcdd646`
- 原 run：`fe8bf353-2115-5511-9a5f-d3b08127544d`
- Sim 恢复子 run：`138f3e63-aeb9-49e2-8d33-09ad576dd36b`，completed。
- task：`task-e8bd5b8fd9474a45853301fbe252f5f5`
- 父角色任务：`task-8a7781c92db9`，finished。
- Codex 原生任务：`task-e22650c6ad57`；thread
  `01a16018-28b8-74f3-b16b-1ecfbafe96e8`，turn `01a16018-38a9-7c02-a871-cf2436f2ac94`。

五处 revision 完全一致：
`qwenpaw-role-v1:6ba451f47883316eb45374373f3a62585e6de2c9be7eb29c89cbcedd0a41a5a4`。
自动字节验收 passed，Sim 实际条件 selectedPath 指向 passEnd，End 的 task/result
与原任务一致。原生终态与独立 resource_exit 均已持久；退出实证 pid=0、running=false、
status=exited，租约与本项目活动容器均为 0，主维护保护未改。

随后实际重复运行脚本仅观察，`observation.json` 记录新提交 0、Sim 写请求 0，
仍为同一个任务。没有新增第二个正例或故障模型任务。

## 可信命令测试模式（尚未现场完成）

`--test-commands` 使用独立 `var/pinned-test-flow`，默认模式仍保留原成功身份并只读重放。
新模式冻结 schema 2 策略：Codex 只写唯一 `pinned-add-<suffix>.py`，内容为 add 函数；
测试正文由服务端冻结在 argv 中，使用 runpy 读取该文件并执行三个 unittest
（2+3=5、-2+3=1、0+0=0）。AutomaticVerifier 使用同证据目录中的 DockerTestExecutor，
镜像取现有 `var/executor/config.json` 的固定 image ID，环境指纹进入策略。
测试和资源回执必须真实通过后才允许 Sim 成功分支；不将模型声称通过当测试证据。

本次执行在 create 阶段返回 uncertain，尚未启动模型：

- 原操作：`create-d329e0c1e37f425c`，唯一 native POST `/api/v2/workflows`。
- 私有 `state.json` 保留 create intent；journal 保存 uncertain/receipt NULL。
- 官方只读完整工作流列表仅找到旧成功图；没有据此推断原请求未受理。
- 原脚本未保存原生响应状态/正文，精确时间窗口的 Sim 日志无对应错误，根因未确认。
- 保留原状态，不换 attempt、不重复创建，不启动新模型；此模式不得记为 passed。

### 复用已验证模板的独立模式

`--test-commands --reuse-known-template` 使用 `var/pinned-test-flow-known-template`。
它不是 uncertain 创建请求的重试：完全跳过 create，原 unknown state/journal 保留不改。
仅可从已 passed 的旧 pinned-role 报告取固定 workflow，使用 SQLite backup 将旧 journal
一致复制到新私有目录，保留原 AuthoringAPI 创建所有权凭证，源库只读打开。

保存前核对真实草稿与原规范图一致、旧恢复子执行已终态、完整运行列表没有其他
未结束运行；原父 pause 只有在其已验证子执行完成时才作为历史允许保留。
随后仅保存/发布已知模板的新版本，并提交一个独立的新版本运行。不会接管新出现的
unknown 创建资源，不将旧回执关联到其他 Sim 实例。执行须等待主线确认最终测试
环境指纹；当前仅完成静态准备。

### 已知模板 revision 2 的真实命令验收结果

2026-10-22，最终执行器环境就绪后，唯一调用上述复用模式，exec 90055 退出 0。
`var/pinned-test-flow-known-template/report.json` 为 passed_live；旧 uncertain 创建仍完整保留。

- 同一 workflow：`fc628016-3198-4921-ae23-ad3e35c3fd51`，新 revision 2。
- 新绑定：`published-2afddb7f46c3944c813243fba26af15827f728b4f08828ef`。
- 原 run：`0229ed15-d452-5a51-b729-899f58dfb7b5`；恢复子 run：
  `eb3ef1ae-dbe7-4af4-afb5-4440a08277c5`，completed。
- task：`task-bf16d83d15eb4591837c1cf9d75ebd8e`。
- 父任务：`task-b57fa8ed09c9`，finished；Codex 原生任务 `task-6d483cd9c076`，
  thread `01a16027-8415-7850-984a-5f59aeb6c711`，turn `01a16027-915c-73b2-86c4-4553b3b3578d`。
- 冻结测试环境：`docker-test-v1:2e2ca0ae0c3140abd61bbbb49d835dbdc74eca10f807037041eaca652b76a98c`。

真实 `addition-unittests` check 的 exitCode=0、status=passed；stderr 包含三个用例
均 ok、`Ran 3 tests` 和 `OK`。自动产物校验与命令测试共同通过，Sim 实际选择 passEnd。
证据目录中的 test job 已持久 `removed=true`；随后补充只读 Docker 精确完整 ID 查询
确认测试容器不存在，见 `test-cleanup-observation.json`（未重新执行测试）。

Codex 的 resource_exit、测试执行器退出及清理证据彼此独立。租约为 0，活动执行容器
为 0，主维护状态未改。实际再次运行同模式只观察，提交/Sim 写请求均 0。
这些证据只证明本轮固定三个 unittest 与声明产物，不代表全产品测试覆盖。

## 独立可信测试失败场景（真实运行通过）

```sh
.venv/bin/python probes/full_workflow/pinned_roles.py --test-commands --reuse-known-template --test-failure
```

此组合使用独立 `var/pinned-test-failure-known-template`。缺少前两个 flag 时拒绝运行；存在新场景 state 时仍仅观察原身份，不创建或重投。

仅复用 `var/pinned-test-flow-known-template` 已核验模板及只读 SQLite backup；另外读取最初 pinned-role-flow 与最新测试通过场景的 state/report/root-check，精确核对已完成 child 身份。发布前拒绝其他未终态运行，允许历史父 paused 的身份仅为上述两条 root-check 对应原 root。仍要求原草稿编译结果与已知模板一致，不创建新图。

本场景让 Codex 写入故意错误的 `add` 实现（返回 `left - right`），然后检查文件内容。服务端字节策略冻结的就是这个错误文件，因此字节检查应通过；独立可信三条 addition unittest 仍要求加法，因此测试必须 exit 1、verification failed、Sim 走 failEnd。期望原生执行 succeeded，业务验收 false。仍核对五处角色 pin、测试容器清理、原角色父任务结束、资源退出和租约归零。

真实执行 session 50865 已 exit 0。复用 workflow `fc628016-3198-4921-ae23-ad3e35c3fd51`，发布 revision 3：

- root：`da3379e0-c3da-5765-92bd-080ef0f0c5d6`
- task：`task-4f118a0762814ecebb444a0438c256c9`
- resume child：`353d6c53-14c4-47b5-bbf9-70609b7b8c2b`

实际结果：Codex execution succeeded、字节检查 passed、独立三条 unittest exit 1 / failed、verification failed。Sim condition 选择 failEnd，动态 End 携带原 taskId、真实 output 和 failed 状态；businessVerified=false。父角色 finished，五处角色版本一致，资源退出证据存在、resourcesReleased=true；工作区租约 0，owned active containers 0，测试容器 removed=true，主服务 guard 不变。

默认重复命令已实际执行，仅观察原 task/root/child，newSubmissions=0、nativeWritesThisInvocation=0。证据见独立目录的 state.json、report.json、observation.json 和 verification-evidence。此结果证明可信测试可以否决模型成功与字节匹配，不代表完整产品验收。

## 活动独立测试取消场景（真实组合验收通过）

`--test-commands --reuse-known-template --test-cancel` 单独使用 `var/pinned-test-cancel-known-template`，与 `--test-failure` 互斥。从最新已 root-check 通过的失败测试模板备份 authoring journal，只读核对原三份 state/report/root-check 的精确历史 root/child；其他活动身份阻止发布。

Codex 写正确 add。第一条独立测试先写容器内 `/tmp/agent-line-test-started` 再等待 60 秒，随后运行三条加法 unittest；第二条测试用于证明取消后未创建/未启动。探针确认原生任务 succeeded/resourcesReleased、第一测试完整容器身份与 running 状态，并通过只读 docker exec 读取标记后，先保存停止意向，再向原 run 控制路由提交固定 operationId/expectedVersion 0 的 stop。

要求 execution 保持 succeeded、verification canceled，第一测试有实际退出及 removed 证明、第二测试 createIntent/startIntent=false 且无 containerId；原 run stop_requested 且 Sim cancelled，没有 resume 写入或后继 End。继续核对五处角色版本、冻结测试策略与正确产物 hash、父角色 finished、退出证据、租约和活动容器归零。已有 state 仍只观察，不重投未知 stop。实际 session 79870 已 exit 0，复用同一 workflow 发布 revision 4：

- root：`8ab69839-61fc-5ca7-af46-2fe9e04ec9b1`
- task：`task-dbd093098d514af6a624e58001ef2a5a`
- 活动测试容器：`291c9464c5e4e974c3a37c3ea8dbf6fd80da86ac66fc8a1455735dd7732fdab0`

真实检查次序为：进程/owned 活动资源安静检查 → 三份祖先证明与草稿一致性 → 固定 revision/角色/测试策略 → Codex succeeded 且 resourcesReleased → Docker running/标签/完整 ID 核对 → 只读读取容器 started 标记 → 原 root 固定 stop 操作。结果 execution 保持 succeeded，verification canceled；第一测试退出码 137，退出证明及 removed=true 已持久化；第二测试 createIntent=false、startIntent=false、无 containerId，disposition=not_started。Sim root 原生 cancelled，本地 control stop_requested；上游只有 deploy/execute/cancel POST，**没有 resume POST 或后继 End**。

父角色 finished，五处 roleRevision 一致、发布/运行/任务测试策略 hash 一致，正确 add 产物 hash 匹配。工作区租约与 owned 活动容器均为 0，主 guard 不变。再次运行同模式只观察原身份，newSubmissions=0、nativeWritesThisInvocation=0。未修改 QwenPaw 主程序、上游、原记忆引擎或角色配置，未新增服务。

证据：`var/pinned-test-cancel-known-template/state.json` 保存活动测试观察、固定 stop 意向/回执和最终控制状态；`report.json` 保存结果及上游方法路径；`verification-evidence/` 保存 first exited/removed 与 second not_started；`observation.json` 保存默认重跑零写记录。

根代理独立复核另存 `root-check.json`：直接查询原生执行、父任务、Sim、插件绑定和实际容器列表，核对本地账本/取消回执/产物/冻结策略及主 guard，全部通过。运行摘要新增三项测试计数是在该探针应用组装后补入，由单独API回归验证，不修改原 stop 回执或将其宣称为新字段现场。
