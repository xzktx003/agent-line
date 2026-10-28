# 容器执行隔离验收

本切片不修改 QwenPaw 源码、现有服务或角色。官方 `harnesses/codex/discovery.py:112` 支持 `CODEX_BINARY`；`app_server.py:78` 通过指定 executable 启动 `app-server --listen stdio://`。独立验证直接使用已安装 QwenPaw 的真实 `CodexAppServerClient(binary=launcher)`，不是替换模型执行结果。

## 文件与运行

- `deploy/executor/launcher.py`：固定服务端配置的 Docker launcher，只接受 app-server/version。
- `probes/executor/prepare.py`：建立独立目录与0600配置；只复制此前授权的项目隔离 auth.json，不读全局历史/config/skills。
- `probes/executor/verify.py`：真实容器文件边界与真实原生模型验证。
- `var/executor/config.json`：固定 workspace、独立 CODEX_HOME、不可变镜像ID及只读 native executable 与同版本 codex-code-mode-host。
- `var/executor/evidence*.json`：成功或失败记录。认证值不打印。验证脚本默认只展示已保存证据；只有显式 `--new-attempt` 且前次已知终态才创建新任务。

从项目父目录运行准备脚本和验证脚本，解释器使用 `agent_line/.venv/bin/python`。launcher 可直接执行 `--version`。集成独立 QwenPaw 实例时在该实例设置 `CODEX_BINARY` 为 launcher 绝对路径；不要修改现有实例环境。工作区在容器中保持相同绝对路径，兼容 native cwd 契约。

## 已验证的边界

Docker 使用非root当前 UID/GID、只读根文件系统、全部 capabilities 移除、no-new-privileges、默认seccomp、128进程、2GB内存、2CPU、128MB临时目录。仅绑定固定项目目录、独立 Codex 状态目录及只读可执行文件。没有挂载宿主home、Docker socket或QwenPaw服务凭据。

实际 shell 在项目内写入成功，向 `/etc`、`/root` 的写入失败；通过符号链接写入 `/etc` 失败；Docker socket、宿主全局认证和本实例服务凭据路径不可见。容器内允许的 danger-full-access 仅禁用嵌套 Codex 沙箱，外围 Docker 边界仍生效，与主机直接使用该模式不同。

初次选择到了残留的0.134.0可执行文件，模型返回不支持错误。失败证据保留。后改为当前 Node CLI实际使用的嵌套0.163.0二进制，未改变用户模型或全局安装。第二个已知失败为新版 Codex 所需的同目录 `codex-code-mode-host` 未挂载，模型如实报告未执行；已补只读挂载并保留失败结果。

## 仍需完成的生产要求

已从固定本地源镜像提取显式工具和动态库，生成69,483,308字节专用镜像（原镜像约12.3GB）。包含POSIX工具、Git HTTPS和Python3.12标准库，不包含Docker、curl、apt、gcc、GPU工具；真实检测通过。镜像ID记录在 image-build.json 并固定到服务配置。仍需供应链扫描，后续不同语言项目需明确增加专用工具镜像。模型访问网络仍使用 Docker bridge，未实现目的域名出口限制，容器可访问网络上的服务；不可宣称防数据外传或完整网络隔离。Codex 独立认证目录对执行进程可读写，后续应考虑认证代理/更窄凭据。

该验证证明进程与文件系统边界，不证明角色工具权限、审批拒绝、跨重启取消、主线调度集成。容器退出后 --rm 清理，不影响其他容器。需要故障/强杀清理及生命周期集成验收后才能打开生产 capability。

## 实际模型执行结果

最终容器原生 turn `01a15e1d-6b08-7711-adf1-91c7f3e0395d`（thread `01a15e1d-6ad7-74b0-a107-270d0d3338c7`）completed，项目内文件由模型实际工具创建且外部读取精确匹配 `CONTAINER_NATIVE_OK\n`。独立边界探针 exit0，模型也报告 `/etc` readonly、`/root` permission denied、socket不存在。证据 `var/executor/evidence.json`；完整原生 rollout 保留于独立 `var/executor/codex-home/sessions`。退出后该验收容器已自动删除。


## 生命周期与正式接入准备

