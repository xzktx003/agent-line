# 父角色原生记忆到容器执行的真实验收

探针 `probes/qwenpaw/container_parent_smoke.py` 使用新任务 `container-parent-memory-001`、派生session及独立随机记忆文件，不修改旧host探针、配置、桥接代码或角色。没有重启QwenPaw。

## 写入决策与路径

本次写入属于已授权的隔离功能验收。通过官方 `PUT /api/workspace/memory/{独立文件名}?section=digest`，以 `X-Agent-Id: agent-line-g0-parent` 写入测试任务代号、随机唯一标记和独立产物文件要求；随后调用官方BM25 reindex。文件名随机且不覆盖既有记忆，原始决策记录在私有receipt中。没有直接修改原生记忆文件或数据库。

父角色提示只包含任务代号，不含随机标记。服务凭据先绑定持久task/session，模型只能提供instruction，不能决定target、workspace或任务身份。执行使用当前正式 `agent-line-container-codex` 的固定容器工作区。

## 验收条件

1. 原生结构化事件中真实memory_search调用与同call_id成功结果，结果同时包含独立来源文件和随机标记。
2. 后续agent_line_delegate调用的唯一instruction参数包含标记；同call_id成功结果给出固定task和upstreamTaskId。
3. 容器原生rollout的user输入包含标记及独立文件要求，证明检索结果实际注入执行任务。
4. 原生thread/turn与委派upstreamTaskId一致，任务成功；外部读取产物精确等于标记加换行。
5. 受信exitReceipt绑定同thread/turn、固定workspace、owner和完整容器ID，记录删除前inspect的实际退出。

工具调用顺序、参数、结果与来源联合核对；模型最终文字声称不算通过。角色记忆对Codex生效体现为父角色召回后显式注入，不能据此宣称Codex自身运行了QwenPaw记忆生命周期或自动写回。

## 复跑与证据

从agent_line目录运行 `.venv/bin/python probes/qwenpaw/container_parent_smoke.py`。私有证据在 `var/qwenpaw/container-parent-memory/receipt.json` 与 `parent-native-result.json`，文件权限0600。先持久登记再提交；未知提交不重投，复跑只查询已存身份。上游内存任务若在重启后丢失，不自动重新发起模型。

## 实际结果

本轮全部条件通过：父角色原生任务 `task-32827865227c`；委派执行任务 `task-4df8512931eb`；native thread `01a15e36-9f9d-75f3-b3aa-d93f11e36de4`、turn `01a15e36-9fe7-7e01-b0de-5a75756c5cff`。receipt标记 `verified:true`，包含关联memoryCallId/delegateCallId、独立来源文件、实际委派instruction、native user注入时间、产物校验以及容器退出proof。

这是新正式容器路径上的真实父角色记忆利用，不是重用旧host结果或在提示中预先注入答案。本轮未测试自动结果记忆写回、跨重启召回、并发多角色或端到端Sim编排；不改变这些能力的独立验收状态。基础flake8检查通过。
