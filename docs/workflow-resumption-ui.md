# 工作流恢复执行查询

本增量只修改工作台查询展示。父运行 `execution` 原样呈现；`resumeSubmission` 显示最新恢复意图，`resumption` 显示最近已接受恢复的真实子执行。最新意图为 intent、uncertain 或 unknown 时优先显示未知，并将已有子执行明确标记为此前记录，不能由此前成功或失败推断最新结果。

恢复失败、恢复状态未知都有中文提示；保留父运行和子运行标识供核对，无自动 POST、重试按钮或伪造验收结论。没有恢复字段的响应保持原展示。凭证仍只在页面内存，退出清空查询结果，epoch 阻止迟到响应回填。

## 验证计划与边界

先执行 JavaScript 语法检查。后端落地后使用主线提供的只读隔离服务与已存在的真实失败子运行取证；如需导入原回执到独立 journal，必须明确记为历史证据导入，不称新提交。浏览器覆盖父 paused / 子 failed、较新未知意图优先级、无恢复字段兼容、退出清空、桌面和手机布局、CSP 与零写请求。不得新建流程、启动 run 或调用模型。实际结果与截图将在完成后补入。

## 本轮真实浏览器结果

`probes/sim/resumption_browser.py` 已在隔离服务 `127.0.0.1:3196` 完成只读验收。该服务由主线启动；既有执行/恢复回执的证据导入来源与 hash 记录在 `var/resumption-browser/receipt-import.json`，不代表本轮进行了新提交。浏览器使用服务端真实 GET 返回的父 paused、恢复子执行 failed，确认中文失败提示、两个原始运行标识及独立验收边界均可见。

证据位于 `var/resumption-browser/report.json`；桌面 1440×1000 截图为 `desktop-page.png` / `desktop-result.png`，手机 390×844 为 `mobile-page.png` / `mobile-result.png`。手机无整页横向溢出，页面错误和 CSP 错误均为零，localStorage/sessionStorage 为零，退出后凭证与结果清空。所有非 GET/HEAD 请求被探针禁止；本轮实际非读取请求为零，无新流程、运行或模型调用。

附加界面故障注入明确只作为展示测试：在真实响应副本中加入较新的 intent、uncertain、unknown 恢复意图，三者均优先显示未知，并将已有失败子执行标为此前记录；去掉恢复字段后保持旧展示。这些模拟响应不写入服务端，不作为额外原生运行证据。

首轮探针在真实截图完成后的模拟阶段因 Playwright 回调第二参数覆盖默认闭包参数而失败；已仅修复探针为单参数闭包。随后整轮只读复验退出码 0，不涉及写操作重试。`node --check frontend/app.js`、探针 `py_compile` 与 diff 空白检查通过。

主线核对 PID 897022 的命令身份后发送 SIGTERM；原 exec 句柄确认 Uvicorn 完成关闭，进程不存在且 3196 无监听。报告已记录该生命周期结果。对比原工作台与桌面/手机结果截图，主线视觉裁决 93/pass，保存在 `.omx/state/workflow-resumption/ralph-progress.json`；无需进一步界面修改。
