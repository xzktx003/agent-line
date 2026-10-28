# Isolated native Luna candidate

最新事实（2026-10-22）：代理传递修正后的第三个独立候选已完成真实父Agent→Codex Luna委派，原任务与主安装不变。详见文末本轮记录；下文前两候选失败为保留的历史现场。

`probes/qwenpaw/luna_candidate.py` creates one private fresh QwenPaw project under
`var/luna-native-candidate-20261022/`. It copies the executor launcher, control,
and lifecycle files into that install, copies only the existing Codex `auth.json`
into a fresh private Codex home, and uses QwenPaw's built-in Kilo free provider
for the parent role. It does not read or copy the main provider configuration.

The probe persists a one-shot intent before official setup and before parent
submission. It never retries a submission whose receipt is unknown. A complete
proof requires a native parent task with a linked successful
`agent_line_delegate` result, matching Agent Line upstream identity, terminal
execution with released resources, an exact `gpt-6-luna` thread/turn/model
receipt, and the expected bytes in the new workspace file.

The first candidate exposed a setup defect: its target had `channels` unset, so
the console channel manager was absent and no native execution task was
created. The production initializer now supplies `ChannelConfig()` for the
target. The corrected candidate completed official isolated setup and binding,
but its single parent task ended with `MODEL_TIMEOUT` before producing output
or a delegate call. It therefore provides no native Luna execution proof. The
private evidence records zero Codex launch receipts, no session mapping, no
Codex rollout, and no target session file. Its parent session file is present.

The parent task is terminal and failed, and the candidate host was stopped
after confirming it had no child processes. The host stop does not imply a
native execution terminal receipt: no native execution was created. The first
candidate's separate `unknown` ledger remains unchanged. Private state and
transcripts remain under the candidate directory; console output is sanitized.

Run from the repository root with:

```bash
.venv/bin/python probes/qwenpaw/luna_candidate.py
```

Each candidate uses a separate task and session identity. The original unknown
task is preserved without retry. The corrected candidate's host identity,
empty child list, and stop result are in its private state. Do not interpret
host shutdown as a successful or terminal Luna turn.

The project setup regression suite passed 60 targeted checks after the
initializer change (15 setup checks and 45 spec/verification checks). The
native model path remains unverified because the corrected parent task timed
out before delegation. The parent was configured to use Kilo free; the timeout
receipt does not establish a provider-side failure or its cause.

## 代理传递修正后的候选

`private_env()` 的白名单遗漏了当前已配置的HTTP(S)/ALL代理与NO_PROXY，
但普通安装和主宿主环境本来会继承这些变量。只修正这个候选脚本，未改上游、
主provider或业务执行模型，也不把代理值写入日志。两项回归先红后绿，验证
转交原网络路由同时仍排除其他环境秘钥、无代理配置时不凭空补值。

无推理GET同一Kilo模型目录：配置代理200/0.9秒，直连ConnectTimeout/16.5秒。
这是当前网络路由证据，不证明旧任务超时的唯一根因。原始两个候选不重投；
新候选使用独立目录/角色/会话/任务，位于`proxy-fixed/`。

新候选已返回completed：父角色真实调用agent_line_delegate，原生Codex返回
succeeded、terminalConfirmed与resourcesReleased，实际文件字节匹配原声明；
候选宿主已在执行退出后停止。独立原生记录核对另保存，不以静态配置或仅存在
文件代替模型执行证明。主19任务/事件、9Agent与配置摘要和业务模型保持。

该证明是正式QwenPaw父角色/官方委派/Codex Luna的真实链路；不等同主适配服务
就绪，不是完整三角色团队、原生审批、取消、记忆写回或只读审查角色验收。
主执行准入继续unknown/readyfalse，没有用候选证明打开主安装。

本候选声明的marker为`AGENT_LINE_LUNA_PROXY_NATIVE_20261022`，加换行共38字节；
初始state在提交前已记录。它与脚本默认候选的marker不同，复核必须使用该次
持久意图，不能把默认常量套到另一候选。父工具初始返回accepted，随后同一
upstreamTaskId由只读轮询核对为终态；这不是重新委派。模型命令exit0与后续
app-server/容器退出是两类证据，容器清理的exit137不能表述成模型命令失败，
也不能表述成容器exit0。原始执行退出回执保持，不改写历史。

独立审查报告：`proxy-fixed/independent-audit.json`。已逐一匹配原生父会话call_id、
委派输入marker、原执行账本upstream/thread/turn、同turn的模型与task_complete、
配对command exit0和实际38字节产物。退出回执精确身份匹配并在task_complete后
4.228秒记录137/runningfalse。主保留核对见`main-preservation.json`。
