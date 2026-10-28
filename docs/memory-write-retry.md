# 记忆明确拒绝与单独重试

目标：编码和产物已完成时，区分记忆提交被明确拒绝与结果未知，并独立恢复记忆。
保持QwenPaw原生ReMe、官方文件API和原审批正文，不修改上游或执行任务，不新增schema。

1. 先补适配器回归。默认保留pending策略；可信部署工厂完成官方身份/角色核对且
   使用禁用环境代理的HTTP客户端后才显式开启本机原生拒绝识别。只有直连本机宿主、PUT返回固定版本可确认的
   前置拒绝401/403/404/422、且后续GET确认为missing，才记failed/official_write_rejected。
   远程/代理来源、5xx、超时、异常正文、读取未知及2xx后missing保持pending；
   已匹配正文仍以官方读回为准。不能凭一条HTTP状态判定所有写入失败可重试。
2. 复用原memory_commits.version和SQLite事务；新增显式retry，仅接受同审核、同正文、
   failed原因及expectedVersion，以CAS持久intent后获得一次PUT机会。重复旧版本拒绝。
   复用原角色锁及删除摘要检查，墓碑/删除不复活；重启或未知仍只GET。failed只读核对
   missing时保留原失败，匹配转committed，不匹配转needs_review。
3. 提取现有API的原审核/来源核对为共享内部处理，保留原reconcile行为；新retry复用
   review_memory、项目和原身份授权、维护守卫，并要求来源仍终态/资源已退出。
   返回commitVersion，任务投影保留memory failed，执行/产物/证据不变。
4. 在原中文审核页增加显式单条记忆重试，沿用按钮、只读正文及退出隔离；未知只核对。
   先针对性验证并发、版本重放、崩溃意图、删除、撤权和产物保留，再进行限定原生故障验证。

当前文档为实施计划，尚不证明AC-13通过。真实宿主/模型回合不得为了测试重复提交；
现有主任务与原记忆保持。完整验收仍需逐项证据，不能把新增failed枚举或fixture当作通过。

## 本批实现与证据（2026-10-22）

已按上述计划接入既有适配器、协调器、认证API和中文审核页；没有新依赖、表、记忆后端或上游补丁。
`POST /extension/v1/memories/{bindingId}/{taskId}/retry` 只接受严格正整数
`expectedVersion`；列表和详情返回 `commitVersion`。原任务必须终态且资源已退出，
授权/项目/原审核决定/正文/来源仍有效。先持久化CAS意图，再给同一正文一次提交机会；
旧版本、未知意图、删除墓碑或内容冲突拒绝重投。

- 最终195项记忆相关单测通过（17.209秒）；错误级flake8、Python编译、JS语法和双上游原版检查通过。
- 76项完全拦截API的浏览器检查通过；11次模拟重试均仅带expectedVersion，
  同版本超时抑制、回执身份不符、版本/原因异常、退出迟到和移动触控均覆盖。
  这是fixture证据，0真实记忆API/模型写入。
- 实际loopback Uvicorn+三份真实SQLite验证：模拟原生PUT403后failed；
  只读核对只加GET且版本保持；显式retry提交同正文后committed；旧版本重放409，
  重开存储不新增PUT。原执行身份、状态、验证、产物和证据保持。
  只mock上游记忆传输，合成任务不冒充真实编码或官方宿主。
- 指定gpt-6-luna独立只读审查未发现阻断项；不代表额外原生执行验证。
- 主适配服务已以原命令/配置重载489073→828314，绑定0.0.0.0:3193；
  备份9配置/9SQLite，19任务与事件、9Agent及原生宿主保持，自己的维护守卫释放。
  原QwenPaw未重启，controlledExecution仍unknown/readyfalse，没有替换就绪证明。

证据：`var/memory-write-retry-20261022/`；HTTP报告
`http/run-7fb730d0343f/report.json`，浏览器`report.json`，部署`main-rollout/report.json`。
视觉对照94/pass保存于`.omx/state/memory-write-retry/ralph-progress.json`。

边界：仅固定官方路由/中间件语义可确认的非空application/json拒绝正文参与分类；
升级该宿主实现时需重新验证。500、网络超时、异常正文或读回不明始终pending。
并发只读核对若先推进版本，晚到拒绝回执可能保守保持pending；不自动放宽CAS。
实际原生“编码成功后明确记忆失败且独立重试”场景仍未完成，AC-13保持in_progress。

部署后真实LAN只读验证19项通过：实际审核历史200、匿名401，既有committed记录版本有效；
桌面/手机正文只读、无重试入口、退出清空，浏览器5GET/0POST。主任务/事件、
9配置/9数据库内容摘要保持。证据`main-native/report.json`；这里只验证已提交记录兼容性，
明确失败与重试路径仍为上述模拟上游的隔离证据。

## 真实编码成功后的明确拒绝与单独重试（2026-10-27）

上面的模拟上游和“仍未完成”是 2026-10-22 的记录。2026-10-27 用一条已经成功且资源已释放的编码任务补了原场景：只临时让官方 PUT 在写文件前返回 422，审核后记为 `failed/official_write_rejected`；恢复原处理器后，用原 `expectedVersion` 官方重试一次并 `committed`。执行身份、产物和证据没有变，没有新的编码任务。AC-13 改为 passed。证据：`var/ac13-explicit-reject-20261027/report.json`。
