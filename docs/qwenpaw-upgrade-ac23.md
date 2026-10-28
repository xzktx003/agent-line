# AC-23：2.2.3b1 到 2.3.0b1

放开的限制只有插件清单的版本下限，从 `2.3.0b1` 放到 `2.2.3b1`。上限仍是 `2.3.0b2`，不含该版本。执行模型仍是 `gpt-6-luna`。没有放开 `10.40.0.2`、taskField 或 user namespace，也没有重启主数据库、QwenPaw `1916664`、适配器 `1237908` 或网关 `1918031`。

隔离清单先放在 `var/upgrade-plugins/ac23-223b1/agent-line`。正式清单在升级证据成立后改为同一范围。没有手工合并 QwenPaw 源码，依赖仍用现有虚拟环境。

`ac23-223b1-2` 把 `8fd464ae`（2.2.3b1）升到 `502b59d`（2.3.0b1）。角色、原生记忆、已发布模板、完成历史、catalog 绑定和 journal 在新版本上一致，`hostAndExtensionMatch` 为 true。它自己的恢复读取返回 HTTP 422，阶段停在 `upgrade_verified`，`verified` 仍是 false，没有改成通过。

同一份离线旧备份副本 `ac23-223b1-3` 上，早前读取也遇到过 422，随后连续 GET 返回 200。2026-10-27 的只读复查第一次 inspect 就是 200，宿主和扩展都与保存的旧观察一致，测试宿主已退出。证据是 `var/upgrade-runtime/ac23-restore-comparison.json`。固定的外部 Sim 没有回滚，也没有做模型、Codex 或九项兼容探针。

因此 AC-23 改为 passed。R-07 映射的 AC-01、AC-23、AC-24、AC-25、AC-68 都已通过，R-07 改为 passed。AC-29 和 R-14 仍是 in_progress。计数为 passed 49 / in_progress 1 / unverified 5。

正式插件文件已改。正在运行的 QwenPaw 仍加载修改前的清单；包含 `plugin.json` 哈希的就绪证明会变为过期，新的证明要等该宿主在这次写入之后重启才能建立。这次没有重启。
