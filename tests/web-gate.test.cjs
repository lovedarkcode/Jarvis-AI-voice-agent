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
function setup({keysToEnter=null,savedSession=null,persistent=false,blocked=false,status=200,verified=null,audio=false,mic=null,respond=null,keepPlayback=false,worklet=false,sampleRate=16000}={}){
 const html=fs.readFileSync('web/demo.html','utf8'),els={};
 for(const match of html.matchAll(/<[^>]+id="([^"]+)"[^>]*>/g)){const el=els[match[1]]=new Element();el.hidden=/\bhidden\b/.test(match[0]);el.inert=/\binert\b/.test(match[0]);}
 els.provider.value='openai';els.language.value='en-IN';els.spoken.checked=false;
 const document={getElementById:id=>{assert(els[id],'Missing DOM ID: '+id);return els[id];},documentElement:new Element(),body:new Element(),activeElement:new Element(),
  querySelector:selector=>selector.includes('storage')?{value:'session'}:new Element(),querySelectorAll:()=>[],createElement:()=>new Element(),addEventListener(){}};
 const calls=[],stored=[],io={streams:[],contexts:[],sockets:[],playbacks:[],events:{}};
 const getUserMedia=async()=>{if(mic)return mic();const track={enabled:true,stopped:false,stop(){this.stopped=true;}};const stream={getTracks:()=>[track],getAudioTracks:()=>[track]};io.streams.push(stream);return stream;};
 const audioNode=()=>({connect(){},disconnect(){this.disconnected=true;}});
 class Context{
  constructor(){this.sampleRate=sampleRate;this.state='running';this.destination={};if(worklet)this.audioWorklet={addModule:async()=>{}};io.contexts.push(this);}
  createMediaStreamSource(){return audioNode();} createGain(){return {...audioNode(),gain:{value:1}};}
  createScriptProcessor(){this.processor=audioNode();return this.processor;} close(){this.closed=true;}
 }
 class Worklet{
  constructor(ctx){this.port={};ctx.workletNode=this;} connect(){} disconnect(){this.disconnected=true;}
 }
 class Socket{
  constructor(url){this.url=url;this.readyState=0;this.sent=[];io.sockets.push(this);}
  send(value){this.sent.push(JSON.parse(value));} close(){this.readyState=3;this.onclose?.();}
 }
 class Playback{
  constructor(url){this.url=url;io.playbacks.push(this);} play(){if(!keepPlayback)queueMicrotask(()=>this.onended?.());return Promise.resolve();}
  pause(){this.paused=true;} removeAttribute(){} load(){}
 }
 const vault={loadCalls:0,loadSession:async()=>{vault.loadCalls++;if(blocked)throw Error('Storage disabled');return savedSession;},hasPersistent:()=>persistent,save:async(...args)=>stored.push(args),clear(){this.cleared=true;},unlock:async()=>keys};
 const c=vm.createContext({document,window:{JarvisVault:vault,addEventListener:(type,fn)=>{io.events[type]=fn;},...(audio?{AudioContext:Context}:{})},console,EventTarget,CustomEvent,AbortController,
  navigator:audio?{mediaDevices:{getUserMedia}}:{},HTMLMediaElement:class {},Option:class {},matchMedia:()=>({matches:false}),localStorage:{getItem:()=>null,setItem(){}},
  fetch:async(url,options)=>{const body=options.body instanceof FormData?options.body:JSON.parse(options.body);calls.push([url,body,options]);if(respond){const result=await respond(url,body,options);if(result)return result;}if(url==='/api/documents'&&status===200){const file=body.get('file'),text=await file.text();return ok({attachment:{name:file.name,kind:'text',segments:[{label:'section 1',text}]},characters:text.length});}return {ok:status===200,status,json:async()=>status===200?{models:{openai:'model'},text:'Hello',audios:[btoa('RIFFaudio')],token:'auth_tokens/test',...(verified?{providers:verified}:{})}:{error:'Update your keys',provider:'openai'}};},
  WebSocket:Socket,Audio:Playback,AudioWorkletNode:Worklet,setTimeout,clearTimeout,Blob,URL,atob,btoa,FormData});
 vm.runInContext(fs.readFileSync('web/app.js','utf8'),c);
 if(keysToEnter){for(const [id,key]of Object.entries(keysToEnter))els[id].value=key;els['keys-form'].onsubmit({preventDefault(){}});}
 return {els,c,calls,stored,vault,io};
}
test('app is hidden in initial HTML and missing keys keep the gate closed',async()=>{
 const {els}=setup();assert(els.app.hidden);assert(els.app.inert);await tick();assert(!els['keys-form'].hidden);assert(els.app.hidden);
});
test('invalid form cannot call the providers or unlock the app',async()=>{
 const {els,calls}=setup();await tick();await els['keys-form'].onsubmit({preventDefault(){}});assert.equal(calls.length,0);assert(els.app.hidden);assert(els['claude-error'].textContent);
});
test('successful verification holds keys in memory and hands off without reloading',async()=>{
 const {els,calls,stored,c}=setup();await tick();for(const [id,key]of Object.entries(keys))els[id].value=key;
 await els['keys-form'].onsubmit({preventDefault(){}});assert.equal(calls[0][0],'/api/keys/verify');assert.equal(stored.length,0);
 assert(!els.app.hidden);assert(!els.app.inert);assert(els['key-gate'].hidden);assert(c.window.JarvisState.snapshot().ready);assert.equal(els.openai_key.value,'');
});
test('cached keys and blocked storage cannot bypass fresh entry or call any provider',async()=>{
 for(const options of [{savedSession:keys},{persistent:true},{blocked:true}]){
  const {els,calls,vault}=setup(options);await tick();assert(els.app.hidden);assert(!els['keys-form'].hidden);
  assert.equal(calls.length,0);assert.equal(vault.loadCalls,0);assert(vault.cleared);
  for(const provider of Object.keys(keys))assert.equal(els[provider].value,'');
 }
});
test('server-side 401 blocks startup and never persists rejected credentials',async()=>{
 const {els,stored}=setup({status:401});await tick();for(const [id,key]of Object.entries(keys))els[id].value=key;
 await els['keys-form'].onsubmit({preventDefault(){}});assert(els.app.hidden);assert.equal(stored.length,0);assert.match(els['key-error'].textContent,/Update/);
});
test('a reload always shows setup even after a successful connection',async()=>{
 const first=setup({keysToEnter:keys});await tick();assert(!first.els.app.hidden);
 const second=setup({savedSession:keys});await tick();assert(second.els.app.hidden);assert.equal(second.calls.length,0);
});
test('authentication failure during chat forgets credentials and reopens mandatory gate',async()=>{
 const {els,c,vault}=setup({keysToEnter:keys});await tick();assert(!els.app.hidden);
 c.fetch=async()=>({ok:false,status:401,json:async()=>({error:'Update keys',provider:'openai'})});
 els.message.value='Hello';await els.send.onclick();
 assert(els.app.hidden);assert(els.app.inert);assert(!els['key-gate'].hidden);assert(vault.cleared);
 assert(!c.window.JarvisState.snapshot().ready);
});

