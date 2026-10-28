# 测试静态精简计划（2026-10-22）

2026-10-23更新：用户已明确恢复测试。以下“不运行”约定描述历史清理批次，不再限制当前工作；保留现有必要测试，按影响范围运行，不恢复已删除的冗余用例，不默认每次全量回归。当前规则以AGENTS.md为准。

用户最新要求：审查并删除不必要测试，保留必要测试，后续不要再测试。本次以此覆盖此前每功能/每批运行测试的安排，不运行 unittest、pytest、浏览器、探针、lint、typecheck 或其他执行验证；不导入测试模块。仅读取源码、AST 结构和已有历史证据。

基线静态盘点：167 个 test_*.py 文件，1753 个 test_ 方法，27177 行。数量与上一批历史运行报告一致，但这不是本次执行结果。

## 范围与顺序

1. 只清理测试和更新相关约定，不修改生产实现、上游、真实数据和服务。
2. 删除只验证旧验收探针 HTTP 客户端上下文开关/释放的两个测试模块：test_memory_deletion_probe.py（2项）、test_upgrade_persistence_probe.py（2项）。这些测试不验证记忆删除或升级恢复业务；生产记忆、删除、升级及认证用例保留，探针脚本与历史证据保留。
3. 删除 probe 的静态图构造、转调、重复模拟观察顺序、普通参数形状与空数据库辅助计数测试，逐项记录下表。关键默认只读、持久意图、原身份、不重投、token保护、实际拒绝证据等用例保留。
4. 修改前用 AST/文本检查跨文件导入和引用，保留被其他测试使用的 fixture、类和辅助函数；仅在确定无引用时移除孤立导入。
5. 静态解析变更文件、核对方法数与引用即可；不执行任何测试，不声称删改后的套件已通过。以 manifest 记录删除依据和保留项。

## 逐项候选

| 文件 | 方法 | 删除理由 / 保留覆盖 |
| --- | --- | --- |
| test_live_trigger_probe.py | test_compiler_document_has_only_native_start_and_end_constant | 探针固定模板的构造镜像；保留同文件完整HTTP流程/丢回执/原运行核对，核心编译器行为由test_sim_authoring.py保留 |
| test_readiness_live_probe.py | test_explicit_execute_is_the_only_route_to_new_work | 仅mock函数转调一次；保留默认零IO、已有态只读、真实HTTP未知准入阻止 |
| test_parallel_roles_probe.py | test_observation_queries_only_original_root_and_child_never_executes | 重复mock观察调用顺序；保留同文件已有未知态、只读复跑及双分支原身份/输出核对 |
| test_upgrade_semantics_probe.py | test_document_is_supported_condition_graph_without_models | 历史升级探针固定图回环；保留升级准入/备份恢复核心用例以及本文件未知意图不重投/插件一致性 |
| test_native_memory_roundtrip_probe.py | test_new_default_and_limits | 仅探针timeout辅助参数的常量/类型检查；保留已存在尝试不能改timeout和legacy不可扩大时限、原生召回委派证据链 |
| test_native_rejection_evidence.py | test_invalid_or_missing_input_fails_with_value_error | 通用空值/坏类型枚举；保留完整原生拒绝正例以及错turn/重复/乱序/伪造输出/审批identity等具体拒绝边界 |
| test_pinned_roles_cli.py | test_publication_counts_use_existing_databases_and_leave_them_empty | 辅助函数对两份空库做重复计数；保留正式任务库、原生角色账本及CLI默认只读/部分初始化/凭证保护 |

## 明确保留

认证、项目隔离、幂等、防重复编码、审批准确绑定、未知状态、资源退出回执、工作区写租约、恢复及备份隔离、记忆墓碑/复活防护、限额原子准入、源码与安装身份一致性、存储缺失不得重建等回归。工作台、模板、静态挂载和插件导航测试实际执行行为/恶意输入，不能只因文件包含JS/HTML就归为无用字符串测试。Compose启动守卫的静态检查暂是部署接线的唯一保护，继续保留。

## 回退与风险

不修改生产fallback或兼容路径；本次不存在需新增的替代实现。被删探针辅助函数今后变更时不再拥有这些细粒度自动检查，这是用户要求减少测试维护成本的范围。核心行为覆盖保持在源码中；本次不运行测试，因此不主张当前剩余套件执行通过。历史1753通过仍为删除前记录。

