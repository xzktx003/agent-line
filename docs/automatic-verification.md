# 现有后台线程自动验收产物

本功能接在现有节点任务完成之后：QwenPaw 原生角色委派 Codex，实际执行退出后，适配器核对部署预先声明的文件字节，再把结果交还 Sim。Sim 继续决定分支。没有新增运行服务、数据库格式、依赖或记忆引擎，也不修改 QwenPaw/Sim 官方源码。

本页描述 schema1：指定文件的 SHA-256、可选精确大小和读取上限。它证明声明的最终文件条件，不证明测试命令执行通过、文件首次由本任务创建，或工作区没有受控边界以外的手工写入。schema1 的 `requireTests:true`、命令字段和未知策略会被拒绝；不能用字节校验冒充测试验收。需要独立测试命令时使用新增的显式 schema2，见[可信测试命令自动验收](automatic-test-verification.md)。

## 配置位置

在现有私有服务 JSON 中加入可选对象，不增加另一份配置文件：

```json
"verification": {
  "evidenceDirectory": "/absolute/private/verification-evidence"
}
```

目录须提前存在、由当前用户持有、权限为 0700、使用实际绝对路径，不能经过符号链接。它与所有登记项目工作区不能互相包含。单份回执及其锁文件为 0600。

策略放在同一服务配置的 `bindings.<roleBindingId>.verificationPolicy`，例如：

```json
{
  "schemaVersion": 1,
  "policyId": "empty-result-v1",
  "kind": "artifact_sha256",
  "requireTests": false,
  "artifacts": [{
    "path": "result.txt",
    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "exactSize": 0,
    "maxBytes": 1024
  }]
}
```

此例只要求 `result.txt` 为空文件，部署时应换成实际业务策略。每份策略允许 1–32 个不重复的相对路径，全部读取上限合计不超过 64,000,000 字节；省略 `exactSize` 表示不要求精确大小，省略 `maxBytes` 默认 10,000,000。

策略来自服务端角色绑定，在发布回执、原运行快照和任务载荷中逐层冻结。浏览器和模型不能提供或覆盖可信策略，模型输出中的 hash、testsPassed 或同名字段不会改变验收条件。发布后修改角色策略不追溯改变旧发布和旧运行。策略有误或没有装配验收器时阻止发布，并显示中文配置提示。

这项配置只接通验收，不授予执行能力；CLI 的受控执行准入仍为 unknown，不能用配置自报 confirmed。主服务尚未启用本可选配置，真实验证使用隔离探针。

## 租约与恢复

现有 DispatchLoop 在任务 succeeded、真实执行资源退出、原始工作区租约仍属于该任务时调用验收器。`resourcesReleased:true` 只表示执行器退出；有自动验收策略的任务在验收提交前继续保留原工作区租约，避免下一任务先改写同一文件。父子路径也受同一冲突检查保护。

验收回执绑定 taskId、完整执行身份、策略正文及哈希，使用确定文件名落盘。通过或失败回执提交与释放原租约在同一事务完成。已有回执先核对并复用；证据已落盘但数据库提交失败时，重建应用不会重跑 Codex或重新读取已改变的产物。重复旧回执不能释放后继任务的租约。

原租约丢失、执行退出证明不足或证据不可核对时保持 pending/阻塞，不重新抢租约后宣称恢复了原任务的文件归属。受信策略失败记录为 verification failed；实际执行成功与业务验收失败分别保存，Sim 按该结果走失败路径。

## 验证范围

自动化回归覆盖服务装配、策略冻结、模型注入拒绝、原租约保留、并发验收、跨应用恢复和故障事务。真实两任务通过/失败分支及证据落盘后提交失败的恢复见 [真实后台验收探针](automatic-verification-live.md)。根代理另行只读复核证据位于 `var/automatic-verification-flow/root-check.json`。

无网络、只读项目目录的可信测试命令已有独立执行与真实工作流正例，配置及限制见上述schema2文档。自定义输出 schema、完整生产准入和全部 PRD 验收仍未完成。底层文件核对接口及历史证据见 [产物校验](verification.md)。
