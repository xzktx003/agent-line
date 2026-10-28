# Sim 中文搭建公开 API 实测

本轮仅创建和修改隔离工作区的新流程 `agent-line-authoring-probe`。未修改上游、调用模型/角色/记忆或解除主服务准入限制。本验收使用公开 API，不代表浏览器验收。

## 真实结果

私有证据在 `var/sim-authoring-probe/`：report.json、state.json、compiled.json、readback.json、published.json、versions.json 均为 0600；凭据仅从现有私有配置读取，不写报告。

- workflowId：`be72657a-a0f3-4f86-8ab7-8c86e39059ba`
- 验证版本：2；deploymentVersionId：`f17377a0-69e3-4bd7-810f-6d5d318c66ef`
- create → save_state → get_state → decode_document → deploy → versions → get_version 全部成功；草稿和确切发布图均还原中文文档及位置。
- 第二次独立进程运行通过，`writesThisInvocation=0`，没有重新创建、保存、发布或执行。

| 输入 input | 原生 runId | 原生结果 |
|---|---|---|
| `{"answer":true}` | `f083d563-363b-4d56-a00b-35cfa7f03223` | completed，output.data.branch=yes |
| `{"answer":false}` | `779d010f-8f8d-41a2-b72f-7bd0c255301d` | completed，output.data.branch=no |
| `{}` | `b13a300c-ba3e-4657-a567-d2ad12ddf919` | failed，condition_field_missing |
| `{"answer":"true"}` | `09c7d0b6-b582-40ab-a372-a5754154c4b1` | failed，condition_type_mismatch |

首次版本发现编译错误：Sim 引用按节点名称查找，不按块 ID。原生 run `628d42b9-16a5-45de-9d10-a5b84b91273e` 失败且未重投。修复后在同一探针流程发布版本 2，四种输入用新 runId；旧记录保留。源码依据为 `executor/variables/resolvers/block.ts` 的 findBlockIdByName。现在使用例如 `<开始.input>`；名称只接受 Unicode word 字符及中文，拒绝大小写规范化后重复名称和引用语法字符。

## 入口和重复执行边界

仓库根目录运行 `.venv/bin/python probes/sim/authoring_probe.py`。脚本先认证读取列表，所有写操作意图和 runId 在发送前 fsync 持久化。已有回执复用；未知 create/save/deploy 拒绝重发，未知 execute 仅观察原 run。它是单操作者探针，没有多进程锁，不应并行启动。

## 适配接口

`SimAuthoringClient(sim_client, workspace_id)` 复用连接和 key，工作区固定。`list_workflows(cursor=None,limit=50)`、`versions(wid,cursor=None,limit=50)` 返回 `{data:[...],nextCursor}`；`create(name,description=None)`、`get_detail(wid)`、`get_state(wid)`、`save_state(wid,state,dry_run=False)`、`deploy(wid)`、`get_version(wid,revision)` 返回解包 data。

get_version 使用正式 `/api/v2/workflows/{wid}/versions/{正整数}`，先核对详情工作区，再核对版本。版本响应没有 workflowId，作用域依靠详情检查和固定路径。真实 deploy 回执使用 **activeDeployment.deploymentVersionId**，不是 activeDeployment.id；版本响应顶层 id 与之相同。

异常 AuthoringError.code 是固定诊断码；AuthoringUncertain 表示写入结果不明；UnsupportedDocument 表示无法安全投影。错误不回显原始响应。

## 有界归一化

反解仅移除本轮真实回读和固定源码支持的默认值，再严格重编译比对其余全部内容：

- block：horizontalHandles=true、height=0、advancedMode=false、errorEnabled=false、triggerMode=false、data={}、locked=false。
- edge：type="default"、data={}。
- 发布图有限数值 lastSaved 时间戳，以及空 loops/parallels。

源码依据：`lib/workflows/persistence/prepare-state.ts` 默认值、`lib/workflows/persistence/utils.test.ts` 持久读形状、`lib/workflows/persistence/utils.ts` lastSaved、`lib/api/contracts/deployments.ts` 发布图 schema。非默认值、未知字段/节点、表达式改写、非空容器均拒绝，不静默丢弃。单测验证默认值通过而 errorEnabled=true 拒绝。

## 条件和限制

固定模板 IIFE 对路径逐段 own-property 检查，严格类型/有限数值校验，再使用有限比较操作。字符串中的引用/环境模板字符编码为 Unicode 转义，不能插入上游引用。pinned `executor/handlers/condition/handler.ts` 将 expression-threw 转成运行失败；真实缺字段和错类型已验证不会进入 else。

非空 variables、Agent、loop、parallel 不支持。Human requiredBoolean 仅映射原生 inputFormat boolean，不创建审批绑定或宣称原生强制必填；本轮未执行 Human。发布检查不是 CAS，不能消除其他编辑器并发改图，上层必须比较确切发布图并在不一致时返回 publication_mismatch；不能声称 execute 固定版本。真实执行仅覆盖上表，其他有限比较操作未逐一真实运行。16 项编译/MockTransport 测试和定向 flake8 通过。
