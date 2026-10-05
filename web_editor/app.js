const $ = selector => document.querySelector(selector);
const names = {launch:'启动程序',launch_wait:'启动并等待',retry_group:'启动与重试',monitor:'进程监控',sliding_window:'滑动监控组',kill:'清理进程'};
const modeNames = {'':'继承默认',foreground:'前台 · 检测键鼠活动',background:'后台 · 忽略键鼠活动'};
const optionNames = {...names,...modeNames,game:'游戏',script:'脚本',all_dead:'全部稳定退出',script_dead:'仅脚本稳定退出',any_exit:'任意监控进程退出',auto:'自动识别引擎'};
let data, revision, schemas, selected=[], tab='common', operations=[], undo=[], redo=[], rawInitial='', busy=false, noticeTimer, cardHost;
const invalidInputs=new Map(), pendingInputs=new Set(), fieldFlushers=new Map(), advancedOpen=new Map();
function ensureFields() {for(const flush of [...fieldFlushers.values()])flush();if(invalidInputs.size)throw new Error([...invalidInputs.values()][0]);}
const clone = value => structuredClone(value);
const equal = (a,b) => JSON.stringify(a) === JSON.stringify(b);
const field = (key,label,kind='str',def='',choices=[]) => ({key,label,kind,default:def,choices});
const F = field;
function h(tag,className,text) {const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node;}
function button(text,action,className='') {const node=h('button',className,text);node.type='button';node.addEventListener('click',()=>run(action));return node;}
function getAt(value,path) {for(const key of path){if(value==null||!Object.hasOwn(value,key))return undefined;value=value[key];}return value;}
function putAt(value,path,item) {for(const key of path.slice(0,-1)){if(!Object.hasOwn(value,key)||value[key]==null)Object.defineProperty(value,key,{value:{},writable:true,enumerable:true,configurable:true});value=value[key];}Object.defineProperty(value,path.at(-1),{value:item,writable:true,enumerable:true,configurable:true});}
function node() {return getAt(data,selected);}
function kind() {return !selected.length?'global':selected.at(-2)==='queues'?'queue':'task';}
function notify(text,error=false) {clearTimeout(noticeTimer);$('#notice').textContent=text;$('#notice').className=error?'error':'';$('#notice').hidden=false;if(!error)noticeTimer=setTimeout(()=>$('#notice').hidden=true,6500);}
async function run(action) {try{await action();}catch(error){notify(error.message,true);}}
async function api(action,extra={}) {
  const response=await fetch(`/api/${action}`,action==='config'?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision,operations,...extra})});
  const result=await response.json();if(!response.ok)throw new Error(result.error||'配置服务请求失败');return result;
}
function updateStatus() {
  const pending=operations.length || invalidInputs.size || pendingInputs.size || tab==='yaml' && $('#yaml-text') && $('#yaml-text').value!==rawInitial;
  $('#dirty').className=pending?'pending':'';$('#dirty').replaceChildren(h('i'),document.createTextNode(pending?'修改已暂存 · 尚未保存':'全部更改已保存'));
  $('#undo').disabled=!undo.length;$('#redo').disabled=!redo.length;
  for(const id of ['save','validate','reload','add-queue','add-task'])$(`#${id}`).disabled=busy||!data;
}
function remember() {undo.push({data:clone(data),operations:clone(operations),selected:clone(selected)});if(undo.length>80)undo.shift();redo=[];}
function applyLocal(op) {
  if(op.op==='set')putAt(data,op.path,clone(op.value));
  if(op.op==='delete'){const parent=getAt(data,op.path.slice(0,-1));if(Array.isArray(parent))parent.splice(op.path.at(-1),1);else if(parent)delete parent[op.path.at(-1)];}
  if(op.op==='append'){let list=getAt(data,op.path);if(list==null){putAt(data,op.path,[]);list=getAt(data,op.path);}list.push(clone(op.value));}
  if(op.op==='move'){const list=getAt(data,op.path);list.splice(op.to,0,list.splice(op.from,1)[0]);}
}
function commit(ops,next=selected,renderMain=true) {
  if(!ops.length)return;if(renderMain)ensureFields();remember();for(const op of ops){applyLocal(op);operations.push(op);}selected=clone(next);
  if(!node())selected=[];
  if(renderMain)render();else{renderNav();updateHeading();renderMetrics();updateStatus();}
}
function patch(path,value) {return value===undefined?{op:'delete',path}:{op:'set',path,value};}
function fieldDisplay(value,spec) {
  value=value??spec.default;
  if(spec.kind==='bool')return Boolean(value);
  if(['lines','optional_lines'].includes(spec.kind))return Array.isArray(value)?value.join('\n'):String(value||'');
  if(spec.kind==='list')return Array.isArray(value)?value.join(', '):String(value||'');
  return value==null?'':String(value);
}
function parseField(text,spec) {
  if(spec.kind==='bool')return text;
  if(['int','number'].includes(spec.kind)){if(!text.trim())return undefined;const value=Number(text);if(!Number.isFinite(value)||spec.kind==='int'&&!Number.isInteger(value))throw new Error(`${spec.label}：请输入${spec.kind==='int'?'整数':'有限数值'}`);return value;}
  if(['lines','optional_lines'].includes(spec.kind))return text?text.split('\n'):spec.kind==='optional_lines'?undefined:[];
  if(spec.kind==='list')return text.replaceAll('，',',').split(',').map(x=>x.trim()).filter(Boolean);
  if(['name','exe'].includes(spec.key)&&!text.trim())throw new Error(`${spec.label}不能为空`);
  return text.trim()?text:undefined;
}
function makeField(spec,value,onChange) {
  const row=h('div','field-row');const label=h('label','field-label',spec.label);label.append(h('span','field-key',spec.key));
  const id=`field-${crypto.randomUUID()}`;label.htmlFor=id;
  let input;
  if(spec.kind==='bool'){input=h('input','field-input');input.type='checkbox';input.checked=fieldDisplay(value,spec);}
  else if(['lines','optional_lines'].includes(spec.kind)){input=h('textarea','field-input');input.rows=5;input.spellcheck=false;input.value=fieldDisplay(value,spec);}
  else if(spec.choices?.length){input=h('select','field-input');let choices=[...spec.choices];const text=fieldDisplay(value,spec);if(!choices.includes(text))choices.push(text);for(const choice of choices){const option=h('option','',choice===''?'继承默认 / 未设置':optionNames[choice]||choice);option.value=choice;input.append(option);}input.value=text;}
  else {input=h('input','field-input');input.type=spec.secret?'password':'text';input.autocomplete='off';input.value=fieldDisplay(value,spec);input.placeholder=spec.kind==='int'||spec.kind==='number'?'留空继承默认值':'未设置';if(spec.kind==='int'||spec.kind==='number')input.inputMode='decimal';}
  input.id=id;input.name=spec.key;const getter=()=>spec.kind==='bool'?input.checked:input.value;const initial=getter();
  let previous=initial;
  if(onChange){const flush=()=>{try{if(getter()!==previous||invalidInputs.has(id)){const value=parseField(getter(),spec);input.setCustomValidity('');invalidInputs.delete(id);pendingInputs.delete(id);if(getter()!==previous)onChange(value);previous=getter();}updateStatus();}catch(error){input.setCustomValidity(error.message);invalidInputs.set(id,error.message);notify(error.message,true);updateStatus();}};fieldFlushers.set(id,flush);input.addEventListener('input',()=>{if(getter()===previous)pendingInputs.delete(id);else pendingInputs.add(id);updateStatus();});input.addEventListener('change',flush);input.addEventListener('blur',flush);}
  row.append(label);if(spec.kind==='bool'){const wrap=h('div','toggle-label');wrap.append(input,document.createTextNode('启用'));row.append(wrap);}else row.append(input);
  return {row,input,getter,initial};
}
function card(title,hint='') {
  const section=h('article','card'),head=h('div','card-heading'),text=h('div');text.append(h('h2','',title));if(hint)text.append(h('p','',hint));head.append(text);const body=h('div','card-body');section.append(head,body);(cardHost||$('#content')).append(section);return {section,head,body};
}
function formCard(title,hint,path,fields) {
  const {body}=card(title,hint);for(const spec of fields){const full=[...path,spec.key];const control=makeField(spec,getAt(data,full),value=>commit([patch(full,value)],selected,false));body.append(control.row);}
}
function launchSummary(value) {return (value.exe||'未设置程序').split(/[\\/]/).at(-1);}
function matcherSummary(value) {return (value.names||[]).join(', ')||'任意进程名';}
function itemRow(body,title,description,actions,index) {
  const row=h('div','item');if(index!==undefined)row.append(h('span','item-number',String(index+1).padStart(2,'0')));
  const text=h('div','item-content');text.append(h('div','item-title',title));if(description)text.append(h('div','item-description',description));row.append(text);
  const controls=h('div','item-buttons');for(const action of actions)controls.append(button(action.label,action.action,action.danger?'danger':''));row.append(controls);body.append(row);
}
async function editDialog(title,path,fields,hint='') {
  const source=getAt(data,path)||{};if(typeof source!=='object'||Array.isArray(source))throw new Error('此节点必须是 YAML 对象，请在高级 YAML 中修正。');
  $('#dialog-title').textContent=title;$('#dialog-hint').textContent=hint||'仅修改你编辑的字段，其他字段与注释保留。';$('#dialog-fields').replaceChildren();$('#dialog-error').hidden=true;
  const controls=fields.map(spec=>({spec,...makeField(spec,source[spec.key])}));for(const control of controls)$('#dialog-fields').append(control.row);
  $('#dialog-form').onsubmit=event=>{event.preventDefault();try{const ops=[];for(const {spec,getter,initial} of controls)if(getter()!==initial)ops.push(patch([...path,spec.key],parseField(getter(),spec)));commit(ops);$('#editor-dialog').close();}catch(error){$('#dialog-error').textContent=error.message;$('#dialog-error').hidden=false;}};
  $('#editor-dialog').showModal();controls[0]?.input.focus();
}
function objectCard(title,path,fields,hint='') {const {body}=card(title,hint);itemRow(body,title,'独立设置 · 点击编辑查看全部字段',[{label:'编辑设置',action:()=>editDialog(title,path,fields,hint)}]);}
function removeItem(path,title='这项设置') {if(confirm(`删除${title}？保存前不会修改文件。`))commit([{op:'delete',path}],selected);}
function listCard(title,path,fields,template,summary,description,hint='') {
  const {body}=card(title,hint);const items=getAt(data,path)||[];
  items.forEach((value,index)=>{
    const itemPath=[...path,index],actions=[{label:'编辑',action:()=>editDialog(title,itemPath,fields,hint)}];
    if(index>0)actions.push({label:'↑',action:()=>commit([{op:'move',path,from:index,to:index-1}])});
    if(index<items.length-1)actions.push({label:'↓',action:()=>commit([{op:'move',path,from:index,to:index+1}])});
    actions.push({label:'删除',danger:true,action:()=>removeItem(itemPath)});itemRow(body,summary(value),description(value),actions,index);
  });
  if(!items.length)body.append(h('div','empty','尚未设置，按需新增即可'));
  body.append(button('＋ 新增一项',()=>commit([{op:'append',path,value:clone(template)}]),'add-inline'));
}
function matchCard(title,path,hint='') {listCard(title,path,schemas.matcher,{names:['script.exe']},matcherSummary,value=>[
  value.path_contains&&`路径包含 ${value.path_contains}`,value.python_root&&`Python 根目录 ${value.python_root}`,value.exclude_names?.length&&`排除 ${value.exclude_names.join(', ')}`
].filter(Boolean).join(' · ')||'按进程名称匹配',hint||'进程名支持 * 通配符；同一条规则的名称与路径同时匹配，不同规则分别匹配。');}
function renderNav() {
  const host=$('#navigation');host.replaceChildren();const query=$('#search').value.trim().toLowerCase();
  const navButton=(title,path,className='')=>{const b=button('',()=>selectNode(path),`nav-item ${className}${equal(path,selected)?' selected':''}`);b.append(h('span','nav-dot',!path.length?'◇':getAt(data,path)?.enabled===false?'○':'●'),h('span','nav-text',title));return b;};
  host.append(navButton('全局设置',[]));host.append(h('div','nav-label','QUEUES & TASKS'));
  let shown=0;
  (data.queues||[]).forEach((queue,index)=>{
    const path=['queues',index],queueMatch=(queue.name||'').toLowerCase().includes(query);const tasks=[];
    const walk=(values,parent,depth=0)=>values.forEach((task,i)=>{const p=[...parent,i];if(queueMatch||!query||task.name?.toLowerCase().includes(query))tasks.push({task,p,depth});walk(task.monitors||[],[...p,'monitors'],depth+1);});
    walk(queue.tasks||[],[...path,'tasks']);if(query&&!queueMatch&&!tasks.length)return;
    shown++;const b=navButton(queue.name||'未命名队列',path,'queue');b.append(h('span','count',String((queue.tasks||[]).length)));host.append(b);
    for(const {task,p,depth} of tasks)host.append(navButton(`${depth?'↳ ':''}${task.name||'未命名任务'}`,p,depth?'child':'task-link'));
  });
  if(query&&!shown)host.append(h('div','nav-empty','没有找到匹配的队列或任务'));
}
function countTasks(queues) {let count=0;const walk=tasks=>{for(const task of tasks){count++;walk(task.monitors||[]);}};for(const queue of queues)walk(queue.tasks||[]);return count;}
function updateHeading() {
  const value=node(),type=kind();$('#heading').textContent=type==='global'?'全局设置':value?.name||'未命名任务';
  const queue=selected.length?data.queues[selected[1]]:null;
  $('#breadcrumbs').textContent=type==='global'?'配置 / 全局设置':type==='queue'?`配置 / ${queue.name}`:`配置 / ${queue.name} / ${value?.name||'任务'}`;
  $('#subtitle').textContent={global:'为所有任务设置默认运行策略、活动检测与结果通知。',queue:'安排每天的触发时间，按执行顺序管理队列中的任务。',task:'把启动、监控和清理规则拆开编辑，修改时不再面对整份 YAML。'}[type];
}
function renderMetrics() {
  const value=node(),type=kind(),host=$('#metrics');host.replaceChildren();let metrics;
  if(type==='global')metrics=[['调度队列',String(data.queues?.length||0),'个队列','每天按配置时间依次触发'],['任务总数',String(countTasks(data.queues||[])),'个任务','包括嵌套的子监控'],['默认执行模式',modeNames[data.defaults?.execution_mode||'foreground'].split(' · ')[0],'','任务可单独覆盖默认模式']];
  else if(type==='queue')metrics=[['每天触发时间',(value.times||[]).join(' · '),'','24 小时制 · 可设置多个时间'],['执行任务',String(value.tasks?.length||0),'个任务','连续同名并行组同时启动'],['调度状态',value.enabled===false?'已停用':'已启用','','未启用的队列不参与定时调度']];
  else metrics=[['任务类型',names[value.type||'monitor']||value.type,'','使用对应的启动与退出判定'],['执行模式',(modeNames[value.execution_mode||'']||'继承默认').split(' · ')[0],'','后台任务忽略键鼠活动'],['任务状态',value.enabled===false?'已停用':'已启用','',value.parallel?`并行组：${value.parallel}`:'按队列顺序执行']];
  for(const [label,value,unit,note] of metrics){const item=h('div','metric');item.append(h('div','metric-label',label));const v=h('div','metric-value',value);if(unit)v.append(h('small','',unit));item.append(v,h('div','metric-note',note));host.append(item);}
}
function renderActions() {
  const host=$('#node-actions');host.replaceChildren();if(!selected.length)return;
  const listPath=selected.slice(0,-1),index=selected.at(-1),list=getAt(data,listPath);
  for(const [label,delta] of [['↑ 上移',-1],['↓ 下移',1]]){const b=button(label,()=>commit([{op:'move',path:listPath,from:index,to:index+delta}],[...listPath,index+delta]));b.disabled=index+delta<0||index+delta>=list.length;host.append(b);}
  host.append(button('复制',()=>{const copy=clone(node());copy.name=uniqueName(`${copy.name} 副本`,list);commit([{op:'append',path:listPath,value:copy}],[...listPath,list.length]);}));
  host.append(button('删除',()=>{if(confirm(`删除“${node().name}”及其子项？保存前不会修改文件。`))commit([{op:'delete',path:selected}],kind()==='queue'?[]:selected.slice(0,-2));},'danger'));
}
function renderCommon() {
  const value=node(),type=kind(),path=selected;
  if(type==='global'){
    formCard('默认运行策略','任务留空时继承默认值，时间单位均为秒。',['defaults'],[
      F('execution_mode','默认执行模式','str','foreground',['foreground','background']),F('poll_interval','巡检间隔','number',5),F('stable_dead','稳定退出时长','number',20),F('heartbeat','心跳间隔','number',60),F('kill_timeout','清理最长等待','number',600)]);
    formCard('日志保留','设为 0 关闭自动清理。',[],[F('log_retention_days','日志保留天数','int',30)]);
    formCard('活动检测','前台任务检测真实键鼠输入；后台任务忽略输入。',['activity_pause'],[F('enabled','真实输入检测','bool',false),F('idle_resume_seconds','空闲后恢复（秒）','int',300)]);
    formCard('控制热键','暂停、跳过等控制热键在后台任务期间仍然有效。',['activity_pause','control_hotkeys'],[F('pause_resume','暂停 / 恢复','str','Ctrl+Alt+='),F('prev_task','上一个任务','str','Ctrl+Alt+-'),F('next_task','下一个任务','str','Ctrl+Alt+Shift+='),F('reset_progress','进度清零','str','Ctrl+Alt+0')]);
    const secret=F('sendkey','SENDKEY');secret.secret=true;formCard('结果通知','保存后由调度器使用通知设置。',['notify'],[F('enabled','Server酱推送','bool',false),secret]);
  }else if(type==='queue'){
    formCard('队列设置','多个触发时间用逗号分隔，例如 06:10, 14:10, 22:10。',path,[F('name','队列名称'),F('enabled','参与调度','bool',true),F('times','每日触发时间','list',[])]);
    renderQueueTasks(value,path);
  }else{
    formCard('任务属性','任务未单独设置执行模式时继承全局或监控组的设置。',path,[F('name','任务名称'),F('enabled','参与执行','bool',true),F('execution_mode','执行模式','str','',['','foreground','background']),F('parallel','并行组名称')]);
    const type=value.type||'monitor',sets={launch:[F('delay_after','启动后等待（秒）','number')],launch_wait:[F('timeout','总超时（秒）','number')],retry_group:[F('attempts','最多尝试次数','int'),F('attempt_timeout','单次超时（秒）','number'),F('appear_timeout','等待启动时限（秒）','number')],monitor:[F('timeout','总超时（秒）','number'),F('appear_timeout','等待启动时限（秒）','number')],sliding_window:[F('window_size','同时监控数量','int')]};
    formCard('运行参数','数值留空表示继承默认值。',path,[...(sets[type]||[]),F('poll_interval','巡检间隔（秒）','number'),F('stable_dead','稳定退出时长（秒）','number')]);
    if(type==='sliding_window'){const {body}=card('子监控任务','使用左侧导航进入子任务，或点击下方新增。');(value.monitors||[]).forEach((task,i)=>itemRow(body,task.name,'进程监控',[{label:'配置',action:()=>selectNode([...path,'monitors',i])}],i));body.append(button('＋ 新增子监控',addTask,'add-inline'));}
    if(['monitor','retry_group','launch_wait'].includes(type))formCard('模拟器 · ADB','端口可填写 IP:端口；自动检测仅在调度器运行时执行。', [...path,'adb'],[
      F('path','ADB 程序路径'),F('serial','连接地址 / serial'),F('auto_detect','运行时自动获取端口','bool',false),F('mumu_path','MuMu 安装目录'),F('mumu_index','MuMu 实例编号','int'),F('mumu_keep_alive','后台保活兼容','bool',false),F('mumu_bridge','桥接兼容','bool',false),F('timeout','命令超时（秒）','number',15),F('enabled','启用延后后台清理','bool',false),F('startup_stable','启动稳定时长（秒）','number',10),F('packages','待清理包名','list',[])]);
  }
}
function renderQueueTasks(value,path) {const {body}=card('队列执行顺序','选择任务后可配置、复制或调整顺序。');(value.tasks||[]).forEach((task,i)=>itemRow(body,task.name,`${names[task.type]||task.type} · ${task.parallel?`并行组 ${task.parallel}`:'顺序执行'} · ${task.enabled===false?'已停用':'已启用'}`,[{label:'配置',action:()=>selectNode([...path,'tasks',i])}],i));if(!value.tasks?.length)body.append(h('div','empty','队列还没有任务'));body.append(button('＋ 新增任务',addTask,'add-inline'));}
function renderDetails() {
  const value=node(),type=kind(),path=selected,p=(...parts)=>[...path,...parts];
  if(type==='global'){objectCard('鸣潮启动器',['ww_launcher'],schemas.ww,'启动器路径、游戏目录和按钮识别规则。');return;}
  if(type==='queue'){renderQueueTasks(value,path);return;}
  const taskType=value.type||'monitor';
  if(['launch','launch_wait'].includes(taskType))objectCard('启动程序与参数',path,schemas.launch,'参数一行一个，路径内的空格无需额外加引号。game = 游戏，script = 脚本。');
  if(taskType==='retry_group')listCard('启动顺序',p('launch'),[...schemas.launch,F('wait_for_exit','等待此程序退出后启动下一项','bool',false),F('timeout','等待退出超时（秒）','number')],{exe:'C:/path/script.exe',role:'script'},launchSummary,s=>`${s.exe||'未设置程序'} · ${(s.args||[]).length} 个参数${s.wait_for_exit?' · 等待退出':''}`,'按列表顺序启动。参数一行一个；上下箭头调整启动次序。');
  if(['monitor','retry_group'].includes(taskType))objectCard('完成条件',path,[F('success_when','完成条件','str','',taskType==='monitor'?['','all_dead','script_dead']:['','any_exit','all_dead'])],'空白使用任务默认行为；模拟器任务通常需要等待所有脚本稳定退出。');
  const matchSections={retry_group:[['watch','等待与监控进程'],['cleanup','重试清理名单']],monitor:[['kill','失败时清理名单']],launch_wait:[['kill','退出后清理名单']],kill:[['targets','清理目标']]};
  for(const [key,title] of matchSections[taskType]||[])matchCard(title,p(key),taskType==='monitor'?'留空时使用全部监控进程。每条规则可同时限制名称与路径。':'');
  if(taskType==='monitor'){
    const cats=value.categories||{},keys=Object.keys(cats);
    const {body}=card('进程类别','各类别独立累计退出次数；类别模式存在时优先于简单的游戏 / 脚本模式。');
    for(const name of keys)itemRow(body,name,`${cats[name].role||'script'} · 退出阈值 ${cats[name].max_exits??'默认'}`,[{label:'编辑',action:()=>editDialog(`类别 · ${name}`,p('categories',name),[F('role','角色','str','script',['game','script']),F('max_exits','退出次数阈值','int')])},{label:'删除',danger:true,action:()=>removeItem(p('categories',name),`类别“${name}”`)}]);
    if(!keys.length)body.append(h('div','empty','当前使用简单的游戏 / 脚本模式'));
    body.append(button('＋ 新增进程类别',()=>{const name=prompt('类别名称，例如：游戏本体、脚本主体、Python')?.trim();if(!name)return;if(Object.hasOwn(cats,name))throw new Error('类别名称已存在');commit([{op:'set',path:p('categories',name),value:{role:'script',max_exits:3,matchers:[{names:['script.exe']}]}}]);},'add-inline'));
    for(const name of keys)matchCard(`${name} · 匹配规则`,p('categories',name,'matchers'));
    for(const [key,title] of [['game','游戏进程'],['script','脚本进程']])if(!keys.length||value[key]){objectCard(`${title} · 退出阈值`,p(key),[F('max_exits','退出次数阈值','int')]);matchCard(title,p(key,'matchers'));}
  }
  const advanced=h('details','advanced-group'),groupKey=JSON.stringify(selected);advanced.open=advancedOpen.get(groupKey)||false;advanced.addEventListener('toggle',()=>advancedOpen.set(groupKey,advanced.open));advanced.append(h('summary','','更多策略 · 分辨率巡检、补跑与收尾'));cardHost=h('div','advanced-body');advanced.append(cardHost);$('#content').append(advanced);
  if(['monitor','retry_group'].includes(taskType)){
    objectCard('1080p 窗口巡检',p('resolution_check'),schemas.resolution,'异环需关闭启动参数重启；巡检期限最多 180 秒。');
    objectCard('各引擎的分辨率启动参数',p('resolution_check','launch_args'),schemas.resolution_args,'参数一行一个。自动识别模式需填写 unity 和 ue；固定引擎只需填写对应项。');
    if(taskType==='retry_group')matchCard('游戏窗口匹配',p('resolution_check','matchers'));
    objectCard('补跑脚本',p('recovery','script'),schemas.launch,'补跑时使用的脚本程序、目录与参数。');
    objectCard('补跑 Python 目录',p('recovery'),[F('python_paths','Python 根目录 · 一行一个','lines',[])]);
  }
  if(taskType==='retry_group'){
    objectCard('日志错误检测',p('error_log'),[F('path','日志文件路径'),F('pattern','错误正则表达式','str','ERROR'),F('max_restarts','最多错误重启次数','int',10)],'只检查新写入的日志内容，匹配错误后清理并重启脚本。');
    if(value.error_log){const {body}=card('关闭日志错误检测');body.append(button('移除日志检测设置',()=>removeItem(p('error_log'),'日志检测设置'),'danger'));}
  }
  matchCard('打断时最小化窗口',p('minimize_matchers'));
  objectCard('打断与收尾策略',path,[F('pause_kill','暂停时清理进程','bool',['retry_group','monitor','launch_wait'].includes(taskType)),F('master_close_task','关联主控关闭任务'),F('kill_timeout','清理最长等待（秒）','number'),F('linger','失败后后台清场（秒）','number'),F('heartbeat','心跳间隔（秒）','number'),F('launch_interval','启动项间隔（秒）','number')]);
  cardHost=null;
}
async function renderYaml() {
  const current=clone(selected);const hint=h('div','hint-box','只编辑当前节点，避免面对整份配置。这里支持注释和扩展字段；全局节点的队列通过左侧导航管理。');$('#content').append(hint);
  const text=h('textarea','yaml');text.id='yaml-text';text.spellcheck=false;text.setAttribute('aria-label','当前节点 YAML');text.placeholder='正在读取 YAML…';text.disabled=true;$('#content').append(text);
  const bar=h('div','yaml-toolbar');bar.append(h('span','','表单与 YAML 双向同步，保存时自动校验。'),button('应用 YAML 修改',applyRaw,'primary'));$('#content').append(bar);
  const result=await api('node',{path:current});if(tab!=='yaml'||!equal(selected,current))return;text.value=result.text;rawInitial=result.text;text.disabled=false;
  text.addEventListener('input',updateStatus);text.addEventListener('keydown',event=>{if(event.key==='Tab'){event.preventDefault();const a=text.selectionStart,b=text.selectionEnd;text.setRangeText('  ',a,b,'end');updateStatus();}});updateStatus();
}
async function applyRaw() {
  const input=$('#yaml-text');if(tab!=='yaml'||!input||input.disabled||input.value===rawInitial)return;
  const op={op:'yaml',path:clone(selected),text:input.value},result=await api('preview',{operations:[...operations,op]});remember();data=result.data;operations.push(op);rawInitial=input.value;renderNav();updateHeading();renderMetrics();updateStatus();notify('YAML 已应用到当前配置，点击保存后写入文件。');
}
function render() {
  if(!data)return;invalidInputs.clear();pendingInputs.clear();fieldFlushers.clear();cardHost=null;renderNav();updateHeading();renderMetrics();renderActions();$('#content').replaceChildren();
  for(const button of document.querySelectorAll('[data-tab]')){button.classList.toggle('selected',button.dataset.tab===tab);button.setAttribute('aria-selected',String(button.dataset.tab===tab));}
  if(tab==='common')renderCommon();else if(tab==='details')renderDetails();else run(renderYaml);updateStatus();
}
async function selectNode(path) {ensureFields();await applyRaw();selected=clone(path);if(!node())selected=[];render();}
function uniqueName(name,values) {const set=new Set(values.map(v=>v.name));let result=name,i=2;while(set.has(result))result=`${name} ${i++}`;return result;}
function taskTemplate(type,name) {const base={name,type,enabled:true,execution_mode:'foreground'},matcher={names:['script.exe']};if(['launch','launch_wait'].includes(type))Object.assign(base,{exe:'C:/path/script.exe',args:[]});else if(type==='retry_group')Object.assign(base,{launch:[{exe:'C:/path/script.exe',role:'script'}],watch:[matcher],cleanup:[clone(matcher)],attempts:2});else if(type==='monitor')Object.assign(base,{script:{matchers:[matcher]},timeout:3600});else if(type==='sliding_window')Object.assign(base,{window_size:1,monitors:[taskTemplate('monitor','子监控 1')]});else base.targets=[matcher];return base;}
async function addQueue() {ensureFields();await applyRaw();const queues=data.queues||[];commit([{op:'append',path:['queues'],value:{name:uniqueName('新队列',queues),enabled:true,times:['06:00'],tasks:[]}}],['queues',queues.length]);}
async function addTask() {
  ensureFields();await applyRaw();if(!selected.length){notify('请先在左侧选择一个队列或监控组。',true);return;}
  const child=kind()==='task'&&node().type==='sliding_window';const path=child?[...selected,'monitors']:kind()==='queue'?[...selected,'tasks']:selected.slice(0,-1),items=getAt(data,path)||[];
  const create=type=>commit([{op:'append',path,value:taskTemplate(type,uniqueName('新任务',items))}],[...path,items.length]);
  if(child||path.at(-1)==='monitors'){create('monitor');return;}
  $('#dialog-title').textContent='新建任务';$('#dialog-hint').textContent='先选择任务类型，再填写启动与监控规则。';$('#dialog-error').hidden=true;
  const control=makeField(F('type','任务类型','str','retry_group',Object.keys(names)));$('#dialog-fields').replaceChildren(control.row);
  $('#dialog-form').onsubmit=event=>{event.preventDefault();create(control.getter());$('#editor-dialog').close();};$('#editor-dialog').showModal();control.input.focus();
}
async function load() {
  if((operations.length||invalidInputs.size||tab==='yaml'&&$('#yaml-text')?.value!==rawInitial)&&!confirm('重新载入会丢弃尚未保存的修改，继续吗？'))return;
  const result=await api('config');({data,revision,schemas}=result);operations=[];undo=[];redo=[];if(!node())selected=[];$('#file-path').textContent=result.path;render();
}
async function validate() {ensureFields();await applyRaw();const result=await api('validate');notify(result.message);}
async function save() {
  if(busy)return;ensureFields();busy=true;$('.sidebar').inert=true;$('main').inert=true;updateStatus();try{await applyRaw();const result=await api('save');({data,revision,schemas}=result);operations=[];undo=[];redo=[];render();notify(result.message);}finally{busy=false;$('.sidebar').inert=false;$('main').inert=false;updateStatus();}
}
async function history(direction) {
  await applyRaw();const from=direction==='undo'?undo:redo,to=direction==='undo'?redo:undo;if(!from.length)return;to.push({data:clone(data),operations:clone(operations),selected:clone(selected)});const value=from.pop();({data,operations,selected}=value);render();
}
$('#search').addEventListener('input',()=>{if(data)renderNav();});
for(const b of document.querySelectorAll('[data-tab]'))b.addEventListener('click',()=>run(async()=>{ensureFields();await applyRaw();tab=b.dataset.tab;render();}));
for(const [id,action] of Object.entries({reload:load,validate,save,'add-queue':addQueue,'add-task':addTask,undo:()=>history('undo'),redo:()=>history('redo')}))$(`#${id}`).addEventListener('click',()=>run(action));
for(const id of ['dialog-close','dialog-cancel'])$(`#${id}`).addEventListener('click',()=>$('#editor-dialog').close());
document.addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='s'){event.preventDefault();if($('#editor-dialog').open)return;document.activeElement?.blur();run(save);}if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='z'&&!['INPUT','TEXTAREA'].includes(document.activeElement.tagName)&&!$('#editor-dialog').open){event.preventDefault();run(()=>history(event.shiftKey?'redo':'undo'));}});
window.addEventListener('beforeunload',event=>{if(operations.length||invalidInputs.size||pendingInputs.size||tab==='yaml'&&$('#yaml-text')?.value!==rawInitial){event.preventDefault();event.returnValue='';}});
updateStatus();run(load);
