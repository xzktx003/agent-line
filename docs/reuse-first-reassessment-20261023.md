# 从原仓库能力重新梳理实施方案

用户最新要求：轻适配，复用已有仓库功能，减少重开发。本文重新评估当前实现，不把历史实现规模或未验收条目当作继续加代码的理由。本轮为源码审计与实施决策；没有删除模块、迁移数据或切换服务。

## 0. 再次纠偏后的执行约定

**交付的是 QwenPaw 的工作流接入扩展。日常管理用 QwenPaw 原页面，流程编辑用 Sim 原页面；自写部分只处理两者之间缺少的连接。** 复用优先采用官方页面、API和配置；复制上游整套源码会形成另一份升级负担，不作为默认办法。

此前虽已提出复用，却仍在多个管理页面、图转换和可选能力上推进，实施范围没有真正收住。以后不能再按“还有哪些AC没通过”逐项扩建平台。PRD v2.1继续保存需求和差距，本节约束实现方式与当前工作顺序，不修改基线摘要，也不把延期需求标记为通过。

| 归属 | 直接使用 | 本仓库的边界 |
| --- | --- | --- |
| QwenPaw | Agent创建/配置、会话、ReMe检索与记忆生命周期、原生执行审批 | 选择已有Agent、关联任务与准确审批身份；不维护另一套角色或记忆引擎 |
| Sim | 原画布、条件/循环/并行/子流程、保存、版本、原生触发与调度 | 绑定已有Agent节点、传入输入、按原等待身份回传结果；上游支持不等于联合链路已验收 |
| 原生Codex Harness | 实际执行、thread/turn及原生执行结果 | 交给原QwenPaw角色委派，核对本次产物与结果；不新建Codex运行平台 |
| 官方插件 + 现有适配服务 | 中文入口、任务/服务摘要、身份与回执连接 | 优先修改已有连接处；不新增服务、数据库、依赖或通用扩展框架 |

当前只推进一个出口：**原生画布搭建 → 绑定已有角色 → 原生记忆参与任务 → Codex执行 → 同一Sim等待点恢复 → if/else**。沿用第9节已登记的候选身份，避免再建一批平行演示。第9节的模型选择等待是历史状态，第13节记录暂停期间实际使用gpt-6-luna完成了同一候选流程；后续不重复提交旧任务，调试继续遵守用户指定模型。

当前冻结独立编辑器、复杂图转换、团队/预算/健康页面、自然语言草稿、Kanban附加能力的新增开发；保留现有数据和可用路径。原生循环、并行等先复用上游，再确认任务身份/恢复适配是否支持，不为追求功能齐全继续添加自有展开语法。SwarmClaw和DeepSeek Harness只借鉴已需要的交互与边界，不增加运行依赖。

清理顺序固定为：①原生替代链路可用；②入口改用原页面并保留旧任务查询；③删除无调用的重复实现。当前尚未满足第一步，不批量删除旧代码或重载服务来制造完成状态。中文扩展操作保持简洁，原版Sim仍有英文的部分明确记为缺口；不用另写画布解决翻译问题。升级保持上游原版，接口变化时只修插件/连接处，不承诺任意未来版本零适配。

## 1. 结论

主要偏差不是没有调用上游，而是围绕上游又开发了较完整的产品表层：独立画布、节点配置表单、自定义图格式、转换/反向识别、子流程展开，以及较多管理和治理页面。这增加了升级、联调与验证负担。

应恢复PRD第5.3节已经给出的首选路径：**QwenPaw主入口和原生Agent/记忆 + Sim原版流程编辑与调度 + 原生Codex执行 + 必要插件和跨系统接线**。不再扩展独立平台。

不是所有扩展代码都重复。Sim和QwenPaw之间没有天然共享的任务ID、幂等回执和审批归属；这些接线不能靠删代码省去。现有任务数据库也不能因收缩架构而清空或替换。

## 2. 当前规模和偏差证据

本轮对选定目录中Python/JS/HTML/CSS做只读行数统计，排除上游、运行数据、测试、字节码和网关Console构建产物：

