# 业务结果与条件分支

Agent节点除原三种格式外，已增加[自定义扁平字段格式](output-fields.md)；目录来自授权角色接口的outputSchemaIds，编辑器只提供后端声明且本地认识的格式。新节点默认保持原task-result，旧后端未声明目录时只提供原格式。未知或不可用的原标识保留显示，不静默改写，也不允许继续保存/发布。

## 编码结果

选择coding-result-v1时，Codex最终正文必须是单个完整JSON对象，例如：

```json
{"summary":"已完成接口实现","status":"completed","changedFiles":["src/health.py"]}
```

三个字段都必需，不接受额外字段。summary为1至4000字符的非空文本；status只接受completed、blocked或failed。changedFiles为最多256个不重复的项目相对路径，每项1至512字符，不允许绝对路径、空路径段、点/上级路径段、反斜线、冒号或NUL。没有改动时可为空数组。

路径列表和status是本次Agent的结果声明，不证明文件存在、字节符合要求或测试通过。原独立产物/测试策略和验证状态仍负责这些判定，不能用status="completed"代替它们。

Sim条件可选：result.output.result.status，与字符串completed、blocked或failed比较。

## 审查意见

选择review-result-v1时，最终正文例如：

```json
{"summary":"发现一个边界问题","approved":false,"issues":["空输入未处理"]}
```

三个字段都必需，不接受额外字段。summary同上；approved必须为布尔值；issues为最多100项非空字符串，每项最多2000字符，没有问题时可为空数组。此结果是Agent审查意见，不是人工审批、权限授权或测试通过证明。

Sim条件可选：result.output.result.approved，与布尔值true或false比较。两个格式也都可读取result.output.result.summary作为字符串；数组暂不提供标量比较快捷字段。

## 原始结果、身份与恢复

两种业务JSON正文上限为64000个UTF-8字节。拒绝重复键、缺字段、坏类型、非有限值、无效Unicode或夹杂说明，不猜测补字段，也不重新调用模型。整段正文恰好是一个无语言标记或 json 标记的 Markdown 围栏、且内部就是原完整 JSON 时，只把该正文投影为 structuredOutput 和回传 result；保存的 text 不改写。围栏前后还有说明、多层围栏、其他语言标记，或内部仍不合法时，保持无效并转人工，不查询暂停、不恢复、不创建新尝试。

原任务output继续保留Codex桥的text/nativeStatus/toolEvidence。API详情只在结构有效时额外返回structuredOutput；向Sim回传的output在保留原三个字段的同时附加result对象。任务和运行的发布快照原有outputSchemaId就是契约身份，无新增数据库/服务/Agent。冻结回传比对沿用相同确定性投影，task-result及失败执行保持原输出；后来的正常记忆事件不改写原回传事实。

角色委派前会追加所选格式要求，不新增模型调用或权限；加上要求后超过原28000字符限制时，在执行claim前拒绝，不形成未知提交。结果格式有效仍需原独立验证/资源退出/记忆策略满足后才能成功回传。未来若改变这两种业务契约的字段语义，必须发布新格式ID，不能静默改变v1。

## 当前证据

源码完成时仅Python AST及指定gpt-6-luna只读静态审查；未新增或运行测试、Node、浏览器、HTTP业务查询、真实模型或探针，也未修改正式配置。随后[2026-10-23重载](backend-rollout-20261023.md)已将本批源码装入主适配进程；仅确认启动、端口及数据保留，未实际查询格式目录或做浏览器/业务验收，原QwenPaw插件未重载。旧后端缺格式目录时仍只显示原格式。用户定义扁平字段现已补充，任意嵌套JSON Schema及真实结构输出到原生Sim分支的完整验收仍待完成，不据部署提高AC状态。

## 嵌套结果格式增量（2026-10-23）

新增独立fields-result-v2，支持有界嵌套对象与数组，保留v1契约。条件只接受逐层必填对象路径下的标量，编译、冻结策略、任务发现、结果校验与原回传沿同一契约。详见[structured-fields.md](structured-fields.md)。本轮仅源码/AST，旧业务/浏览器证据不覆盖新格式，不宣称完整任意JSON Schema或功能验收通过。
