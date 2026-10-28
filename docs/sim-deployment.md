# 独立 Sim 部署

部署定义：`deploy/sim/compose.yaml`。Docker Compose project 为 `agent-line`，不会操作其他项目容器。上游源码保持原样。

## 固定版本

Sim 应用、realtime、migrations、cron 均从当前源码提交 `27b53561def7db4eaa9ad2f12c04ea1a0eaaf91c` 的 `-amd64` 标签解析并固定为镜像 digest。核查日期 2026-10-21。上游 `.github/workflows/ci.yml:392` 起明确每提交推送 `${github.sha}-amd64`；完整多架构 SHA 标签当时尚未找到，但对应 amd64 标签可读取。

| 镜像 | SHA256 digest |
|---|---|
| simstudio | `41eb452d1552ae465fa71afa5fac57ed6590fb05bbb9f7a330de14375b1a9668` |
| realtime | `3059e650bac964d3099679aef0bb14c380a53a073faee88cd8fcfb3352e83e5f` |
| migrations | `c2cff1627f5d79d4c96d17f0ed9550ea0046b07f06d6466a9ab2bc44e7565fd2` |
| cron | `9cbf8de37f3ac7b3d18d62e9783aab5962ef202a9a17636d64e6fa1dd3c9dbcc` |
| pgvector 0.8.2-pg17 | `feb68f4f15446397d8cac7f4fe48fe4586de83160d1fc48b46283312d1a33966` |
| Redis 7.4.8-alpine | `7aec734b2bb298a1d769fd8729f13b8514a41bf90fcdd1f38ec52267fbaa8ee6` |

Sim 镜像 OCI labels 继承了 Bun 基镜像元数据，revision 字段指向 Bun，不可当作 Sim 源码版本证明。本部署采用上游 SHA 标签发布规则与实际 registry digest 的关联。没有使用漂移 latest。

## 启停和健康

在 agent_line 目录运行：

```bash
# 仅全新安装执行一次；已有实例不要执行 init
# 首次 init 使用本地固定镜像；代理回退见后文
docker pull pgvector/pgvector@sha256:feb68f4f15446397d8cac7f4fe48fe4586de83160d1fc48b46283312d1a33966
./deploy/sim/run.sh init
# 以下为日常操作
./deploy/sim/run.sh config --quiet
./deploy/sim/run.sh up -d
python3 deploy/sim/health.py
./deploy/sim/run.sh ps -a
./deploy/sim/run.sh stop
```

`init` 只接受整个 `var/sim` 不存在且没有本 Compose 项目容器的新安装。它生成私有环境文件及数据目录，再复用现有 `db` 服务的临时实例执行官方 PostgreSQL 首次初始化；确认正式数据库进程就绪、停止并移除该实例后才返回成功。Redis 与 uploads 仍由原服务在首次启动时准备。Docker 必须可用，固定 PostgreSQL 镜像须预先拉取或导入；初始化使用 `--pull never`，不会在失败后自动重试、重置秘密或删除部分数据。已有目录或半完成初始化需要核对恢复，不能重新运行 `init` 修复。

日常 `run.sh` 和默认 `python3 deploy/sim/prepare.py` 只读检查已有私有环境文件及必需秘密，缺失或权限异常时拒绝，不自动生成文件或 chmod。启动类命令还检查三个原数据目录与恢复隔离标记；缺目录、符号链接或 `RESTORE-DO-NOT-START.json` 会在调用 Docker 前阻止启动。所有持久数据绑定及可选 cron 绑定都禁止 Docker 自动补建源目录。命令须放在参数首位，例如 `run.sh up -d`；不支持用全局 Compose 参数前置隐藏命令。

正常 `db` 容器的入口还会在官方 entrypoint 之前检查原 `PG_VERSION`、`global/pg_control`、`base` 与 `pg_wal`，拒绝缺失、空控制文件及异常链接；这也覆盖 Docker 自动重启。守卫只读挂载，缺脚本时 Docker 不能补建源路径。普通启动绝不调用首次初始化入口。初始化持有原目录的文件锁，且启动预检会拒绝残留的数据库初始化实例；原初始化进程退出或观察失败不能成为并发打开同一数据目录的理由。

这些检查仍不能鉴定任意被替换的完整集群、证明磁盘持久可写或覆盖全部组件，完整 AC-29 保持待验收。不增加安装身份清单、状态机或额外常驻服务；首次数据库初始化复用原服务的一次性实例，结束即清理。`config`、`ps`、`stop` 仍可检查或停止存在恢复标记的安装，但不会修复缺失环境文件。

不要执行未过滤的 `docker compose config` 或 `docker inspect` 并把结果公开，它们会展开秘密。`config --quiet` 只校验。

