# 发布前检查现有运行能力

QwenPaw 继续负责正式 Agent、会话与原生 ReMe 记忆；Sim 继续保存和调度图。
本功能只补现有适配服务的发布检查和中文提示，不增加服务、依赖或上游补丁。

## 用户可见行为

草稿可以先保存。发布包含当前 `task-result` Agent 节点的流程时，服务端检查
当前执行、结果回传、独立验收、角色版本和节点明确要求的记忆提交绑定。
能力缺失时不调用 Sim deploy，返回原操作的持久回执，页面保留草稿并显示原因。

| outcome | 含义 |
| --- | --- |
| `execution_required` | 执行证据未就绪，或节点执行/等待/结果回传尚未接通 |
| `verification_required` | 缺少合法的受信验收策略，或实际验证器不支持该策略 |
| `role_binding_required` | 项目、工作区、正式角色或当前原生角色版本不匹配 |
| `memory_commit_required` | 节点要求记忆提交，但没有同项目、绑定、角色和 QwenPaw 实例的提交通道 |
| `approval_binding_required` | 人工节点所需的现有审批接线未就绪 |

不含 Agent 的普通控制流程沿用原发布逻辑；人工节点仍需独立审批能力。
本检查不执行 Agent、Codex 或测试命令。正常执行时仍重新核对准入和角色，
发布成功不代表任务已运行或全部 PRD 已交付。

## 实现边界

- `AuthoringAPI` 在原提交日志操作内检查，再调用原 Sim deploy；HTTP 不能传入能力开关。
  缺少检查器、检查异常或返回未知值均拒绝发布。鉴权、项目归属和草稿冲突检查保持。
- `create_app` 根据现有 dispatcher、TaskFlowRuntime、AutomaticVerifier 和可选 MemoryAPI
  装配检查器，不接受调用方预置的发布许可。发现、等待、版本核对、回传必须使用
  同一 Sim 连接和现有日志/任务/暂停账本；项目、工作区、角色版本和验收策略必须一致。
- 复用现有 `preflight`，通过非任务 ID 前缀读取当前原生角色版本。只发描述 GET，
  不登记任务、不占工作区、不委派执行。支持当前正式 BoundRoleGateway 和受信探针的
  直接 RoleDispatchGateway，不因包装类型不同误判。
- 复用实际验证器的 `validate_policy`。schema2 测试策略没有配置测试执行器时拒绝；
  不以模型最终文本或单纯策略结构合法代替验收能力。
- `requireMemoryCommit=false` 不要求扩展记忆提交通道，角色原生记忆照常保留。
  `true` 检查已有 MemoryCoordinator 的同角色、同原生 origin 身份；这项检查不证明
  原生索引删除、跨项目全部读取路径隔离或自动经验沉淀已完整实现。
- 原 operation 的成功、拦截、未知和中断状态重放均读取原日志，不重新检查或部署。
  补好配置后的再次发布须是用户明确的新操作，不能自动换编号重投未知操作。

目前 Agent 输出与验收仅支持已实现的 task-result 编码路线：schema1 为预先声明的
文件字节策略，schema2 可仅运行测试而不预先声明产物 SHA。通用规划/分析角色的独立
验收策略仍待实现，不能把这项实现限制解释为 PRD 要求所有 Agent 都预知最终代码 SHA。

## 验证与限制

- 自动化测试使用真实临时 SQLite、HTTP 和实际服务组装；原生 HTTP 为明确的 fixture。
  覆盖缺能力不部署、当前角色变化、同名角色不同原生实例、验收器实际拒绝、旧回执
  重放，以及纯控制流程正常发布。原有传播/冻结测试明确标注其 synthetic 能力。
- 实际 Sim 探针在独占目录 `var/publication-capability-native` 创建并保存一个 Agent
  草稿。实际 `load_app` 在缺执行/验证器配置时返回 `verification_required`；根代理另用
  原生 GET 核对草稿文档一致且 `isDeployed=false`。只有 create/save 两次原生修改，
  deploy/execute/resume 和模型调用均为零。首次报告保留为 `initial-report.json`。
  独立审查另读实际 SQLite 和三次原生 GET，确认同一草稿未部署、任务/租约为零，
  记录为 `acceptance-review.json`。
- 探针首次须显式 `--execute`；已有 state 时即使带该参数也只 GET 原 operation/草稿。
  未知操作不重发。`root-check.json` 记录独立核对；主配置/准入守卫保持。
