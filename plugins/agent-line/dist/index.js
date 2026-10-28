// Official QwenPaw host React/router/menu APIs; upstream editor remains untouched.
const sdk = window.QwenPaw;
if (!sdk?.host?.React || !sdk.route?.add || !sdk.menu?.add) {
  throw new Error('Agent Line：宿主缺少必要的官方插件接口');
}
const React = sdk.host.React;
const h = React.createElement;
function navigationPaths(config) {
  if (!config || config.workbenchPath !== '/workbench/' || config.simEditorPath !== '/workspace') {
    throw new Error('网关页面配置不受支持，请核对网关版本后重试。');
  }
  return {workbench: config.workbenchPath, sim: config.simEditorPath};
}
function AgentLinePage() {
  const [paths, setPaths] = React.useState(null);
  const [status, setStatus] = React.useState('正在读取工作台入口…');
  const [error, setError] = React.useState(false);
  const [attempt, setAttempt] = React.useState(0);
  const [view, setView] = React.useState('workbench');
  const [revision, setRevision] = React.useState(0);
  React.useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    let active = true;
    setPaths(null); setError(false); setStatus('正在读取工作台入口…');
    fetch('/agent-line-gateway/config', {signal: controller.signal, credentials: 'same-origin'})
      .then(async response => {
        if (!response.ok) throw new Error('无法读取入口配置，请确认正在使用统一网关的 QwenPaw 页面后重试。');
        let config;
        try { config = await response.json(); }
        catch { throw new Error('网关未返回有效页面配置，请核对网关版本后重试。'); }
        const loaded = navigationPaths(config);
        if (!active) return;
        setPaths(loaded);
        setStatus('无需手动连接，任务与服务列表在下方显示。');
      })
      .catch(failure => {
        if (!active) return;
        setError(true);
        setStatus(failure.name === 'AbortError' ? '读取入口配置超时，请检查网关连接后重试。'
          : failure instanceof TypeError ? '无法连接网关，请检查网络后重试。' : failure.message);
      })
      .finally(() => clearTimeout(timer));
    return () => {active = false; clearTimeout(timer); controller.abort();};
  }, [attempt]);
  function confirmDiscard() {
    return !view || window.confirm('此操作会关闭当前页面，清除页面中的临时凭证和未保存内容。已提交的任务仍继续运行。是否继续？');
  }
  function open(next) {
    if (!paths || next === view || !confirmDiscard()) return;
    setView(next); setRevision(value => value + 1);
  }
  const controlStyle = {minHeight:'44px',padding:'8px 14px',font:'inherit',cursor:'pointer'};
  const title = view === 'sim' ? 'Sim 原版工作流编辑器' : 'Agent Line 中文工作台';
  return h('main', {style: {padding:'16px', minHeight:'100%', minWidth:0, display:'flex', flexDirection:'column', gap:'12px', boxSizing:'border-box',lineHeight:1.6}},
    h('h1', {style:{fontSize:'20px',margin:0}}, '工作流工作台'),
    h('p', {role:'status','aria-live':'polite',style:{margin:0}}, status),
    error ? h('button', {type:'button',style:{...controlStyle,alignSelf:'flex-start'},onClick:()=>setAttempt(value=>value+1)}, '重试读取入口') : null,
    h('p', {style:{margin:0}}, '直接查看已有任务和已配置服务；启动、取消、编辑与审核时，再使用对应页面的操作授权。Agent、会话与记忆由 QwenPaw 管理。'),
    h('div', {style:{display:'flex',flexWrap:'wrap',gap:'10px'},'aria-label':'打开工作流页面'},
      h('button', {type:'button',style:controlStyle,disabled:!paths || view==='workbench',onClick:()=>open('workbench')}, '任务与服务'),
      h('button', {type:'button',style:controlStyle,disabled:!paths || view==='sim',onClick:()=>open('sim')}, '打开原版画布'),
      h('button', {type:'button',style:controlStyle,disabled:!paths,onClick:()=>{if(confirmDiscard())setRevision(value=>value+1);}}, view==='sim' ? '重新加载画布' : '重新加载列表')),
    h('p', {style:{margin:0,fontSize:'14px'}}, view==='sim'
      ? '原版画布部分界面为英文，使用 Sim 独立账号。退出 QwenPaw 不会退出 Sim，请分别退出登录。'
      : '切换或重新加载前，请保存编辑内容。已提交的任务会继续运行。'),
    view && paths ? h('iframe', {key:revision,src:paths[view],title,style:{border:'1px solid #ddd',borderRadius:'8px',width:'100%',height:'75vh',minHeight:'420px',boxSizing:'border-box',flex:'1 0 420px'},referrerPolicy:'same-origin'})
      : h('p', {style:{margin:0}}, error ? '入口读取失败，请重试。' : '正在打开任务与服务列表…'));
}
sdk.route.add('agent-line', {id:'agent-line.home',path:'/agent-line',component:AgentLinePage});
sdk.menu.add('agent-line', {id:'agent-line.entry',label:'工作流工作台',route:'agent-line.home',location:'primary.settings'});