launcher现在作为监督进程保留，不直接exec Docker。每次启动生成随机owner、宿主PID及 `/proc` start_ticks、UID，并以label标识唯一容器。独立guardian不继承stdio连接；监督进程退出或PID被复用后，guardian只检查精确名称与四项label吻合的容器，再按inspect得到的完整容器ID清理。SIGTERM走finally立即清理；SIGKILL由guardian清理。短暂轮询覆盖Docker创建窗口，不扫描/删除其他用户容器。

`probes/executor/lifecycle_probe.py` 实测stdio断连、SIGTERM、SIGKILL三种情况全部无遗留，证据 `var/executor/lifecycle-evidence.json`。正常官方client.stop在真实执行探针中执行，容器也退出。宿主掉电无需清理运行进程；但Docker daemon挂起、guardian同时被杀仍需进程管理器级reaper，当前不能声称覆盖所有故障。

最小镜像再次真实执行成功：thread `01a15e20-7097-7143-a884-14fc9d5ada3f` / turn `01a15e20-70d1-7723-bd45-6e0b59aad032`，模型创建文件，外部内容验收通过。当时镜像构建使用本机固定镜像；当前入口已要求显式`--source-image sha256:完整ID --output var/全新目录`，不自动修改正式config或tag。脚本只从指定已有镜像读取工具，不下载依赖，当前接口及全新安装顺序见[fresh-install.md](fresh-install.md)。配置仍是单workspace固定绑定；正式role使用该路径必须创建在此workspace，不能任意选择其他目录。

网络下一步应采用独立容器网络及显式HTTP CONNECT出口代理：只允许已核实的模型服务认证/请求域名，禁止private/link-local/metadata地址与DNS重绑定，控制目标端口；Git依赖下载单独策略。执行容器不可直接外连，只有代理可外连。需防止绕过proxy环境变量，不能只设HTTPS_PROXY就声称限域。当前bridge未更改共享daemon网络，尚未实现上述强制出口策略。

清理反向验收另创建了本探针自己的未运行容器，故意使用不匹配owner标签；cleanup明确抛错且容器仍存在，随后探针按自身刚创建的完整ID删除。证据 `var/executor/ownership-evidence.json`，没有访问或停止其他用户容器。

最终追加的“先删除旧产物再运行”复验（thread `01a15e21-9907-7511-a123-414faf921366`）等待180秒无新native事件，探针超时退出并执行client.stop；所属容器已清理，状态记录为timeout，不冒充模型成功也不自动重投。前次成功证据保存在按turnId命名的evidence文件；目前重复验收稳定性仍未通过。

## 取消修复：可信容器退出回执

原生turn_aborted无法保证工具子进程结束，取消验收已证明后续文件仍被写入（见cancellation.md）。新增的退出证明不使用“容器不存在”推断成功。

launcher透明转发原始stdio JSON行，只从thread/start、thread/resume、turn/start响应提取真实thread/turn；不持久化请求内容、提示、工具输出或密钥。每次容器的0600 owner文件持久记录PID/start_ticks/UID/random owner/full container ID/workspace及native身份，单独文件锁保护更新。turn响应在身份落盘后才转发给QwenPaw。

受信服务可执行：

```bash
.venv/bin/python deploy/executor/control.py stop --thread THREAD --turn TURN --workspace FIXED_WORKSPACE
.venv/bin/python deploy/executor/control.py observe --thread THREAD --turn TURN --workspace FIXED_WORKSPACE
```

也可在受信后端导入 `control.proof(thread_id,turn_id,workspace,stop=True)`。模型看不到宿主控制目录，无该接口凭据。control要求唯一身份绑定，拒绝其他active turn或未收到turn回执的pending执行；关闭接受新turn后停止该容器。

退出证明必须来自删除前真实Docker inspect，检查全部owner标签、完整ID、State.Running=false、Pid=0、Status=exited/dead、非空FinishedAt，并记录ExitCode、StartedAt、镜像ID、workspace及绑定身份。文件fsync与原子替换后才remove容器。若容器已被其他方式删除且没有退出proof，返回unknown/false，不补造证据。

