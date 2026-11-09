// Exercise the shipped inline controller with a small DOM and bridge adapter.
// No browser build pipeline or third-party packages are needed.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

const html = fs.readFileSync(path.join(__dirname,'../pages/phone-control/index.html'),'utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)][0][1];
const defaults = {
  control_backend:'app', operit_base_url:'', app_direct_urls:'http://phone.example:8260',
  allowed_user_ids:'owner', app_aliases_json:'{}', sleep_guard_packages:'video', sleep_guard_exempt_apps:'chat',
};
const status = config => ({success:true,control_backend:'app',config:config || defaults});
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes,no) => { resolve=yes; reject=no; });
  return {promise,resolve,reject};
}
function element(id,tag='') {
  const listeners = new Map();
  const attrs = {};
  const classes = new Set((tag.match(/class="([^"]*)"/)?.[1] || '').split(' '));
  return {
    id, value:tag.match(/value="([^"]*)"/)?.[1] || '', checked:tag.includes('checked'),
    disabled:false, hidden:false, textContent:'', innerHTML:'', dataset:{},
    className:[...classes].join(' '),
    classList:{add:name=>classes.add(name),remove:name=>classes.delete(name),contains:name=>classes.has(name)},
    setAttribute:(key,value)=>{attrs[key]=value;},removeAttribute:key=>{delete attrs[key];},
    addEventListener:(name,callback)=>{
      if(!listeners.has(name)) listeners.set(name,[]);
      listeners.get(name).push(callback);
    },
    dispatch(name) { for(const callback of listeners.get(name) || []) callback({preventDefault(){}}); },
  };
}
async function dashboard() {
  const elements = new Map();
  for(const match of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) elements.set(match[1],element(match[1],match[0]));
  const events = new Map(), timers = new Map(), calls = [];
  let timerId=0;
  const transport = {
    get:async name=>name==='status'?status():{success:true},
    post:async ()=>({success:true}),
  };
  const document = {
    hidden:false,
    getElementById:id=>{assert.ok(elements.has(id),'element exists: '+id);return elements.get(id);},
    querySelectorAll:selector=>selector==='.notice'?[...elements.values()].filter(el=>el.className.includes('notice')):[],
    addEventListener:(event,callback)=>events.set(event,callback),
  };
  const context = vm.createContext({
    document,crypto:webcrypto,Uint8Array,console,
    window:{AstrBotPluginPage:{
      apiGet:(name,params)=>{calls.push({method:'get',name,params});return transport.get(name,params);},
      apiPost:(name,body)=>{calls.push({method:'post',name,body});return transport.post(name,body);},
    }},
    setTimeout:callback=>{const id=++timerId;timers.set(id,callback);return id;},
    clearTimeout:id=>timers.delete(id),
  });
  vm.runInContext(script,context,{filename:'phone-control/index.html'});
  await Promise.all(vm.runInContext('Array.from(pending.values())',context));
  calls.length=0;
  return {context,elements,document,events,timers,calls,transport,
    edit(id,value){elements.get(id).value=value;elements.get(id).dispatch('input');}};
}

test('audit escapes every untrusted column, object key and nested value',async()=>{
  const d=await dashboard();
  d.transport.get=async()=>({success:true,records:[{
    time:'<img src=x onerror=alert(1)>', operation:'<script>alert(2)</script>',
    '<svg onload=alert(3)>':{text:'<iframe src=x>'},
  }]});
  await d.context.loadAudit();
  const rendered=d.elements.get('auditBody').innerHTML;
  assert.doesNotMatch(rendered,/<(?:img|script|svg|iframe)\b/);
  for(const tag of ['img','script','svg','iframe']) assert.ok(rendered.includes('&lt;'+tag));
});

test('care diagnostic reads per-user quotas and distinguishes an absent snapshot',async()=>{
  const d=await dashboard();
  d.context.renderAutonomy({care:{}});
  assert.match(d.elements.get('careRows').innerHTML,/未收到关怀快照/);
  d.context.renderAutonomy({care:{autonomy_enabled:true,mode:'preview',daily_limit:5,users:{
    '<owner>':{sent_today:2,remaining:3,eval_today:7,recent:[{at:2,outcome:'failed',reason:'deliver_failed',detail:'<b>失败</b>'}]},
  }}});
  const rendered=d.elements.get('careRows').innerHTML;
  assert.match(rendered,/2\/5，剩 3/);
  assert.match(rendered,/7 次/);
  assert.match(rendered,/deliver_failed/);
  assert.match(rendered,/&lt;owner&gt;/);
  assert.doesNotMatch(rendered,/<owner>|<b>/);
});

test('background status refresh preserves settings and policy drafts across tabs',async()=>{
  const d=await dashboard();
  d.edit('allowed_user_ids','draft-owner');
  d.edit('sleep_guard_packages','draft-video');
  d.edit('app_shared_token','secret draft');
  await d.context.refresh();
  assert.equal(d.elements.get('allowed_user_ids').value,'draft-owner');
  assert.equal(d.elements.get('sleep_guard_packages').value,'draft-video');
  assert.equal(d.elements.get('app_shared_token').value,'secret draft');
});

