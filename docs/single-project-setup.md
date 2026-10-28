# 全新安装登记自己的单个代码项目

`deploy/project/setup.py` 将项目目录、原生协调角色、Codex 执行角色和项目测试命令
接入现有单桥配置。使用 QwenPaw 官方离线 CLI、配置模型及保存接口，保留原生
ReMe；没有新增常驻服务、运行时配置层、依赖或上游源码补丁。

这是初始化命令。当前已有任务的主实例不会被切换，旧执行桥不能原地改工作区。
多个项目共用一个宿主的方案仍停止实施；本命令也不是完整从零安装器。

## 前提与命令

在一份尚无 Agent Line 执行历史的安装中，先按现有部署文档准备 Python 环境、
固定上游、官方 QwenPaw 初始化与私有登录资料。宿主必须处于确认停机状态。
准备现有 Sim 工作区及其私有认证文件，不创建新的 Sim 服务。

当前原生宿主及就绪证明使用 `http://127.0.0.1:显式端口`；不接受localhost、IPv6、
无显式端口或HTTPS作为此内部地址。局域网及TLS入口仍由原有外层网关提供。
安装后通过现有 `deploy/qwenpaw/manage.py start` 启动时，首次即使用spec中的端口，
重启沿用同一记录；端口占用、原记录与spec冲突或安装未完成时拒绝，不自动换端口。

代码目录必须已经存在，由当前用户拥有，不能与扩展安装目录或 Codex 状态目录
相互包含。普通 `AGENTS.md`、`SOUL.md`、`README.md` 和业务代码保持原样。
官方运行时需要使用的 `agent.json`、`harnesses/`、`sessions/`、`memory/`、
`skills/`、`skill/`、`skill.json`、`jobs.json`、`chats.json`、`drivers/`、
`credentials.yaml` 若已经存在，会拒绝初始化，避免覆盖项目内容或收养旧状态。

Codex 状态目录必须是当前用户的0700目录，只能为空或预置一个普通0600
`auth.json`。不会复制当前主实例的会话、数据库、配置或插件。可执行二进制及
相邻的 `codex-code-mode-host` 必须已准备好、当前用户拥有且不允许组或其他用户
写入。执行镜像与测试镜像必须显式使用完整本地 image ID；命令不拉取镜像。

将 spec 放入安装之外或已有私有目录，设为0600。以下仅展示格式；路径、模型、
provider、工作区及 image ID 均需填写当前安装的实际值，不使用验证项目的固定值：

```json
{
  "schemaVersion": 1,
  "project": {
    "id": "my-project",
    "name": "我的代码项目",
    "workspace": "/srv/projects/my-project"
  },
  "coordinator": {
    "id": "my-project-coordinator",
    "name": "项目开发协调",
    "providerId": "REPLACE_WITH_CONFIGURED_PROVIDER",
    "modelId": "REPLACE_WITH_ACTUAL_MODEL"
  },
  "executor": {
    "id": "my-project-codex",
    "name": "项目Codex执行",
    "model": "REPLACE_WITH_ACTUAL_CODEX_MODEL",
    "sandbox": "workspace-write",
    "approvalPolicy": "on-request",
    "binary": "/srv/tools/codex",
    "imageId": "sha256:REPLACE_WITH_64_LOWERCASE_HEX_DIGITS",
    "codexHome": "/srv/agent-line/var/codex-home"
  },
  "qwenpaw": {"baseURL": "http://127.0.0.1:52403"},
  "sim": {
    "authFile": "/srv/private/sim-auth.json",
    "workspaceId": "REPLACE_WITH_EXISTING_SIM_WORKSPACE_ID"
  },
  "verification": {
    "policyId": "project-unit-tests",
    "imageId": "sha256:REPLACE_WITH_64_LOWERCASE_HEX_DIGITS",
    "tests": [{
      "id": "unit-tests",
      "argv": ["python3", "-m", "unittest", "discover", "-s", "tests"],
      "timeoutSeconds": 120,
      "maxOutputBytes": 65536
    }]
  },
  "memoryQuery": "本项目的构建约定、审查清单与已核验经验"
}
```

## 可选的三角色团队

省略 `team` 时保留原来的单开发协调角色。全新安装可在同一spec顶层增加：

