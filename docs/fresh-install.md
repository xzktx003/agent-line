# 全新安装顺序

适用于独立的全新 Agent Line 安装目录。已有 `.venv`、任务、正式角色或服务应走[升级](upgrade-policy.md)及[备份恢复](installation-backup.md)，不能重跑初始化命令修复。当前主实例不执行下列步骤。

本仓库新增 `deploy/bootstrap.py` 收拢上游和 Python 依赖准备；不宣称它已完成整个产品的一键安装。服务、账户、原生角色、Codex隔离和业务就绪分别处理，避免“安装成功”被当作执行验收成功。

## 1. 准备系统工具及明确安装范围

当前依赖锁只支持已选的 Linux x86_64 / CPython 3.12 目标。需要已有 Python 3.12（含venv/ensurepip）、Git、Docker及Compose；构建官方Console还需满足固定上游package-lock要求的Node/npm。脚本不安装系统包、不修改系统Python或用户全局配置。

Sim使用现有Compose固定digest镜像，不需要在本机安装Bun来运行源码。模型provider、Codex认证/可执行文件、项目代码目录及测试策略需按本次安装明确提供，不自动从其他工具或已有主实例导入。

## 2. 仅查看依赖准备计划

在本仓库根目录：

```bash
python3.12 -m deploy.bootstrap
```

缺省为plan，只读取版本锁与环境是否存在并输出步骤，不执行Git、pip、Docker或模型。`plan`成功不代表安装前提已全部通过；真正安装时还核平台、目录权限和已有上游身份。

## 3. 全新上游与Python环境

```bash
python3.12 -m deploy.bootstrap install
```

行为边界：

- 只接受全新的`.venv`；已有环境即拒绝，不升级、覆盖或删除它。
- 已有上游必须匹配`upstreams.lock.json`的仓库根、origin、完整commit及干净工作树。缺失上游才从既定公开仓库拉取对应commit。已有仓库不fetch/reset，不改分支或工作文件。
- 新建仓库本地环境，按现有`requirements-runtime.lock.txt`安装准确版本；从锁定QwenPaw commit的Git归档副本构建安装，不在上游工作树写build/egg-info。
- 清除继承的全部`PIP_*`、`GIT_*`和影响Python定位/字节码输出的设置，避免外部GIT_DIR等把操作指向其他仓库；明确使用`https://pypi.org/simple`；关闭用户pip配置及交互keyring。当前不提供隐式企业镜像回退，网络不可达就失败，不能偷偷更换包源。
- pip缓存、构建临时目录、字节码、日志和报告位于本仓库私有`var/bootstrap-*`；不把环境变量或原始安装日志打印到终端。代理和系统证书仍使用调用环境的正常网络配置。
- 官方构建工具按上游`pyproject.toml`在隔离构建环境准备。现有依赖锁只有版本、没有制品hash，构建工具也未完整锁定；不能声称完成离线安装或跨平台完全复现。
- 安装最后仅做pip依赖元数据一致性检查，不运行本产品测试、导入宿主启动服务或调用模型。成功报告范围为`python_and_pinned_sources_only`，businessReadiness仍unverified。

若失败，保留本轮私有日志、已拉取源码和未完成环境；不自动重试、清理或回滚。输出的失败报告给出阶段，日志可能含私有网络上下文，不应公开粘贴。已有部分目录需要先核对其归属和状态，不能靠重复install覆盖。源码在安装中变动或锁文件变化会拒绝给出准备完成结论。

## 4. 官方Console与局域网入口

沿[QwenPaw部署](qwenpaw-deployment.md)安装官方Console依赖并构建原生页面。npm缓存也可显式放本仓库：

```bash
npm_config_cache="$PWD/var/npm-cache" npm --prefix upstream/qwenpaw/console ci --no-audit --no-fund
npm_config_cache="$PWD/var/npm-cache" npm --prefix upstream/qwenpaw/console run build
```

统一局域网入口另沿[同源网关](gateway.md)运行`deploy/gateway/build-console.sh`，使用官方basePath构建变量生成`deploy/gateway/qwen-console`。它依赖上一步node_modules，不另装依赖。网关与宿主静态目录不同，两份产物不能混用；不向上游业务源码打补丁。

以上为将来显式执行安装时的构建步骤，本轮按用户要求没有运行Node、构建或测试。

## 5. Sim、QwenPaw与私有数据

按[Sim部署](sim-deployment.md)预先取得固定镜像，只有全新目录且没有本项目容器时执行一次`deploy/sim/run.sh init`，随后按原入口启动。已有目录、恢复保护或部分初始化必须核对，不能重置数据库及密钥。

按[QwenPaw部署](qwenpaw-deployment.md)执行一次`manage.py init`，使用官方插件及独立工作区/秘密目录；只初始化本安装，不收养用户其他Agent和记忆。项目接线前保持宿主停机，避免官方配置与角色创建并发。

## 6. Codex执行镜像与项目接线

准备本安装明确授权的Codex状态目录、二进制及相邻code-mode helper；不修改全局CODEX_HOME或用户认证。执行与独立验收镜像分别明确选择，不能将普通基础镜像有Python就当作全部业务依赖齐全。

修正后的最小执行镜像入口不再依赖开发机残留镜像，也不自动修改正式配置：

```bash
.venv/bin/python deploy/executor/build_image.py \
  --source-image sha256:REPLACE_WITH_64_LOWERCASE_HEX_DIGITS \
  --output var/executor-image-new
```

源镜像必须已在本地，包含Python3.12、`/usr/lib/python3.12`及脚本列出的POSIX/Git工具与动态库；不自动拉镜像或安装工具。输出目录必须全新，父链由当前用户拥有、无符号链接或其他用户写权限；先创建安全的`var`父目录。提取容器无网络、只读根、无capabilities、不挂宿主目录或Docker socket。产物无tag导入，输出完整镜像ID与构建记录，不改`var/executor/config.json`。构建失败保留本轮文件，不自动复用部分archive或移除其他镜像。

把该ID及业务测试镜像、provider/model、项目目录等填入本次私有spec，按[单项目接线](single-project-setup.md)依次`check`、`install`；检查成功与配置完成不代表真实任务通过。项目接线负责生成本安装的执行/适配配置，不手动复制主实例已有秘密与账本。

## 7. 启动与验收边界

由既有`manage.py start`、`agent_line.runtime serve`和`deploy/gateway/run.sh`启动本安装，地址由实际监听和配置决定，不照抄文档中的旧PID/端口。匿名首页只读任务服务列表；敏感详情和操作沿原权限。

执行就绪必须有当前真实任务/独立验收/原生身份与资源收尾证据，不能伪造readinessFile、复用另一安装证明或用HTTP健康替代。按用户最新要求，本轮未执行这些测试或探针；当前新增脚本只有源码及AST审阅，完整从零安装仍待实际验收。
