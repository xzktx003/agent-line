# 通用触发入口与原事件核对

继续P1 R-18及PRD运行可追溯性。复用已有TriggerAPI与提交日志，不建调度器，不加入Jira连接器或模拟事件，不修改正式触发配置。

- 原授权GET触发目录仅给scope内启用项目的固定触发ID、项目、绑定、类型及configured标记；不宣称有活跃调度器、健康或新执行准入。
- 新只读POST lookup接事件ID（schedule仍规范UTC分钟），保持ID不进URL。由固定配置决定kind/binding，不允许事件正文指定流程。核对冻结触发定义及原publication pin/root同一SQLite读快照，计算原确定性run ID，不调用start或reserve。
- 抽取原trigger namespace及WorkflowGateway运行身份纯函数共享，保持旧canonical/UUID算法完全一致；同名触发器改定义拒绝，不返回其他版本历史。
- 未记录与回执未知都safeToResubmit=false，不用查不到证明未执行；不发原生查询，不返回事件正文、输入、回执或秘密。结果只回填已有运行表单，再显式查询。
- 工作台默认折叠“已配置自动入口”，目录与事件核对均原授权，只读lookup POST不提交事件；未配置404明确提示且不显示空创建/启动操作。退出/切凭证/触发项变化使旧回执失效，不持久保存事件ID。
- 根backend/app；Luna frontend独立模块/HTML，Luna静态复核。无测试/Node/HTTP/浏览器/模型/探针/lint或构建；原维护重载核对。仍未交付触发配置编辑或实际外部调度验收，完整PRD不因此完成。
