# QwenPaw 升级与回退应用语义探针

`probes/upgrade/qwenpaw_semantics.py` 使用本地已核验的两个官方提交：旧版
`28f2b9f624d4917268f964cffc8c3cba519f8b87`、新版
`502b59d88464d5c9e8be4a507347282d6a54e0be`。两者均为 2.3.0b1。
正文继续由 QwenPaw 原生记忆管理，扩展只使用官方 HTTP 和插件入口，不添加记忆后端。

执行入口：

```sh
.venv/bin/python -m unittest tests.test_upgrade_semantics_probe -v
.venv/bin/python probes/upgrade/qwenpaw_semantics.py --attempt native-semantics-2
```

每个 attempt 都位于私有 `var/upgrade-runtime/<attempt>`。存在该目录时，命令只读取
原报告，不重启宿主、不发送 Sim 或 QwenPaw 写请求。每个首次写请求之前持久化 intent；
未知回执保留原身份，不自动重试。测试失败的 attempt 不能清空后重新使用。

## 实际覆盖路径

1. 用现有 Runtime 启动独立旧版工作目录和秘密目录，安装当前扩展插件。
2. 通过官方认证接口创建固定测试角色、写入一条原生记忆；核对角色 ID、名称、后端、
   描述及记忆 hash。未认证插件入口应为 401，登录后为 200。
3. 在本测试安装中显式建立 TaskStore、SimJournal 和独立编辑凭证。当前 AuthoringAPI
   经 TestClient 驱动真实 Sim 创建、保存、发布唯一专属 Start→Condition→End 图，
   不是调用替身，也不在本地执行图。
4. 通过 SimJournal 记录原运行意图，实际运行成功分支，核验原 run 已 completed 和输出。
   保存发布 ID、revision、bindingId、完整受支持文档、catalog 绑定和历史回执。
5. 停止测试 QwenPaw，关闭 HTTP 客户端，对整个测试 installation 做离线目录备份；
   包含原生配置/记忆/认证、已安装插件和本切片扩展账本。
6. 在同一路径启动新版。核验角色、官方记忆、认证、已安装前端包与当前源码 hash、
   同一模板/发布版本/历史，以及从原账本重新构造的 catalog 绑定。
7. 停止新版并保留升级后目录，将旧备份恢复到原测试路径，再启动旧版重复上述核验。
   原路径恢复保留了角色配置中的绝对工作区引用，不把旧代码接入新版修改后的数据。
8. 退出全部测试宿主，保留报告、备份、升级后目录与原 Sim 专属资源供复核。

## 明确边界

Sim 使用现有固定部署、同一 workspace 和 API key；只创建本探针专属图和运行，
不改已有图。Sim 数据不包含在本次测试备份内，恢复旧 QwenPaw/扩展快照后仍指向
**同一未回滚的 Sim 历史**。因此证明的是 QwenPaw 升级期间模板和历史仍可访问、
本地绑定和原 journal 保留，不是整套 Sim/PostgreSQL/Redis 回滚。

本切片选用无模型条件图，没有人工审批节点，所以不建立空审批账本冒充审批兼容。
不调用 Codex、不验证模型 memory_search 召回或委派、不补齐九项兼容探针，
不切换正式安装，也不单独将 AC-23 标为通过。运行复用既有依赖，直接依赖检查由
Runtime 执行；不宣称重建完整传递依赖环境。

首次 `native-semantics-1` 保留失败现场：条件输入缺少 Start 要求的 input 包装，
原生运行明确 failed；另外草稿 GET 会重排节点，原始列表相等断言过严。修正为
规范编译图比对和正确 input 包装后，使用新 attempt，不重投旧运行。

## 本次真实结果

`var/upgrade-runtime/native-semantics-2/report.json` 的 `phase=complete`、
`verified=true`，含旧版实际源码来源、新版来源及恢复旧版来源。

- workflowId：`00ca2f47-3ba3-4f8a-b1bf-932e63d29e19`
- 原 runId：`df33b358-e7cd-479e-9d2f-ad8b09cee114`，completed，输出 accepted。
- revision：1；deploymentVersionId：`776f7be9-6a40-4223-8580-a45de5b061db`。
- bindingId：`published-61558ff76db9d844e6694842d482e253c340773ef195cbb3`。
- 已核验角色字段：ID、name、backend、description；记忆正文一致，报告保存 hash。
  不将这四项字段比对扩大为所有未来角色配置字段都已兼容。
- 已安装插件 `dist/index.js` 在三个阶段均与当前源码 SHA256 匹配。
- 同一原生模板、发布身份、完成历史、catalog 绑定和 accepted journal 回执在三个阶段一致。
- `offline-backup` 是升级前测试安装；`post-upgrade-installation` 保留升级后测试安装；
  `installation` 为恢复的旧版副本。所有路径均在本 attempt 下。
- exec 6884 正常退出 0；已根据两侧 process.json 的 PID/startTicks 检查进程不存在。

5 项 unittest 通过，覆盖支持图编译、丢回执保留 intent 禁重投、已收回执禁重投、
已有 attempt 只读返回和安装前端包不匹配拒绝。py_compile 和限定文件 diff 检查通过。
这些测试辅助验证探针，不替代上述真实 HTTP 证据。

根代理另行只读检查 `offline-backup`、`post-upgrade-installation` 和恢复后的 `installation`：实际角色、原生记忆 hash、安装 JS hash、原 journal 回执均一致，所有测试 PID/startTicks 已退出；再用真实 Sim GET 核对原模板、发布版本和已完成运行。复核记录为 `var/upgrade-runtime/native-semantics-2/root-check.json`，原生写入数0。
