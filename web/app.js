/* Plain-JS state provider: secrets remain in this closure, never in events or URLs. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id), vault=window.JarvisVault;
  const providers=['claude','openai','sarvam','gemini'];
  const labels={claude:'Claude',openai:'OpenAI',sarvam:'Sarvam',gemini:'Gemini'};
  const prefixes={openai:['sk-'],claude:['sk-ant-'],gemini:['AIza','AQ.'],sarvam:['']};
  function hasPrefix(provider, key) {
    return (prefixes[provider] || ['']).some(prefix => key.startsWith(prefix));
  }
  const state={keys:null,models:null,messages:[],document:null,uploading:false,uploadRevision:0,uploadController:null,busy:false,revision:0,controller:null,
    recorder:null,stream:null,recordTimer:null,recordCancelled:false,audio:null,audioURL:null,returnFocus:null,
    live:null,liveHandle:'',pendingMic:null,liveRetries:0,liveLine:null,liveStarting:null,voiceRevision:0,voiceSuspended:false};
  const changes=new EventTarget();
  window.JarvisState=Object.freeze({subscribe(fn){const listener=e=>fn(e.detail);changes.addEventListener('change',listener);return()=>changes.removeEventListener('change',listener);},
    snapshot:()=>({ready:!!state.keys,busy:state.busy,provider:$('provider').value})});
  function publish(){changes.dispatchEvent(new CustomEvent('change',{detail:window.JarvisState.snapshot()}));}
  function status(text){$('state').textContent=text;}
  function log(text){const row=document.createElement('div');row.textContent=new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})+' · '+text;$('activity').append(row);while($('activity').children.length>100)$('activity').firstChild.remove();$('activity').scrollTop=$('activity').scrollHeight;}
  function error(text,target='chat-error'){$(target).textContent=text;}
  function preference(key,fallback){try{return localStorage.getItem('jarvis.'+key)||fallback;}catch{return fallback;}}
  function remember(key,value){try{localStorage.setItem('jarvis.'+key,value);}catch{}}
  function voiceProvider(){const provider=$('provider').value;return ['sarvam','gemini'].includes(provider)&&state.keys?.[provider+'_key']?provider:null;}
  function updateVoice(){
    const provider=voiceProvider();
    $('spoken').disabled=!provider;$('spoken').checked=!!provider&&preference('spoken','true')==='true';
    $('voice-availability').textContent=provider?`${labels[provider]} handles voice input and spoken replies. Voice starts automatically; allow microphone access.`:'Select Sarvam or Gemini with a key in Settings to enable voice input and spoken replies.';
    paintVoice();
  }
  function theme(value){const dark=value==='dark';document.documentElement.dataset.theme=dark?'dark':'light';document.querySelectorAll('.theme span').forEach(x=>x.textContent=dark?'Light mode':'Dark mode');remember('theme',value);}
  theme(preference('theme','dark'));
  document.querySelectorAll('.theme').forEach(b=>b.onclick=()=>theme(document.documentElement.dataset.theme==='dark'?'light':'dark'));
  function closeSidebar(){document.body.classList.remove('sidebar-open');$('menu-toggle').setAttribute('aria-expanded','false');}
  $('menu-toggle').onclick=()=>{const open=document.body.classList.toggle('sidebar-open');$('menu-toggle').setAttribute('aria-expanded',String(open));};
  $('sidebar-backdrop').onclick=closeSidebar;
  function dialog(id){closeSidebar();document.querySelectorAll('dialog[open]').forEach(d=>d.close());$(id).showModal();}
  document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>b.closest('dialog').close());
  $('settings-button').onclick=$('header-settings').onclick=()=>dialog('settings-dialog');
  $('audio-settings').onclick=$('settings-audio').onclick=()=>{dialog('audio-dialog');listDevices();};
  $('activity-button').onclick=()=>{$('activity-panel').hidden=!$('activity-panel').hidden;closeSidebar();};
  $('close-activity').onclick=()=>{$('activity-panel').hidden=true;};
  $('fullscreen').onclick=async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await document.documentElement.requestFullscreen();}catch{error('Full screen is unavailable in this browser.');}};

  function validate(keys){
    const errors={};
    for(const p of providers){const key=keys[p+'_key']||'';if(!key)continue;const accepted=(prefixes[p]||['']).filter(Boolean);
      if(key.length<16||key.length>512||!hasPrefix(p,key)||!/^[!-~]+$/.test(key))errors[p]=`Enter a valid ${labels[p]} key${accepted.length?' starting with '+accepted.join(' or '):''}.`;
    }if(!providers.some(p=>keys[p+'_key']&&!errors[p])){if(!Object.keys(errors).length)errors.claude='Enter at least one provider API key.';return errors;}return {};
  }
  function cleanInputs(){providers.forEach(p=>{$(p+'_key').value='';$(p+'_key').type='password';});document.querySelectorAll('[data-reveal]').forEach(b=>{b.textContent='Show';b.setAttribute('aria-pressed','false');});}
  function showGate(message='',editing=false){
    stop();state.returnFocus=document.activeElement;document.querySelectorAll('dialog[open]').forEach(d=>d.close());closeSidebar();
    $('app').hidden=true;$('app').inert=true;$('key-gate').hidden=false;$('keys-form').hidden=false;$('cancel-keys').hidden=!editing;
    $('gate-title').textContent=editing?'Manage your API keys':'Connect your AI workspace';
    error(message,'key-error');
    $('openai_key').focus();
  }
  function enter(keys,models){
    state.keys=Object.freeze({...keys});state.models=models;
    const available=providers.filter(p=>keys[p+'_key']);
    const preferred=$('provider').value;
    $('provider').replaceChildren(...available.map(p=>new Option(labels[p],p)));
    $('provider').value=available.includes(preferred)?preferred:available[0];
    $('key-gate').hidden=true;$('app').hidden=false;$('app').inert=false;cleanInputs();
    updateVoice();publish();$('message').focus();log(available.map(p=>labels[p]).join(', ')+' ready.');
    if(voiceProvider())ensureLive();else releasePendingMic();
  }
  function authFailure(provider){
    state.keys=null;vault.clear();cleanInputs();publish();
    showGate(`${provider||'A provider'} rejected the API key or its permissions. Update your keys to continue.`);
  }
  async function request(path,body,{signal,verification=false,form=false}={}){
    const response=await fetch('/api/'+path,{method:'POST',headers:form?{}:{'Content-Type':'application/json'},body:form?body:JSON.stringify(body),signal,cache:'no-store',credentials:'omit',redirect:'error'});
    let data;try{data=await response.json();}catch{throw new Error('The server returned an unexpected response. Check the deployment and retry.');}
    if(!response.ok){if(response.status===401&&!verification)authFailure(data.provider);throw new Error(data.error||'Request failed. Please try again.');}
    return data;
  }
  function verifiedKeys(keys,result){return Object.fromEntries((result.providers||providers.filter(p=>keys[p+'_key'])).map(p=>[p+'_key',keys[p+'_key']]));}
  async function verify(keys){const invalid=validate(keys);if(Object.keys(invalid).length)throw new Error(Object.values(invalid)[0]);return request('keys/verify',keys,{verification:true});}
  document.querySelectorAll('[data-reveal]').forEach(button=>{button.onclick=()=>{const input=$(button.dataset.reveal),show=input.type==='password';input.type=show?'text':'password';button.textContent=show?'Hide':'Show';button.setAttribute('aria-pressed',String(show));};});
  $('keys-form').onsubmit=async event=>{
    event.preventDefault();if($('save-keys').disabled)return;
    const keys=Object.fromEntries(providers.map(p=>[p+'_key',$(p+'_key').value.trim()]));const invalid=validate(keys);
    providers.forEach(p=>{$(p+'-error').textContent=invalid[p]||'';$(p+'_key').setAttribute('aria-invalid',String(!!invalid[p]));});
    if(Object.keys(invalid).length){$(Object.keys(invalid)[0]+'_key').focus();return;}
    if(keys.gemini_key||keys.sarvam_key)primeMic();
    $('save-keys').disabled=true;$('cancel-keys').disabled=true;$('save-keys').textContent='Verifying your providers…';error('','key-error');
    try{const result=await verify(keys);enter(verifiedKeys(keys,result),result.models);for(const warning of result.warnings||[])log(warning);}
    catch(e){releasePendingMic();error(e.message||'Keys could not be verified. Please try again.','key-error');}
    finally{$('save-keys').disabled=false;$('cancel-keys').disabled=false;$('save-keys').textContent='Connect & start application';}
  };
  $('edit-keys').onclick=()=>{showGate('',true);providers.forEach(p=>{$(p+'_key').value=state.keys?.[p+'_key']||'';});};
  $('cancel-keys').onclick=()=>{if(state.keys)enter(state.keys,state.models);};
  $('forget-keys').onclick=()=>{state.keys=null;vault.clear();cleanInputs();publish();showGate('Keys have been cleared. Enter a key to continue.');};

  const avatar=document.querySelector('.avatar svg').cloneNode(true);
  function message(role,text){$('welcome').hidden=true;const row=document.createElement('div');row.className='message '+role;if(role==='assistant'){const icon=document.createElement('span');icon.className='avatar';icon.append(avatar.cloneNode(true));row.append(icon);}const body=document.createElement('div');body.className='message-body';body.textContent=text;row.append(body);$('feed').append(row);$('conversation').scrollTop=$('conversation').scrollHeight;while($('feed').children.length>200)$('feed').firstChild.remove();}
  function busy(value){state.busy=value;$('send').disabled=value||state.uploading;$('provider').disabled=value;publish();}
  function stop(){
    cancelUpload();abortTurn();endLive();
    $('record-label').textContent='Voice';status(state.keys?'Ready':'Setup required');
  }
  function abortTurn(){
    state.revision++;state.controller?.abort();state.controller=null;
    state.live?.controller?.abort();if(state.live?.turnBased)resetUtterance(state.live);
    if(state.audio){state.audio.pause();state.audio.removeAttribute('src');state.audio.load();state.audio=null;}
    if(state.audioURL){URL.revokeObjectURL(state.audioURL);state.audioURL=null;}
    busy(false);
  }
  $('interrupt').onclick=()=>{abortTurn();interruptPlayback();muteLive(true);log('Muted. Voice stays connected.');};
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('key-gate').hidden)return;if(e.key==='Escape')$('interrupt').onclick();});
  async function send(text){
    if(!state.keys||state.busy||state.uploading||!text.trim())return;
    abortTurn();interruptPlayback();const revision=state.revision;state.controller=new AbortController();const signal=state.controller.signal;
    text=text.trim().slice(0,16000);message('user',text);state.messages.push({role:'user',content:text});state.messages=state.messages.slice(-23);$('message').value='';error('');busy(true);status('Thinking…');
    const provider=$('provider').value;log(`Sending to ${labels[provider]||provider}.`);
    try{
      const data=await request('chat',{provider,key:state.keys[provider+'_key'],messages:state.messages,attachment:state.document},{signal});
      if(revision!==state.revision)return;
      message('assistant',data.text);state.messages.push({role:'assistant',content:data.text.slice(0,16000)});if(state.live)paintVoice();else status('Ready');
      if(voiceProvider()===provider&&$('spoken').checked){
        status('Preparing speech…');const speech=await request('speech',{provider,key:state.keys[provider+'_key'],text:data.text.slice(0,2500),language:$('language').value},{signal});
        if(revision!==state.revision)return;
        if(!(speech.audios||[]).length)throw new Error(`${labels[provider]} returned no speech audio.`);
        for(const encoded of speech.audios||[]){
          if(revision!==state.revision||!$('spoken').checked)break;
          const bytes=Uint8Array.from(atob(encoded),c=>c.charCodeAt(0));state.audioURL=URL.createObjectURL(new Blob([bytes],{type:'audio/wav'}));
          state.audio=new Audio(state.audioURL);if($('speaker').value&&state.audio.setSinkId)await state.audio.setSinkId($('speaker').value);
          status('Speaking…');await new Promise((resolve,reject)=>{state.audio.onended=resolve;state.audio.onerror=()=>reject(new Error('Audio playback failed.'));signal.addEventListener('abort',resolve,{once:true});state.audio.play().catch(()=>reject(new Error('Reply is ready. Enable playback in your browser or turn off spoken replies.')));});
          if(state.audioURL){URL.revokeObjectURL(state.audioURL);state.audioURL=null;}
        }
      }
    }catch(e){if(e.name!=='AbortError'&&revision===state.revision){error(e.message);log('Request could not complete.');}}
    finally{if(revision===state.revision){busy(false);if(state.live)paintVoice();else status('Ready');}}
  }
  $('send').onclick=()=>send($('message').value);
  $('message').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();send($('message').value);}};
  $('new-chat').onclick=()=>{clearDocument();state.liveLine=null;state.messages=[];$('feed').replaceChildren();$('welcome').hidden=false;error('');closeSidebar();$('message').focus();};
  $('provider').value=preference('provider','openai');$('provider').onchange=()=>{remember('provider',$('provider').value);abortTurn();endLive();updateVoice();publish();ensureLive();};
  $('spoken').checked=preference('spoken','true')==='true';$('spoken').onchange=()=>{remember('spoken',String($('spoken').checked));if(!$('spoken').checked){interruptPlayback();if(state.audio){state.audio.pause();state.audio.onended?.();}}};
  $('language').value=preference('language','en-IN');if(!$('language').value)$('language').value='en-IN';$('language').onchange=()=>{remember('language',$('language').value);abortTurn();endLive();ensureLive();};
  function cancelUpload(){
    state.uploadRevision++;state.uploadController?.abort();state.uploadController=null;state.uploading=false;
    $('file-name').textContent=state.document?.name||'';busy(state.busy);
  }
  function clearDocument(){
    cancelUpload();abortTurn();endLive();state.document=null;$('file-name').textContent='';$('remove-document').hidden=true;ensureLive();
  }
  $('remove-document').onclick=clearDocument;
  $('file').onchange=async e=>{
    const file=e.target.files[0];if(!file||!state.keys)return;e.target.value='';
    if(file.size>3500000){error('Attach a document under 3.5 MB.');return;}
    if(!/\.(pdf|docx|xlsx|xlsm|txt|md|rst|csv|tsv|json|log)$/i.test(file.name)){error('Attach PDF, Word (.docx), Excel (.xlsx/.xlsm), or a text file.');return;}
    cancelUpload();abortTurn();endLive();state.uploading=true;busy(false);paintVoice();error('');
    const revision=state.uploadRevision;state.uploadController=new AbortController();
    $('file-name').textContent='Reading '+file.name+'...';
    try{
      const form=new FormData();form.append('file',file,file.name);
      const result=await request('documents',form,{form:true,signal:state.uploadController.signal});
      if(revision!==state.uploadRevision)return;
      state.document=result.attachment;$('file-name').textContent=state.document.name;$('remove-document').hidden=false;
      log(`Document ready: ${state.document.name} (${result.characters.toLocaleString()} characters). Ask a question by voice or text.`);
      for(const warning of result.warnings||[])log(warning);
    }catch(e){if(e.name!=='AbortError'&&revision===state.uploadRevision){$('file-name').textContent=state.document?.name||'';error(e.message||'Document upload failed.');}}
    finally{if(revision===state.uploadRevision){state.uploading=false;state.uploadController=null;busy(false);ensureLive();paintVoice();}}
  };

  async function listDevices(){
    if(!navigator.mediaDevices?.enumerateDevices){error('Audio device selection is unavailable in this browser.','audio-error');return;}
    try{const devices=await navigator.mediaDevices.enumerateDevices();for(const [id,kind] of [['microphone','audioinput'],['speaker','audiooutput']]){const selected=$(id).value||preference(id,'');$(id).replaceChildren(new Option('System default',''));let n=0;devices.filter(d=>d.kind===kind).forEach(d=>$(id).add(new Option(d.label||`${id} ${++n}`,d.deviceId)));$(id).value=selected;if(!$(id).value)$(id).value='';}}
    catch{error('Could not list audio devices. Check browser permissions.','audio-error');}
  }
  $('speaker').disabled=typeof HTMLMediaElement.prototype.setSinkId!=='function';$('speaker-note').textContent=$('speaker').disabled?'This browser uses your system-default speaker.':'';
  ['microphone','speaker'].forEach(id=>$(id).onchange=()=>remember(id,$(id).value));
  $('refresh-devices').onclick=async()=>{try{const stream=await navigator.mediaDevices.getUserMedia({audio:true});stream.getTracks().forEach(t=>t.stop());await listDevices();error('','audio-error');}catch{error('Microphone access was denied or no device is available. Check browser permissions.','audio-error');}};

  function canStream(provider=voiceProvider()){return !!(navigator.mediaDevices?.getUserMedia&&(window.AudioContext||window.webkitAudioContext)&&(provider!=='gemini'||state.document||typeof WebSocket==='function'));}
  function micConstraints(){const device=$('microphone').value||preference('microphone','');return {audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true,...(device?{deviceId:{exact:device}}:{})}};}
  function primeMic(){if(!canStream()||state.pendingMic||state.live)return;state.pendingMic=navigator.mediaDevices.getUserMedia(micConstraints());state.pendingMic.catch(()=>{});}
  function releasePendingMic(){const pending=state.pendingMic;state.pendingMic=null;if(!pending)return;pending.then(stream=>stream.getTracks().forEach(t=>t.stop())).catch(()=>{});}
  function paintVoice(){
    const voice=voiceProvider(),live=state.live;
    $('record').disabled=!voice||state.uploading;
    if(state.uploading){$('record-label').textContent='Voice';status('Reading document...');return;}
    $('record').classList.toggle('live',!!live&&!live.muted);
    if(!voice){$('record-label').textContent='Voice';$('record').title='Select Sarvam or Gemini with a key in Settings to enable voice';if(!state.busy)status($('app').hidden?'Setup required':'Ready');return;}
    if(!live){$('record-label').textContent='Voice';$('record').title=`Start listening with ${labels[voice]}`;if(!state.busy)status($('app').hidden?'Setup required':'Tap Voice if listening did not start');return;}
    if(live.muted){$('record-label').textContent='Muted';$('record').title='Unmute and keep streaming';status('Muted');}
    else{$('record-label').textContent='Listening';$('record').title=`Mute ${labels[voice]} microphone`;if(!state.busy)status(live.processing?'Transcribing…':live.speaking?'Speaking…':'Listening');}
  }
  function ensureLive(){
    if(!voiceProvider()||state.live||state.uploading||state.voiceSuspended||$('app').hidden||!canStream())return;
    if(state.liveStarting)return state.liveStarting;
    const revision=state.voiceRevision;
    state.liveStarting=startLive().catch(e=>{if(revision===state.voiceRevision&&!$('app').hidden)error(e.message||'Voice could not start.');}).finally(()=>{state.liveStarting=null;paintVoice();if(revision!==state.voiceRevision)ensureLive();});
    return state.liveStarting;
  }
  function endLive(reason){
    if(reason!=='failed')state.voiceRevision++;
    const live=state.live;state.live=null;state.liveLine=null;
    if(reason!=='reconnect')state.liveHandle='';
    releasePendingMic();
    if(!live){paintVoice();return;}
    live.closed=true;live.controller?.abort();interruptPlayback(live);
    try{if(live.ws&&live.ws.readyState<2)live.ws.close();}catch{}
    try{live.node?.disconnect();}catch{}
    try{live.micCtx?.close();}catch{}
    try{live.playCtx?.close();}catch{}
    live.stream?.getTracks().forEach(t=>t.stop());
    paintVoice();
  }
  function interruptPlayback(live=state.live){if(!live)return;live.speaking=false;(live.sources||[]).forEach(source=>{try{source.stop();}catch{}});live.sources=[];live.nextTime=0;}
  function muteLive(muted){const live=state.live;if(!live)return;live.muted=muted;live.stream?.getAudioTracks().forEach(track=>{track.enabled=!muted;});if(live.turnBased)resetUtterance(live);if(muted&&live.ws?.readyState===1)live.ws.send(JSON.stringify({realtimeInput:{audioStreamEnd:true}}));paintVoice();}
  function liveBubble(role){
    if(state.liveLine?.role===role)return state.liveLine;
    state.liveLine=null;$('welcome').hidden=true;
    const row=document.createElement('div');row.className='message '+role;
    if(role==='assistant'){const icon=document.createElement('span');icon.className='avatar';icon.append(avatar.cloneNode(true));row.append(icon);}
    const body=document.createElement('div');body.className='message-body';row.append(body);$('feed').append(row);
    state.liveLine={role,body,text:''};return state.liveLine;
  }
  function mergeTranscript(previous,next){if(!next)return previous||'';if(!previous||next.startsWith(previous))return next;if(previous.endsWith(next))return previous;return previous+next;}
  function showTranscript(role,text){const line=liveBubble(role);line.text=mergeTranscript(line.text,text);line.body.textContent=line.text;$('conversation').scrollTop=$('conversation').scrollHeight;}
  function pcm16(float32,srcRate){
    let samples=float32;
    if(srcRate!==16000){const ratio=srcRate/16000,len=Math.max(1,Math.round(float32.length/ratio));samples=new Float32Array(len);for(let i=0;i<len;i++)samples[i]=float32[Math.min(Math.round(i*ratio),float32.length-1)];}
    const out=new Int16Array(samples.length);
    for(let i=0;i<samples.length;i++)out[i]=Math.max(-32768,Math.min(32767,Math.round(samples[i]*32767)));
    return out;
  }
  function bytesToBase64(bytes){let binary='';for(let i=0;i<bytes.length;i+=0x2000)binary+=String.fromCharCode.apply(null,bytes.subarray(i,i+0x2000));return btoa(binary);}
  function b64ToBytes(data){const binary=atob(data),bytes=new Uint8Array(binary.length);for(let i=0;i<binary.length;i++)bytes[i]=binary.charCodeAt(i);return bytes;}
  function enqueuePcm(live,bytes){
    if(!live.playCtx||live.closed||state.busy||!$('spoken').checked)return;
    const samples=new Int16Array(bytes.buffer,bytes.byteOffset,Math.floor(bytes.byteLength/2));
    if(!samples.length)return;
    const ctx=live.playCtx,ratio=24000/ctx.sampleRate,len=Math.max(1,Math.round(samples.length/ratio));
    const audio=ctx.createBuffer(1,len,ctx.sampleRate),channel=audio.getChannelData(0);
    for(let i=0;i<len;i++)channel[i]=samples[Math.min(Math.floor(i*ratio),samples.length-1)]/32768;
    const source=ctx.createBufferSource();source.buffer=audio;source.connect(ctx.destination);
    const start=Math.max(ctx.currentTime+0.02,live.nextTime||0);source.start(start);live.nextTime=start+audio.duration;live.speaking=true;live.sources.push(source);
    source.onended=()=>{live.sources=live.sources.filter(item=>item!==source);if(!live.sources.length){live.speaking=false;if(state.live===live)paintVoice();}}
    paintVoice();
  }
  function resetUtterance(live){live.chunks=[];live.preRoll=[];live.sampleCount=0;live.speechSamples=0;live.silenceSamples=0;}
  function wavRecording(chunks){
    const length=chunks.reduce((sum,chunk)=>sum+chunk.length,0),buffer=new ArrayBuffer(44+length*2),view=new DataView(buffer);
    const word=(offset,text)=>{for(let i=0;i<text.length;i++)view.setUint8(offset+i,text.charCodeAt(i));};
    word(0,'RIFF');view.setUint32(4,36+length*2,true);word(8,'WAVE');word(12,'fmt ');view.setUint32(16,16,true);
    view.setUint16(20,1,true);view.setUint16(22,1,true);view.setUint32(24,16000,true);view.setUint32(28,32000,true);
    view.setUint16(32,2,true);view.setUint16(34,16,true);word(36,'data');view.setUint32(40,length*2,true);
    let offset=44;for(const chunk of chunks)for(const sample of chunk){view.setInt16(offset,sample,true);offset+=2;}
    return new Blob([buffer],{type:'audio/wav'});
  }
  async function transcribeUtterance(live,chunks){
    const revision=state.revision;live.processing=true;live.controller=new AbortController();paintVoice();
    try{
      const form=new FormData();form.append('provider',live.provider);form.append('key',live.key);form.append('language',$('language').value);
      form.append('audio',wavRecording(chunks),'recording.wav');
      const result=await request('transcribe',form,{form:true,signal:live.controller.signal});
      if(live.closed||live.muted||state.live!==live||revision!==state.revision)return;
      const text=(result.text||'').trim();if(text)await send(text);
    }catch(e){if(e.name!=='AbortError'&&!live.closed&&revision===state.revision)error(e.message||'Speech recognition could not complete.');}
    finally{live.processing=false;live.controller=null;resetUtterance(live);if(state.live===live)paintVoice();}
  }
  function collectUtterance(live,pcm){
    // Keep a short lead-in and submit on silence. Each REST clip stays below 30 seconds.
    if(live.processing||state.busy){resetUtterance(live);return;}
    let energy=0;for(const sample of pcm)energy+=(sample/32768)**2;
    const speech=Math.sqrt(energy/pcm.length)>0.015;
    if(!live.chunks.length&&!speech){live.preRoll.push(pcm);while(live.preRoll.length>4)live.preRoll.shift();return;}
    if(!live.chunks.length){live.chunks=live.preRoll;live.preRoll=[];live.sampleCount=live.chunks.reduce((sum,chunk)=>sum+chunk.length,0);}
    live.chunks.push(pcm);live.sampleCount+=pcm.length;
    if(speech){live.speechSamples+=pcm.length;live.silenceSamples=0;}else live.silenceSamples+=pcm.length;
    if(live.silenceSamples>=11200||live.sampleCount>=400000){
      const chunks=live.chunks,enough=live.speechSamples>=4800;resetUtterance(live);
      if(enough)transcribeUtterance(live,chunks);
    }
  }
  function sendMic(live,pcm){
    if(live.closed||live.muted||!live.ready)return;
    if(live.turnBased){collectUtterance(live,pcm);return;}
    if(!state.busy&&live.ws?.readyState===1)live.ws.send(JSON.stringify({realtimeInput:{audio:{data:bytesToBase64(new Uint8Array(pcm.buffer)),mimeType:'audio/pcm;rate=16000'}}}));
  }
  async function attachMic(live,stream){
    const Ctx=window.AudioContext||window.webkitAudioContext;let ctx;try{ctx=new Ctx({sampleRate:16000});}catch{ctx=new Ctx();}
    live.micCtx=ctx;if(ctx.state==='suspended')await ctx.resume();if(live.closed)return;
    live.stream=stream;const source=ctx.createMediaStreamSource(stream);const rate=ctx.sampleRate;
    const worklet=`class J extends AudioWorkletProcessor{process(i){const c=i[0]&&i[0][0];if(c)this.port.postMessage(c.slice());return true;}}registerProcessor('j',J);`;
    try{
      const url=URL.createObjectURL(new Blob([worklet],{type:'application/javascript'}));try{await ctx.audioWorklet.addModule(url);}finally{URL.revokeObjectURL(url);}if(live.closed)return;
      const node=new AudioWorkletNode(ctx,'j');let pending=[],length=0;
      node.port.onmessage=e=>{const chunk=pcm16(e.data,rate);pending.push(chunk);length+=chunk.length;if(length<1024)return;const out=new Int16Array(length);let offset=0;for(const part of pending){out.set(part,offset);offset+=part.length;}pending=[];length=0;sendMic(live,out);};
      const silent=ctx.createGain();silent.gain.value=0;source.connect(node);node.connect(silent);silent.connect(ctx.destination);live.node=node;
    }catch{
      if(live.closed)return;
      const processor=ctx.createScriptProcessor(4096,1,1);const silent=ctx.createGain();silent.gain.value=0;
      processor.onaudioprocess=e=>sendMic(live,pcm16(e.inputBuffer.getChannelData(0),rate));
      source.connect(processor);processor.connect(silent);silent.connect(ctx.destination);live.node=processor;
    }
  }
  async function startLive(){
    const provider=voiceProvider(),revision=state.voiceRevision,key=state.keys?.[provider+'_key'];
    if(state.live||!provider)return;
    if(!canStream(provider))throw new Error('Voice needs microphone and audio support in your browser. You can still type.');
    status('Starting live voice…');
    let stream;try{stream=state.pendingMic?await state.pendingMic:await navigator.mediaDevices.getUserMedia(micConstraints());state.pendingMic=null;}
    catch(e){state.pendingMic=null;throw new Error(e.name==='NotAllowedError'?'Allow the microphone to start live voice, then tap Voice.':'Could not open the microphone. Check Audio settings and browser permissions.');}
    if(revision!==state.voiceRevision||state.voiceSuspended||$('app').hidden){stream.getTracks().forEach(t=>t.stop());return;}
    const live={provider,key,turnBased:provider==='sarvam'||!!state.document,closed:false,muted:false,ready:false,speaking:false,processing:false,controller:null,retries:state.liveRetries,stream,ws:null,sources:[],nextTime:0,handle:state.liveHandle};
    state.live=live;
    try{
    if(live.turnBased){
      resetUtterance(live);live.ready=true;await attachMic(live,stream);
      if(!live.closed){log(`${labels[provider]} voice listening${state.document?' with document retrieval':''}.`);paintVoice();}return;
    }
    const Play=window.AudioContext||window.webkitAudioContext;live.playCtx=new Play();if(live.playCtx.state==='suspended')await live.playCtx.resume();
    if($('speaker').value&&live.playCtx.setSinkId){try{await live.playCtx.setSinkId($('speaker').value);}catch{}}
    let token='',model='gemini-3.8-live',constrained=false;
    try{const issued=await request('live/token',{key,language:$('language').value||'en-IN'});token=issued.token;model=issued.model||model;constrained=!!issued.constrained;}
    catch{token='';}
    if(live.closed){stream.getTracks().forEach(t=>t.stop());return;}
    const socket=token
      ?`wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContentConstrained?access_token=${encodeURIComponent(token)}`
      :`wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=${encodeURIComponent(key)}`;
    const ws=new WebSocket(socket);live.ws=ws;
    ws.onopen=()=>{if(live.closed){ws.close();return;}ws.send(JSON.stringify({setup:{model:'models/'+model,generationConfig:{responseModalities:['AUDIO'],speechConfig:{voiceConfig:{prebuiltVoiceConfig:{voiceName:'Kore'}}}},systemInstruction:{parts:[{text:'You are Jarvis in a continuous live voice conversation. Keep spoken replies short and match the person\'s language.'}]},inputAudioTranscription:{},outputAudioTranscription:{},sessionResumption:live.handle?{handle:live.handle}:{},realtimeInputConfig:{automaticActivityDetection:{silenceDurationMs:700}}}}));};
    ws.onmessage=event=>{
      let data;try{data=JSON.parse(event.data);}catch{return;}
      if(data.setupComplete){live.ready=true;state.liveRetries=0;log('Live voice streaming.');paintVoice();return;}
      if(data.sessionResumptionUpdate?.newHandle)state.liveHandle=data.sessionResumptionUpdate.newHandle;
      if(data.goAway&&!live.closing){live.closing=true;setTimeout(()=>{if(state.live===live){endLive('reconnect');ensureLive();}},800);return;}
      const content=data.serverContent;if(!content)return;
      if(content.interrupted){interruptPlayback(live);state.liveLine=null;paintVoice();}
      if(content.interimInputTranscription?.text||content.inputTranscription?.text)showTranscript('user',content.interimInputTranscription?.text||content.inputTranscription.text);
      if(content.outputTranscription?.text)showTranscript('assistant',content.outputTranscription.text);
      for(const part of content.modelTurn?.parts||[]){const inline=part.inlineData||part.inline_data;if(inline?.data)enqueuePcm(live,b64ToBytes(inline.data));}
      if(content.turnComplete)state.liveLine=null;
    };
    ws.onerror=()=>{if(state.live===live)error('Live voice connection failed. Tap Voice to try again.');};
    ws.onclose=()=>{if(live.closed||state.live!==live)return;if(state.liveRetries<3&&voiceProvider()==='gemini'&&!$('app').hidden){state.liveRetries++;endLive('reconnect');ensureLive();}else{endLive();error('Live voice disconnected. Tap Voice to start again.');}}
    await attachMic(live,stream);
    paintVoice();
    }catch(e){if(state.live===live)endLive('failed');throw e;}
  }
  $('record').onclick=async()=>{
    if(!voiceProvider()){error('Select Sarvam or Gemini with a key in Settings to use voice.');return;}
    error('');
    if(!state.live){state.liveRetries=0;if(!canStream()){error('Voice needs microphone and audio support in your browser.');return;}await ensureLive();return;}
    muteLive(!state.live.muted);
  };
  window.addEventListener?.('pagehide',()=>{state.voiceSuspended=true;abortTurn();endLive();});
  window.addEventListener?.('pageshow',()=>{state.voiceSuspended=false;ensureLive();});
  function initialize(){
    // Older versions persisted credentials. Always require fresh entry now.
    vault.clear();cleanInputs();
    showGate();
  }
  initialize();
})();