| 目录 | 文件数 | 行数 | 判断 |
| --- | ---: | ---: | --- |
| agent_line | 72 | 17,316 | 包含必要接线，也混入较多编排配置与可选增强 |
| frontend | 39 | 9,574 | 重开发主要集中区，应优先退出重复交互 |
| plugins/agent-line | 6 | 1,714 | 官方插件接入及受管执行边界，需按职责逐项保留 |
| deploy | 17 | 3,498 | 保留现有启动/部署接线，停止扩建通用部署平台 |

合计32,102行；这是规模统计，不是可删除行数或功能完成度。

证据：`frontend/editor.js:1305`起包含自写画布交互；`agent_line/sim_authoring.py`维护自己的document编译和解码；`agent_line/publication.py:101`起对复杂图依赖decode/compile重新构建。三者形成依赖，不能只删除editor.js就算完成收缩。

## 3. 从需求反推复用归属

| 用户能力 | 原有实现应当承担 | 只需补的接线 | 现有代码处理 |
| --- | --- | --- | --- |
| Agent创建、配置、重启后复用 | QwenPaw官方Agent API和配置 | 节点记录既有Agent ID/工作区；缺失时提示 | 不增加角色管理引擎；保留绑定校验 |
| 会话与长期记忆 | QwenPaw会话、ReMe、memory_search | 任务上下文交给原角色；必要来源引用 | 不开发检索/索引/过期引擎；原生缺口明确受限 |
| 流程搭建和连线 | Sim原版画布 | 嵌入入口及角色绑定小面板 | 独立画布停止扩展，准备替换后退出 |
| 条件、循环、并行、子流程 | Sim原生节点与引擎 | 输出字段与角色绑定对接 | 不再添加自有编排语法和展开规则 |
| 保存、复制、发布、版本 | Sim原生API和版本数据 | 记录已发布版本与绑定的对应关系 | 保留必要版本引用；不建第二套版本权威 |
| Agent调用Codex编码 | QwenPaw原生Codex Harness | 节点→原角色→原生thread/turn对应 | 复用当前桥，不能绕过原角色直接丢掉记忆链 |
| 异步等待、恢复与后继节点 | Sim原生等待/resume | 真实结束后按同一run/context返回结果 | 保留现有结果回传，禁止适配器自己跑后继 |
| 审批 | QwenPaw原生执行审批、Sim人审节点 | 将工作流任务和准确审批请求关联 | 保留身份/幂等适配；不另建审批引擎 |
| 查看已有任务/服务 | 原生运行记录 + 当前只读总览 | 少量中文状态和导航 | 保留无需连接的首页，减少额外操作面板 |
| 测试和结果证据 | Codex执行结果、既有独立验证 | 展示本轮事实及产物引用 | 复用现有验证器，不建通用评测平台 |
| 升级与备份 | 上游原版发布 + 现有管理/备份工具 | 插件兼容核对、固定版本和数据恢复说明 | 不增加自动升级控制器 |
| 团队、预算、模板、定时、Webhook | 优先对应官方功能/配置 | 只补已经确认需要的差异 | 暂停可选增强扩张，不整套搬入其他平台 |
| Kanban、自然语言草稿、语义增强 | 对应原仓库/工具已有能力 | 必要时少量链接或协议适配 | 保留既有代码和数据，暂不作为继续扩建主线 |

P1/P2并未因此全部完成或从需求里删除。这里调整的是实现顺序与复用方式；无法直接复用的功能要保留差距，不能再用“PRD有条目”自动推导出必须重写整套能力。

## 4. 当前原仓库证据

以下是当前锁定checkout的源码能力，不是当前服务端到端验收结果。

