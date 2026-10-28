# 独立升级候选源码准备

`python scripts/prepare_upgrade_candidate.py qwenpaw <40位提交SHA>` 或 `sim <40位提交SHA>` 只准备一个独立候选 checkout。当前工具不会安装依赖、构建、运行候选代码、切换服务、修改锁文件、操作当前 upstream 或迁移数据。

仓库标识仅允许 qwenpaw/sim；远程地址读取 `upstreams.lock.json`，并与程序中的两家官方 GitHub 地址逐字核对。CLI 没有 remote/path 覆盖参数。版本必须是完整十六进制 40 位 SHA，不接受分支、标签、短 SHA 或 shell 表达式。Git 以参数数组启动，不使用 shell；禁用全局/系统 Git 配置、Git 环境覆盖、hooks、fsmonitor 和递归子模块，避免候选准备时触发本机定制命令。

候选存放于 `var/upgrade-candidates/<仓库>-<SHA>/`，使用 `git clone --no-checkout --no-local` 后 detached checkout。同目录的 `.state.json` 记录 preparing/ready/failed、目标 SHA、remote 和相对路径；成功始终带 `runtimeValidated:false`，不把源码准备冒充升级兼容。提交不存在、Git 失败或超时会保存 failed，不改当前安装。Git 输出不原样记录，避免意外保存服务器文本或凭据。

已有目标必须是独立 checkout、origin 完全匹配、HEAD 精确匹配，并且没有修改、未跟踪文件或被忽略的额外文件，才可幂等返回 ready。否则拒绝，不执行 reset、clean、删除或覆盖用户文件。存储目录和 checkout 的相关符号链接会被拒绝。同一目标使用排他锁阻止并发准备；中断留下的锁需要先核实进程与现场，不自动删除。

失败可能留下独立候选的部分克隆，这是刻意保留的诊断现场，不是当前安装污染。对于不完整目标，后续调用会验证并拒绝，不能自动重新下载掩盖失败。工具不保证任意 SHA 可通过远程克隆取得；远端未公布的提交会失败。

## 验证证据

`tests/test_upgrade_candidate.py` 的 6 项测试全部通过。测试在临时目录建立真实本地 Git 仓库，测试内替换固定地址表；生产 CLI 不提供该替换入口。覆盖：固定旧提交而原 checkout 保持新提交、幂等复用、脏目录拒绝并保留编辑、错误 HEAD 拒绝不 reset、错误 origin 拒绝、缺失提交明确 failed 且原安装与锁文件不变、非法版本/仓库/remote 在克隆前拒绝、符号链接及并发锁拒绝。

```bash
.venv/bin/python -m unittest discover -s tests -p test_upgrade_candidate.py
```

主线随后已实际从官方远程克隆当前锁定提交 `502b59d88464d5c9e8be4a507347282d6a54e0be`，返回 ready/reused=false/runtimeValidated=false；第二次运行返回 reused=true，未重新克隆。两次操作后当前上游完整性检查仍通过。状态记录在 `var/upgrade-candidates/qwenpaw-502b59d88464d5c9e8be4a507347282d6a54e0be.state.json`。这是同版本候选准备实测，没有运行候选代码、切换服务或证明跨版本兼容。

## 已验证本地对象缓存

可显式追加 `--use-local-cache`。仅从本项目候选目录中选择 ready、官方 origin、
完整 SHA、干净独立 checkout 且包含目标 commit 的候选；不接受任意缓存路径。
克隆使用 Git 传输模式，不共享 hardlink 或 alternates，随后将目标 origin 设回
锁定官方地址，核对源缓存和目标完整性。状态记录 acquisition=verified_local_cache
与 cacheCommit。缓存不满足时明确失败，不自动转为网络下载。

此模式用于已取得完整官方历史后的另一个固定提交。它不证明最新远端内容，
不改变 runtimeValidated=false 的含义。缓存11项测试通过（含原有准备测试）；
实际28f2b9f提交从已核验502b59d候选取得，目标独立且干净。
此前远程下载已TimeoutExpired终止，失败目录和状态保留在
var/upgrade-candidates/qwenpaw-28f2b9f624d4917268f964cffc8c3cba519f8b87.failed-download-1*。
