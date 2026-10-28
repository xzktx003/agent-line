# 条件实际命中记录

运行图内新增默认折叠、手动读取的“条件命中记录”。目的为查看原生Sim当前可见的条件块决定，不从定义箭头、任务是否存在或默认分支猜测本次走向；不构造另一份执行账本。

## 数据来源与核对

GET /extension/v1/workflows/{binding}/runs/{run}/view/decisions要求原服务权限、准确运行publication pin与原提交记录，以及当前已核验图的graphSha256。后端读取对应历史revision并核deployment ID、规范图hash与解码，按逻辑条件nodeId稳定分页，每次最多20。

固定Sim condition handler输出selectedOption、conditionResult、selectedPath。其原运行GET selectedOutputs只能指定block，无法按结果字段选择，因此适配器临时读取有界块响应，再白名单提取规则ID/布尔结果/目标原生blockId。includeOutput=false，不请求最终工作流输出；要求identity编码、64KiB读取块与1MiB响应上限，拒绝压缩、坏编码、重复JSON键、非有限数或响应身份错误。不记录/返回来源输入、工具正文、selectedTitle、成本或文件；这不等于上游完全不返回这些块内数据。

只有命中规则存在于原条件定义、该branch恰好一条边、原生目标与该边一致、conditionResult类型为Boolean且else=false/非else=true时才matched。matched仅回逻辑节点、原生节点、规则、边、目标、默认标记及布尔结果。没有可见块输出为unobserved，响应元数据不一致为unverified；两者均不代表未执行、跳过或false，权限隐藏/快照未保留也可能没有输出。

这是native_current_block_outputs范围，不是全部历史激活、重试或恢复子运行的完整时间线，也不能证明目标节点已经执行。读取时间仅为适配器观察时间。原图边仍代表定义，不因一个当前回执就染成完整实际路径。

## 界面与生命周期

沿原运行图的授权身份与固定图，手动读取/刷新、按逻辑ID分页，不进入本地自动刷新或原生状态按钮。前端核scope、identity、graphhash、节点类型和原边的source/target/branch；默认命中要求else与false，普通命中要求非else与true。失败清除本记录区的旧摘要，原图保持；图/身份/退出变化清页码并丢弃迟到响应。文字行展示，无新卡片或执行动作。

## 当前证据

根完成三个Python源文件及AST，Luna独立前端、另一Luna只读静态复核。没有新增或执行测试、Node、HTTP、浏览器、模型、lint/typecheck/build。维护重载只证明进程和历史数据/配置保持，不证明真实Sim当前回执、失败/权限隐藏、分页或浏览器功能验收。完整PRD与AC20/22/13保持。