for(const provider of ['openai','claude','sarvam','gemini']){
 test(provider+' alone unlocks, selects and sends with the correct key without saving it',async()=>{
  const {els,calls,stored}=setup({verified:[provider]});await tick();
  els[provider+'_key'].value=keys[provider+'_key'];
  await els['keys-form'].onsubmit({preventDefault(){}});
  assert(!els.app.hidden);assert.equal(els.provider.value,provider);
  assert.equal(els.provider.children.length,1);
  assert.equal(stored.length,0);
  assert.equal(els.record.disabled,!['sarvam','gemini'].includes(provider));
  els.spoken.checked=false;els.message.value='Hi';await els.send.onclick();
  const chat=calls.find(([url])=>url==='/api/chat')[1];
  assert.equal(chat.provider,provider);assert.equal(chat.key,keys[provider+'_key']);
 });
 test(provider+' alone connects after fresh key entry',async()=>{
  const {els}=setup({keysToEnter:{[provider+'_key']:keys[provider+'_key']},verified:[provider]});
  await tick();assert(!els.app.hidden);assert.equal(els.provider.value,provider);
 });
}
test('failed optional key is not saved or selectable',async()=>{
 const {els,stored}=setup({verified:['sarvam']});await tick();
 els.openai_key.value='bad';els.sarvam_key.value=keys.sarvam_key;
 await els['keys-form'].onsubmit({preventDefault(){}});
 assert(!els.app.hidden);assert.equal(stored.length,0);
 assert.equal(els.provider.value,'sarvam');assert.equal(els.provider.children.length,1);
});

