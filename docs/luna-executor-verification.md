# 使用指定 Luna 模型的执行器核对

2026-10-22，通过已安装的官方 QwenPaw `CodexAppServerClient` 和原有 Docker
launcher，实际提交了一次显式 `gpt-6-luna` 工具回合。没有模型回退，没有修改业务
角色的 `gpt-6.1-sol` 配置。

核对结果：

- `thread/start` 返回模型为 `gpt-6-luna`；原生 rollout 中相同 turn ID 的
  `turn_context.model` 也为该模型，且有同 turn 的 `task_complete`。
- 原生回合为 completed，有一次 exitCode=0 的 commandExecution；独立读取新建
  验收文件，字节精确匹配预先声明的内容。
- 客户端停止后，原线程/回合对应的 Docker 退出回执及 launcher 退出得到确认。
- 执行前后主服务、QwenPaw 配置、九份 Agent 配置和执行器配置摘要保持。

探针是 `probes/executor/luna_current.py`，私有证据在
`var/luna-current-executor-20261022/{state,exit-proof,observation}.json`。
已有状态下运行以下命令只核对原身份、当前文件及原生 rollout，不再次调用模型：

```sh
.venv/bin/python -m probes.executor.luna_current
```

`newSubmissions: 0` 描述这次观察操作。首次执行曾有一次真实模型调用，不能据此
宣称整项验证零调用。首次事件计数来自仅包含一个任务的新 app-server；后续独立
rollout 核对补充了准确 turn ID 与模型对应关系。

这证明现有客户端、指定模型和容器内工具执行可用。它没有创建 QwenPaw 原生
父 Agent 任务，没有经过其 `agent_line_delegate`，也没有运行项目独立测试，
不能替代完整委派、记忆闭环或当前主安装的就绪证明。

使用原有容器边界内的 danger-full-access；这不是原生 read-only 沙箱验证。
随包 bwrap 已能挂载和执行版本查询，但 namespace 权限问题仍保留，未改系统
策略、增加权限或绕过只读角色的执行检查。

后续完整链路使用仓库内独立候选安装及其固定 Luna 绑定。主实例桥接目标是安装级
固定配置，临时覆盖模型会改变角色版本；因此不会借调试修改原业务绑定，候选安装
结果也不能直接打开主实例准入。

该独立验证随后发现并修复了全新项目初始化遗漏 Console 通道的问题：
`deploy/project/native.py` 现在使用官方 `ChannelConfig`，原生后台任务具有 Console
通道，其他外部通道保持默认禁用。回归先在旧实现失败，修复后15项项目安装检查
（含真实官方离线初始化）及45项项目规格/就绪命令检查通过。修复后真实父任务又在
工具调用前超时；没有借此声称链路通过或重投原未知任务。具体记录见
[原生候选验证](luna-native-candidate.md)。

2026-10-22后续更新：候选脚本补传原代理环境后，新建的proxy-fixed独立候选
已完成真实QwenPaw父角色→agent_line_delegate→Codex Luna链路，候选宿主
在终态/资源退出后停止，主配置与历史保持。见[luna-native-candidate.md](luna-native-candidate.md)。
独立链路通过仍不替代主安装就绪或只读审查角色：零模型probe确认默认bwrap
及显式legacy Landlock均在执行前失败，不能靠换开关宣称只读验证通过。