- QwenPaw：`upstream/qwenpaw/src/qwenpaw/app/routers/agents.py:441`及797/1130/1707附近提供列表、创建、更新和删除；无需扩展再维护Agent生命周期。
- QwenPaw：`agents/memory/base_memory_manager.py:135`提供memory_search，`reme_light_memory_manager.py:680`实现原生检索；无需自写检索引擎。
- QwenPaw：`app/approvals/service.py:439`和`app/routers/approval.py:81`起提供原生审批；扩展保留任务/thread/turn映射，不替代原生决定。
- QwenPaw：`app/routers/frontend_plugin.py`已有前端插件分发；当前`plugins/agent-line/plugin.py:227`使用官方注册，不需要新的插件宿主。
- Sim：`upstream/sim/apps/sim/app/workspace/[workspaceId]/w/[workflowId]/page.tsx:12`已有编辑页；`executor/handlers/api/handler.ts:63`已有HTTP工具执行。
- Sim：`app/api/v2/workflows/[workflowId]/runs/[runId]/resume/route.ts:93`起存在异步/排队恢复回执，单run路由提供查询。HTTP请求返回或202仅是回执，不是Codex完成。
- 当前`agent_line/authoring_api.py:818`附近已经调用原生deploy并核对版本；不是另写了底层发布引擎。需要退出的是重复产品交互与过度转换，不能将所有包装一概删掉。

上述QwenPaw相对源码均位于`upstream/qwenpaw/src/qwenpaw/`；Sim API相对源码均位于`upstream/sim/apps/sim/`。

## 5. 最小目标链路

1. 在QwenPaw官方页面管理已有角色、会话与记忆。
2. 从现有官方插件进入Sim原版画布搭建、保存、发布流程。
3. 通过小型绑定面板将原生节点与已有角色ID关联；原生画布中的接口能力不足时不注入DOM或改上游源码。
4. 节点经必要适配提交给原QwenPaw角色，由其召回原生记忆并调用Codex。
5. 执行回执、真实结束、审批及未知状态沿现有身份核对；Sim在原等待点接收结果后决定if/else。
6. 现有简洁首页聚合任务状态，详细Agent/会话/运行信息优先导航回原页面。

不预先规定必须使用某一种HTTP节点拼法。Sim同步HTTP节点的超时、提交后等待点建立竞态、任务比等待点更早完成、重启后的回传、审批归属，都需要在这一条链路上验证。没有证明前，不声称“直接换成HTTP调用即可”。

## 6. 保留、替换与删除顺序

**先保留的必要接线：** 现有插件注册/原生Harness调用、受管角色和项目绑定、任务幂等、准确thread/turn与审批映射、未知状态核对、原结果回传、原生记忆调用。现有账本保留原状，不借精简迁移或删数据。

**优先替换的重复实现：** 独立SVG画布、通用节点编辑/布局、额外流程目录及版本展示、复杂自定义图转换。替代为Sim页面和官方数据；中文小面板只覆盖节点绑定等缺口。原生页面中文覆盖仍需单独验收，不能拿英文画布冒充全中文。

**停止扩张的部分：** 额外模板品类、团队关系可视化、预算/健康面板、自然语言草稿、记忆期限/诊断、Kanban附加展示。现阶段不新增配套配置、抽象、表或后台线程，不擅自删除已有能力。

**删除前置条件：** 原生替代链路通过必要回归；旧流程和既有任务历史仍可读；权限与中文核心操作可用；旧发布运行继续按原身份观察；依赖引用已核清。先移除新增流程对自定义图格式的依赖，再退出重复编辑器和转换分支，最后删除确实无调用的辅助模块。不能同时改画布、运行协议和数据库。

第一实施批次只核对一个原生Sim流程：已有角色→原生记忆→Codex→原等待恢复→条件分支。优先改已有接线，不增加新服务、新框架、第二套账本或上游补丁。验证不了就定位具体缺口，不能自动恢复“独立编辑器继续扩建”的路线。

## 7. 测试与交付边界

用户已恢复必要测试。上一批154项现有测试通过，是所选当前回归证据，不能证明原生画布替代、只读沙箱、ReMe索引失效或完整升级已通过。后续按替换范围复用这些测试，再补最小真实链路证明；不每次全量，不为行数或覆盖率堆测试。

