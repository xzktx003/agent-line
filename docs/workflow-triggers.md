# 通用触发入口

在现有适配服务增加可选 Webhook 和分钟事件入口，复用手动启动使用的 `WorkflowGateway.start()`。QwenPaw 继续管理 Agent、会话和原生记忆，Sim 继续执行图；此模块不运行计时循环、不推进节点、不增加服务、依赖、数据库表或记忆引擎。Jira 专用集成不在范围。

当前已完成事件接线、真实 Sim 零模型重放，以及复用既有 cron 的实际时钟投递验证。主环境仍未配置 `triggerFile` 或启用可选 cron 挂载，外部业务 Webhook 发送方和配置界面尚未接通；不能将隔离验证视为正式定时任务已部署。原准入和维护守卫保持。

## 可选配置

适配服务配置保持 schemaVersion=1，按需增加：

```json
{"triggerFile": "triggers.json"}
```

该文件必须是当前用户所有、0600 的普通文件，禁止符号链接；正常启动只读取，不初始化数据。内容例如：

```json
{
  "schemaVersion": 1,
  "triggers": {
    "incoming-event-v1": {
      "projectId": "your-project",
      "bindingId": "published-YOUR_EXISTING_PUBLISHED_BINDING",
      "type": "webhook"
    },
    "daily-check-v1": {
      "projectId": "your-project",
      "bindingId": "published-YOUR_EXISTING_PUBLISHED_BINDING",
      "type": "schedule"
    }
  }
}
```

绑定必须来自原可信发布目录，固定项目、Sim workflow 与发布版本；事件正文不能选择项目、角色、绑定或版本。旧发布不再活动时拒绝新事件，不静默采用最新定义；旧已受理事件仍查询原运行。

触发器 ID 是持久定义身份。首次接收在原 SimJournal.operations/schema1 冻结 `{projectId,bindingId,type}`；同 ID 改绑或改类型返回409，包括应用重启后。若需要新的定义，显式使用新 ID。相同旧定义只读核对，维护期间不会为了重放新增元数据；首次冻结和新运行均遵守原准入。

## 请求与身份

两入口均使用现有服务 Bearer 认证和项目授权，返回原工作流启动回执；202 不代表完成。调用方自行保存稳定事件身份。

| 入口 | 正文 |
| --- | --- |
| `POST /extension/v1/triggers/{id}/webhook` | `{"eventId":"source-event-123","input":{"value":1}}` |
| `POST /extension/v1/triggers/{id}/schedule` | `{"slot":"2026-10-22T06:00:00Z","input":{"value":1}}` |

Webhook 的 eventId 不能为空或包含控制字符。定时 slot 只能是明确 UTC 分钟、秒为00，必须来自唯一指定调度者的原发生时间，不能根据接收时间补造。入口只验证事件，不自行判断时间或启动定时器。

trigger ID、类型和 eventId/slot 共同产生有界、域隔离的防重键。业务 input 原样进入原持久日志：相同键/输入返回原运行；相同键改输入409；未知受理保持原意图，不自动重发。原发布冻结、任务/审批预留、自动验收要求及维护检查全部通过共享启动路径执行。

## 当前真实证据

`probes/sim/triggers_live.py --execute` 已创建并发布独立 Start→End 流程，验证一个 Webhook 事件、两个明确分钟槽，实际只有3次原生执行。重复 Webhook 和同槽无新增执行，改输入409；重建 API、TaskStore 和 SimJournal 后重放三个事件，仍无新执行。

根代理另读实际 Sim 运行列表、原输出/日志部署ID和原 SQLite：三个原运行全部 completed、End marker 一致、发布版本一致、两触发定义冻结；任务表和租约均0，未调用 QwenPaw、Codex 或记忆。原生修改总计6次（创建/保存/发布各1，执行3）；默认重复实测0原生写。现场 `var/sim-triggers-live/{report,root-check,observation}.json`。

```bash
# 无现场时不加开关只显示未执行；有现场后即使加开关也只读原身份
.venv/bin/python probes/sim/triggers_live.py
```

24项入口测试、7项工厂/私有配置测试、9项探针契约测试覆盖重放、并发、输入冲突、改绑、维护、未知恢复、发布漂移和项目隔离。结合下述实际时钟与独立审计，AC-34 已通过；AC-35、完整 R-18 尚未通过。

## 可选 cron 部署准备与当前限制

`deploy/sim/trigger_cron.py` 为现有 Sim cron 准备私有发送脚本和合并后的 crontab，保留官方维护作业。它只生成文件，不启动定时器。发送脚本从私有配置读取凭证，命令行不携带令牌；同一分钟的事件仍通过原入口去重，不另建调度器。

配置文件及 token 文件须为当前用户所有的0600普通文件，例如：

