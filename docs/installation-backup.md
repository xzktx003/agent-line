# 安装级私有备份与隔离恢复

此切片通过 `deploy/backup/installation.py` 备份本项目安装数据与扩展代码，区别于 `agent_line/backup.py` 的单 TaskStore 快照。不会自动停服务，不调用 git pull，不覆盖当前安装，不声称单 DB 即完整升级回退。

## 数据盘点与范围

|数据|处理|
|---|---|
|Sim PostgreSQL|保留数据库进程，在其他应用写入者停机后使用 `pg_dump --format=custom --serializable-deferrable` 一致性逻辑快照；不复制运行中 PGDATA|
|Sim Redis|停止 Redis 后归档完整 `var/sim/redis`，包括 AOF 持久数据；不把 Redis 视为可随意丢弃的缓存|
|Sim uploads|完整归档 `var/sim/uploads`，通过只读容器挂载读取容器 UID 所属目录|
|Sim 密钥/配置|包含 `var/sim` 下环境、验收账户与关联配置；私有权限，不打印|
|QwenPaw|完整 `var/qwenpaw` 与 `var/qwenpaw-secrets`：角色配置、原生记忆/检索、会话、历史、插件持久数据、执行回执与认证秘密|
|Codex|完整 `var/codex-g0` 与 `var/executor/codex-home`，包括身份、会话和 SQLite；仅排除 tmp 中临时启动器和指向外部二进制的符号链接|
|扩展所有数据库|整个 var 下 TaskStore、SimJournal、PauseBindings、memory commit、审批/配置/产物等，不仅备份 tasks.sqlite；保留 SQLite WAL/SHM，恢复后检查每个 SQLite 头识别出的 DB 完整性|
|执行工作区|本项目 var 下所有独立测试/执行工作区均包含|
|扩展部署代码|agent_line、deploy、plugins、frontend 与上游/运行依赖锁文件|

不包含用户原有 Hermes、其他仓库或外部业务工作区，也不打包 Python/Node 环境、Docker 镜像和上游 git 源码；恢复应用前须按锁文件重建对应依赖。PG dump 只覆盖项目数据库，不包含全实例角色/表空间；隔离恢复创建专用角色并以 `--no-owner` 导入。恢复数据与源版本兼容性是后续升级准入的独立关卡。

## 显式静止门禁

必须先由协调者确认没有在途操作，关闭本项目写入服务；不允许以“看起来空闲”替代停机。命令本身不停止任何服务。

- 本项目 adapter、QwenPaw、gateway、Codex/worker 等写入进程停止；监听 3190/3192/3193/3194 必须关闭。
- `agent-line` Compose 项目中除 db 外，所有容器停止，包含 Sim 应用/realtime/cron/Redis；独立 executor 容器停止。
- 项目 PG 保持运行供 dump，但不允许其他数据库客户端连接。
- 主 TaskStore 所有任务必须终态并已释放资源；存在 uncertain 或未释放任务时拒绝。
- 提供 10 分钟内的私有 attestation，根路径精确匹配，明确 `confirmedNoExternalWriters=true`。管理员仍须排除未受当前进程/容器检测覆盖的外部写入者，不能仅依赖扫描证明全局静止。
- dump 和归档前后各检查一次门禁；最终 manifest 只有检查通过后才产生。失败的未完成目录不得视为有效备份。

attestation 格式（0700 私有目录中的 0600 文件）：

```json
{"root":"/absolute/path/to/agent_line","confirmedNoExternalWriters":true,"issuedAt":0}
```

issuedAt 必须为实际完成协调后的当前 Unix 时间，不能预填或只为绕过门禁修改。

## 创建、校验与恢复

```bash
.venv/bin/python deploy/backup/installation.py create --bundle /private/new-bundle --attestation /private/quiesce.json
.venv/bin/python deploy/backup/installation.py validate --bundle /private/new-bundle
.venv/bin/python deploy/backup/installation.py restore --bundle /private/new-bundle --target /private/new-restored-installation
.venv/bin/python deploy/backup/installation.py restore-postgres --bundle /private/new-bundle --target /private/new-restored-installation
```

bundle 必须位于当前安装之外。包含私有 installation.tar、sim-postgres.dump 和 manifest；manifest 有整体文件 hash/大小以及归档中每个普通文件的 SHA256/大小。hash 用于意外损坏和内容一致性检查，不是攻击者可同时修改 bundle/manifest 情况下的数字签名。

恢复前验证所有 hash 和成员清单，拒绝绝对/父目录路径、重复成员、符号/硬链接、设备等特殊文件。目标必须不存在且不得与当前安装交叠。目录 0700、普通文件 0600；脚本如需执行应使用解释器调用并在批准的部署过程恢复必要可执行权限。

恢复后检查 SQLite integrity_check，并给 TaskStore 写入 `installation_restore_requires_external_reconciliation` 准入保护。创建 `RESTORE-DO-NOT-START.json`，不启动 QwenPaw、Sim 应用或 worker。历史 native identity 不可重放；配置中的绝对路径须显式重新绑定到隔离目标，不能直接启动指向原工作区的配置。

PG 恢复使用新名称容器、新数据目录、`--network=none`，不发布端口、不连接原数据库；仅本地 Unix socket 导入。快照前后逐个 public 表精确计数，并在隔离恢复后完全比对表名/行数。验证结束移除容器，保留新目录与报告。该隔离临时实例使用 trust 认证仅因无网络且无应用接入，不能将其配置用于生产。原安装不变。

## 验证状态

