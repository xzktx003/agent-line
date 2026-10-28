# 实施记录

下面是整理公开 README 之前的实施说明，链接仍按仓库根目录书写。它记录当时的接线范围，不代表每一项都已完成验收。


以 QwenPaw 为主程序，通过插件接入 Sim 工作流和 Codex 执行能力的中文 Agent 工作台。

实施以复用原生能力为先：Agent、会话与长期记忆继续使用 QwenPaw 官方实现；Sim
保存和调度流程，Codex 完成编码任务。新增页面优先从 QwenPaw 插件入口接入，
必要的连接逻辑集中在现有适配服务，不额外增加记忆引擎、Agent 平台或流程调度器。
具体边界见 [主程序与原生记忆约束](docs/native-memory-policy.md)。

当前实施方向以[从原仓库能力重新梳理的收缩方案](docs/reuse-first-reassessment-20261023.md)为准：优先复用Sim原版画布，停止独立编辑器与可选增强扩张；先验证原生流程与已有Agent/Codex接线，再逐步退出重复实现。现有任务历史、身份、权限和未知回执保护保留，尚未完成代码删减或原生替代验收。

当前工作只收敛一条原生链路：Sim画布 → 已有QwenPaw角色与原生记忆 → Codex → Sim结果分支。Agent/记忆管理及流程编辑直接使用对应原页面，扩展只补绑定、任务关联与回传。尚未通过的PRD条目保留差距，不作为继续扩建独立平台的理由；具体冻结范围和删减前提见上述方案第0、12节。

只读升级准入增加[严格证据输入校验](docs/upgrade-evidence-input.md)，不自动拉取、运行探针或切换服务；本轮只做静态审查。

本目录是独立扩展仓库。`upstream/qwenpaw` 与 `upstream/sim` 是独立上游检出，禁止向其受版本控制文件写入业务补丁。运行数据位于 `var/`，不进入代码仓库。

需求基线：`../docs/PRD-QwenPaw-Workflow-2026-10-21.md`（v2.1）。Jira 专用集成不在本次范围。

当前处于 G0 兼容验证和基础实现阶段；启动成功或单元测试通过不代表完整 PRD 已验收。验收记录见 `docs/acceptance.json`。

## 验证

用户已于2026-10-23恢复测试。后续优先运行与改动相关的现有核心回归，
按失败和风险扩大范围，不默认每次全量。保留关键用例，不恢复已删除的冗余测试；
历史静态精简记录见 [测试清理说明](docs/test-cleanup-plan.md)。

```bash
.venv/bin/python -m unittest discover -s tests -v
python3 scripts/check_upstreams.py
```

上游检查为只读操作。只有固定版本、工作树完整性和远程身份符合锁文件才通过；不自动更新、清理或重置上游。

首次创建任务库使用 `TaskStore.initialize(path)`；正常启动只使用 `TaskStore(path)`，数据库缺失或版本不符拒绝启动。

## 当前入口与已验证范围

QwenPaw 主入口：`http://10.30.0.28:3194/qwenpaw/`。登录后从设置中的“工作流工作台”
打开已有中文工作台，进入流程搭建或查询任务；原版 Sim 画布保留为可选入口。
独立工作台地址仍为 `http://10.30.0.28:3193/workbench/`。业务操作使用部署生成的服务令牌；
秘密配置在忽略目录 `var/adapter/service.json`，不要提交或打印。
启动方式和接口见 [适配服务部署](docs/adapter-deployment.md)。
既有任务次数与时限已生效，口径及兼容说明见[执行限制](docs/execution-limits.md)。
编辑器可选节点限额已沿发布、运行快照接到任务发现（后端已重载，尚未功能验收），见[节点限额配置](docs/node-limits-editor.md)。
[跨节点运行额度](docs/run-budget.md)、运行用量汇总和准确激活回传代码已接线并重载主适配服务；未执行这些新增能力的功能验收，部署范围见[重载记录](docs/backend-rollout-20261022.md)。
[Agent 团队关系图](docs/team-view.md)入口：`http://10.30.0.28:3193/workbench/team.html`，使用独立流程编辑凭证。

执行任务观察增加[有界查询退避](docs/polling-backoff.md)，取消与到期处理保留原优先路径；新增逻辑尚未进行功能验收。

流程目录另提供[本地提交摘要](docs/workflow-submission-summary.md)，与原生运行数据分开说明。

任务详情可进入[同节点全部激活与尝试](docs/node-attempt-directory.md)，沿用原任务中心分页核对。

Agent结果支持[有界嵌套对象与数组契约](docs/structured-fields.md)，复杂定义按需展开；本轮尚未功能验收。

人工确认节点可配置[审批有效期](docs/human-approval-ttl.md)，到期拒绝新决定，不自动推进流程；本轮尚未功能验收。

