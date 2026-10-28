# 节点结果回传与失败分支

`CompletionRelay` 只向已绑定的 Sim 外部任务等待点回传结果，不计算后继、不替代图调度，也不处理真正人工审批。workflow/run/block/context、pauseKind=human、pausePointCount=1 必须与当前实际暂停一致；发送由持久 SimJournal 防止重复。

`ResumeBinding.allow_failure_branch` 默认为 false。成功执行仍须独立 verificationStatus=passed、resourcesReleased=true，并满足绑定的记忆回写策略。启用失败分支后可额外回传三类结果：

- executionStatus=failed，含匹配当前原生执行ID的受信 execution_terminal 证据。
- executionStatus=succeeded、verificationStatus=failed，含匹配当前task和完整原生identity的独立验证回执，且至少一项检查失败。
- executionStatus=failed，含唯一匹配task及完整原生identity的execution_interrupted和resource_exit证据；中断原因必须为owned_executor_exited_without_native_terminal、nativeStatus=unknown、cancellationRequested严格为false，退出receipt必须确认为进程已退出。它表示执行器中断，不表示收到Codex原生终态。

这些失败都必须已确认资源退出；require_memory_commit仍按绑定执行。输出保留原执行/验证状态，不把失败改成成功。图通过这些字段配置条件和有限重试；适配层不选择分支或自行增加业务尝试。

已确认的测试失败是执行成功、独立验证失败、证据匹配当前任务和原生身份且至少一项检查失败。资源已退出并且记忆策略已满足时，若绑定允许失败分支，回传一次原验证状态，不查询暂停、不重发执行、不新建尝试。没有失败分支时保持原任务，原因是 `verification_failure_needs_human`。证据不完整、资源未退出或记忆未提交时仍只等待，不转人工。

已确认的 Codex 执行失败是执行状态 `failed`、资源已退出、记忆策略已满足，并且有匹配当前执行 ID 的已确认 `execution_terminal` 证据。若绑定允许失败分支，回传一次原失败状态，不查询暂停、不重发执行、不新建尝试。没有失败分支时保持原任务，原因是 `execution_failure_needs_human`。证据不完整、资源未退出或记忆未提交时仍只等待。记忆写入失败时，执行失败仍不进入失败分支。

已确认的执行器中断是执行状态 `failed`、资源已退出、记忆策略已满足，并且有唯一配对的 `execution_interrupted` 与 `resource_exit` 证据。中断原因、未知原生状态、取消意图为 false，以及进程已退出的回执都必须匹配当前任务和原生身份。若绑定允许失败分支，回传一次原失败状态，不查询暂停、不重发执行、不新建尝试。没有失败分支时保持原任务，原因是 `execution_interrupted_needs_human`。证据不完整、进程仍在运行、取消意图不明或记忆未提交时仍只等待。它不表示收到了 Codex 原生终态。

旧激活的迟到结果只能返回自己已经记下的回执。同一运行里已经有更高循环轮次的已接受恢复时，更早的 `block_loopN` 结果不再恢复暂停，原因是 `stale_activation_not_current`。不查询当前暂停，不重发执行，不覆盖新尝试，也不处理新的审批。当前这一轮自身的结果仍可回传。

已确认的原生权限拒绝必须同时满足：执行状态已是成功或失败、资源已退出、记忆策略已满足、恰有一条已接受的拒绝并且任务与原生身份一致、唯一执行策略仍是只读沙箱和 `never`。有失败分支时回传一次原状态，不查询暂停，不调用执行，不新建尝试。没有失败分支时保持原任务，原因是 `permission_denied_needs_human`。执行仍在进行、证据不完整或控制项已变化时仍只等待，不提前判整单失败，也不改写沙箱或审批策略。

编码已经成功、独立验收通过、资源已退出，且绑定要求记忆提交时，记忆状态恰好是 `failed` 不再当成结果未就绪。完成回传原因是 `memory_write_needs_retry`。不查询暂停，不恢复流程，不调用执行，不新建尝试。原任务、产物和尝试标识保持不变。记忆仍待核对、需要审核或尚未请求时，继续只等待。不要求提交记忆时，原结果仍可回传，回传内容保留失败的记忆状态，不会因此重新编码。执行失败在记忆写入失败时同样不进入失败分支。

提交在受理前被明确拒绝时，响应必须同时是 HTTP 409 或 422，以及精确头 `X-Agent-Line-Submission: rejected_unaccepted`。原因记为 `submission_rejected_unaccepted`，任务回到 registered，不保留执行身份，自动循环不再重发。只有显式修正后的一次性证明可以再提交一次。未知结果、5xx、超时、普通冲突，以及已经带有执行身份的拒绝，仍记为 uncertain，不当成受理，也不自动重提。

canceled、unknown、未取得退出证明或仅有模型自评失败均不触发自动失败回传。已有绑定不能原地修改失败策略；新运行应使用新绑定。旧持久JSON未含allow_failure_branch时按false读取，与原策略重复登记不冲突。

当前新增模块测试覆盖执行失败/验证失败的证据检查、默认拒绝、取消与资源未释放拒绝，以及旧绑定兼容；尚未把本次新增失败策略声明为完整真实Codex失败→Sim修复循环验收通过。

中断前已请求取消的任务即使最终记为failed，也保留cancellationRequested=true，不自动送入失败分支。缺少取消意图字段同样不允许自动回传，不能把未知当false。新增回归覆盖配对证据缺失、不同identity、活进程及取消意图。