`resourcesReleased=true`只有exitReceipt存在才返回；主线还必须核对exitReceipt内精确thread/turn、workspace、owner/fullID与本任务绑定。该receipt只证明容器进程退出，不替代原生终态判定或业务验证。不同任务共享容器时可能拒绝停止，应采用独立执行角色/会话边界，不能为了取消一个任务杀其他active任务。

正式角色无需全局环境变量：上游 `harnesses/registry.py:202,213` 的 `backend_settings.binary` 为官方支持项，并参与adapter实例cache key。`probes/executor/register_role.py` 通过官方Agent CRUD仅创建/配置 `agent-line-container-codex`，workspace固定executor/workspace，binary固定launcher。该脚本需在维护窗口获准后执行；不修改旧角色，不自动切换桥接target，不把角色保存成功当执行验证成功。

### 正式角色运行已验证

备份窗口结束后，只新增 `agent-line-container-codex`，官方GET回读确认binary/workspace配置。`probes/executor/formal_role_smoke.py` 通过官方console任务提交并观察：thread `01a15e30-8b93-7122-b10f-98bf1ddd939c` / turn `01a15e30-8bc3-7a71-b32c-ae65158b4e06`，实际工具创建formal-container-proof.txt，外部内容检查通过，再通过control获得真实容器退出proof。完整证据 `var/executor/formal-role-execution.json`。没有切换主线桥接配置或修改旧角色。

独立修复取消验收也已通过：`exit_receipt_probe.py` 对thread `01a15e2c-fbb9-7120-8b34-a812d364a39f` / turn `01a15e2d-07b6-7310-bafc-d967d19c34d6`，观察stage1后native interrupt，再受信control终止精确owner容器，删除前inspect得到exitCode137和实际FinishedAt；等过原35秒sleep后stage2仍未出现。证据 `var/executor/container-cancel-evidence.json`，与旧宿主取消失败证据分开保留。

### 并发与协议限制

launcher在协议锁与owner文件锁内检查accepting、先登记pendingTurns再转发turn/start。control在同owner锁内拒绝pending或其他active turn，再置accepting=false；间隔中的新turn直接收到RPC错误。`control_contract_test.py` 在临时目录验证未知身份、多owner、pending turn、其他active turn均拒绝，没有Docker或模型调用。

仅允许已核验的initialize、account/read、model/list、skills/list、skills/extraRoots/set、thread/list/read/start/resume、turn/start/interrupt方法。未跟踪的review/start、command/exec、compact及账户登录/退出等接口明确拒绝；这是受控执行角色的能力限制，不声称支持整个原生交互功能。协议原始行保持转发，记录仅含身份，审批响应仍按原协议转发。

## 正式桥接切换与 Dispatcher 复验（最新状态）

经维护窗口授权，插件固定target已切至 `agent-line-container-codex`，workspace与CODEX_HOME分别绑定 `var/executor/workspace`、`var/executor/codex-home`。`container_control` 和 `container_launcher` 指向本项目受信脚本。`backend_settings` 实际为dict，binary精确匹配。旧插件配置备份到0600的 `var/qwenpaw-secrets/plugin-before-container.json`；旧原生账本与旧任务ID不修改。官方认证安装API成功后，installed plugin.py/execution_bridge.py/前端JS逐字节核对。

初次热安装因控制脚本组可写而被拒绝409，随后将控制脚本设600、launcher设700再安装成功。这是权限检查有效的实测，不绕过检查。observe现先撤销旧缓存resourcesReleased和resourceExitReceipt，只有当前绑定的正向退出证明重新通过才放行。

`probes/executor/bridge_probe.py` 使用独立TaskStore与全新taskID，完整经过 Dispatcher → Gateway → 插件 → 正式Codex角色 → 容器：

- success：task `task-15ac68c86f034ba6bd030702b64b9b33`，native thread `01a15e32-c4ea-7982-b9e8-85f7824fe28f` / turn `01a15e32-c53a-74f3-8c16-50646e1c414b`。task_complete、真实容器退出、独立文件精确内容均通过。
- cancel：task `task-ddedb25696c24951bc76341b2f607814`，native thread `01a15e33-3eae-7090-82c7-d0759ac28745` / turn `01a15e33-3f0c-7ab2-8d43-68742bdd0e02`。第一阶段写入后request_cancel，仅reconcile；同turn_aborted、完整容器ID的退出证明通过；等待超过原35秒sleep后第二阶段文件仍不存在。

