# 部署JSON的确定性读取

主服务配置、Sim凭据和触发配置统一复用原execution_runtime._json解析器，与执行、审批、记忆、编辑等既有部署配置一致。任意对象层级的重复字段不再由后值覆盖；NaN、Infinity、-Infinity不能作为配置数字。主服务schemaVersion须严格整数1，true不再被当成1。

重复项目、token、role/trigger标识、嵌套字段都必须显式消除歧义后才可启动。错误使用固定说明，不带冲突字段名、字段值、原始JSON或凭证。不自动修配置、不选择其中一个值、不绕过执行准入或重建数据。

原文件路径和权限检查保留；触发文件仍受无symlink/owner-only/普通文件/1MiB限制。有效配置格式与业务值不变，未新增依赖、配置层或迁移。共享解析器用于现有部署JSON，不改变任务/事件/Sim回执的历史解析或官方QwenPaw配置引擎。

2026-10-23仅源码/三个Python AST/Luna静态复核；额外使用标准库只读核对当前8份实际部署JSON，未发现重复字段、非有限常量或JSON解析问题，没有导入应用执行检查。该核对不是负面行为测试或全部配置兼容验收。未运行测试、Node、HTTP、浏览器、模型、lint/typecheck/build。正式配置字节保持，部署见backend-rollout-config-json-20261023.md，AC不提升。
