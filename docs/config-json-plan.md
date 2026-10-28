# 部署JSON拒绝歧义

PRD权限/项目身份/固定触发与恢复要求配置确定。当前主服务/Sim凭据/触发文件使用默认json.loads，会把重复键后值覆盖；原其他部署文件已复用execution_runtime._json拒绝重复键。统一复用该既有解析器，不增加配置层、依赖或格式。

- runtime主配置与Sim凭据、trigger_runtime保留原文件权限/链接/大小检查，只替换解析；主schemaVersion必须严格整数1，拒绝布尔true。
- 原_json补parse_constant拒绝NaN/Infinity/-Infinity，错误不携带字段名、凭证或正文。其他既有调用方继承一致行为，不修改有效配置结构。
- 不修改正式配置或上游文件。只源码/AST/对现有部署JSON的标准库只读核对，无测试/应用导入/Node/HTTP/浏览器/模型/lint/build；Luna静态复核后原维护重载并核数据保留。