证据分别为 `var/executor/bridge-probes/success.json`、`cancel.json`，两者verified=true。复验使用同一个正式角色顺序执行不同任务，证明单workspace角色可复用。control拒绝共享容器中其他active/pending turn；没有扩大并发准入或自动解除主库maintenance。旧宿主取消失败任务仍保留原始失败证据，不能据新路径成功推断其已获得可追溯退出proof。

单元PluginLoader契约、control并发拒绝契约和基础flake8检查通过。网络强制限域、跨宿主故障、全产品并发与升级恢复仍独立待验，不把本切片当全生产交付。

主线TaskStore加入resource_exit持久证据后，`probes/executor/reconcile_receipts.py` 在新进程仅reconcile已有success/cancel两个任务，没有提交新任务。两条实际exitReceipt均进入TaskStore evidence，重新打开数据库后精确一致，原executionIdentity保持不变。两份验收JSON均补充 `resourceExitPersistedAfterReopen:true`。

## 2026-10-22：随包沙箱 helper 与剩余环境限制

已安装 Codex 0.163.0 自带 codex-resources/bwrap；原最小镜像与 launcher 未接入它。因此“本机缺少 bwrap”并不准确。当前 launcher 从官方二进制相邻资源目录读取 helper，校验真实路径、属主、可执行位与权限，并只读挂载到容器 /usr/local/bin/bwrap。不存在时保留旧版本行为，存在但不安全时拒绝。就绪指纹纳入 helper 内容摘要与修改时间，更新后需要重新验证，不能沿用旧源码证明。

使用真实 launcher 构造的无网络、无认证、空工作区探针执行 bwrap --version 已返回0。保持现有容器权限运行 namespace 探针仍失败；宿主原始 helper 也报告 uid map Permission denied。只读检查发现 AppArmor 非特权 user namespace 限制启用，但未证明它是唯一原因。直接 Codex sandbox 探针超时，其自有容器已精确停止删除；没有重复模型调用。

[官方沙箱文档](https://learn.chatgpt.com/docs/sandboxing)说明 Linux 使用 PATH 中或随包提供的 bwrap，并依赖相应 user namespace 能力。本轮只修改仓库内 launcher 与指纹检查，没有修改 Codex 安装、AppArmor、sysctl、全局用户配置或上游源码，也未放宽容器权限。真实规划/审查只读执行仍待解决和验证。

证据：var/workflow-templates-20261022/launcher-helper-check.json、bundled-bwrap-check.json、sandbox-native-check-3.json。版本探针成功不等于只读模型执行成功。

## 当前只读沙箱的零模型核对（2026-10-22）

新增`probes/executor/readonly_sandbox.py`，在全新空工作区/无认证CodexHome、
原不可变镜像与现有Docker权限下测试。外层容器可以写入验收控制文件；
默认Codex read-only在执行读写检查之前即因bwrap user namespace创建失败退出1。
本机CLI列出deprecated的use_legacy_landlock，但显式启用后仍在命令执行前
panic并要求bubblewrap隔离app-server sockets，退出101；不能据flag存在断言
旧实现已生效，也不能把未执行写入当作“只读成功”。哨兵完整，三个自有容器
按精确ID/标签核对后移除，0模型调用，无配置/权限/系统修改。

证据`var/readonly-sandbox-20261022/run-3bbc5653481d/`。这排除了直接启用旧开关
作为当前版本可靠替代的方向，仍保留只读角色执行阻塞。
[官方沙箱文档](https://learn.chatgpt.com/docs/sandboxing)说明Linux的bwrap与user namespace前提；
本轮没有按文档示例修改宿主AppArmor或sysctl，也未降低冻结的只读/审批策略。

2026-10-23补充：构建模块导入不再执行Docker；缺Python3.12标准库的源镜像拒绝，不生成声称可用的空stdlib镜像。输出使用私有新目录，原有执行/测试配置不被自动改写。仅源码/AST/Luna复核，未执行新构建或验收，不将旧构建证据当当前脚本证明。
