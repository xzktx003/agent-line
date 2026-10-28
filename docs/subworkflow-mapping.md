# 子流程输入与返回字段映射

在[编译展开预览](subworkflow-expansion.md)请求中增加可选inputMapping和outputMapping。省略或null沿用整对象传递；配置后只返回明确列出的字段，不自动复制其他字段。预览只读，后续[单层受控保存/发布](subworkflow-admission.md)另经来源、角色和父预算准入；没有新增映射编辑器表单，也没有真实执行验收。

两种映射共用同一格式，例如把父输入中的requirements改名为task，并附加固定语言：

```json
{
  "schemaVersion": 1,
  "fields": [
    {"name": "task", "kind": "path", "path": ["requirements"]},
    {"name": "language", "kind": "literal", "value": "中文"}
  ]
}
```

inputMapping的源是原inputFrom指定的完整对象，结果成为子入口input。outputMapping的源是原子结束节点的响应对象：resultFrom模式中仍有agentLineResult包装，因此选取具体编码结果状态时，路径可能为["agentLineResult","output","result","status"]，前提是该源角色真实声明并返回这一结构；固定JSON结束模式则从原固定对象直接选字段。不从长期记忆或模型成功自述推导本次验收结论。

schemaVersion严格整数1，fields为1–32行且目标name不重复。name和path每段为1–64字符ASCII标识（首字符字母或下划线），禁__proto__/constructor/prototype。path为1–8层对象字段，不是表达式或JSONPath；不支持数组索引，可整体复制数组。literal只允许JSON值，不接受JS代码、模板或函数；字面字符串中的小于号与双花括号会编码转义，避免成为Sim动态引用。

静态定义<=32768字节UTF8；字面值和实际结果均限制深度8、总节点256、字符串4000码点、数组100项、对象32键，禁NUL和原型键，数字须有限且绝对值<=9007199254740991。结果根计为深度0；实际映射JSON序列化<=65536字节UTF8。动态来源结构更大时，定义通过仍可能在执行时明确拒绝，不能把静态配置校验当作实际输入已通过。

固定生成代码逐层检查自有属性和对象类型，缺失字段、错类型、非法值、深度/大小超限抛固定错误；不会把缺失变null、false或空对象。投影使用null-prototype对象，读取和写入都不使用原型继承，输出大小按Unicode码点计算UTF8字节，不依赖新增运行时库。只有编译器生成的固定来源表达式进入代码，没有用户可提交的source_expression、eval或Function入口。

inputMapping/outputMapping冻结在原入口marker，返回出口复制outputMapping；validate_groups重建时重新验证全部映射并比较规范JSON，不能只篡改出口或子图成员。无映射时原marker配置与function body保持不变。原版本hash、角色/项目权限、父图hash复核、200节点/1000边及子图限制均保留；没有新数据库、模型调用、角色创建或记忆系统。

根与指定gpt-6-luna完成源码复核和四个Python文件AST解析。修正了生成IIFE缺顶层return、literal深度偏移和JS键名末尾换行边界。未新增或运行测试、映射编译行为、Node/JS、HTTP、浏览器、模型探针、lint/typecheck/build；实际预览/原生函数输出未验收，完整PRD/AC20/22/13保持。