## 完成记录

按上述范围删除11项冗余测试，其中两个纯探针客户端辅助测试文件整文件移除；清除随删除失去用途的导入。现存165个测试文件、1742个测试方法，测试源码较基线减少131行。源码AST静态解析与跨文件引用检查完成；本次没有运行任何测试、浏览器、探针、lint或类型检查，剩余套件没有本轮执行通过声明。私有逐文件摘要和可回退原件保存在var/test-cleanup-20261022/。没有按固定比例删除有独立边界价值的用例；未来是否运行以用户后续明确指令为准。

## 补充静态复审

再次使用 AST 盘点现存165个测试文件、1742个方法，并比较方法体的 AST；未发现完全相同的方法体。这只能排除字面重复，不能证明不存在语义重复。另对探针、工作台、插件导航、环境和后台工作循环的相关源码做定向阅读，并由独立只读审查复核12个探针/环境测试文件，未确认可进一步删除的高置信度冗余项，本次未强行减量。

相似名称的方法仍可能分别覆盖未知提交与已收据提交、创建与执行、初始化与恢复、认证与跨项目访问等不同边界；不能只靠名称相似、方法短或测试总数来删除。此前11项删除仍是累计精简结果，本次没有额外删除，也没有运行测试。更新AGENTS.md中的命令说明，明确这些命令仅为参考，用户重新明确要求前不得自动执行。

## 2026-10-23 本轮精简计划

本轮仍只静态阅读/AST，不运行测试或任何功能探针。复核现存165文件/1742方法，不以固定目标数量删减，也不合并方法虚减计数。仅删除以下低价值结构检查；不修改生产代码、数据和服务。

| 文件 / 方法 | 删除依据 | 保留保护与明确损失 |
| --- | --- | --- |
| test_parallel_roles_probe.py / test_document_roundtrip_is_exact_static_fork_without_loop_or_native_container | 历史探针固定图的结构镜像，又对编译器做一次往返；不验证真实并行执行 | 保留test_static_parallel_compiler的真实编译往返/策略检查、test_compiled_parallel_integration及本探针原身份/输出证据核对。探针固定文案和固定图结构不再单独锁定 |
| test_static_parallel_compiler.py / test_parallel_has_no_scheduler_or_dynamic_code | 只执行fork代码并断言返回dict、不含resumeInput；没有验证是否存在调度器/动态执行，方法名超出断言能力 | 保留同文件真实分支结果聚合、非法状态/策略拒绝与编译往返，及并行集成。fork空对象形状不再单独检查 |
| test_workflow_templates.py / test_catalog_cannot_be_mutated_and_contains_only_portable_logic_metadata | 目录展示元数据的精确键集合、fixture字符串和副本隔离不值得当前阶段独立维护一项Node子进程检查 | 保留模板实例化/真实编译/人工审批/失败出口/角色映射隔离与独立图身份检查。catalog返回副本隔离不再有独立测试；这不是等价覆盖声明 |

范围外：保留修复模板三轮上限（用户可见行为）、凭证过滤、默认零执行、去重/不重投、审批/项目隔离、租约、原生记忆与恢复相关回归。未发现可凭名称或继承关系继续批量删除的依据。修改前保留原件，修改后只解析变更文件并统计方法数量。

### 本轮完成记录

本轮删除上述3个方法、28行测试代码，并移除探针测试中不再使用的两个编译器导入。剩余165个测试文件、1739个测试方法；从首次1753项基线累计删除14项。没有按比例大删：审查的配置加载与HTTP集成分别覆盖独立边界，保留；当前PublicationRuntimeTests直接继承unittest.TestCase，未发现所谓重复继承15项的问题，也没有为此改动代码。

仅做源码阅读、AST解析和计数，没有运行测试、Node、浏览器、模型、探针、lint或类型检查，不能宣称剩余测试通过。私有回退原件与删除清单位于var/test-cleanup-20261023/。目录副本和历史探针固定结构失去独立细粒度检查属于本次有意识的维护成本取舍。后续不自动运行测试、也不为普通功能新增测试，沿用AGENTS.md已记录的约定。