function feedAudio(io,value,count=1,size=1024){
 const ctx=io.contexts.findLast(ctx=>ctx.processor||ctx.workletNode);assert(ctx,'Microphone processor is attached');
 for(let i=0;i<count;i++){const data=new Float32Array(size).fill(value);if(ctx.workletNode)ctx.workletNode.port.onmessage({data});else ctx.processor.onaudioprocess({inputBuffer:{getChannelData:()=>data}});}
}
const speechTurn=io=>{feedAudio(io,0.1,8);feedAudio(io,0,12);};
const ok=data=>({ok:true,status:200,json:async()=>data});

test('PDF, Word and Excel uploads use multipart extraction and become chat references',async()=>{
 for(const name of ['invoice.pdf','policy.docx','sales.xlsx','sales.xlsm']){
  const {els,calls}=setup({keysToEnter:{openai_key:keys.openai_key}});await tick();
  await els.file.onchange({target:{files:[new File(['Extracted reference'],name)]}});
  const upload=calls.find(([url])=>url==='/api/documents');
  assert.equal(upload[1].get('file').name,name);assert.equal(upload[1].get('key'),null);
  assert.equal(upload[2].headers['Content-Type'],undefined);assert.equal(els['file-name'].textContent,name);
  assert(!els['remove-document'].hidden);els.message.value='Explain this';await els.send.onclick();
  const chat=calls.find(([url])=>url==='/api/chat')[1];assert.equal(chat.attachment.name,name);
  assert.equal(chat.attachment.segments[0].text,'Extracted reference');
 }
});

test('Gemini document voice transcribes, retrieves through chat and speaks using only Gemini',async()=>{
 const {els,calls,io}=setup({keysToEnter:{gemini_key:keys.gemini_key},audio:true});await tick();
 const socket=io.sockets[0],stream=io.streams[0];assert(socket);
 await els.file.onchange({target:{files:[new File(['Warranty lasts 18 months.'],'policy.docx')]}});await tick();
 assert.equal(socket.readyState,3);assert(stream.getTracks()[0].stopped);assert.equal(io.sockets.length,1);
 speechTurn(io);await tick();
 const form=calls.find(([url])=>url==='/api/transcribe')[1];
 assert.equal(form.get('provider'),'gemini');assert.equal(form.get('key'),keys.gemini_key);
 const chat=calls.find(([url])=>url==='/api/chat')[1],speech=calls.find(([url])=>url==='/api/speech')[1];
 assert.equal(chat.provider,'gemini');assert.equal(chat.attachment.name,'policy.docx');
 assert.equal(speech.provider,'gemini');assert.equal(speech.key,keys.gemini_key);
 assert.match(els['record-label'].textContent,/Listening/);
 speechTurn(io);await tick();assert.equal(calls.filter(([url])=>url==='/api/chat').length,2);
 els['remove-document'].onclick();await tick();assert.equal(io.sockets.length,2);
 assert.equal(els['file-name'].textContent,'');assert(els['remove-document'].hidden);
 els.message.value='Hi';await els.send.onclick();assert.equal(calls.filter(([url])=>url==='/api/chat').at(-1)[1].attachment,null);
});

