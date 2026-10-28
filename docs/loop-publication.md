# 有限循环发布与运行快照核验

只有显式冻结的 `loopPolicies` 允许编译器支持的有限循环；缺失策略仍走原单激活限制。验证要求完整图可无损解码并重新编译，循环体及生成的固定 function guard、成员、代码、连线、开启状态和策略严格一致；实际执行快照与不可变发布版本的完整规范图必须相同。任何用户任意 function、未知成员、额外人工节点、并行容器或回边均不因此获得许可。

固定上游的公共日志存在两项已核实投影差异：

- `upstream/sim/apps/sim/lib/workflows/executor/execution-core.ts:857` 传入日志的 workflowState 只有 blocks/edges/loops/parallels，省略 variables；公共日志接口原样投影经过 credential sanitizer 的快照。
- `upstream/sim/apps/sim/lib/workflows/comparison/normalize.ts:322` 起明确记录 absent edge handles 的 null 持久化等价性。真实 `var/sim-finite-loop-guard/run-proof.json` 同样捕获了 null handles。

适配仅对日志副本规范化：先验证原发布图的 variables 是空对象，才将日志中**缺失**的 variables 补为空对象；只删除值严格为 null 的 sourceHandle/targetHandle。显式 null 或非空 variables、非空未知 handle、其他字段不抹除。原发布图和原响应不修改；最后仍比较完整编译图，不能把业务变化当投影差异。

新增回归先复现拒绝官方形状，再验证精确默认值兼容以及非默认值仍拒绝。Guard 在真实 Sim 中是否正确阻断非法结果由独立原生执行探针验证，本模块单测不替代该证据。
