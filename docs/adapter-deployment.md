# 适配服务启动

使用项目 `.venv`，不会改写上游代码。首次创建数据库需显式执行：

```sh
.venv/bin/python -m agent_line.runtime init --database /absolute/private/tasks.sqlite
.venv/bin/python -m agent_line.runtime serve --config /absolute/private/service.json --host 127.0.0.1 --port 3193
```

配置必须由当前用户持有且权限为 `0600`。格式：

```json
{
  "schemaVersion": 1,
  "database": "tasks.sqlite",
  "tokens": {"REPLACE_WITH_RANDOM_SERVICE_TOKEN": ["project-id"]},
  "projects": {"project-id": {"workspace": "/absolute/existing/workspace"}},
  "bindings": {"binding-id": {"projectId": "project-id", "roleAgentId": "native-role-id"}}
}
```

数据库相对路径以配置文件目录为基准。缺失数据库不会自动重建；缺失项目、无效绑定或可被其他用户读取的配置导致启动失败。令牌不要放在命令行参数或 URL 中。

新初始化的任务库是结构版本 5，含 `recovery_records`、事件契约字段和事件类别。版本 2、3、4 仍可打开。版本 2 要先显式迁移到 3 才能写恢复记录，再显式迁移到 4 才能写事件来源、运行和载荷，再显式迁移到 5 才写入类别。三次迁移都不改任务版本、状态，也不注册新尝试。相同来源和事件 ID 的重复事件只保留第一笔，载荷不能改写终态。发生时间更旧的终态不会成为最新终态。正式库 `var/adapter/tasks.sqlite` 本轮没有迁移。带裁决的核对接口也拒绝在版本 2 上写入，而且不会先去核对执行。备份清单记录库里的实际版本；版本 2、3、4 和 5 都能恢复到新文件，版本 1 或其他未知版本仍拒绝。

`GET /extension/v1/health` 必须携带 Bearer 服务令牌，返回存储状态和执行能力状态。HTTP 200 只证明服务与存储可用；应同时检查 `readyForExecution`。
未配置可信验收证据时，CLI 默认执行能力为 unknown。可选 `readinessFile` 接入当前部署的真实执行证据；仍禁止通过配置布尔值直接打开任务执行。它确认能力，不解除维护或恢复守卫。

可在同一服务配置增加 `verification.evidenceDirectory`，把受信文件字节策略接入已有后台线程；策略位于对应 `bindings`，配置与限制见 [后台自动验收](automatic-verification.md)。该配置不打开受控执行准入，也不提供测试命令执行能力。

本机隔离验证实例：`http://127.0.0.1:3193`，配置与数据库在忽略目录 `var/adapter/`，验证结果在 `var/adapter/health.json`。当前无正式角色绑定，仅验证认证与服务启动。真实请求已验证匿名 401、带凭证 200、readyForExecution=false。这不是 LAN 产品入口，LAN 网关仍待实现。

## 工作流入口

### 受控执行的证据准入

使用现有 `executionFile` 与自动验收 `verification` 配置时，可增加 `readinessFile: "readiness.json"`。该私有0600文件由 `agent_line.execution_readiness.prepare_readiness` 对当前服务任务库的已完成隔离验收任务生成；不接受模型输出或手工填写的 passed/confirmed。证明覆盖全部配置的执行绑定，每项仅引用 projectId、roleBindingId、taskId，不能自由指定另一套成功样例数据库。

校验使用当前配置的任务库、父投递账本、QwenPaw 原执行回执/角色绑定、独立产物和测试文件、容器与启动器退出、Runtime.stop 收尾。角色修订、策略、工作区、实际执行身份和原始证据须一致；按策略需要测试时检查真实命令退出及独立记录，不能用模型“测试通过”声明替代。

证明同时固定两个原版上游、扩展与执行器源码、实际注册二进制、当前本地 QwenPaw 进程和已安装插件。执行任务须晚于该宿主启动及执行相关文件版本。仅修改校验器 `execution_readiness.py` 时，旧证明仍因摘要变化失效，但允许新算法只读重核原任务；其他执行源码、配置、二进制、插件变更仍要求相应新验收证据。正常775二进制按注册路径、内容摘要及可读可执行事实核对，不修改共享文件权限，也不承诺约束其他已具主机写权限的用户。

启动时证据缺失或失效保持 unknown，并在认证健康接口返回固定中文 `executionReadinessReason`。健康查询、HTTP新任务、后台发现及新执行预检会核对当前证明；源码、执行环境、角色或证明变化时停止新执行。原任务历史、取消和原身份观察继续沿原接口；不会释放未知租约或重新编码。读取使用SQLite的ro/query_only一致事务，不迁移、不改业务行、不主动checkpoint；WAL读者可能由SQLite维护自身sidecar，不将其冒称磁盘绝对零写入。

真实验证在 `var/execution-readiness-native/`：同一个真实Codex任务完成独立测试后，正常 `load_app` 从无证明unknown/503变为confirmed/ready；随后在**候选库**加维护锁，能力仍confirmed、ready=false，新POST503且历史200。主服务配置及原守卫保持，未新启动候选常驻服务。初次校验因新增的二进制权限限制失败，原state保留；修正后对同原任务只读核验并生成证明，没有重复模型任务。见 `root-runtime-confirmation.json`。

配置可选 `sim` 对象，字段为 `authFile`（0600、含 baseUrl/apiKey 的 JSON）、
`journal`（已经显式初始化的 SimJournal 数据库路径）、`installationId`（固定安装标识，不随重启更换）、
`bindings`（绑定名映射到 projectId/workflowId）。路径相对服务配置目录。
同一个原生工作流不能跨项目绑定，避免读取其他项目运行。

- POST `/extension/v1/workflows/{binding}/runs`：`{idempotencyKey,input}`。
- GET `/extension/v1/workflows/{binding}/runs/{runId}`：读取登记状态及 Sim 执行结果。
- POST `/extension/v1/workflows/{binding}/runs/{runId}/resume`：`{contextId,input}`。

三者均使用服务 Bearer 令牌及项目范围；恢复只接受本安装登记的运行。
启动和恢复先提交持久化日志，再调用 Sim。重复请求返回原记录，输入改变返回409。
202 是提交状态回执，须查询原生 execution.status 和 output 判定结果。

真实验证证据 `var/adapter/workflow-live.json`：两个条件分支经正式 HTTP 入口启动，
重复请求和适配服务重启后保留原 run ID，精确输出分别为 yes/no。
实际发现 Sim 查询默认不带输出，已显式传 includeOutput=true 并用原 run 验证，未重投。
这证明控制流程入口，不证明 QwenPaw/Codex 节点或产品全部验收。

## 中文工作台入口

启动参数加 `--host 0.0.0.0 --port 3193` 可监听局域网。本机当前入口：
`http://10.30.0.28:3193/workbench/`。静态页面公开加载，业务 API 仍需服务令牌；
工作流列表只返回令牌有权访问的绑定与项目，不暴露原生配置。
CSP 限制脚本、样式和接口到同源，静态挂载不允许读取父目录。
本机已用局域网地址验证 HTTP 与认证，其他局域网设备访问尚未验收。

### 可选项目并发上限

项目配置项可增加 `"maxConcurrentTasks": 2`（整数1–100）。这是本扩展执行任务的并发上限，原角色及工作区锁仍生效；不是同时运行流程数，也不包括独立验收/记忆阶段。缺省保持原行为。任务会冻结登记时声明，旧未结束任务的较小上限继续生效；相同幂等键重放复用原声明。详见[项目并发](project-concurrency.md)。当前正式项目未开启，本轮未进行真实并发验收。