本轮仅完成证据梳理与收缩方案，运行代码尚未减量，也未声称完整PRD完成。源文件统计和两项Luna只读审计均未访问正式数据或修改上游。下一步的判断标准是减少实际重复实现和维护面，同时保持用户可用链路，而不是增加更多审计文件或功能入口。

## 8. 后续第一批实际验证

已直接复用现有`probes/sim/workflow_probe.py`和Sim官方API验证原生图，未经过`compile_document/decode_document`。同一发布图的if/else两个分支均真实完成，原生Human等待点在另一个Python进程恢复并核对准确输出。现有`PublicationVerifier.verify_run`也通过了该原生发布版本与实际运行快照核对。因此普通单次激活原生图已有可复用路径，不需要先开发新的执行框架；此前“复杂图依赖编译器”的判断仍成立，不能扩展成所有图都必须重写接线。

首次复用旧探针时，固定名称触发409；保留原intent并通过只读列表和上游冲突分支确认同名记录存在，没有重投或收养旧流程。仅修复该既有探针：新尝试使用持久化UUID派生名称与节点ID；旧状态不迁移，未知请求不重投。必要回归先复现409，再验证独立尝试、重开原状态、legacy原身份及unknown不重投。42项现有接线测试和1项新增针对性回归共43项通过；补充legacy/unknown场景后该回归再通过，错误级flake8通过。

真实记录位于私有`var/native-sim-reuse-20261023/independent-state.json`及`report.json`，失败原件为同目录`state.json`。只在现有验证工作区新建两个专用流程和三个无模型运行，均已完成；没有修改现有流程、上游、正式配置、任务库或原生记忆，没有重启服务。未执行Codex，也未验证原生画布的角色绑定交互，不能以此提升完整AC或宣称全链路完成。下一批应沿这条已有原生路径接通既有角色，而不是继续实现编译器或新增后台模块。

## 9. 原生图到已有角色的实际接线

下一批直接复用`WorkflowGateway`、`load_task_flow`、`SimWaitClient`、`PublicationVerifier`、`ExternalTaskDiscovery`和既有本地账本类；没有新增生产模块、接口、线程、图格式或编译器。通过官方Sim API新建一个专用原生图：开始→原生Human机器等待→条件→结束；taskInput来自原生开始节点，agent策略绑定已有项目/角色/revision。该Human节点明确用于外部机器任务，不冒充人工审批。

现有启动守卫首先因未给候选工厂装配AutomaticVerifier而返回503，发生在原生运行提交之前；补齐实际验证器及候选发现准入后，同一流程首次启动成功。没有降低守卫、改主服务就绪或重复提交未知请求。首次观察尚未登记，随后仅观察同一run即登记一项任务；再次观察保持同一task和全部任务记录不变，角色ID/revision及输入均匹配。证明接线可复用，不需要先增加新的运行模块。

私有现场为`var/native-role-reuse-20261023/`：`state.json`保存原流程/run/task身份，`binding.json`是本次候选绑定，`preparation-report.json`记录结果。本次候选TaskStore与主TaskStore分离，候选任务仅registered、没有提交Codex，也没有工作区写租约；原生run目前在等待点，尚未回传模拟结果。没有常驻候选worker或新服务。

真实模型前的预检通过现有官方鉴权和execution-binding核对，角色revision匹配，执行角色仍为原生Codex backend，父角色仍用remelight。当前业务执行模型为gpt-6.1-sol，父协调角色为其原配置模型；未修改任一配置。此前用户指定Codex调试为gpt-6-luna，因此已经明确请求本次业务链路验收是否可沿用既有模型，回答前不执行模型相关动作。原生ReMe真正召回、Codex实际执行、产物独立验收及返回分支均保持未验证，不能用registered记录替代。

最后只读核对主库仍为19任务、活动0、租约0；无正式数据/配置修改或服务重启。后续继续观察/提交这一个已登记候选身份，不重新建流程或任务。

## 10. 原生画布与统一入口实际验证