人工节点可填写[随运行冻结的确认事项](docs/human-confirmation.md)，审批详情及确认框展示原问题；本轮尚未功能验收。

运行图提供手动读取的[条件命中记录](docs/run-condition-decisions.md)，只显示与固定图匹配的当前原生决定；本轮尚未功能验收。

工作台首页已取消服务连接表单，直接显示[已有任务与服务摘要](docs/direct-overview.md)。
[运行管理](http://10.30.0.28:3193/workbench/runs.html)沿用原操作授权，提供
[业务任务与多次运行关联](docs/work-items.md)、[跨运行节点目录](docs/business-task-directory.md)、
[固定版本只读运行图](docs/run-view.md)、可选的[本地状态自动更新](docs/run-view-refresh.md)
以及手动读取的[原生流程与资源摘要](docs/native-run-summary.md)。
这些新增界面目前只有源码/部署核对；遵守用户不再测试的要求，未进行功能验收，原生路径命中仍未知。

主入口已接通原生角色/ReMe 与真实 Codex 执行：接入验证流程可新建、保存、发布，
并在独立三项测试通过后进入 Sim 的成功分支，旧任务历史保留。当前只配置了
“工作流接入验证”项目及其固定验收策略；其他业务项目的工作区、角色和可信策略
仍需配置，不能直接视为任意编码任务已经可用。切换、备份和验证范围见
[主工作台接入](docs/main-workflow-rollout.md)。

全新安装现可用[单项目初始化命令](docs/single-project-setup.md)显式登记自己的代码目录、
原生角色、模型及测试命令；已有主实例拒绝覆盖。该命令不生成执行就绪证明，
从初始化到真实业务就绪的自动引导仍未完成。
首次就绪核对CLI已补充：`.venv/bin/python -m deploy.project.verify` 默认只读，
首次显式 `--execute` 仅提交一次受控核对并使用原验证策略。新项目真实组合联调
留待功能批次末完成，见上述部署说明。

已取得真实运行证据：Sim 条件分支、人工暂停后重启恢复；工作流入口重复提交及
适配服务重启后保持原运行；QwenPaw 正式角色和记忆跨重启复用；主服务持久任务
经执行桥调用 Codex 写入并校验文件；经批准的记忆通过官方 API 写回并读回。
另已验证原生记忆的两任务召回与 Codex 注入，以及 Sim 机器等待驱动后台自动登记、
真实编码、独立文件验收和恢复。中文 Agent 配置页可保存、发布既有角色绑定。
新发布流程现可自动加入运行入口，条件读取 Agent 本轮结果，结束输出保留动态对象；
真实发布 → 原生角色 → Codex → 独立文件验收 → 成功分支已在隔离环境通过，
浏览器配置读回、发布链接及手机操作也已验证。限定循环的编辑、发布、逐轮身份与反馈接线
已实现，原生三轮上限及固定校验节点已验证。Agent 与人工确认现可共用流程，两次真实 Codex
任务分别批准和拒绝，命中相应分支；循环后人工确认也已用显式模拟机器结果验证。
暂停后保留活动 Codex 的结果、应用重建后显式继续及活动任务真实取消已在隔离环境验证。
已把受信文件字节验收接入现有后台线程；真实通过/失败分支与回执落盘后的恢复已验证，
字节核对不代表执行测试套件通过。另已新增显式测试策略，真实 Codex 实现后由独立受限容器运行三个 unittest，通过后自动回传 Sim；测试命令、环境和限制见下方文档。真实多轮、混合流程与运行控制的范围见下方探针文档。生产准入、完整画布与
升级切换等仍待交付；主环境未启用新的可选配置，完整 PRD 尚未验收。

最新隔离候选已在正式 `runtime.load_app` 下完成实际新任务：浏览器关闭重开后，
同发布版本复用原角色/ReMe、新 Codex 身份与独立三项测试通过，Sim 实际成功分支
可查。AC-03、AC-70 经独立审查通过，主配置仍保持；见
[发布能力与正常启动复用验证](docs/publication-capabilities.md)。

- [Sim 实际流程验证](docs/sim-workflow-probe.md)
- [记忆真实验证](docs/memory-probe.md)
- [记忆提交总账](docs/memory-commit.md)
- [人工记忆审核与页面](docs/memory-review.md)
- [原生记忆回执丢失后的只读核对](docs/memory-reconciliation.md)
- [受控记忆删除与当前索引核验限制](docs/memory-deletion.md)
- [QwenPaw 主程序与原生记忆约束](docs/native-memory-policy.md)
- [原生记忆两任务闭环的当前验证与失败记录](docs/native-memory-roundtrip.md)
- [AC-01 上游与扩展边界验收](docs/upstream-boundary-acceptance.md)
- [中文流程搭建接口与当前边界](docs/workflow-authoring.md)
- [复制已有流程为独立草稿](docs/workflow-copy.md)
- [从内置模板创建团队交付或有限修复草稿](docs/workflow-templates.md)
- [发布新版时原运行保持原快照](docs/workflow-republication.md)
- [发布前能力检查与草稿保留](docs/publication-capabilities.md)
- [中文搭建页真实浏览器验收](docs/workflow-editor-browser.md)
- [Agent 节点后台接线与限制](docs/task-flow-runtime.md)
- [真实自动工作流验收](docs/automatic-task-flow.md)
- [Agent 配置页真实浏览器验收](docs/agent-editor-browser.md)
- [发布后自动生成运行入口](docs/published-workflows.md)
- [发布流程到真实 Codex 的完整探针](docs/published-task-flow.md)
- [Agent 结果条件与动态结束的六项原生验证](docs/sim-agent-result-probe.md)
- [结果引用与运行链接的浏览器验证](docs/editor-result-references.md)
- [可选编码/审查JSON结果与条件字段选择（已重载，待业务验收）](docs/business-output.md)
- [恢复子执行状态与真实失败展示](docs/workflow-resumption.md)
- [已登记历史运行目录与只读分页（仅源码）](docs/run-directory.md)
- [任务事件时间线与手动分页](docs/task-events.md)
- [从运行详情查找节点任务](docs/run-task-directory.md)
- [简洁的 macOS 风格界面](docs/macos-interface.md)
- [有界修复循环的任务接线](docs/loop-task-runtime.md)
- [固定循环校验节点的真实验证](docs/finite-loop-guard-probe.md)
- [Sim 原生静态并行与汇合的五场景验证](docs/parallel-native.md)
- [静态并行编译、真实账本与原生 Sim 组合验证](docs/compiled-parallel.md)
- [原生角色与真实 Codex 的双分支流程](docs/real-parallel-role-workflow.md)
- [连续 Codex 会话的插件收尾](docs/codex-runtime-retirement.md)
- [通用 Webhook 与分钟事件入口](docs/workflow-triggers.md)
- [已配置自动入口与原事件核对](docs/trigger-inspection.md)
- [运行额度摘要](docs/run-budget-summary.md)
- [本地运行检查与断连查询](docs/run-health.md)
- [原生记忆召回与委派快照](docs/native-memory-trace.md)
- [JSON 流程模板导入与导出](docs/portable-workflow-template.md)
- [从数据清单创建静态批量流程](docs/static-batch.md)
- [中文静态并行与汇合配置](docs/editor-static-parallel.md)
- [多阶段静态并行与汇合](docs/multi-parallel.md)
- [固定版本子流程引用核对（只读）](docs/subworkflow-reference.md)
- [固定子流程编译展开预览（只读）](docs/subworkflow-expansion.md)
- [子流程声明式输入与返回映射](docs/subworkflow-mapping.md)
- [单层子流程受控保存、发布与父运行预算](docs/subworkflow-admission.md)
- [有限循环编辑页浏览器验证](docs/editor-finite-loop.md)
- [真实 Codex 自动循环探针](docs/automatic-loop-flow.md)
- [Agent 与人工确认共用流程的配置与限制](docs/mixed-workflows.md)
- [真实 Codex 执行后批准与拒绝的完整探针](docs/automatic-mixed-approval.md)
- [原生循环接人工确认的模拟结果探针](docs/mixed-approval-native.md)
- [中文人工确认条件的离线浏览器验证](docs/editor-human-conditions.md)
- [原生 Codex 执行权限审批接入](docs/native-execution-approvals.md)
- [执行权限审批中文界面与离线验证](docs/native-approval-ui.md)
- [原生审批隔离实测状态与限制](docs/native-approval-live.md)
- [流程暂停、继续与停止的实现边界](docs/run-controls.md)
- [真实 Codex 运行控制验证](docs/automatic-run-controls.md)
- [原生取消与停止恢复窗口](docs/run-controls-native.md)
- [后台自动产物验收的配置与边界](docs/automatic-verification.md)
- [按原验收记录核对并下载产物](docs/artifact-access.md)
- [可信测试命令、独立容器验收与真实工作流](docs/automatic-test-verification.md)
- [测试阶段停止、应用重建与真实退出证据](docs/test-cancellation-native.md)
- [中文停止验收交互及模拟浏览器验证](docs/verification-cancel-ui.md)
- [真实 Codex 自动验收及提交故障恢复](docs/automatic-verification-live.md)
- [QwenPaw 内中文工作台的真实浏览器核验](docs/qwenpaw-workbench-plugin.md)
- [升级准入](docs/upgrade-policy.md)
- [独立升级候选准备](docs/upgrade-candidate.md)
- [固定提交升级、模板与历史保留及旧备份恢复](docs/qwenpaw-upgrade-semantics.md)
- [角色配置版本冻结、变更检查与迁移边界](docs/role-revisions.md)
- [原生 ReMe 角色与 Codex 的真实冻结版本工作流](docs/pinned-role-workflow.md)

`probes/dispatcher/live.py` 是隔离 G0 的真实执行探针，首次运行会创建一个明确的
文本文件任务；重复运行只复用同一持久任务并核对，不重新提交。它不会打开公开
任务准入，也不把探针通过自动登记为独立业务验证通过。

项目流程目录的搜索、运行信息与分页语义见 [workflow-directory.md](docs/workflow-directory.md)；当前主后端已部署，真实局域网搜索验证见下方部署记录。

画布交互见 [canvas-interactions.md](docs/canvas-interactions.md)；原生归档与历史读取的真实边界见 [workflow-archive-boundary.md](docs/workflow-archive-boundary.md)。

归档前快照、只读审计与未知核对已实现，真实独立流程验证见 [workflow-archive-boundary.md](docs/workflow-archive-boundary.md)。当前受控执行因源码变化保持unknown/readyfalse，需重新验证后开放。

当前主后端已按原配置重载，真实局域网页面的搜索、保存与归档审计已通过；19任务、事件和9个Agent保持。机器任务仍unknown/readyfalse，待当前源码角色重验。详情见 [backend-rollout-20261022.md](docs/backend-rollout-20261022.md)。

指定 `gpt-6-luna` 的真实工具回合已通过，原生模型/回合、文件产物和容器退出均已核对；详见 [Luna执行器核对](docs/luna-executor-verification.md)。这项独立探针不替代原生父Agent委派或主安装就绪证明。

原巡检线程的健康事实与中文提示已部署，见 [execution-observer-health.md](docs/execution-observer-health.md)：主LAN桌面/手机真实查询通过，原19任务与9个Agent保持；巡检正常和节点执行准入分别展示，主受控执行仍unknown/readyfalse。

首页地址：`http://10.30.0.28:3193/workbench/`。打开直接查看已有任务和已配置服务，可筛选、分页、刷新，无需手动服务连接。运行操作入口为“运行管理”，保留原操作授权；服务“已配置”不代表原生服务健康已核验。实现边界见 [任务与服务总览](docs/direct-overview.md)。

任务列表与详情现显示登记时间和最近持久事件摘要，旧记录缺失时明确尚无记录，见[任务活动信息](docs/task-activity.md)。最近事件不是在线或业务成功证明。

Agent节点现支持[自定义结果字段](docs/output-fields.md)：用表单选择字段名、类型与必填项，字段定义随发布及任务冻结，下游条件从声明的标量字段选择。仍需独立验收，不以字段内容替代测试或人工审批；未进行本轮浏览器/业务测试。

[任务等待与关注事项](docs/task-attention.md)：首页与运行管理会说明角色/工作区占用、额度/时限、受理未知、资源退出、独立验收和记忆待处理情况；只读取既有状态，不自动重试任务。

流程搭建现在可查看[节点角色配置摘要](docs/role-summary.md)：角色名称、配置访问、原生记忆和验收方式，以及已提供的原生执行启动快照；未提供的模型/默认权限明确显示未知，不当作实时健康。

静态批量流程支持[结构化项追踪](docs/batch-item.md)：每项身份、输入指纹和结果摘要随原任务链路固定，私有详情可展开本项数据，旧流程不回填。动态列表及真实批量执行仍待完成和验收。

项目可配置[执行并发上限](docs/project-concurrency.md)，沿现有领取事务约束执行名额；不新增调度服务。当前正式项目未开启，真实并发争抢尚待验收。

[记忆批量人工审核](docs/memory-batch.md)复用原逐项审核接口，逐条核对后在本页暂存、统一确认并顺序提交。失败或结果未知即停止，已发项只查询原记录；没有新记忆系统或后台批量作业。

全新机器的准备顺序见[从零安装](docs/fresh-install.md)。`python3.12 -m deploy.bootstrap`默认只显示计划；显式`install`仅准备锁定上游及新的Python环境，拒绝替换已有`.venv`，不启动服务或初始化账户/记忆。执行镜像构建改为显式源镜像及新输出目录，不再依赖本机残留ID或自动更新正式配置。当前仅源码审阅，尚未实测完整从零安装。

编辑器提供[发布版本历史与只读快照](docs/version-history.md)，可核对旧版本及读取时本页草稿差异；不恢复、发布或执行所选旧版。原生固定子流程版本调用仍未完成。