```json
{
  "schemaVersion": 1,
  "adapterBaseUrl": "http://host.docker.internal:3193",
  "tokenFile": "token",
  "jobs": [{"triggerId": "daily-check-v1", "cron": "0 9 * * *", "input": {}}]
}
```

```bash
# 仅准备文件；目标目录必须不存在。该 HTTP 主机仅在明确可信的本机网络使用。
.venv/bin/python deploy/sim/trigger_cron.py --config /absolute/private/cron.json \
  --output /absolute/private/prepared-cron --trusted-http-host host.docker.internal
# 仅验证 Compose 配置，不更新任何服务；实际启用仍需已有可信 triggerFile/发布绑定。
AGENT_LINE_SIM_TRIGGER_CRON_DIR=/absolute/private/prepared-cron ./deploy/sim/run.sh config --quiet
```

显式设置目录时，原包装脚本先核验清单、原始官方作业、文件摘要、属主和权限，再加载 `triggers.compose.yaml`，只修改原 cron 服务。私有目录只读挂载；cron 使用准备文件的当前用户 UID/GID，避免主机与镜像用户不同导致0600文件不可读。未设置该变量时仍使用原部署。实际固定镜像的无网络、只读临时容器已验证凭证文件可读、发送脚本可执行和 `supercronic -test` 成功，未执行作业，检查容器已删除。

cron 表达式遵循现有容器时区；事件 slot 来自发送脚本启动时唯一一次读取的 UTC 分钟。若调度延迟跨分钟，slot 也随之变化；当前不提供补跑、每个预期时刻恰好投递或自动重试保证。`onceSlot` 仅用于限定探针的一个明确 UTC 分钟，不冒充完整 occurrence 调度能力。

真实时钟探针 `probes/sim/cron_delivery_live.py` 首次尝试在复制后的权限核验中停止：镜像用户1001无法读取主机用户持有的私有目录。原 crontab 从未替换、未发 reload、无投递及原生执行。根核对同一容器、原字节、主配置和停止的监听器后，按精确清单删除临时目录与本地凭证，现场保留在 `var/sim-cron-delivery/root-reconciliation.json`。随后修复复制后先核对内容、再显式调整容器文件属主及恢复原元数据的路径，14项回归通过。旧现场即使加 `--execute` 也只观察，不能静默重装或换事件身份重投。

## 实际时钟投递与独立验收

确认前次零执行且已清理后，显式启动新的权限修复验证，前置证据保存在 `var/sim-cron-delivery-uid-fixed-preflight.json`；没有重投未知任务。原 Sim cron 容器在 **2026-10-21 23:18:00 UTC**（北京时间次日07:18）实际投递，首请求在该分钟后0.073秒到达。固定 onceSlot 仅允许这一个分钟，重复事件仍走同一原工作流入口。

- 同分钟首次与重放请求均202；确定性 runId 为 `8a8bc5b8-e59c-5d8a-9312-ae80609e277d`，只有一次原生 execute，已确认202。
- 原工作流 `a7bc8c38-d612-4262-80e8-fe19ecb4632d` 的发布 revision1、部署版本、End marker 和实际 completed 一致；原 journal 的触发定义、业务输入、运行身份及发布冻结均匹配。
- 十一项安装和恢复操作全部有回执。原官方 crontab 逐字节恢复，属主和权限为 `0:0:644`，两个重新读取 crontab 的日志已观察；容器身份未变。私有目录及凭证删除、临时HTTP监听停止，任务和租约为0，主配置与守卫保持。
- 根代理直接重查原 Sim run/log、SQLite、当前容器文件与监听器，另由独立审查者核对事件身份、持久定义、源码和历史Webhook证据。对应 `var/sim-cron-delivery-uid-fixed/{report,root-check,acceptance-review-AC34}.json`。

这个验证没有调用 Agent、Codex 或记忆，也未修改 QwenPaw。重复查询只观察原身份：

```python
from probes.sim.cron_delivery_live import ROOT, run_probe
result = run_probe(directory=ROOT / "var/sim-cron-delivery-uid-fixed")
assert result["nativeWritesThisInvocation"] == 0
```

该结果满足 AC-34 的事件去重验收，未完成整个 R-18、角色/工作区等待策略或定时配置界面。原生历史入口的三个运行与本次新增的一个定时运行分别保留，不改写较早的历史审计结论。

## 后续接线

继续复用现有 Sim cron 容器作为唯一计时来源，保留其官方维护任务，不为同一流程再建 QwenPaw 定时任务。固定上游 Schedule block 不输出可信 occurrence 身份，不能编造 `<schedule.scheduledFor>`；正式定时配置须在发布绑定、凭据及维护状态核对后显式启用。工作区排队复用原租约，显式角色并发策略和等待原因界面仍需补齐。