在模型选择待回复期间，继续完成无需模型的入口验证。发现3194无监听，回环与局域网地址均连接失败；3190/3192/3193及原生QwenPaw端口仍正常。确认没有本仓库gateway:app进程后，直接复用`deploy/gateway/run.sh`恢复网关，PID3677392、监听0.0.0.0:3194。原配置未改，其他服务未重启；固定入口配置HTTP读回及/proc工作目录、监听均确认，私有记录为同目录`gateway-recovery.json`。

浏览器初次启动缺少libatk，改用历史验证已存在的依赖/字体缓存，仅设置本次测试进程环境，没有安装依赖或修改系统文件。随后发现只允许GET会拦住Sim原生Socket鉴权；源码确认其为一次性会话令牌，最终仅额外放行该鉴权和当前工作区visit元数据请求。原业务图写入、运行、审批、记忆等修改均未放行。第二个旧断言将原生显示标签当作节点ID，画布实际已显示但测试超时；最终按`.react-flow__node`的data-id逐项核对，而非更改产品名称迎合测试。

最终真实浏览器结果：QwenPaw插件直接显示任务与服务首页，切换原版Sim画布后打开同一候选workflow，5个原生节点ID全部匹配；页面错误0、业务修改请求0、模型调用0。明确有Socket会话POST1次、工作区visit元数据POST1次及登录操作，不能称为全部网络零写入。报告和截图在`browser-report.json`、`native-canvas-readonly.png`。

额外读回原生草稿和发布版本：blocks/loops/parallels/variables一致，edges的区别仅为草稿省略发布版本中的空data字段，其余逐项相等，不声称响应字节完全相同。主库19任务、活动0、租约0；唯一候选任务仍registered、未提交Codex。结果见`browser-state-check.json`。本轮新增生产代码0，不以此宣称全中文画布、节点绑定表单或Codex/记忆完整闭环已经完成；模型选择仍沿原问题待回复，不擅自修改业务角色模型。

## 11. 原生发布连接点：已有源码与未完成边界

已有发布接口现有源码补了显式nativeBindings请求：流程仍须有现有项目创建归属记录；编辑由原版Sim承担，发布沿原权限/状态摘要/幂等操作日志。绑定只接受节点ID、已有roleBindingId和任务说明等必要配置，角色ID/revision/验证策略均由服务端原解析器决定。所有原生Human节点须明确区分Agent任务或人工审批，不自动猜测；保留现有单次激活/不可重复上下文的限制。只为原生状态增加明确的发布摘要标记，继续使用原发布目录与启动检查，不增加表或运行服务。

复用现有发布测试夹具覆盖原生图发布/回读/目录、幂等、混合/纯人工等待及拒绝越权/遗漏节点；该批相关73项本地回归通过。旧发布请求、哈希和SaveRequest保持原契约；不因新路径静默收编原生Sim中无项目创建记录的流程。暂停期间又加入了`frontend/native-binding.js`，复用原编辑页角色列表和publish操作；本轮已核对其代码及后端脱敏等待摘要，不再重复实现表单。原生替代的完整浏览器操作、实际命中记忆及任意原生循环/子流程仍不能据此宣称完成。

## 12. 具体收缩对象与退出条件

| 现有代码 | 当前处理 | 删除或缩小的前提 |
| --- | --- | --- |
| `frontend/editor.js`（1783行）、`editor.html/css`、`canvas-layout.js` | 停止新增画布、布局和节点表单能力 | 原版Sim编辑与角色绑定实际可用，旧流程仍能查询和继续 |
| `frontend/subworkflow-editor.js`（574行）、`version-history.js`（397行） | 停止重复子流程/版本产品交互 | 对应原生页面承接操作，权限与历史访问保持 |
| `agent_line/sim_authoring.py`（740行）中的compile/decode | 新原生图路径绕过；旧路径保留兼容 | `authoring_api`、`publication`、`published_workflows`、`run_view`及模板/批处理等调用已退出；其中官方API客户端不能随编译器一起删除 |
| `subworkflow_expansion/mapping/reference/admission.py` | 不再扩展自有展开和映射规则 | 新流程沿原生子流程执行并通过身份/恢复核对，旧发布仍可观察 |
| 原插件执行桥、任务/提交账本、结果回传 | 保留、复用，暂不重构 | 它们承担跨系统关联、幂等和未知状态处理；不能仅以行数判为重复 |

