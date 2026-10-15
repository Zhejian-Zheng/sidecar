const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function element(id){
  if(!elements.has(id))elements.set(id,{hidden:false,value:'',
    setAttribute(){},removeAttribute(key){delete this[key];},replaceChildren(){},append(){}});
  return elements.get(id);
}
let respond;
let created=0, revoked=0;
const context=vm.createContext({
  document:{hidden:false,getElementById:element,addEventListener(){},createElement:()=>element('temp')},
  sessionStorage:{getItem:()=>null},setInterval(){},
  URL:{createObjectURL(){created++;return 'blob:new';},revokeObjectURL(){revoked++;}},
  fetch:()=>new Promise(resolve=>{respond=resolve;}),
});
vm.runInContext(fs.readFileSync('src/sidecar/static/app.js','utf8'),context);
(async()=>{
  element('frame').src='blob:old';
  vm.runInContext("config={mode:'android'};token='secret';showFrame=true;current='first';imageUrl='blob:old';",context);
  const pending=vm.runInContext('refreshFrame()',context);
  vm.runInContext("selectTask('second')",context);
  assert.equal(element('frame').src,undefined,'switching task must clear its old frame');
  assert.equal(revoked,1);
  respond({status:200,blob:async()=>({})});
  await pending;
  assert.equal(created,0,'a delayed old task response must not render under the new task');
  context.fetch=async()=>{throw new Error('offline');};
  vm.runInContext("lastLiveFrame=Date.now();",context);
  await vm.runInContext('refreshFrame()',context);
  assert.match(element('video-status').textContent,/非实时/,'offline frames must lose live label');
  for(const status of ['completed','waiting_user']){
    context.api=async path=>path.endsWith('/video')?{status:'stopped',recording_available:false}:
      {request:{goal:'task'},status,reason:'internal reason',report:'shared report '+status,events:[]};
    await vm.runInContext("detail('second')",context);
    assert.equal(element('reason').textContent,'shared report '+status);
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
