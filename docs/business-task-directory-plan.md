# 按业务任务查看所有节点记录

补PRD Mission/13.7：从已登记业务任务直接查看其多次运行的节点状态与身份，复用原TaskStore目录，不建立新的任务调度/聚合状态。

- TaskStore.list_tasks新增可选work_item_id，匹配原冻结payload.workItem.workItemId，同时必须提供已授权project_id；可叠加完整workflow/run范围。原SQL参数化筛选和同快照游标核对纳入固定业务身份，status变化仍不使游标失效。
- 原私有node-tasks目录增加workItemId查询；仅请求该条件时在filters回显新字段，未请求保持原三字段契约。匿名overview不扩大参数或字段。
- 业务任务条目新增“查看节点任务”，显式点击清原运行/状态筛选并读取原目录，不改选用目标、不启动流程。原手工筛选可填业务任务ID，但必须同项目；退出/清范围/切运行清理旧条件，整页记录和筛选回显核对。
- 不跨库推测“业务完成”，空目录只代表当前查询无节点，不声称未提交或已成功。保留执行/验收/记忆独立状态与历史无关联记录。
- 根负责store/service/app；Luna仅work-items.js/runs.html，独立Luna静态复核后原维护重载。无新增/运行测试、Node、浏览器、HTTP、模型/探针、lint或构建，不改官方源码/记忆/配置。
