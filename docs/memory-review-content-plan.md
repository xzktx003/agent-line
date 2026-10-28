# 记忆审核正文的服务端检查

当前经验草稿生成包含【请填写经验结论/适用条件/失效条件/适用版本/操作边界】。原单项/批量界面会阻止未完成草稿，但review HTTP及记忆提交claim没有对应检查，单项路径也缺少批量已有的当前凭证保护。

沿原MemoryReviewLedger和MemoryCommitStore补检查：新审核在任何持久登记前拒绝未完成占位、NUL与当前审核凭证；原首次write claim及明确失败retry claim在创建出站意图前检查正文，不能从旧approved/failed行绕过。intent/pending的原只读核对和历史查询保持，不改已有正文、摘要、decision或删除墓碑。

单项前端复用既有批量凭证检测，禁用并提示；提交handler在生成decision/本页tombstone前再检查。无新表/迁移/记忆后端/额外服务/依赖，不定义伪原生过期协议，不私改ReMe索引。完整过期/检索过滤仍未交付，不以此补丁宣称完成。

根实现，指定gpt-6-luna独立静态复核，只源码及Python AST，不新增或运行测试/Node/HTTP/浏览器/模型/lint/typecheck/build。需后端部署时沿原空闲备份维护重载。
