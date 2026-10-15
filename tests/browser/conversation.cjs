const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {value:'',hidden:false,disabled:false,children:[],
    setAttribute(){},removeAttribute(){},replaceChildren(){this.children=[];},
    append(...nodes){this.children.push(...nodes);}});
  return elements.get(id);
};
const storage = new Map();
const requests = [];
let loseResponse = true;
let nextReply = {task_id:null, message:'你好，今天想聊些什么？', status:'information'};
const context = vm.createContext({
  document:{getElementById:element,addEventListener(){},createElement:()=>({textContent:''})},
  sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  crypto:require('node:crypto').webcrypto,setInterval(){},URL,console,
  fetch:async(url, options)=>{
    assert.equal(url,'/api/chat');
    requests.push(JSON.parse(options.body));
    if(loseResponse){loseResponse=false;throw new Error('Response lost');}
    return {ok:true,status:200,json:async()=>nextReply};
  },
});
vm.runInContext(fs.readFileSync('src/sidecar/static/app.js','utf8'),context);
vm.runInContext("token='test';config={mode:'android'};refresh=async()=>{};",context);
const submit = ()=>element('chat-form').onsubmit({preventDefault(){}});
(async()=>{
  assert.equal(typeof element('chat-form').onsubmit,'function','page must accept chat messages');
  element('chat-message').value='你好呀';
  await submit(); await submit();
  assert.equal(requests[0].request_id,requests[1].request_id,'lost response retry uses same identity');
  assert.equal(requests[1].messages.length,1,'retry must not duplicate user history');
  assert.ok(element('chat-log').children.some(n=>n.textContent.includes('今天想聊些什么')));
  nextReply={task_id:'a'.repeat(32),message:'已进入队列',status:'queued'};
  element('chat-message').value='设置八点闹钟';
  await submit();
  assert.equal(requests[2].messages.length,3,'followup includes prior conversation');
  assert.equal(vm.runInContext('current',context),'a'.repeat(32));
  element('disconnect').onclick();
  assert.equal(element('chat-log').children.length,0,'disconnect clears conversation');
})().catch(e=>{console.error(e);process.exitCode=1;});
