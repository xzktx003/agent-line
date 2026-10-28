# 通用项目绑定：现有执行桥的静态接线

> 状态：停止实施，未部署。用户再次要求保持 QwenPaw 为主程序、保留原生记忆并控制复杂度；本轮未完成的多项目桥接改动已撤回，现有单执行桥保持。下文只保留为未采纳的方案记录，不作为当前安装要求或下一步默认任务。后续若确需多项目，先核对官方现成配置能力，再决定最小接线方式。

目标：同一个QwenPaw宿主支持项目专属正式角色/ReMe、Codex目标和代码工作区。
复用现有ExecutionBridge、Dispatcher和容器执行器，不新增运行服务、后端框架、
记忆提供器、安装状态机或依赖。官方源码保持原版。

## 配置与接口

- 旧 `execution_bridge`、旧顶层 `managed_role_ids`/`codex_target_agent_id` 和所有
  旧路径、账本、schema1 descriptor保持。未知旧任务不迁移、不重投。
- 插件可选 `project_bridges` 字典，键为项目ID；每项使用原执行桥字段，增加
  `installation_id`、`executor_config`，并有自己的 `managed_role_ids`。
  注入的 `project_id` 来自字典键，不接受请求或模型指定目标/工作区。
- 每项静态挂到 `/api/agent-line/projects/{projectId}`，包含原执行桥的相同接口。
  全部继承原宿主 `require_identity`，再核对各自serviceToken。
  named descriptor为schema2，带固定projectId/installationId及原字段。
- 工具和middleware仍只注册一次，按真实当前Agent ID查项目contract。跨项目不得
  复用parent、target、重叠工作区、CODEX_HOME、账本或serviceToken；target不能是
  任一项目的协调角色。非法配置保留已声明角色的拒绝执行守卫，不能让Loader跳过
  插件而失去保护。新named回执保存项目/安装范围，旧账本不能被重挂成新项目。
- 适配服务gateway可选 `projectRoute`，必须等于本项projectId。官方console地址
  仍为宿主 `/api`，插件路径独立。schema1保留原单origin所有权限制；schema2只有
  项目/安装/角色/工作区完全匹配才能接通，不能仅删除origin限制。

## 执行与验收

现有launcher支持受信 `--executor-config` 前缀；原默认配置不变。每项目注册一个
固定参数的短launcher脚本，QwenPaw原生backend_settings.binary指向它。配置与脚本
位于项目工作区/CODEX_HOME之外且仅可信所有者可写。复用全局launch记录目录与原
control/lifecycle，按真实workspace+thread+turn核对唯一资源，不能跨项目控制。
当前仍使用同一固定Codex二进制和容器镜像，只隔离工作区和状态。

readiness按gateway选择原legacy或对应named配置，绑定原生receipt/role binding、
CODEX_HOME rollout、指定launcher/config、独立测试与资源退出。旧证明不自动升格。
已有回执的查询、取消和未知身份保留，不能为升级收养另一项目的旧账本。

验收包括：

1. 单宿主legacy和两个named project的静态路由、唯一工具/middleware、真实身份
   选择；非法/重复配置仍保留拒绝守卫。
2. A token不能访问B；A role不能绑定B；错误schema/project/installation/target/
   workspace或共享身份拒绝。模型不能增加project或目录参数。
3. 原schema1与原任务不变；named同taskId不能读写或取消另一账本；重启未知不重投。
4. launcher参数仅改变受信配置，镜像/二进制不变，代码/状态挂载分开；跨项目退出
   控制按原实际身份拒绝。
5. 两项目各实际原生ReMe角色→Codex→项目文件→独立测试→资源退出/租约0；分别
   保存原生记忆，验证召回/输入不会串项目。完整记忆隔离仍按AC-11/40的所有路径
   要求核对，不能仅凭分开目录判通过。
6. 同原主入口保留旧历史并复核legacy实际执行；普通runtime以当前证据启动。

此为执行计划，不是已完成功能。当前正式配置尚无named项目；改动前须维护、备份，
完成后在PROGRESS.md与验收总账按实际证据更新，不缩减完整PRD。