```json
"team": {
  "planner": {
    "id": "my-project-planner", "name": "需求分析",
    "providerId": "REPLACE_WITH_CONFIGURED_PROVIDER", "modelId": "REPLACE_WITH_ACTUAL_MODEL"
  },
  "reviewer": {
    "id": "my-project-reviewer", "name": "代码审查",
    "providerId": "REPLACE_WITH_CONFIGURED_PROVIDER", "modelId": "REPLACE_WITH_ACTUAL_MODEL"
  }
}
```

原 `coordinator` 对应developer，三个父角色与Codex target的ID必须各自不同。
安装使用官方接口创建三个独立原生工作区、ReMe与角色版本，绑定到现有执行与记忆接口，
不复制其他角色记忆。planner/reviewer通过既有bridge的readonly角色列表使用
`read-only`、`never`执行控制；developer沿用显式spec权限。三角色共用已登记代码目录，
仍受现有工作区写租约约束，不代表能同时写同一目录。

安装后可在[内置模板](workflow-templates.md)中映射三角色；单角色安装只可使用
开发修复模板。已有安装不能通过追加team原地初始化或覆盖角色。真实三角色执行及
记忆独立性尚待批次末验收。当前随包bwrap已由launcher挂载，版本查询通过；
namespace权限问题仍未解决，只读沙箱工具曾返回失败，
不能把模型正常结束或项目基线测试通过当作只读角色已可执行；不会降低沙箱绕过。

`sim-auth.json` 沿用现有格式 `{ "baseUrl": "...", "apiKey": "..." }`，须为
当前用户的0600普通文件。spec保存文件引用，不复制或公开Sim key。

```sh
# 只读检查，不启动官方进程、模型或Docker任务
.venv/bin/python -m deploy.project.setup check --spec /absolute/private/project.json
# 一次性初始化；先持久化独占安装意图，再运行官方离线操作
.venv/bin/python -m deploy.project.setup install --spec /absolute/private/project.json
# 只读查看本次安装阶段，不重试写入
.venv/bin/python -m deploy.project.setup status
```

## 实际生成的内容

- `var/executor/config.json`：原执行器的固定工作区、状态目录、二进制与镜像。
- 正式原生协调角色及其独立工作区；记忆后端为 `remelight`，禁用其他内置工具，
  仅启用现有委派和状态工具。原生记忆检索及插件工具守卫沿用现有实现。
- 正式 Codex 角色：通过官方配置模型注册在项目目录，只写新的 `agent.json`，
  不运行原生模板初始化，不覆盖项目现有说明文件。角色显式使用官方 Console
  通道配置，其他外部消息通道默认禁用；这是原生后台任务 API 的执行前提。
- 官方安装的 Agent Line 插件、原有执行回执和记忆治理库，以及独立服务凭证。
- `var/adapter/` 中现有格式的 service、execution、authoring、approval、memory、
  task-flow 配置与八个显式初始化的账本。编辑、执行、审批、记忆审核凭证互相独立，
  保存于私有配置，默认有效期一年。初始化不打印凭证或原生输出。
- `var/single-project-setup.json`：固定安装身份、spec摘要和安装阶段。

可信测试策略采用已有 schema2，允许没有预知文件哈希的业务测试，测试命令、
环境、超时和输出限制在配置中明确声明。没有固定 add 函数或验证任务的文件名。
当前测试执行器仍是无网络、只读工作区的独立容器：测试所需运行环境必须已在镜像
中具备，不能把此安装命令视为任意语言项目的依赖安装器。

## 原有数据、失败与执行准入

发现任何已有 adapter/executor 目录、执行桥配置、正式角色ID、回执库、重启配置
或安装标记均拒绝；即使目录为空也不会自动接管。停机检查遇到超时等未知状态
会拒绝。密钥账户按官方 SECRET_DIR 规则隔离，外来账户 override 与持久环境中
安装控制项不会被沿用。

配置失败保留 `incomplete`，后续 install 不自动重试、替换身份或删除现场。
官方步骤失败时，私有 `var/adapter/native-step-error.json` 保存有界诊断；原始
输出不进入终端或公开报告。此初装命令尚无中断安装的自动续接功能。

