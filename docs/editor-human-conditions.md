# 编辑器人工确认条件来源

条件来源下拉新增所有路径必经的人工确认节点。选择后固定字段 `result.approval.approved`、布尔类型，只允许 eq/ne；true 表示批准、false 表示拒绝。失效来源保留原 ID、提示并拒绝应用/保存。Agent 原字段与开始输入格式不变；结束结果引用仍仅支持 Agent，不接纳人工确认节点。

`probes/sim/editor_human_offline.py` 完成桌面 1440×1000、手机 390×844 离线浏览器验证。所有 HTTP 被固定 fixture 拦截，不启动服务、不真实发布、不调用模型。验证固定字段、比较方式/类型限制、模拟保存 source/false 保留、End 排除人工来源、删除来源后的 ID 保留与拒绝应用、退出清空、storage0、CSP/pageerror0、手机无整页溢出。模拟保存次数为1，真实HTTP为0。

首轮测试使用 Playwright 对 option 的 is_disabled 语义断言失败，改为读取真实 DOM disabled 属性后通过；这是探针断言调整，非产品逻辑放宽。随后补齐 fixture 的角色列表 source 与保存 digest，第二次最终截图验证通过。

报告、桌面/手机截图与视觉裁决在 `.omx/state/editor-human-conditions/`。对照 `var/sim-authoring-http/browser/reference-workbench.png`，最终 visual-verdict 为92/pass，共两轮，无新增视觉体系。node --check 与探针 py_compile 通过。该离线结果不替代真实 Sim 人工审批发布/恢复的集成验收。