test('save deduplicates requests and keeps edits made while awaiting the server',async()=>{
  const d=await dashboard(), response=deferred();
  d.transport.post=()=>response.promise;
  d.edit('allowed_user_ids','first-edit');
  const first=d.context.saveConfig(), second=d.context.saveConfig();
  assert.equal(first,second);
  assert.equal(d.elements.get('saveBtn').disabled,true);
  await Promise.resolve();
  d.edit('allowed_user_ids','second-edit');
  response.resolve({success:true,config:{...defaults,allowed_user_ids:'first-edit'}});
  await first;
  assert.equal(d.calls.filter(call=>call.method==='post').length,1);
  assert.equal(d.elements.get('allowed_user_ids').value,'second-edit');
  assert.equal(d.elements.get('saveBtn').disabled,false);
});

test('late status response cannot revert a just-saved configuration',async()=>{
  const d=await dashboard(), response=deferred();
  d.transport.get=name=>name==='status'?response.promise:Promise.resolve({success:true});
  const read=d.context.refresh();
  await Promise.resolve();
  d.edit('allowed_user_ids','saved-owner');
  d.transport.post=async()=>({success:true,config:{...defaults,allowed_user_ids:'saved-owner'}});
  const save=d.context.saveConfig();
  await Promise.resolve();
  await Promise.resolve();
  response.resolve(status());
  await Promise.all([read,save]);
  assert.equal(d.elements.get('allowed_user_ids').value,'saved-owner');
});

test('transport rejection and unsuccessful API responses produce visible local errors',async()=>{
  const d=await dashboard();
  d.transport.get=async()=>{throw Error('network offline');};
  await d.context.loadTasks();
  assert.equal(d.elements.get('tasksNotice').textContent,'network offline');
  assert.equal(d.elements.get('tasksBtn').disabled,false);
  d.transport.get=async()=>({success:false,error:'permission denied'});
  await d.context.loadAudit();
  assert.equal(d.elements.get('auditNotice').textContent,'permission denied');
});

test('restart-interrupted tasks offer a manual retry while active and successful tasks do not',async()=>{
  const d=await dashboard();
  d.transport.get=async()=>({success:true,tasks:{
    interrupted:{status:'interrupted'},active:{status:'running'},finished:{status:'completed'},
  }});
  await d.context.loadTasks();
  const rendered=d.elements.get('taskList').innerHTML;
  assert.match(rendered,/data-task-action="operit_task_retry" data-task-id="interrupted"/);
  assert.equal((rendered.match(/data-task-action="operit_task_retry"/g) || []).length,1);
  assert.match(rendered,/data-task-action="operit_task_cancel" data-task-id="active"/);
});

test('same read has one in-flight request even with simultaneous manual refreshes',async()=>{
  const d=await dashboard(), response=deferred();
  d.transport.get=()=>response.promise;
  const first=d.context.loadReminders(), second=d.context.loadReminders();
  await Promise.resolve();
  assert.equal(d.calls.length,1);
  response.resolve({success:true,reminders:{}});
  await Promise.all([first,second]);
});

test('hidden tabs stop polling and resume with one timer',async()=>{
  const d=await dashboard();
  assert.equal(d.timers.size,1);
  d.document.hidden=true;
  d.events.get('visibilitychange')();
  assert.equal(d.timers.size,0);
  d.document.hidden=false;
  d.events.get('visibilitychange')();
  d.events.get('visibilitychange')();
  assert.equal(d.timers.size,1);
  d.elements.get('autoRefresh').checked=false;
  d.elements.get('autoRefresh').onchange();
  assert.equal(d.timers.size,0);
});

test('phone reminder retry reuses UUID after a lost response, then clears successful input',async()=>{
  const d=await dashboard();
  d.elements.get('reminder_text').value='喝水';
  d.elements.get('reminder_minutes').value='15';
  d.transport.post=async()=>{throw Error('lost response');};
  await d.context.createReminder();
  assert.equal(d.elements.get('reminder_text').value,'喝水');
  d.transport.post=async()=>({success:true,idempotent:true});
  await d.context.createReminder();
  const requests=d.calls.filter(call=>call.method==='post');
  assert.equal(requests.length,2);
  assert.equal(requests[0].name,'app/reminders');
  assert.equal(requests[0].body.request_id,requests[1].body.request_id);
  assert.match(requests[0].body.request_id,/^[a-f0-9-]{36}$/i);
  assert.equal(d.elements.get('reminder_text').value,'');
  assert.match(d.elements.get('reminderNotice').textContent,/手机提醒已创建/);
});

test('reminder validation rejects fractional and empty minutes without a server write',async()=>{
  const d=await dashboard();
  d.elements.get('reminder_text').value='休息';
  for(const invalid of ['1.5','','0','10081']) {
    d.elements.get('reminder_minutes').value=invalid;
    await d.context.createReminder();
  }
  assert.equal(d.calls.filter(call=>call.method==='post').length,0);
});
