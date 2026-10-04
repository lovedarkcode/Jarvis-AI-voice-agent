const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const keys={openai_key:'sk-'+ 'a'.repeat(32),claude_key:'sk-ant-'+ 'b'.repeat(32),sarvam_key:'c'.repeat(32),gemini_key:'AIza'+ 'd'.repeat(32)};
class Element {
 constructor(){this.value='';this.hidden=false;this.inert=false;this.disabled=false;this.checked=false;this.children=[];this.dataset={};this.attributes={};this.classList={add(){},remove(){},toggle(){return true;}};this.scrollHeight=100;this.type='password';this.textContent='';}
 setAttribute(k,v){this.attributes[k]=v;} removeAttribute(k){delete this.attributes[k];} focus(){} cloneNode(){return new Element();}
 append(node){this.children.push(node);} replaceChildren(...nodes){this.children=nodes;} add(node){this.children.push(node);} get firstChild(){return this.children[0];}
 querySelector(){return new Element();} closest(){return new Element();} showModal(){this.open=true;} close(){this.open=false;}
}
function setup({session=null,persistent=false,blocked=false,status=200,verified=null}={}){
 const html=fs.readFileSync('web/demo.html','utf8'),els={};
 for(const match of html.matchAll(/<[^>]+id="([^"]+)"[^>]*>/g)){const el=els[match[1]]=new Element();el.hidden=/\bhidden\b/.test(match[0]);el.inert=/\binert\b/.test(match[0]);}
 els.provider.value='openai';els.language.value='en-IN';els.spoken.checked=false;
 const document={getElementById:id=>{assert(els[id],'Missing DOM ID: '+id);return els[id];},documentElement:new Element(),body:new Element(),activeElement:new Element(),
  querySelector:selector=>selector.includes('storage')?{value:'session'}:new Element(),querySelectorAll:()=>[],createElement:()=>new Element(),addEventListener(){}};
 const calls=[],stored=[];
 const vault={loadSession:async()=>{if(blocked)throw Error('Storage disabled');return session;},hasPersistent:()=>persistent,save:async(...args)=>stored.push(args),clear(){this.cleared=true;},unlock:async()=>keys};
 const c=vm.createContext({document,window:{JarvisVault:vault},console,EventTarget,CustomEvent,AbortController,
  navigator:{},HTMLMediaElement:class {},Option:class {},matchMedia:()=>({matches:false}),localStorage:{getItem:()=>null,setItem(){}},
  fetch:async(url,options)=>{calls.push([url,JSON.parse(options.body)]);return {ok:status===200,status,json:async()=>status===200?{models:{openai:'model'},text:'Hello',...(verified?{providers:verified}:{})}:{error:'Update your keys',provider:'openai'}};},setTimeout,clearTimeout,Blob,URL,atob,FormData});
 vm.runInContext(fs.readFileSync('web/app.js','utf8'),c);
 return {els,c,calls,stored,vault};
}
test('app is hidden in initial HTML and missing keys keep the gate closed',async()=>{
 const {els}=setup();assert(els.app.hidden);assert(els.app.inert);await tick();assert(!els['keys-form'].hidden);assert(els.app.hidden);
});
test('invalid form cannot call the providers or unlock the app',async()=>{
 const {els,calls}=setup();await tick();await els['keys-form'].onsubmit({preventDefault(){}});assert.equal(calls.length,0);assert(els.app.hidden);assert(els['openai-error'].textContent);
});
test('successful verification stores keys and hands off without reloading',async()=>{
 const {els,calls,stored,c}=setup();await tick();for(const [id,key]of Object.entries(keys))els[id].value=key;
 await els['keys-form'].onsubmit({preventDefault(){}});assert.equal(calls[0][0],'/api/keys/verify');assert.equal(stored.length,1);
 assert(!els.app.hidden);assert(!els.app.inert);assert(els['key-gate'].hidden);assert(c.window.JarvisState.snapshot().ready);assert.equal(els.openai_key.value,'');
});
test('blocked storage and persistent vault never reveal the dashboard',async()=>{
 const blocked=setup({blocked:true});await tick();assert(blocked.els.app.hidden);assert.match(blocked.els['key-error'].textContent,/Storage disabled/);
 const locked=setup({persistent:true});await tick();assert(locked.els.app.hidden);assert(!locked.els['unlock-form'].hidden);
});
test('server-side 401 blocks startup and never persists rejected credentials',async()=>{
 const {els,stored}=setup({status:401});await tick();for(const [id,key]of Object.entries(keys))els[id].value=key;
 await els['keys-form'].onsubmit({preventDefault(){}});assert(els.app.hidden);assert.equal(stored.length,0);assert.match(els['key-error'].textContent,/Update/);
});
test('saved session re-verifies and restores ready state',async()=>{
 const {els,calls}=setup({session:keys});await tick();assert(!els.app.hidden);assert.equal(calls[0][0],'/api/keys/verify');
});
test('authentication failure during chat forgets credentials and reopens mandatory gate',async()=>{
 const {els,c,vault}=setup({session:keys});await tick();assert(!els.app.hidden);
 c.fetch=async()=>({ok:false,status:401,json:async()=>({error:'Update keys',provider:'openai'})});
 els.message.value='Hello';await els.send.onclick();
 assert(els.app.hidden);assert(els.app.inert);assert(!els['key-gate'].hidden);assert(vault.cleared);
 assert(!c.window.JarvisState.snapshot().ready);
});

for(const provider of ['openai','claude','sarvam','gemini']){
 test(provider+' alone unlocks, persists, selects and sends with the correct key',async()=>{
  const {els,calls,stored}=setup({verified:[provider]});await tick();
  els[provider+'_key'].value=keys[provider+'_key'];
  await els['keys-form'].onsubmit({preventDefault(){}});
  assert(!els.app.hidden);assert.equal(els.provider.value,provider);
  assert.equal(els.provider.children.length,1);
  assert.deepEqual(Object.keys(stored[0][0]),[provider+'_key']);
  assert.equal(els.record.disabled,provider!=='gemini');
  els.spoken.checked=false;els.message.value='Hi';await els.send.onclick();
  const chat=calls.find(([url])=>url==='/api/chat')[1];
  assert.equal(chat.provider,provider);assert.equal(chat.key,keys[provider+'_key']);
 });
 test(provider+' alone restores from session',async()=>{
  const {els}=setup({session:{[provider+'_key']:keys[provider+'_key']},verified:[provider]});
  await tick();assert(!els.app.hidden);assert.equal(els.provider.value,provider);
 });
}
test('failed optional key is not saved or selectable',async()=>{
 const {els,stored}=setup({verified:['sarvam']});await tick();
 els.openai_key.value='bad';els.sarvam_key.value=keys.sarvam_key;
 await els['keys-form'].onsubmit({preventDefault(){}});
 assert(!els.app.hidden);assert.deepEqual(Object.keys(stored[0][0]),['sarvam_key']);
 assert.equal(els.provider.value,'sarvam');assert.equal(els.provider.children.length,1);
});
