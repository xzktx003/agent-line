# 嵌套结果结构 fields-result-v2

Agent可选新的“嵌套结构（高级JSON）”。普通平面字段仍使用fields-result-v1，不改变旧契约、旧规则或运行数据。新格式在outputFields中声明结构，结果正文输出匹配的JSON对象；原输出正文仍保留，受校验的structuredOutput才进入原Sim结果投影。

## 定义与示例

```json
{"fields":[
  {"name":"analysis","type":"object","required":true,"fields":[
    {"name":"approved","type":"boolean","required":true},
    {"name":"summary","type":"string","required":true}
  ]},
  {"name":"files","type":"array","required":true,"items":{"type":"object","fields":[
    {"name":"path","type":"string","required":true},
    {"name":"changed","type":"boolean","required":true}
  ]}}
]}
```

对应正文例如：

```json
{"analysis":{"approved":false,"summary":"需要修复边界问题"},"files":[{"path":"src/main.py","changed":true}]}
```

scalar类型为string、boolean、number、integer、string_array。object必须有fields；array必须有items，其结构仅type及对应的fields/items，不含name或required。每对象允许1–32个唯一字段且至少一项必填；所有对象拒额外键，可选字段允许省略，不允许null冒充。字段名沿原ASCII安全标识规则，禁止constructor/prototype/__proto__。

根深度0，字段/数组项类型各增加一级，最多4级、128个类型节点；定义规范JSON最多16KiB。string最多4000字符，string_array最多100项且每项2000字符，字符串禁止NUL；array最多100项；数字必须有限且绝对值不超过9007199254740991，integer为严格整数。正文总上限仍64000个UTF-8字节；组合数量再多也不能突破正文大小。

这是受控结构定义，不是任意JSON Schema标准实现。不提供远程引用、默认值、null联合类型或隐式类型转换，不猜测补字段、不转换类型、不再调用模型。整段正文恰好是单一 Markdown 围栏且内部为原 JSON 时，只投影该正文，不改已保存原文。

## 条件与前端操作

选择该格式后展开高级区编辑定义，应用节点配置后保存、发布。编辑器严格解析重复键、非有限数与超安全数值。平面字段转嵌套格式时复制当前有效字段；高级正文已编辑时须先应用或明确重选节点放弃，再切格式，避免静默丢稿。原嵌套定义改为平面格式需显式重建；切普通格式仍需原明确丢弃勾选。退出/切项目清临时定义，不写浏览器存储。

字段建议只提供逐层必填对象路径下的标量，例如result.output.result.analysis.approved。数组、对象整体、可选字段或可选祖先下的字段不参与标量条件，后端编译同样拒绝。approved仍只是Agent意见，不授予人工审批/工具权限；结构通过不替代独立产物和测试验收。

## 接线与证据

沿原编译器Agent marker、发布agentPolicies、运行快照、审批策略核验及task discovery把v2与outputFields冻结到任务，原幂等哈希包含完整定义。执行前追加有界格式要求，成功后由原output_contract校验，独立验收和新的Sim回传继续使用原错误阻断。没有新调度器、状态表、依赖、Agent、上游补丁或原生记忆实现。

纯Python/JS校验器由gpt-6-luna实现，根集成并修复首版对象额外键误拒；由另一Luna静态复核。只有五Python AST与源码检查，无新增/运行测试、Node、浏览器、HTTP、模型、lint/typecheck/build。真实嵌套产出/保存发布/Sim条件/迟到回执及浏览器展示均尚待功能验收，AC20/22/13保持。