上述是本次源码行数与调用方核对，不是已删除量。当前没有可以不经替代验证就整块删除的编辑主链路。本轮只更新实施约束，不新增生产代码、测试用例、依赖或运行服务；必要测试复用既有用例，不进行全量测试。


## 13. 父连接失败后的同任务重投

第 9 节之后实际卡住的不是父模型，也不是 Sim 等待点身份。第一次父任务 `task-181f74444703` 在调用工具前因 `kilo-auto/free` 连接失败结束，私有任务停在 `submission_uncertain` / `execution_observation_unknown`，没有 Codex 回执。原暂停点只接受 `task-b9da9911ff3f403488c8e9894017fa9f`，且 `allowFailureBranch=false`，所以新任务号接不上。

因此只放开“父连接失败且没有执行回执时，不得重投同一任务”这一条。`readmit_parent_connection_failure.py` 把该私有任务恢复到合法提交前状态，插件绑定保留且未撤销。失败现场在 `var/native-role-reuse-20261023/evidence/parent-connection-failure-before-readmit.json`，以及同目录的 `tasks.sqlite.before-readmit`、`roles.sqlite.before-readmit`。父角色、`g0-codex`、ReMe、主库和验收总账都没有改。

第二次父任务 `task-5013d7588d99` 仍使用 `kilo-auto/free`。它先调用 `memory_search`，返回 no memory results，再调用 `agent_line_delegate`。Codex 身份是 `upstreamTaskId=task-fa050afa6ac5`，`codexThreadId=01a168d2-6c7d-78b0-8c48-2c03a73361cb`，`codexTurnId=01a168d2-7ebd-73a0-a528-1287a56f3aed`。容器模型仍是 `gpt-6-luna`。私有任务版本 13，状态 succeeded/accepted/verification passed/resources released，事件顺序包含 registered、两次提交相关事件、accepted、execution_observed、succeeded、resources_released、verification_passed。产物 `native-sim-reuse-e738dea47e95419fa4ffdc9c3440212d.txt` 为 49 字节，sha256 `1c8e41d213a68cebc1be2227054e0c2062d9dc24d2860c60a173998449ccfa15`。

只读核对 Sim：原 run `5538d7e6-5435-54a3-b554-c38388340df2` 于 `2026-10-23T12:53:44.497Z` completed；恢复子 run `bc67b89c-6eff-4abe-9602-74b06cf8c9bb` 也 completed 且 error 为空。条件节点 `selectedPath.blockId` 是 pass 响应节点，其 data 为 `{"branch":"pass","taskId":"task-b9da9911ff3f403488c8e9894017fa9f"}`。退出码 137 的来源是成功后的 `docker-inspect-before-remove`。

两个容易误判的字段不代表未完成。`verifiedComplete=false` 由 `agent_line/adapters/sim.py` 的传输回执和 `workflows.py` 的对外 receipt 固定写出，文档 `sim-adapter.md` 已说明不能用回执自述代替运行观察。私有 `runs.sqlite` 的 `complete=0` 是因为 `approval_discovery.observe_transition` 只有在 `terminal=True` 时才把它写成 1，而这次探针完成 resume 后没有再调用它；`registration_status` 仍是 `external_task_bound`，submission 已是 accepted。私有角色投递观察也还停在 running。这些是账本未回写，不需要再发 resume，也不手改终态。`continue-luna-report.json` 没有生成。

主库仍为 19 条任务、非终态 0、租约 0。进程仍是 QwenPaw PID 1728972 端口 52403、适配器 PID 2100029 端口 3193、网关 PID 3677392 端口 3194。检索工具确实执行过，但没有返回既有记忆，因此不能把 AC-40 或完整召回标成通过，也不修改 `acceptance.json`。汇总证据是 `var/native-role-reuse-20261023/evidence/same-task-readmit-observation.json`。