应用仅绑定 `127.0.0.1:3190`，realtime 仅绑定 `127.0.0.1:3192`；检查时这两个端口空闲。Postgres/Redis 不发布宿主端口。最终局域网入口由统一网关提供，接入网关时配置 `SIM_PUBLIC_URL`、`SIM_SOCKET_URL` 和必要的 `SIM_TRUSTED_ORIGINS`，然后仅重建本项目应用与 realtime 服务。

启动顺序包含数据库迁移和 uploads 目录所有权初始化两个一次性容器，成功状态是退出码 0，不是 running。不要用一次性容器的停止状态误判部署失败；`health.py` 分别检查常驻/一次性容器与真实 HTTP，并写 `var/sim/health.json`。它还在已迁移的 `workflow` 表执行零行 UPDATE 并回滚，拒绝只读事务、缺表或权限错误，不改变业务行；连接、语句、锁与外层命令均有超时，失败只报告固定状态，不输出数据库错误正文。这是事务写准入检查，不是数据页落盘或未来可写保证，也不替代 Docker 的基础就绪检查。

## 数据与秘密

- PostgreSQL：`var/sim/postgres`。
- Redis AOF：`var/sim/redis`。
- 上传文件：`var/sim/uploads`，初始化容器只修改该目录所有权为 Sim 镜像 UID/GID 1001。
- 秘密和环境：`var/sim/sim.env`，由本机 CSPRNG 生成，非示例口令。
- 所有上述路径均在根 `.gitignore` 已有 `/var/` 下。

升级前须备份 PostgreSQL（优先逻辑转储）、上传目录与原秘密。不得删除 ENCRYPTION_KEY 或重置秘密代替升级。数据库迁移后的降级不能只改镜像标签；必须根据迁移兼容性恢复相应备份并核对执行状态。

## 此机器 Docker Hub 下载回退

Docker daemon 直连 Docker Hub 超时，而本机 shell 的 HTTPS 代理可访问。提供 `pull-via-proxy.py`，不修改 daemon 或系统代理：使用已固定 manifest digest 下载，校验 manifest、config 与每层 SHA256，再通过 docker load 导入。只处理本部署显式允许且固定 digest 的公共镜像。

```bash
python3 deploy/sim/pull-via-proxy.py postgres
python3 deploy/sim/pull-via-proxy.py redis
```

导入标签分别是 `agent-line/postgres:sha256-feb68f4f15446397` 和 `agent-line/redis:sha256-7aec734b2bb298a1`。全新安装使用代理导入时，通过 `SIM_POSTGRES_IMAGE=agent-line/postgres:sha256-feb68f4f15446397 ./deploy/sim/run.sh init` 指定本地固定镜像；成功后将该值和 Redis 标签写入生成的私有 env 的 `SIM_POSTGRES_IMAGE` 与 `SIM_REDIS_IMAGE`，再普通启动。本机已经完成此配置。导入后 config/image ID 分别为 `sha256:bdb5945cfea65945c40134bc74e11d59d404cb054aa4e3fd2c392d8097545f4f`、`sha256:9210b8dc25f122eb00e5572dcc7147c8e11fb1a08308b088e06c9d5dd2aa49d6`。

全新代理下载缓存位于 `var/image-cache/sim`，避免预拉镜像时提前创建安装目录而阻止显式初装；已经存在的 `var/sim/image-cache` 继续原地复用，不搬迁或删除。验证过的内容可复用；凭据 token 不写入日志或文件。脚本还允许 `simstudio`、`realtime`、`migrations` 这三个固定 digest 的 GHCR 镜像；本次它们最终均通过 Docker 原生拉取成功，未启用其本地导入覆盖。部署健康不代表工作流完整验收通过，G0 场景仍须依据 `g0-sim.md` 逐项运行。

## 本次执行状态

2026-10-21 18:35（北京时间）已完成整栈真实健康验证：`var/sim/health.json` 中全部九项检查通过，`healthy:true`。Postgres、Redis、realtime、应用为 healthy，cron 为 running，migrations 与 uploads-init 均退出 0。应用根页面与 realtime `/health` 均返回 HTTP 200。未认证访问 `/api/v2/workflows` 返回 401。

额外核查：应用进程 UID 1001，实际 cwd `/app/apps/sim`，持久 uploads 挂载可写；应用端口只绑定 127.0.0.1，数据库和 Redis 的 PortBindings 均为空；上游 `git status --short` 无输出。`prepare.py` 重跑前后私有 env 哈希不变，权限始终 0600；Python 编译与 shell 语法校验通过。

下载进程已终止收束：原生 app/realtime/migrations 拉取会话 14217/19201/58598 均退出 0；代理 app 备用会话 75141 因同一镜像已原生拉取成功而停止，退出 143；整栈启动会话 78650 退出 0。常驻工作由 Docker 容器维护，通过 `run.sh ps -a` 查询，不依赖这些已完成的终端会话。

账号、专用验收 workspace/API key 与条件/暂停恢复探针由独立验收切片建立；本部署切片没有创建业务工作流或触发模型任务。此后不会为验证主动重启其他项目服务。
