# 受控执行用量接线计划

目标：从既有插件已经核对的专用 Codex thread/turn 的显式 `token_usage_record` 提取已报告 token 累计快照，带原身份进入任务证据和中文详情。QwenPaw 父角色、模型调用次数、费用和全任务总量仍未知，不能用单个 Codex 快照替代完整预算计量。

范围：现有 execution_bridge 的只读观察、Dispatcher 可信终态证据、服务端公开投影与已有详情文案。无新数据库/服务/依赖，不读其他角色记录，不改 QwenPaw 或用户全局文件，不追加真实任务或模型调用。

约束：只有既有 metadata/cwd/唯一task_started/turn匹配已通过的完整序列可提取；明确记录的 thread_id/turn_id 须一致，session/root须连续。每条单次usage累加必须与turn_token_usage完全一致，重复response_id仅在整条记录一致时去重，缺前缀/倒序/冲突保持未知。不采用无turn_id的token_count或线程累计。样本数不是完整模型调用次数。终态首次快照（含未知）由原回执事务原子冻结，不为补统计而修改旧任务或触发重新执行。

只源码阅读和AST静态解析，无新增或执行测试、探针、模型、Node、浏览器或正式数据写入。子工作流固定历史版本仍受当前Sim接口限制，不因此开放不满足约定的节点；完整AC-66/AC-33不得计通过。
