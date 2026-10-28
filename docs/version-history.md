# 发布版本历史与快照

流程搭建页增加默认折叠的“发布版本历史”。先打开项目流程，再显式读取官方版本列表；按revision选择一项，读取对应deployment ID、当次活动标记、规范图SHA-256及只读authoring图。列表分页不读取各版本正文；选定后才读取一个快照，不自动轮询。

新接口复用原AuthoringClient的官方versions/get_version GET：

- `GET /extension/v1/authoring/projects/{project}/workflows/{workflow}/versions?limit=20&after=...`
- `GET /extension/v1/authoring/projects/{project}/workflows/{workflow}/versions/{revision}`

沿用独立流程编辑身份、项目scope和原author-create所有权，网络读取前后检查授权；不存在或不属于本项目的流程不能读取。响应只投影版本标识/序号/活动布尔，详情经原decode_document/compile_document验证后返回图；不直接暴露不支持的原生block正文。revision严格正整数且不超过JavaScript安全整数，列表核页长、重复revision/deployment及游标边界。图摘要沿用发布目录的规范编译摘要算法，不是宿主进程或执行健康证明。

前端展示节点/连线数量，并对读取详情时本页草稿做节点/连线只读增删改及变量变化摘要。比较使用发起读取时的页面已应用草稿，不含尚未点击“应用节点配置”的表单输入；之后的草稿修改不自动刷新；节点位置/名称/配置变化也会计入修改，不能把差异数字当语义等价证明。完整JSON默认折叠、显示时隐藏当前编辑凭证；无下载/恢复/复制/保存/发布/执行操作，不将脱敏显示作为可还原原文。

切换项目、流程、编辑身份或清空草稿会清除旧历史和详情，内部请求代次丢弃迟到响应。显式分页/刷新清除旧快照；404或不支持版本不能退回另一版本充当所选快照。选中旧版不会修改当前草稿、当前发布或已有运行。

原生公开execute仍只能选活动部署，不能由此按历史版本调用子流程。本功能补版本核对与未来受控展开的读取基础，不等同固定子流程执行、回滚、递归限制或跨调用预算；AC33与完整R17仍未完成。

本轮仅源码、Python AST和Luna静态复核，没有测试、Node、浏览器、HTTP、模型、lint/typecheck或构建。实际版本接口和界面交互尚未行为验收，不以文件存在或后端启动替代验证。