- 浏览器是完全拦截 HTTP 的离线 fixture，三类拦截提示、草稿保留、原操作查询和
  1440/390px 布局检查通过；证据 `.omx/state/publication-capability-ui/`。
  真实关闭页面后仍会丢失本页 operation ID，不声称已实现跨页面自动找回。

## 接通后的实际发布、执行与正常启动

后续以现有 `pinned_roles.py` 的显式验证模式补齐正向链路，无新增运行架构：

```sh
.venv/bin/python probes/full_workflow/pinned_roles.py --test-commands --publication-capabilities --reuse-known-template
```

首次新建验证脚本复用了旧模板名称，原生返回409；原目录
`var/publication-ready-flow` 的 create intent、uncertain journal 和失败记录完整保留。
独立只读核对显示同名旧模板早已存在，源码有事务前重名检查，但未保存原409正文，
因此不把该数字单独认定为已完成拒绝核对，也不修改或重发该操作。

上述复用模式使用独立 `var/publication-ready-known-template`，复制已验收旧模板的
原创建所有权日志，核对当前草稿、历史运行和三个原生完成子执行后，保存并发布
同一已知模板的新版本。没有再次 create，也没有收编未知资源。未来全新验证名称
已加唯一后缀；原失败目录的 state/journal SHA 在前后保持。

- 既有 workflow `fc628016-3198-4921-ae23-ad3e35c3fd51` 发布为 revision **5**，
  deployment `874cc88d-b420-4384-9d8a-5de546ba6a80`。发布窗口只有一次 QwenPaw
  `GET /api/agent-line/execution-binding`；任务、事件、租约和父投递计数前后均为零。
- 实际 task `task-be00b988fd4340b38799d34afa9f298e` 复用原生 ReMe 角色，委派真实
  Codex 生成 add 函数；受信 schema2 策略独立运行三个 unittest，exit0，产物验证 passed。
  原 Sim run `85c2e073-116a-52c4-a12b-110284ae69ea` 的恢复子 run
  `52dac211-efc0-49cd-9a62-f947462e1945` completed，实际条件选择 passEnd。
  五处角色版本一致，原生资源完成收尾，候选工作区租约归零。
- 同一候选配置通过正式 `runtime.load_app`：没有 readinessFile 时 unknown；依据
  同库已完成任务生成真实证明后 confirmed/ready，历史接口200。不再运行第二个模型任务。
  主服务配置及维护守卫没有切换。
- 临时 loopback Uvicorn 运行上述正式启动工厂，真实 Chromium 两次独立 context
  关闭重开，读回相同草稿指纹、revision5、原角色/原生记忆策略和成功任务。
  业务写入0、伪造响应0、页面/CSP错误0；1440/390px无横向溢出。仅查询已有运行，
  没有用关闭重开动作触发另一次编码。截图及报告在该目录 `browser/`，视觉94/pass。
- 随后另以该正常启动服务的实际 HTTP 提交一次明确的新运行，原模板/角色/验收策略
  不变，仍固定 revision5：run `ad47297d-da1c-5cf9-afa3-16261311107e`，task
  `task-3f4ce01d5fcf49d5a58bd2aa1b7345fa`。这是浏览器重开后的新业务运行，具有新的
  Codex thread/turn；同样三个独立 unittest exit0、Sim passEnd、原生父任务 finished、
  资源退出/租约0。同一实际启动工厂完成了执行，非仅 health 或测试夹具放行。
  原 intent/receipt 和独立核对见 `normal-reopen-run.json`、`normal-reopen-root-check.json`；
  第二任务另经完整原生 receipt/rollout/测试证据核验，配置证明指纹不变。
- 显式模式重复只观察原 run/task，新提交和 Sim 修改均为零。原 QwenPaw 角色、
  配置与原生记忆后端保持；ReMe 正常推理中可以自然积累，不手动冻结记忆正文。

本阶段全量 **1405项测试通过，66.476秒**；后续仅浏览器截图补充另跑7项相关测试。
真实现场与正常启动记录见 `report.json`、`root-check.json`、`readiness.json` 和
`observation.json`。证明范围是一条受信测试路线，不代表所有后端能力组合、
另一台局域网设备、在途升级恢复或完整 PRD 已交付。

独立审查 `acceptance-review.json` 核对两次实际执行、原生 selectedOutputs 和浏览器
时序后，认可 AC-03 与 AC-70 在当前所选 QwenPaw 路线通过；不以此宣称其他需求完成。
临时验证服务已正常退出，PID/监听退出及主配置保持见 `shutdown-check.json`。
