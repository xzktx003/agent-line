# 人工记忆审核与正式写回

运行服务支持可选 memoryFile。记忆审核身份与普通服务凭证分开配置，允许的项目、
正式角色、来源角色绑定和 QwenPaw 地址均来自私有部署配置。请求正文仅接受
decisionId 与 content，不能选择操作者、项目、角色或上游地址。

页面：/workbench/memories.html。先连接记忆审核身份，选择项目角色及已有来源任务，
展开实际结果和证据，再审核正文。新审核要求任务已终态且资源已确认退出；失败任务
可以用于人工审核失败教训，但执行与验收状态分别保留，不会因此变成成功。
来源结果与证据分别限制为16000/64000字符，截断会明确标识。

## 身份与总账

MemoryReviewLedger 只保存审核元数据：项目、角色、任务、受信审核人、唯一决定ID、
正文SHA-256、来源工作流/执行/验收状态、来源证据指纹和时间。它不是并列的长期
记忆正文库。来源快照取自真实TaskStore，不从浏览器接收，也不会在重试时改写
原审核依据。

审核元数据必须先持久化，之后调用既有 MemoryCoordinator → QwenPawMemoryClient
的官方 workspace memory PUT/GET。原生 QwenPaw 仍负责正式记忆内容。提交正文
在批准与出站总账中保留，作为不可变提交证据；不可把该历史副本当作任意时刻的
最新上游正文或重新恢复被删除内容的来源。

同一角色/任务的审核人、决定、正文和来源不可变。不同请求返回409，不覆盖。
intent/pending 后仅通过读回协调，不盲目二次PUT，不重新执行编码任务。
TaskStore记忆状态从最新提交总账同步，与任务结果/执行身份/证据独立。
同步在任务写锁内读取最新记忆状态，迟到pending快照不能覆盖committed。
已提交审核的重复请求不增加任务版本或事件。

同一角色只能有一个规范绑定；禁止角色/提交账本跨项目共享。不同项目可以在同一
宿主使用不同正式角色、独立工作区与账本。此配置校验不代替原生记忆、文件工具和
Codex自动上下文的端到端隔离验收。

## 配置

首次部署显式初始化 MemoryReviewLedger 和 MemoryCommitStore；正常启动只打开
已有私有0600文件，丢库不自动重建。service.json 增加 memoryFile，指向以下私有配置：

```json
{
  "schemaVersion": 1,
  "reviewLedgerPath": "/absolute/private/memory-reviews.sqlite",
  "credentials": [{
    "token": "替换为独立的长随机凭证",
    "subject": "实际审核用户",
    "projectScopes": ["已登记项目ID"],
    "capabilities": ["review_memory"],
    "expiresAt": 0
  }],
  "bindings": {
    "记忆绑定ID": {
      "projectId": "已登记项目ID",
      "roleBindingId": "已登记角色绑定ID",
      "roleAgentId": "已存在正式QwenPaw角色ID",
      "baseURL": "http://127.0.0.1:实际端口",
      "hostCredentialsFile": "/absolute/private/host-login.json",
      "commitLedgerPath": "/absolute/private/memory-commits.sqlite"
    }
  }
}
```

示例不可直接启动：标识必须符合实际登记及ASCII ID约束，token需至少32字符，
expiresAt需为真实未来Unix时间。启动先完成所有本地校验，再正式登录QwenPaw并
核对角色唯一且backend=qwenpaw；失败清理客户端。关闭服务时释放对应连接。

## 接口

均使用独立记忆审核Bearer身份：