test('new chat aborts document extraction and ignores a late upload response',async()=>{
 let finish;const {els,calls}=setup({keysToEnter:{openai_key:keys.openai_key},respond:url=>url==='/api/documents'?new Promise(resolve=>{
  finish=()=>resolve(ok({attachment:{name:'old.pdf',kind:'pdf',segments:[{label:'page 1',text:'Old reference'}]},characters:13}));
 }):null});await tick();
 const upload=els.file.onchange({target:{files:[new File(['PDF'],'old.pdf')]}});
 assert(els.send.disabled);els.message.value='Wait';await els.send.onclick();assert(!calls.some(([url])=>url==='/api/chat'));
 els['new-chat'].onclick();assert(calls.find(([url])=>url==='/api/documents')[2].signal.aborted);
 finish();await upload;assert.equal(els['file-name'].textContent,'');assert(els['remove-document'].hidden);assert(!els.send.disabled);
 els.message.value='Hello';await els.send.onclick();assert.equal(calls.find(([url])=>url==='/api/chat')[1].attachment,null);
});

test('failed replacement preserves the active document and resumes voice',async()=>{
 const {els,calls,io}=setup({keysToEnter:{sarvam_key:keys.sarvam_key},audio:true,respond:(url,body)=>
  url==='/api/documents'&&body.get('file').name==='bad.pdf'?{ok:false,status:400,json:async()=>({error:'Scanned PDF needs OCR.'})}:null});await tick();
 await els.file.onchange({target:{files:[new File(['Good reference'],'good.txt')]}});await tick();
 await els.file.onchange({target:{files:[new File(['Image PDF'],'bad.pdf')]}});await tick();
 assert.equal(els['file-name'].textContent,'good.txt');assert.match(els['chat-error'].textContent,/OCR/);
 speechTurn(io);await tick();assert.equal(calls.find(([url])=>url==='/api/chat')[1].attachment.name,'good.txt');
});

test('a newer document upload wins over an older extraction response',async()=>{
 let finish;const {els,calls}=setup({keysToEnter:{openai_key:keys.openai_key},respond:(url,body)=>url==='/api/documents'&&body.get('file').name==='old.txt'?new Promise(resolve=>{
  finish=()=>resolve(ok({attachment:{name:'old.txt',kind:'text',segments:[{label:'section',text:'Old'}]},characters:3}));
 }):null});await tick();
 const old=els.file.onchange({target:{files:[new File(['Old'],'old.txt')]}});
 await els.file.onchange({target:{files:[new File(['New'],'new.txt')]}});
 assert(calls.find(([url])=>url==='/api/documents')[2].signal.aborted);
 finish();await old;assert.equal(els['file-name'].textContent,'new.txt');
});

test('Sarvam-only voice listens, transcribes, chats with document context, speaks and resumes',async()=>{
 const {els,calls,io}=setup({keysToEnter:{sarvam_key:keys.sarvam_key},audio:true});await tick();
 assert.equal(els.record.disabled,false);assert.match(els['voice-availability'].textContent,/Sarvam/);
 assert.match(els['record-label'].textContent,/Listening/);assert.equal(io.sockets.length,0);
 await els.file.onchange({target:{files:[new File(['Project note'],'note.txt')]}});await tick();
 feedAudio(io,0,20);assert(!calls.some(([url])=>url==='/api/transcribe'));
 speechTurn(io);await tick();
 const form=calls.find(([url])=>url==='/api/transcribe')[1];
 assert.equal(form.get('provider'),'sarvam');assert.equal(form.get('key'),keys.sarvam_key);
 const wav=Buffer.from(await form.get('audio').arrayBuffer());
 assert.equal(wav.toString('ascii',0,4),'RIFF');assert.equal(wav.readUInt32LE(24),16000);
 const chat=calls.find(([url])=>url==='/api/chat')[1],speech=calls.find(([url])=>url==='/api/speech')[1];
 assert.equal(chat.provider,'sarvam');assert.equal(chat.key,keys.sarvam_key);assert.equal(chat.attachment.segments[0].text,'Project note');
 assert.equal(chat.messages[0].content,'Hello');assert.equal(speech.provider,'sarvam');assert.equal(speech.key,keys.sarvam_key);
 assert.equal(io.playbacks.length,1);assert.match(els['record-label'].textContent,/Listening/);
 speechTurn(io);await tick();assert.equal(calls.filter(([url])=>url==='/api/transcribe').length,2);
 assert.equal(calls.filter(([url])=>url==='/api/chat')[1][1].messages.length,3);
 assert(!calls.some(([url])=>url==='/api/live/token'));
});

