# 子流程声明式字段映射计划

补PRD7.6输入输出结构明确：原preview增加可选inputMapping/outputMapping，每项schemaVersion=1、fields列表1–32，每行唯一name及kind=path/path数组或kind=literal/value。字段和路径限ASCII标识、排除原型污染名称，路径1–8层对象字段；不是用户JS或模板表达式。省略保持原完整对象传递，显式null只作未配置，不把空对象当有效映射。

静态映射定义<=32KiB；字面值只JSON，深度8、节点256、字符串4000码点/数组100项/对象32键，安全有限数字、无NUL或原型键。固定生成JS逐层own-property读取，缺字段/类型/投影过深过大明确抛错，不把缺失当null或false；实际投影同界限且JSON<=64KiB UTF8。字面JSON中的Sim模板符号转义，不允许成为动态引用。

inputMapping以原inputFrom取到的完整对象为源，返回子input对象；outputMapping以原end响应对象（含原agentLineResult包装）为源，返回output对象。两映射同时冻结在入口marker，出口复制输出映射，validate_groups重建时核对一致。marker编解码/原文档/原引用和普通旧图路径不变，映射生成使用固定function代码。

Luna只负责subworkflow_mapping.py验证/固定JS生成；根负责expansion/sim_authoring/authoring_api接线；另一Luna只读复核。仍仅preview，保存/发布/模板导入导出禁包装，未开放执行/嵌套或预算。不新增状态库/依赖/上游补丁/模型/记忆引擎。仅源码/AST，禁止测试/Node/HTTP/浏览器/模型/lint/build，不提升AC。
