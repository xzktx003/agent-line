'use strict';
(function (scope) {
  // Portable draft recipes only. No paths, credentials, real role IDs,
  // native workflow identities, prior results or publication identity.
  const slotNames = {planner:'规划角色', developer:'开发角色', reviewer:'审查角色'};
  const template = (id, name, description, category, slots, inputContract, outputContract, completionCriteria, stages) => ({
    id, name, description, category,
    slots:[...new Set(slots)].map(id=>({id,name:slotNames[id]})),
    inputContract, outputContract, completionCriteria, stages,
  });
  const definitions = [
    template('workflow-draft-v1','自然语言流程草稿','将开始输入中的自然语言需求整理为可导入的流程 JSON 草稿；仅使用经过核对的只读规划角色。','设计',['planner'],'提供要设计的流程需求、约束和预期行为。','输出 workflow-draft-v1 格式的可移植流程模板，供人工核对后导入。','结构核对不代替人工确认；不会自动保存或发布，也不会执行新流程。',['workflow-draft']),
    template('delivery-review-v1','需求—实现—测试—审查—人工验收','三个长期角色分别分析、实现、审查；各节点使用当前配置的独立验收，最后由人工决定。验收未通过进入失败出口。','交付',['planner','developer','reviewer'],'提供需求原文、项目约束与预期行为。','输出需求分析、实现结果、独立验收结果及审查意见。','规划、实现、审查各自真实验收通过，且人工明确批准；否则有失败或退回结果。','legacy'),
    template('bounded-repair-v1','实现—测试—有限修复','复用一个开发角色，根据本轮真实测试结果最多执行三次；通过后等待人工验收，耗尽后进入失败出口。','修复',['developer'],'提供需求、复现步骤或当前失败结果。','输出最多三轮的实现与验收结果。','独立验收真实通过且人工明确批准；失败或三轮耗尽进入失败出口。','legacy'),
    template('feature-delivery-v1','新功能需求交付','从验收边界规划到受控实现、独立审查和人工验收，适用于新增用户可见能力。','交付',['planner','developer','reviewer'],'提供用户目标、行为示例、约束和验收条件。','输出方案、实现摘要、验收证据和审查意见。','规划、实现、审查均通过真实验收且人工批准；失败、拒绝均有终态。',['plan','develop','review','approve']),
    template('bug-fix-v1','缺陷定位与修复','保留复现信息，先规划定位方法，再由开发角色修复并由独立角色审查。','修复',['planner','developer','reviewer'],'提供症状、复现步骤、期望与实际行为。','输出根因假设、修复摘要、验收证据和审查意见。','每个执行阶段真实验收通过且人工批准；失败或拒绝进入对应终态。',['plan','develop','review','approve']),
    template('incident-diagnosis-v1','故障只读诊断','适用于线上或环境故障的证据整理；规划和复核均只读，不触发修复。','诊断',['planner','reviewer'],'提供故障现象、时间范围及已获准的日志/指标摘录。','输出证据、影响范围、假设、排查建议和只读复核意见。','规划与复核的真实验收通过并经人工确认；任何写操作均不在本流程内。',['plan-read','review-read','approve']),
    template('dependency-upgrade-v1','依赖升级评估与实施','先评估版本差异和风险，人工批准后才允许固定开发角色改动，再独立审查。','依赖',['planner','developer','reviewer'],'提供目标依赖、目标版本、兼容约束和已有证据。','输出升级方案、实现摘要、验证证据与审查意见，不执行发布。','前置人工批准、各阶段真实验收及最终人工批准齐备；拒绝或失败有出口。',['plan','approve','develop','review','approve']),
    template('security-review-v1','安全只读审查','固定规划和审查角色只读检查给定材料，整理可复核发现，不实施修复。','审查',['planner','reviewer'],'提供待审查变更、威胁边界和已授权的材料。','输出按风险排序的发现、证据位置和建议。','规划与审查真实验收通过，人工确认审查记录；不得产生代码写入。',['plan-read','review-read','approve']),
    template('performance-optimization-v1','性能优化提案与受控实施','先记录基线并提出优化计划；人工同意后才进入开发和独立审查。','优化',['planner','developer','reviewer'],'提供性能目标、场景、指标定义及已有基线。','输出基线与计划、实现和对比证据、独立审查意见。','人工前置同意及各阶段真实验收通过，最终人工批准；失败/拒绝显式结束。',['plan-read','approve','develop','review','approve']),
    template('docs-update-v1','文档更新','适用于按既定行为更新使用说明、接口文档或运维说明，并由独立角色核对。','文档',['developer','reviewer'],'提供文档主题、变更事实、目标读者和格式要求。','输出文档变更摘要及事实/链接核对意见。','文档写作由固定开发角色完成，真实验收与只读审查通过后人工确认。',['docs','review-read','approve']),
    template('api-contract-change-v1','接口契约变更','先梳理兼容影响并由人工批准契约方向，再实施和审查接口变更。','接口',['planner','developer','reviewer'],'提供现有契约、拟议输入输出及兼容性要求。','输出影响分析、拟定契约、实现和审查摘要。','契约前置批准、实施与审查真实验收、最终人工批准；失败拒绝均终止。',['plan-read','approve','develop','review','approve']),
    template('reviewed-repair-v1','有限修复并独立复审','开发角色最多依据三轮真实验收结果修复；通过后交给独立审查角色。','修复',['developer','reviewer'],'提供变更目标、失败证据及修复边界。','输出有限修复结果、独立审查意见和验收证据。','修复循环真实通过且独立审查通过并经人工批准；耗尽或拒绝有终态。',['repair3','review','approve']),
    template('codebase-onboarding-v1','代码库只读梳理','面向接手项目的只读结构、入口、约定和风险梳理，最后由人工确认资料范围。','梳理',['planner'],'提供项目背景、关注领域和已授权的只读材料。','输出入口地图、关键流程、约定、风险和待确认问题。','只读规划真实验收通过并由人工确认；不触发修改任务。',['plan-read','approve']),
    template('migration-review-v1','迁移方案审核','仅审核迁移步骤、回滚边界和风险；不生成或应用迁移。','迁移',['planner','reviewer'],'提供迁移目标、现状说明、候选方案和风险约束。','输出只读方案评估、风险清单和人工决策材料。','规划和独立审核真实验收通过，人工确认方案意见；流程不执行迁移。',['plan-read','review-read','approve']),
    template('migration-implementation-v1','迁移脚本编写草稿','人工批准方案后，由固定开发角色编写迁移脚本草稿并独立审查；不应用迁移。','迁移',['planner','developer','reviewer'],'提供获准的迁移目标、约束、回滚要求和非敏感结构说明。','输出计划、脚本变更摘要、静态/独立审查证据；不运行迁移。','人工前置批准、各阶段真实验收及最终人工批准齐备；拒绝/失败有出口。',['plan-read','approve','develop','review','approve']),
    template('dependency-investigation-v1','依赖故障排查','针对依赖冲突、漏洞告警或行为变化整理证据和候选处置，不自动升级。','依赖',['planner'],'提供依赖名称、观察到的异常、版本信息和已授权证据。','输出依赖关系线索、根因假设、影响与处置选项。','只读规划真实验收通过并经人工确认；不改清单或锁文件。',['plan-read','approve']),
    template('release-readiness-v1','发布前就绪核对','审查角色只核对给定发布证据与清单，人工作最终判断；不发布。','发布准备',['reviewer'],'提供候选版本说明、验收结果和发布检查清单。','输出缺项、风险、证据状态与就绪建议，不创建发布身份。','只读核对真实验收通过且人工确认；任何发布动作均不发生。',['review-read','approve']),
    template('changelog-v1','变更日志草稿','开发角色依据提供的已确认变更记录写日志草稿，人工核对后结束。','文档',['developer'],'提供本次已确认变更、面向用户的影响和目标版本占位说明。','输出可编辑的变更日志草稿及人工确认结果。','固定角色写作真实验收通过并人工批准；不创建版本或发布。',['changelog','approve']),
    template('compatibility-fix-v1','兼容性修复','规划与只读审查识别影响后由人工授权开发角色修复，再由人工验收。','兼容性',['planner','reviewer','developer'],'提供受影响版本/环境、兼容目标及复现证据。','输出影响分析、审查意见、修复摘要及验证证据。','只读分析和审查通过、前置批准、实施验收及最终人工批准齐备。',['plan-read','review-read','approve','develop','approve']),
    template('safe-refactor-v1','安全重构','规划范围和行为边界经人工批准后，开发角色最多三轮重构并交独立审查。','重构',['planner','developer','reviewer'],'提供目标模块逻辑名称、要简化的问题及必须保持的行为。','输出范围计划、重构摘要、真实验收和独立审查意见。','前置批准、有限循环验收、独立审查和最终批准均通过；拒绝/失败有终态。',['plan-read','approve','refactor3','review','approve']),
    template('decision-record-v1','架构决策记录草稿','规划角色整理选项和取舍，固定开发角色只写决策记录文档，交由人工确认。','决策记录',['planner','developer'],'提供决策问题、约束、候选方案、证据和决策人待定信息。','输出包含背景、选项、取舍和待决事项的决策记录草稿。','分析和文档阶段真实验收通过且人工确认；不代表正式架构批准。',['plan-read','adr','approve']),
  ];
  const recipes = new Map(definitions.filter(item=>item.stages !== 'legacy').map(item=>[item.id,item.stages]));
  function catalog() {
    return definitions.map(({id,name,description,category,slots,inputContract,outputContract,completionCriteria}) =>
      structuredClone({id,name,description,category,slots,inputContract,outputContract,completionCriteria}));
  }
  function instantiate(templateId, mapping, roles, prefix) {
    const selectedTemplate = definitions.find(item => item.id === templateId);
    if (!selectedTemplate) throw new Error('此模板不可用，请重新选择。');
    if (!/^[A-Za-z][A-Za-z0-9_]{0,39}$/.test(prefix)) throw new Error('模板草稿标识无法核验。');
    if (!mapping || typeof mapping !== 'object' || Array.isArray(mapping)
        || Object.keys(mapping).sort().join(',') !== selectedTemplate.slots.map(s=>s.id).sort().join(',')) {
      throw new Error('请为模板的每个角色选择一个当前项目绑定。');
    }
    if (!Array.isArray(roles)) throw new Error('请先读取当前项目的角色绑定。');
    const selected = selectedTemplate.slots.map(slot => {
      const matches = roles.filter(role => role && role.roleBindingId === mapping[slot.id]);
      if (templateId === 'workflow-draft-v1') {
        const role = matches[0], details = role?.details, execution = details?.execution;
        if (matches.length !== 1 || typeof role.roleAgentId !== 'string' || !role.roleAgentId || !details
            || details.taskRole !== 'planner' || details.access !== 'readonly'
            || execution?.source !== 'native_binding_descriptor_at_startup'
            || execution?.sandbox !== 'read-only' || execution?.approvalPolicy !== 'never') {
          throw new Error('自然语言流程草稿仅可使用已配置的规划角色，且须明确为只读访问、只读沙箱和不请求命令审批；角色元数据缺失或策略不符，请核对项目配置。');
        }
        return role;
      }
      if (matches.length !== 1 || typeof matches[0].roleAgentId !== 'string' || !matches[0].roleAgentId
          || matches[0].memoryPolicyId !== 'native-role' || matches[0].outputSchemaId !== 'task-result') {
        throw new Error('角色绑定已失效或不属于当前列表，请刷新并重新选择。');
      }
      return matches[0];
    });
    if (new Set(selected.map(r=>r.roleBindingId)).size !== selected.length
        || new Set(selected.map(r=>r.roleAgentId)).size !== selected.length) {
      throw new Error('分析、开发和审查需要不同的正式角色，不能重复选择同一角色。');
    }
    const nodes = [], edges = [], id = name => prefix + '_' + name;
    if (templateId === 'workflow-draft-v1') {
      const input = '仅依据开始节点提供的原始自然语言需求设计可导入的流程 JSON 模板。不得修改项目文件、执行方案、保存或发布流程，也不得发起新流程。具体 portable JSON 契约由服务端执行指令提供；严格按该契约输出，不要自行扩展。结构核对不代表人工确认。';
      node('start','start','开始',50,110,{});
      node('draft','agent','生成流程草稿',280,110,{
        roleBindingId:mapping.planner,instruction:input,inputFrom:{nodeId:id('start'),field:'input'},
        memoryPolicyId:'native-role',outputSchemaId:'workflow-draft-v1',requireMemoryCommit:false,allowFailureBranch:false
      });
      node('end','end','结束',520,110,{resultFrom:id('draft')});
      edge('start','draft'); edge('draft','end');
      return {schemaVersion:1,nodes,edges,variables:{}};
    }
    function node(key, type, name, x, y, config) { nodes.push({id:id(key),type,name,position:{x,y},config}); }
    function edge(source,target,branch) { edges.push({id:id('edge_'+edges.length),source:id(source),target:id(target),...(branch?{branch}:{})}); }
    function agent(key,name,slot,source,instruction,x,y) {
      node(key,'agent',name,x,y,{roleBindingId:mapping[slot],instruction,
        inputFrom:{nodeId:id(source),field:source==='start'?'input':'output'},memoryPolicyId:'native-role',
        outputSchemaId:'task-result',requireMemoryCommit:false,allowFailureBranch:true});
    }
    function gate(key,name,source,x,y) {
      node(key,'condition',name,x,y,{rules:[{id:id('rule_'+key),source:id(source),field:'result.verificationStatus',operator:'eq',value:'passed'}]});
    }
    node('start','start','开始',50,70,{});
    if (recipes.has(templateId)) {
      const stages=recipes.get(templateId), previousFor=[];
      const instructions={
        plan:'依据开始输入和获授权的原生角色经验整理需求、方案、风险与约束。仅只读分析，不修改文件；若后端无法保证本角色只读，拒绝执行。',
        'plan-read':'仅基于获准材料和本角色获授权的原生经验进行只读分析；不得修改文件。若后端无法保证本角色只读，拒绝执行。',
        review:'结合本角色获授权的原生经验，只读审查前序结果及本轮验收证据；报告发现和理由，不修改代码。若后端无法保证只读，拒绝执行。',
        'review-read':'只读核对前序材料和本轮验收证据，报告发现、证据和理由，不修复、不应用、不发布。若后端无法保证只读，拒绝执行。',
        develop:'仅依据前序方案和本次需求实施获准代码变更；将写入任务委派给固定Codex开发角色，不扩大范围、不应用数据库迁移、不提交、合并或发布。若后端无法保证固定角色委派和权限边界，拒绝执行。',
        docs:'依据已确认事实更新文档草稿；如需代码写入，必须委派固定Codex开发角色。不得改代码、发布或生成凭据。若后端无法保证该边界，拒绝执行。',
        refactor:'在批准范围内委派固定Codex开发角色执行行为保持的重构；仅按每轮真实验收反馈修复，最多三次。不应用迁移、提交或发布。权限无法保证时拒绝执行。',
        repair:'委派固定Codex开发角色按输入或前轮真实失败证据实现/修复；最多三轮，不修改可信验收策略、不应用迁移、不提交或发布。权限无法保证时拒绝执行。',
        changelog:'仅依据已确认变更委派固定Codex开发角色撰写变更日志草稿，不创建版本、不发布。权限无法保证时拒绝执行。',
        adr:'仅将前序分析整理为决策记录草稿，委派固定Codex开发角色写文档；不代表正式批准。权限无法保证时拒绝执行。'
      };
      let lastAgent='';
      stages.forEach((stage,index)=>{
        const y=80+index*190;
        const stageLabel='阶段'+(index+1);
        if(stage==='approve'){
          const human='human_'+index,decision='decision_'+index;
          node(human,'human','人工确认'+stageLabel,740,y,{requiredBoolean:'approved'});
          node(decision,'condition','确认决策'+stageLabel,970,y,{rules:[{id:id('rule_'+decision),source:id(human),field:'result.approval.approved',operator:'eq',value:true}]});
          edge(human,decision); previousFor[index]=decision; return;
        }
        if(stage==='repair3'||stage==='refactor3'){
          const loop='loop_'+index,body='work_'+index,check='verify_'+index;
          node(loop,'loop',(stage==='repair3'?'最多三轮修复':'最多三轮重构')+stageLabel,280,y,{body:id(body),maxIterations:3});
          if(index===0) edge('start',loop);
          agent(body,(stage==='repair3'?'实现与有限修复':'有限重构')+stageLabel,'developer',lastAgent||'start',instructions[stage==='repair3'?'repair':'refactor'],510,y);
          gate(check,'独立验收'+stageLabel,body,740,y);
          edge(loop,body,'body'); edge(loop,check,'done'); edge(check,'failed','else');
          lastAgent=body; previousFor[index]=check; return;
        }
        const slot=stage.startsWith('plan')?'planner':stage.startsWith('review')?'reviewer':'developer';
        const action='work_'+index,check='verify_'+index;
        const labels={plan:'需求规划','plan-read':'只读规划',develop:'受控实现',review:'独立审查','review-read':'只读审查',docs:'文档更新草稿',changelog:'变更日志草稿',adr:'决策记录草稿'};
        agent(action,labels[stage]+stageLabel,slot,lastAgent||'start',instructions[stage],280,y);
        gate(check,'真实验收'+stageLabel,action,510,y);
        if(index===0) edge('start',action);
        edge(action,check); edge(check,'failed','else');
        lastAgent=action; previousFor[index]=check;
      });
      const endY=180+stages.length*190;
      node('success','end','流程完成',510,endY,{resultFrom:id(lastAgent)});
      node('rejected','end','人工拒绝',740,endY,{output:{status:'rejected',businessVerified:false}});
      node('failed','end','验收未通过',970,endY,{output:{status:'verification_failed',businessVerified:false}});
      stages.forEach((stage,index)=>{
        const current=previousFor[index], nextStage=stages[index+1];
        if(stage==='approve'){
          const decision='decision_'+index;
          const next=nextStage ? (nextStage==='approve'?'human_'+(index+1):
            (nextStage==='repair3'||nextStage==='refactor3'?'loop_'+(index+1):'work_'+(index+1))) : 'success';
          edge(current,next,id('rule_'+decision)); edge(current,'rejected','else');
        } else {
          const next=nextStage ? (nextStage==='approve'?'human_'+(index+1):
            (nextStage==='repair3'||nextStage==='refactor3'?'loop_'+(index+1):'work_'+(index+1))) : 'success';
          edge(current,next,id('rule_'+current));
        }
      });
      const context='本模板场景：'+selectedTemplate.description+' 输入契约：'+selectedTemplate.inputContract+' 目标产物：'+selectedTemplate.outputContract+' 完成定义：'+selectedTemplate.completionCriteria+' 保留原始需求、全部约束和前序证据，不得将其改写成已验证事实。';
      nodes.filter(item=>item.type==='agent'&&item.id!==id('start')).forEach(item=>{ item.config.instruction=context+' '+item.config.instruction; });
      return {schemaVersion:1,nodes,edges,variables:{}};
    }
    if (templateId === 'delivery-review-v1') {
      agent('plan','需求分析','planner','start','根据本次开始输入分析需求、项目约定与实现方案。检索本角色获授权的原生经验，保留来源。通过固定Codex角色核对代码和测试入口；仅分析，不修改代码。输出中保留原始需求，附方案、风险及交给开发者的约束，不宣称业务任务已完成。',280,70);
      gate('plan_gate','分析验收','plan',510,70);
      agent('develop','实现任务','developer','plan','依据前序分析结果实现本次需求。召回获授权且仍适用的经验，将完整需求和约束委派给固定Codex角色。在已登记工作区编码；输出保留原始需求、方案、修改及真实测试证据，供后序审查。不自动提交、合并或发布，不修改可信验收策略。',740,70);
      gate('tests_gate','独立测试','develop',740,290);
      agent('review','代码审查','reviewer','develop','审查前序实现结果、变更及本轮测试证据，结合本角色获授权的原生经验。通过固定Codex角色进行只读核查，报告缺陷、风险与理由；不修改代码，不批准权限或代替用户人工验收。',510,290);
      gate('review_gate','审查验收','review',280,290); node('human','human','人工验收',50,290,{requiredBoolean:'approved'});
      edge('start','plan'); edge('plan','plan_gate'); edge('plan_gate','develop',id('rule_plan_gate')); edge('plan_gate','failed','else');
      edge('develop','tests_gate'); edge('tests_gate','review',id('rule_tests_gate')); edge('tests_gate','failed','else');
      edge('review','review_gate'); edge('review_gate','human',id('rule_review_gate')); edge('review_gate','failed','else');
    } else {
      node('repair','loop','有限修复',280,70,{body:id('develop'),maxIterations:3});
      agent('develop','实现与修复','developer','start','依据本次开始输入实现需求。若有前一轮失败反馈，仅依据该轮真实错误修复。复用获授权的原生经验，委派固定Codex角色在已登记工作区编码，不修改可信测试策略、不自动提交或发布；独立验收决定是否继续，最多三次。',510,70);
      gate('tests_gate','独立测试','develop',740,70); node('human','human','人工验收',740,290,{requiredBoolean:'approved'});
      edge('start','repair'); edge('repair','develop','body'); edge('repair','tests_gate','done'); edge('tests_gate','human',id('rule_tests_gate')); edge('tests_gate','failed','else');
    }
    node('decision','condition','验收决策',280,510,{rules:[{id:id('rule_approval'),source:id('human'),field:'result.approval.approved',operator:'eq',value:true}]});
    node('success','end','完成',510,510,{resultFrom:id(templateId==='delivery-review-v1'?'review':'develop')});
    node('rejected','end','退回',510,710,{output:{status:'rejected',businessVerified:false}});
    node('failed','end','验收未通过',970,290,{output:{status:'verification_failed',businessVerified:false}});
    edge('human','decision'); edge('decision','success',id('rule_approval')); edge('decision','rejected','else');
    return {schemaVersion:1,nodes,edges,variables:{}};
  }
  const api=Object.freeze({catalog,instantiate});
  if(typeof module!=='undefined'&&module.exports) module.exports=api;
  else scope.AgentLineTemplates=api;
})(globalThis);