test('voice and typed spoken replies follow the selected provider when both keys exist',async()=>{
 const {els,calls,io}=setup({keysToEnter:{sarvam_key:keys.sarvam_key,gemini_key:keys.gemini_key},audio:true});await tick();
 els.message.value='Hi';await els.send.onclick();
 assert.equal(calls.find(([url])=>url==='/api/speech')[1].provider,'sarvam');
 const sarvamStream=io.streams[0];els.provider.value='gemini';els.provider.onchange();await tick();
 assert(sarvamStream.getTracks()[0].stopped);assert.equal(io.sockets.length,1);
 const socket=io.sockets[0];assert.match(socket.url,/generativelanguage/);
 socket.readyState=1;socket.onopen();socket.onmessage({data:JSON.stringify({setupComplete:{}})});
 els.message.value='Hi Gemini';await els.send.onclick();
 const lastSpeech=calls.filter(([url])=>url==='/api/speech').at(-1)[1];
 assert.equal(lastSpeech.provider,'gemini');assert.equal(lastSpeech.key,keys.gemini_key);
 const geminiStream=io.streams[1];els.provider.value='sarvam';els.provider.onchange();await tick();
 assert.equal(socket.readyState,3);assert(geminiStream.getTracks()[0].stopped);
 speechTurn(io);await tick();assert.equal(calls.filter(([url])=>url==='/api/chat').at(-1)[1].provider,'sarvam');
});

test('Stop cancels playback, mutes Sarvam, and Voice resumes without opening a second microphone',async()=>{
 const {els,c,calls,io}=setup({keysToEnter:{sarvam_key:keys.sarvam_key},audio:true,keepPlayback:true});await tick();
 speechTurn(io);await tick();assert.equal(io.playbacks.length,1);assert(c.window.JarvisState.snapshot().busy);
 feedAudio(io,0.1,30);assert.equal(calls.filter(([url])=>url==='/api/transcribe').length,1);
 els.interrupt.onclick();await tick();assert(io.playbacks[0].paused);assert(!c.window.JarvisState.snapshot().busy);
 assert.match(els['record-label'].textContent,/Muted/);assert(!io.streams[0].getTracks()[0].stopped);
 speechTurn(io);await tick();assert.equal(calls.filter(([url])=>url==='/api/transcribe').length,1);
 await els.record.onclick();assert.match(els['record-label'].textContent,/Listening/);assert.equal(io.streams.length,1);
 speechTurn(io);await tick();assert.equal(calls.filter(([url])=>url==='/api/transcribe').length,2);
 els.interrupt.onclick();await tick();
});

test('provider switching cancels transcription and discards late results',async()=>{
 let finish;const {els,calls,io}=setup({keysToEnter:keys,audio:true,respond:(url)=>url==='/api/transcribe'?new Promise(resolve=>{finish=()=>resolve(ok({text:'stale transcript'}));}):null});
 await tick();els.provider.value='sarvam';els.provider.onchange();await tick();speechTurn(io);
 const transcription=calls.find(([url])=>url==='/api/transcribe');assert(transcription);
 els.provider.value='openai';els.provider.onchange();assert(transcription[2].signal.aborted);
 finish();await tick();assert(!calls.some(([url])=>url==='/api/chat'));assert(els.record.disabled);assert(els.spoken.disabled);
 assert(io.streams[0].getTracks()[0].stopped);
});

