# 人工确认事项与原运行上下文

流程搭建的人工确认节点可填写可选“确认事项”，例如“请核对本次产物和独立验收后，决定是否进入发布准备”。最多2000个Unicode码点、无NUL，保存时去除首尾空白；空文本不增加question字段。文字仅描述业务问题，不授予工具权限，也不替代执行证据或独立测试。

## 版本与持久化

可选question与节点配置一起进入原AL_HUMAN_V1 marker；旧无question图不改变表示，已有TTL-only图保持原marker内容。发布humanApproval.questionsByBlock只允许正式human block，原发布目录按同一白名单核验。纯人审与混合Agent运行分别沿原reserve/reserve_tasks冻结approvalQuestions，重放核验保护原映射。没有新表、审批binding字段迁移或独立记忆库。

审批详情从该approval对应的原运行快照读取，核项目、workflow、run、revision、purpose与human block；不会从当前画布、新版本或节点名称补造旧问题。configured带文字，unconfigured说明原运行未设置，unavailable表示缺少发现读取能力，unknown表示账本/身份/格式无法核对。缺失或错误时不伪造默认问题；不会因此改变原审批授权、有效期或决定防重语义。

问题正文仅出现在原授权审批详情/决定回执，不进入列表、匿名首页、URL或日志。复用详情原查询，不额外请求Sim/QwenPaw。界面以脱敏的textContent显示，在原批准/拒绝确认框也显示相同内容，无HTML执行；原决定/过期/激活/attempt保护继续由后端负责。

## 编辑与兼容

humanApprovalQuestionSupported明确true才开放编辑。旧后台无能力时，无question旧节点仍可使用原路径；已存在question的节点保留并拒绝应用/保存/发布。无效原内容不得通过表单自动清空，必须用户明确修改或清空；未知human配置保留且阻止覆盖。退出、切项目、非human节点及重新读取角色目录均沿原生命周期清理/禁用，未应用修改遵守原离开确认。

标记最大32768字符，覆盖2000个补充平面字符经过ensure_ascii/base64的最坏长度，解码仍经严格human config与原完整图重编译核验，不私读或改写上游。

## 当前证据

Luna实现三个前端文件，根集成五后端文件，五Python AST及另一Luna只读静态复核。未新增或执行测试、Node、浏览器、HTTP、模型、lint/typecheck/build。维护重载仅证明进程和原数据/配置保持，不证明真实新审批、旧运行回读、重启或界面已验收。完整PRD与AC20/22/13保持。
