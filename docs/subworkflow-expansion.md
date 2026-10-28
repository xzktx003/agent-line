# 固定子流程编译展开预览

本增量把已核验版本展开为父图中的实际节点与边，并编译为Sim函数入口/出口。后续已接[单层受控保存/发布与父运行预算](subworkflow-admission.md)，须满足真实来源回执、角色策略和项目预算准入；预览接口本身仍只读。完整嵌套、界面配置及真实执行验收仍未完成，AC-33保持未验证。下文“尚未开放”记录的是最初预览批次的历史边界。

后续已补可选[声明式输入/返回映射](subworkflow-mapping.md)，配置和固定代码生成已接预览/marker重建；尚无对应编辑器表单或实际执行验收。下文整对象语义是省略映射时的行为。

只读POST `/extension/v1/authoring/projects/{project}/subworkflows/preview` 接受原resolve的parentWorkflowId/reference，加上expectedParentStateSha256、callId、edgeId、inputFrom。callId为新逻辑出口节点ID，edgeId为已保存父图中要插入子流程的唯一现有边；inputFrom使用原节点输入格式，选择父start.input或前置Agent/join.output。父子所有权与原凭证先核对，父图当前hash须匹配，指定子版本先经read_reference核验；预览结束再次检查父图hash及凭证权限。

build_preview只复制内存文档：原边转入子入口，子内部边和节点按调用ID确定性重命名，唯一子end转为返回出口，再连回原父边目标。子Agent输入、条件来源、循环体、并行/汇合引用均同步改名；原子条件input字段引用改为本次子入口的input，不误读父start输入。不会自动修改父后续节点的业务输入选择。

subflow_input编译为固定function返回{input:原明确来源}，subflow_output返回{output:原end响应对象}。resultFrom出口保留agentLineResult包装字段，literal出口保留原JSON对象，不伪造执行/独立验收成功。子出口不再使用response提前结束父图，整个图仍由Sim推进，没有新调度器、正式角色、记忆后端或子run。

入口AL_SUBFLOW_INPUT_V1 marker保存精确reference、原sourceDocument、callId、inputFrom；出口AL_SUBFLOW_OUTPUT_V1保存对应入口及原结束输出映射。marker基于已支持的function code注释，不依赖未证实会保留的原生data额外字段。decode解析后必须完整重编同一native图。validate_groups按原来源hash重新生成片段，核对全部成员ID/类型/名称/配置及内部边，唯一外部入口/出口且无旁路或重叠；视觉坐标可调整，不能修改子逻辑。配置用规范JSON比较，不让布尔/数字相等混淆来源。

当前限制：仅单层、子图恰好一个end，拒包含包装节点的嵌套来源；batchItem来源节点因原冻结数据项身份尚未接调用路径而明确拒绝，不改旧批量身份。原图200节点/1000边、最多10组静态并行、最多1个loop且不可混用规则保持。来源文档<=256KiB，单个marker编码<=524288字符，编译态<=1,000,000字节；编码界限可能比文档原始字节限制更早触发。无任意JS或动态表达式输入。

接口返回source=validated_subworkflow_expansion_preview/readOnly=true/executionSupported=false、父图hash、精确子引用、展开后文档和图摘要/数量。不调用Sim保存/部署、任务提交、模型、记忆或journal写入；原save/publish以及可移植模板导入/导出遇包装节点均拒绝，防止预览内容变为未准入执行或错误跨安装模板。编译器不承担实际项目授权；将来开放保存/发布前必须在服务端重核所有来源与预算，而不能只信marker内自述。

根与指定gpt-6-luna完成源码审查和三个Python AST解析。修正了入口/出口ID混淆、条件入口未映射、源编译前递归拒绝、batch身份限制与布尔数字比较问题，并保留旧human.resumeInput引用。未运行测试、Node、HTTP、浏览器、模型探针、lint/typecheck/build；未取得真实Sim状态保存/归一化、执行或恢复证据，不提升完整PRD或AC。
