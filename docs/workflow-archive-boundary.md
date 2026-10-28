# 原生归档与历史读取边界

2026-10-22 对固定 Sim 部署创建一个独立空流程，通过官方 DELETE /api/v2/workflows/{id} 验证软归档。探针不发布、不执行模型、不碰正式流程、角色或主日志；创建与归档意图分别写入自己的私有 var/workflow-archive-20261022/sim.sqlite。重复运行只观察原身份，未知结果不自动提交第二次。

真实结果：归档回执为 deleted=true、archived=true，归档前详情、runs、versions 均为HTTP200，归档后均为HTTP404；scope=archived 的工作区列表仍找到确切流程。再次观察只有4次GET，既无POST也无DELETE。原生归档会隐藏活动上下文，不能据此声称历史被物理删除，也不能承诺原历史接口仍可访问。

新增 SimAuthoringClient.archive 和 list_archived，沿用原工作区身份检查、固定路径、无重定向和变更未知语义。archive 仅接受严格软归档确认，不接受 archived=false 或别的流程身份；列表保留官方分页。SimJournal 原操作集合新增 archive，不迁移或添加表，意图始终先于远端操作。15项针对性检查通过；组合相关回归73项通过（5.676秒）。

当时尚未开放正式归档API或界面。后续已补下述审计API与中文入口；主后端仍待部署。FR-WF-05完整验收保持未完成，尤其外部原生并发、历史保留策略与故障组合仍需覆盖。没有改上游数据库、源码或权限策略。

探针命令：.venv/bin/python -m probes.sim.archive_boundary。首次会创建并软归档其自有空流程，之后只观察原记录；不删除探针日志后重新运行。证据 var/workflow-archive-20261022/{before,report}.json。探针工作流保留归档，未执行restore或硬删除。

## 归档前审计与中文入口（后续实现）

新增 workflow_archive.py，复用原SimJournal，不新增数据库、调度器、记忆实现或上游补丁。归档入口只接收既有项目所有权下的流程及预期草稿摘要；在原维护守卫和编辑锁内，读取原草稿、分页可见版本及完整定义、已结束运行详情（含API投影后的输出）、原运行发布引用。快照先随archive意图完整落盘，再通过官方接口软归档。任何读失败、非终态/未知运行、本地未确认提交、版本身份矛盾、索引变化或容量超限都会在DELETE前拒绝。

单份记录总数最多1000（版本/运行/本地根运行/发布引用合计），完整意图UTF-8最多16MiB；不截断后假称完整。未结束状态包含pending/queued/running/paused/redacting，仅completed/failed/cancelled可进入快照。此处保存的是捕获时官方API可见历史，不补造已由上游保留策略清除的日志。

同operationId先核对项目、主体与稳定请求，返回原记录，不重抓快照、不重复DELETE；同流程有任何archive intent/uncertain/accepted时，拒绝另一个操作编号再次归档。原保存/发布在共用编辑锁内阻止新变更，新运行入口也共用同一路径锁（含静态绑定）。旧发布引用保留供历史访问，原已登记运行重放不因归档变成新执行。

接口：POST workflows/{workflow}/archive，GET archives、archives/{operationId}，POST archives/{operationId}/reconcile，均位于原 /extension/v1/authoring/projects/{project}/ 下。审计读取验证项目权限、accepted-create所有权、workflow身份和快照摘要；Sim不可达时仍只读原本地快照。核对接口只查官方archived scope，找到精确身份才将原未知记录确认为已归档；找不到仍未知，绝不重投DELETE。核对不是恢复流程。

中文界面提供归档当前流程、归档分页、只读版本/运行摘要、完整审计下载和未知状态核对。未保存或未应用配置时禁止归档，确认提示解释原生触发器会停用。退出、切项目清空审计，迟到响应不回填，凭证和快照不写浏览器存储。归档未确认的活动列表条目禁止打开编辑；确认后从活动列表移除。

## 当前证据

- 新增10项归档检查：意图内快照先于DELETE、断连审计、同op/不同op防重、项目范围、hash、全部非终态、容量和版本身份、未知保存/发布/新运行阻断、显式只读核对、并发静态start被同锁阻挡。相关首轮组合45项通过。
- 本批集中全量1685项通过（93.533秒）；其后只补快照总量和版本索引/详情一致性守卫及一项边界测试，最终10项归档检查通过（0.693秒）。该最后增量未重复全量。错误级Python lint及JS语法通过。
- 4个真实浏览器场景使用拦截API：1440/390确认归档、丢失浏览器回执后仅查询原操作、原生结果未知后只核对；每场仅1次模拟archive提交，含审计下载和退出后迟到响应隔离。初次发现归档成功时busy守卫跳过活动列表刷新，已修复并复验。var/workflow-archive-ui-20261022/report.json。
- 真实独立空运行历史场景保存/发布1版本并软归档，随后阻断全部原生请求，仍可读取原草稿和完整版本定义；重复观察0原生请求。var/workflow-archive-audit-20261022/{report-first,report,audit}.json。
- 另一个自有纯开始/结束流程实际执行一次Sim零模型运行，终态completed后归档；断开Sim仍可读1版本、1运行、声明输出kept。主操作日志全行摘要保持、没有模型调用。重复观察0原生请求/无第二运行。var/workflow-archive-run-audit-20261022/{report-first,report,audit}.json。

后续主后端已按原配置重载，真实主界面专用草稿保存/归档/审计通过，见backend-rollout-20261022.md。受控执行仍unknown/readyfalse，旧证明不会开放新机器任务；部署结果不能代替当前源码角色执行验证。

## 尚存限制

本实例的保存、发布和新运行通过共用锁串行，但原生Sim界面、外部API和自有触发器不参与此锁。捕获结束紧邻归档再次检查索引与草稿仍无法消除最后读取到DELETE间的外部竞态；当前上游没有原子expected-version/无活动运行的归档条件。不会据此宣称严格覆盖所有在途归档故障或提升整体验收。原生归档会联动停用schedules/webhooks/chat/MCP等资源；当前扩展不提供恢复、不级联删除角色、ReMe、代码项目或原账本。

单次审计超界或本地已受理根运行不在可见终态列表时保留拒绝；尚无大历史流式导出、长期保留管理、完整外部并发测试。完整PRD继续。
