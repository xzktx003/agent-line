# 单层原生有限循环：真实 Sim 探针

2026-10-22（北京时间）已通过真实 Sim 无模型验证。新增 `probes/sim/finite_loop_probe.py`，只写独立 `var/sim-finite-loop/` 和专用新流程；没有修改生产模块、上游、QwenPaw、角色、记忆、主服务准入或其他流程。

这不是三次展开：发布图只有一个 loop 容器和其内一个 Human v2 机器等待块。原生 Sim 负责重复激活及退出，探针只观察暂停并提供事先明确的结果，不决定调度边。此验证证明上游能力可用，不代表中文编译器/自动任务闭环已支持循环，也不是 Codex 验收。

## 原生图与精确身份

- workflowId：`e386f9dc-3879-46b8-a047-a82f9980d2b2`
- 发布 version：`1`
- deploymentVersionId：`ffcd876f-3247-4888-903c-0b560b272a5f`
- 等待块 ID：`fl_e3edbceb69484a99b4fa7f5907863678_wait`
- 原生容器 `data.loopType=doWhile`，`data.count=3`，但真正上限来自受控条件：`<loop.index> < 3 && <等待.resumeInput.repair> === true`。
- 等待块 `data.parentId` 指向容器、`extent=parent`；容器 `loop-start-source` 连等待块，`loop-end-source` 连 End。PUT 只给 blocks/edges/variables，由 Sim 重建 loops；发布后通过正式 versions API 核对该容器唯一成员和条件。
- Human 的 taskInput 原生模板为 `{"iteration": <loop.index>, "marker": "原生循环"}`；同一次公开 run 查询同时读取完整等待块，校验实际 taskInput 和 `response.resume.contextId` 对应当前 paused.contextId。

## 真实结果

| 用例 | 原父 runId | 实际激活 | 原生结束结果 |
| --- | --- | --- | --- |
| 持续需修复 | `2df9ac00-293f-4ed2-87a3-22c0e9b19407` | `_loop0`、`_loop1`、`_loop2` | completed，exhausted=true，result.iteration=2 |
| 第二轮成功 | `0b379d30-2bb7-4fd2-988d-ec886f53141e` | `_loop0`、`_loop1` | completed，exhausted=false，result.iteration=1 |

第一例 taskInput.iteration 依次是整数 0、1、2，pausePointCount/resumedCount 依次为 1/0、2/1、3/2。每次使用同一个真实 block ID，context 后缀区分激活；不存在输入串轮。三次 repair=true 后流程完成，没有第四次暂停，End 明确输出 exhausted=true。

第二例首轮 repair=true、第二轮 repair=false，完成后 End 输出 exhausted=false，没有第三轮。两例父 run 最终也 completed；每次 resume 回执的新子 run ID 都保存在 state 中，最终结果从最后一个已确认子 run 读取。

在第一例 `_loop2` 活动期间，探针另记一条明确负例意图，用旧 `_loop1` 和不同结果 repair=false 调用正式 v2 resume。原生返回 **409**；随后的原父 run GET 仍在 `_loop2`，之后只对 `_loop2` 交付本轮结果。该请求是有界负例，不是网络失败重试。没有把旧 context 的响应当成新轮成功。

## 写入、复查与边界

首次阶段发生 4 次写：create、save、deploy、execute。之后取证 GET 在等待块尚未执行时使用 selectedOutputs，收到 400；已保留 failure 文件。仅修探针为先 GET 状态、到目标 pause 后再获取输出，沿同 workflow/run 续接，没有重发原写。

续接发生 7 次写：第一例三个 resume、一个明确旧 context 负例（409）、第二例 execute 和两个 resume。累计 **11 次原生写请求，其中 10 次成功回执、1 次预期拒绝**。每个操作先落 payload/intent，收到回执才记 received。未知意图拒绝重发，不能通过新 ID 规避不确定结果。

```bash
# 已有证据时默认只观察原身份
.venv/bin/python probes/sim/finite_loop_probe.py
.venv/bin/python -m flake8 --select E9,F63,F7,F82 probes/sim/finite_loop_probe.py
```

真实再次运行共 7 次 GET、**0 次写**，priorResult=passed_live；静态检查通过。`--continue-known` 只适用于单进程有界续接，复用完全一致的已确认操作；不允许同时运行同目录，不删除 state 后重建以掩盖未知结果。

证据 `state.json` 包含全部意图、回执、图、发布图、暂停输入、context、父子身份和终态；`report.json` 是成功续接报告（本次写数为 7）；`observation.json` 是零写重复观察。全部文件权限已核实为 0600。凭据只在内存读取既有私有 Sim 配置，不打印。

本轮只使用公开 v2 API，获取激活输入和 context 不需要读取数据库或 legacy snapshot。尚未验证适配器/Sim 重启、网络回执丢失后的原生查询恢复、多观察者并发、异常循环结果、真实角色多轮修复或记忆写回。产品接入仍须落实 `finite-loop-feasibility.md` 的发布形状证明、逐激活任务身份及旧结果隔离；不能直接移除现有 DAG 限制。

## 适配输入读取的增量

`SimWaitClient.read_input` 现支持显式 `context_id`：与基础块 ID 不同时，读取同一次原生
完整块输出，核对暂停 context、block、单一活动点，以及 `response.resume.contextId`，
再解析该块的 taskInput。返回完整激活身份，不以全局计数猜轮次。默认调用仍只接受
基础块 context，未放开既有 Discovery、PublicationVerifier 或任何循环执行准入。
调用方仍需单独证明发布图允许的激活，读取成功本身不构成执行授权。

新增 `tests/test_sim_wait_activation.py` 的 12 项回归覆盖三轮身份、旧输出拒绝、输入与传输
边界。主线又对本探针实际捕获的五次暂停响应做了离线格式核对，结果见
`var/sim-finite-loop/activation-reader-report.json`：原输入准确、替换为不匹配的输出 context 后拒绝，
没有新原生请求或写入。这是实际原生格式的解码验证，不冒充新的现场激活或完整自动循环。