`probes/backup/test_installation.py` 的 10 项测试通过：静止声明时效、存活进程拒绝、全文件 hash、成员清单、防路径逃逸/链接、不可覆盖已有目录、源目录保护、私有权限及准入关闭、PG 恢复禁止指向原目录、逐表行数不匹配拒绝宣称成功。fixture PG 文件仅用于 hash 测试，不冒充真实 PostgreSQL 恢复。

本轮已在协调静止窗口完成真实安装快照及隔离 PG 恢复，证据见下文；这不代表恢复后的整套应用已验证。单个数据库通过不能替代角色/记忆/会话、Sim/Redis/uploads 和真实运行映射的整体验证。完整升级自动切换、外部业务工作区备份和跨版本恢复不在本切片内。

## 本轮真实证据（2026-10-21）

协调者确认并实际停止本项目所有已识别宿主 writer 和独立容器；本工具再次通过门禁。历史 `resourcesReleased` 标记有已知失真，所以此次一致性不只依赖该字段，而以实际进程退出、应用监听关闭、应用容器停止和 PG 客户端清空为前提。

- 备份目录：`/data01/home/zukang.xu/workspace/ai_agent/agentteams/agent-line-installation-backup-20261021`
- 清单：该目录 `manifest.json`，包含 3,435 个归档成员，2,668 个普通文件；`installation.tar` 为 1,058,232,320 字节，`sim-postgres.dump` 为 823,611 字节。
- 隔离恢复目录：`/data01/home/zukang.xu/workspace/ai_agent/agentteams/agent-line-installation-restore-20261021`
- PG 报告：恢复目录 `postgres-restore-report.json`，实际完整导入 180 张 public 表，逐表精确行数与备份时一致；network none、没有发布端口、验证容器已移除。
- 文件/SQLite 报告：恢复目录 `restore-data-report.json`；`probes/backup/verify_restored.py` 实际核对 2,668 个普通文件，对 25 个 SQLite 存储分别从归档提取 DB/WAL 后核对完整性和逐表行数，全部匹配。预期差异仅包括主 TaskStore 的准入保护，以及 SQLite 打开/关闭产生的 WAL checkpoint 和 SHM 变化。
- 恢复目录存在 `RESTORE-DO-NOT-START.json`；没有启动恢复侧 Sim/QwenPaw/worker/gateway，Redis 仅完成持久文件原样恢复，未做 Redis 启动验收。

恢复暴露了真实兼容问题：Sim 的 `embedding_projection_mark_update` trigger 通过 `IS DISTINCT FROM` 使用 public.vector 等号运算符，而 pg_restore 默认清空 search_path，直接导入失败。修复仅在隔离导入的私有 SQL 中将恢复会话 search_path 设为 public；保持原 custom dump 不变，保留全部 schema 对象，随后以 ON_ERROR_STOP 和单事务导入成功。失败实例已移除，失败数据目录/私有诊断保留。该版本特定恢复适配集中在备份工具，没有修改 Sim 源码或删去 trigger。

本快照早于该恢复工具修复；恢复时应使用当前已验证的 `deploy/backup/installation.py`，不能假设归档内较早版本恢复工具已包含修复。原数据/代码快照仍如实保留创建时状态。

私有安装灾备包包含认证材料，只供当前机器的受限运维恢复；不是面向用户分享的导出包。不得上传、公开或作为脱敏业务导出。PRD 的“不导出服务凭证”仍适用于用户可下载/共享的导出功能；若交付该功能，需另行实现去凭据的导出路径与验收。

## 接入标准升级操作

1. 在当前和候选上游 SHA、插件/适配器、Codex、数据库 schema 版本之间准备兼容矩阵；使用 `agent_line/upgrade.py` 验证候选 SHA 绑定的必需探针证据。空探针、unknown 或不匹配候选证据必须拒绝升级。
2. 记录安装身份、任务/审批/原生执行身份与时间水位，冻结新准入；先核对实际进程、容器和租约，不能仅凭数据库终态推断所有后台执行已经停止。
3. 协调停写窗口并运行 create。任何门禁失败、未完成 manifest 或 hash 错误都不允许进入正式升级。
4. 验证 bundle，并恢复至独立新目录。运行隔离 PG 导入、文件/SQLite 数据审计；保持恢复环境无自动触发和执行准入。
5. 在新环境重新绑定目录/端口、重建锁定依赖与镜像、验证 Redis、角色 ID/记忆/会话、Sim 模板/版本及历史等应用语义。不要让还含原路径的恢复配置写入原项目。此步骤本轮尚未完成。
6. 用候选版本在独立数据副本上执行真实兼容探针，包括记忆、审批、Codex 身份/取消及工作流恢复；迁移可逆性必须有证据。此处才是跨版本验证，不能用本轮同版本离线恢复代替。
7. 全部升级准入条件通过后才执行正式切换；先只读核验历史，再经维护策略恢复新任务准入，记录实际版本和证据。
8. 如果升级失败，保留升级后数据和代码变化，在另一个新目录恢复升级前完整数据集合；不要仅把旧镜像接到迁移后的数据库。先核对备份水位后所有原生执行和工作区差异，未知项目继续阻塞。
9. 用户业务代码不随数据库恢复自动覆盖；恢复旧库也不重放任务或审批。完成核对后再决定是否切换恢复环境，保留审计与回滚记录。

当前仅证明本项目静止快照、文件/SQLite 离线恢复以及独立同镜像 PG 导入；没有证明跨版本无障碍升级、恢复后的整套应用启用、自动切换/回退或备份后新执行差异的完整故障场景。相关 AC 保持 in_progress。