- GET /extension/v1/memories：授权审核记录分页摘要，无正文。
- GET /extension/v1/memories/bindings：授权项目与角色身份，不返回宿主地址、路径或凭据。
- GET /extension/v1/memories/sources?bindingId=...：匹配角色的任务摘要分页；空页仍可能有nextCursor。
- GET /extension/v1/memories/sources/{binding}/{task}：只读任务结果与证据预览，移除资源receipt.workspace。
- GET /extension/v1/memories/{binding}/{task}：原审核、批准内容及写回事件。
- POST /extension/v1/memories/{binding}/{task}/review：人工审核并协调写回，202与status需分开读取。
- POST /extension/v1/memories/{binding}/{task}/reconcile：请求体必须为 `{}`；核对已有提交，200 返回与详情相同的结构，不表示必然已写回。

已有审核处于 `intent/pending` 时，页面提供“核对写回”。服务根据原审核、正文摘要、决定和来源任务定位提交，仅通过官方 GET 核对；不接受新的正文、决定、角色或项目，不重新编码，也不重复 PUT。`approved` 直接保留原状态，不把核对按钮变成首次写入；提交完成、删除和冲突状态不会因此重新写入。原审核依据保持冻结，后续任务验收补充证据不改写原审核来源。

核对仍需独立 `review_memory` 权限，并经过维护和角色记忆锁后的授权检查。缺记录为404，持久正文/身份不符为409，注入字段或缺少空请求体为422，维护为503。读回不明时继续保持 pending；不能把缺文件或超时当作重新 PUT 的依据。重复核对已完成记录不增加审核、任务版本或写回事件。退出仍清空凭证和正文，迟到回执不恢复旧身份页面。

维护/恢复门禁覆盖整个审核、记忆出站与任务状态同步；拒绝为503且不新增审核。
既有HTTP响应未知时页面保留原标识，仅查询。退出清除正文和决定信息，仅留下无正文
的本页防重标识。新身份只能显示服务端授权返回的数据。正文使用textContent或
textarea，凭证仅页内内存，不写入浏览器存储。

## 真实验证与边界

probes/qwenpaw_memory/review_http.py 使用真实已完成角色委派任务
task-00d41e38e770420b8881543dcfa9c903，经过load_app→人工API→不可变审核→
正式QwenPaw记忆写回与独立GET核对，状态committed。重建应用后复用同一审核，
原任务执行/证据不变；普通服务凭证与内容冲突被拒绝。
证据：var/memory-review-http/report.json。没有创建新模型任务。

浏览器11项交互检查通过，包含mock明确分离的新审核/未知投递/XSS/跨身份退出、
以及真实已提交记录的只读展示；真实浏览器没有再次写入记忆。
证据：var/memory-review-http/browser/report.json与桌面/移动截图。
视觉复核首轮72，字体环境、来源证据入口与退出正文残留修复后94/pass，全部
material findings resolved。机械检测器仅regex降级，不宣称完整对比度检测通过。
环境复用已有 FONTCONFIG_FILE=/tmp/agentteams-browser-fonts/fonts.conf 与
LD_LIBRARY_PATH=/tmp/kanban-browser-libs/usr/lib/x86_64-linux-gnu。

主服务已部署记忆接口和页面；历史来源绑定供查看，原维护保护继续阻止新审核。
局域网URL在本机经LAN地址验证，不等于另一台设备已通过。证据
var/memory-review-http/main-service-report.json。398项全量测试通过。

本切片不是完整记忆治理：没有既有正文编辑或候选自动提炼，也未证明本轮写入经
新会话进入实际Codex上下文。后续已增加受控删除API、schema 2删除总账与插件
持久角色阻止，详见[受控记忆删除](memory-deletion.md)。当前上游索引及缓存尚缺
完整清除证明，不能将文件消失标成已删除。后续删除UI已在源码接线，未运行本轮验收或重载后端；相关PRD验收继续保留。

2026-10-22 已补齐真实回执丢失恢复：官方写入一次、确认丢失后 pending，适配服务重启后由真实页面核对为 committed，重复核对无额外 PUT 或编码。主服务已接入原 native 角色与原提交账本；授权范围仅扩展到该已登记项目。见[记忆写回核对证据与边界](memory-reconciliation.md)。