test('switching provider during microphone startup releases the old stream',async()=>{
 let finish,count=0;const tracks=[{stop(){this.stopped=true;}},{stop(){this.stopped=true;}}];
 const streams=tracks.map(track=>({getTracks:()=>[track],getAudioTracks:()=>[track]}));
 const {els,io,calls}=setup({keysToEnter:{sarvam_key:keys.sarvam_key,gemini_key:keys.gemini_key},audio:true,
  mic:()=>++count===1?new Promise(resolve=>{finish=()=>resolve(streams[0]);}):Promise.resolve(streams[1])});
 await tick();els.provider.value='gemini';els.provider.onchange();finish();await tick();
 assert(tracks[0].stopped);assert.equal(count,2);assert.equal(io.sockets.length,1);
 assert.equal(calls.find(([url])=>url==='/api/live/token')[1].key,keys.gemini_key);
});

test('failed microphone setup releases resources and permits a manual retry',async()=>{
 const {els,c,io}=setup({keysToEnter:{sarvam_key:keys.sarvam_key},audio:true});
 const original=c.window.AudioContext.prototype.createMediaStreamSource;
 c.window.AudioContext.prototype.createMediaStreamSource=()=>{throw Error('Device failed');};await tick();await tick();
 assert.equal(io.streams.length,1);assert(io.streams[0].getTracks()[0].stopped);assert(io.contexts[0].closed);
 assert.match(els['chat-error'].textContent,/Device failed/);
 c.window.AudioContext.prototype.createMediaStreamSource=original;await els.record.onclick();
 assert.equal(io.streams.length,2);assert.match(els['record-label'].textContent,/Listening/);
});

test('Sarvam honors language and spoken reply preferences and bounds long recordings',async()=>{
 const {els,calls,io}=setup({keysToEnter:{sarvam_key:keys.sarvam_key},audio:true});await tick();
 els.language.value='hi-IN';els.language.onchange();await tick();els.spoken.checked=false;els.spoken.onchange();
 feedAudio(io,0.1,391);await tick();
 const form=calls.find(([url])=>url==='/api/transcribe')[1];assert.equal(form.get('language'),'hi-IN');
 assert(form.get('audio').size<30*16000*2+44);assert.equal(calls.filter(([url])=>url==='/api/chat').length,1);
 assert(!calls.some(([url])=>url==='/api/speech'));assert.match(els['record-label'].textContent,/Listening/);
});

test('Sarvam captures through AudioWorklet and resamples a 48 kHz microphone to 16 kHz WAV',async()=>{
 const {calls,io}=setup({keysToEnter:{sarvam_key:keys.sarvam_key},audio:true,worklet:true,sampleRate:48000});await tick();
 assert(io.contexts[0].workletNode);assert(!io.contexts[0].processor);
 feedAudio(io,0.1,8,4096);feedAudio(io,0,12,4096);await tick();
 const wav=Buffer.from(await calls.find(([url])=>url==='/api/transcribe')[1].get('audio').arrayBuffer());
 assert.equal(wav.readUInt32LE(24),16000);assert.equal(wav.readInt16LE(44),3277);
 assert.equal(calls.filter(([url])=>url==='/api/speech').length,1);
});

test('closing the page during microphone startup prevents automatic restart until page return',async()=>{
 let finish,count=0;const track={stop(){this.stopped=true;}},stream={getTracks:()=>[track],getAudioTracks:()=>[track]};
 const {io}=setup({keysToEnter:{sarvam_key:keys.sarvam_key},audio:true,
  mic:()=>++count===1?new Promise(resolve=>{finish=()=>resolve(stream);}):Promise.resolve(stream)});
 await tick();io.events.pagehide();finish();await tick();
 assert(track.stopped);assert.equal(count,1);io.events.pageshow();await tick();assert.equal(count,2);
 io.events.pagehide();
});

test('Sarvam voice authentication failure closes the microphone and returns to key setup',async()=>{
 const {els,c,io,vault}=setup({keysToEnter:{sarvam_key:keys.sarvam_key},audio:true,respond:url=>url==='/api/transcribe'?{ok:false,status:401,json:async()=>({error:'Update your key',provider:'sarvam'})}:null});
 await tick();speechTurn(io);await tick();assert(els.app.hidden);assert(vault.cleared);
 assert(io.streams[0].getTracks()[0].stopped);assert(!c.window.JarvisState.snapshot().ready);
});