成功后的 `configured` 仅表示离线配置已经写入。不会启动服务、执行模型、
安装业务依赖或生成 `readiness.json`。原有 runtime 在缺少真实就绪证明时保持
`unknown`，拒绝新编码任务。下一步仍需通过现有实际执行与独立测试机制形成
同安装证据，才开放准入；不得手填 confirmed 或复制其他项目的成功证明。

## 首次受控执行核对命令

已增加 `deploy/project/verify.py`，用于同一安装在宿主启动后的首次核对：

```sh
# 原有宿主管理器启动QwenPaw，适配器此时仍可保持unknown
.venv/bin/python deploy/qwenpaw/manage.py start
# 默认仅本地查询，不登录、不提交任务、不运行测试
.venv/bin/python -m deploy.project.verify
# 首次显式执行受控核对，按原策略运行独立测试
.venv/bin/python -m deploy.project.verify --execute --wait-seconds 30
```

首次显式执行前检查当前插件、上游、角色指纹、宿主进程、空任务库和无守卫事实；
使用原项目工作区、原角色及配置中的测试策略，持久登记意图后提交一次。任务要求
Codex仅阅读核对项目，不要求修改业务代码；这是任务指令，不是新增只读沙箱保证。
实际权限仍来自原角色与Codex配置，独立测试仍遵守已有容器约束。

单角色已有状态后，再次携带 `--execute` 只核对原任务及其待执行的原独立测试，绝不再次
登记或提交。注册回执丢失时按原幂等key查询；提交意图已记录而受理未知时保留未知，
即使任务仍registered也不重发。不加自动重试、取消或另一套任务身份。

团队安装首次执行前一次持久化planner/developer/reviewer三项完整身份，按顺序核对。
前一项未完成不启动后一项；重放只观察已经登记的原任务，可首次启动预先声明且明确
没有登记意图的下一项。登记或提交意图已写入而回执未知时不会再登记或提交。
默认不带--execute始终只读本地记录。三项覆盖完整之前不会写团队就绪证明。
规划/审查额外要求原执行记录有冻结read-only/never策略，以及实际成功的原生命令证据。
没有成功操作（例如全部exit101）返回readonly_operation_unproven，不把模型自述当作核查成功。

原执行成功、资源退出、独立测试通过且原生父任务结束后，调用原
`prepare_readiness` 核对原回执、rollout、当前源码/配置、实际测试与退出证据，
再写原配置指定的 `readiness.json`。已有证明只能验证、不覆盖。正常适配服务
下一次启动通过原runtime核验，不能靠本命令的历史status直接打开准入。

`--wait-seconds` 限制轮询观察窗口，不会强杀原任务；单次HTTP和独立测试沿用原
配置的各自超时。到期保持pending和原租约，后续继续核对同一任务。默认查询已有
成功记录时只显示 `recorded_confirmed`、`readinessCurrent=not_checked`，不能将重启
之前的记录当作当前就绪。

本批验证仅针对原本地数据库、意图/锁/丢回执等边界；远端Gateway、执行与就绪
校验器为明确替身。真实新项目的Codex/Sim组合联调按用户要求留到功能批次末集中
进行，当前不据此提高AC通过数或宣称新业务安装已ready。调试用Codex任务将显式
选择用户指定的 `gpt-6-luna`，不会自动改用Astra。

## 验证范围

真实官方离线测试在全新临时目录中运行插件安装、角色创建、配置保存与读回。
子进程拦截 socket connect/create_connection，确认没有网络或模型调用；原有业务
说明和代码字节保持，仅增加官方 target `agent.json`。原生 ReMe、角色版本、
Codex模型与审批配置均读回核对。首次官方保存函数导入路径错误已修正，未作用
于当前主实例。

完整 runtime 装配另使用明确的 MockTransport 模拟远端登录、descriptor与角色列表，
证明生成的六份配置和账本兼容、缺证明时unknown/503、任务数0；这部分不能算真实
宿主、业务执行或Sim联调。当前主实例未调用此初始化命令，原角色、记忆与任务保留。

本项不提升 AC-06/07/11/40 等验收状态。首次就绪CLI已实现但真实组合待验收，三角色
配置及逐角色验证命令已实现，真实团队执行、完整记忆隔离以及整份PRD仍待验收。
