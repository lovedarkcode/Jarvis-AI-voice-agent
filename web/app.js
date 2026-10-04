/* Plain-JS state provider: secrets remain in this closure, never in events or URLs. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id), vault=window.JarvisVault;
  const providers=['openai','claude','sarvam'];
  const state={keys:null,models:null,messages:[],document:'',busy:false,revision:0,controller:null,
    recorder:null,stream:null,recordTimer:null,recordCancelled:false,audio:null,audioURL:null,returnFocus:null};
  const changes=new EventTarget();
  window.JarvisState=Object.freeze({subscribe(fn){const listener=e=>fn(e.detail);changes.addEventListener('change',listener);return()=>changes.removeEventListener('change',listener);},
    snapshot:()=>({ready:!!state.keys,busy:state.busy,provider:$('provider').value})});
  function publish(){changes.dispatchEvent(new CustomEvent('change',{detail:window.JarvisState.snapshot()}));}
  function status(text){$('state').textContent=text;}
  function log(text){const row=document.createElement('div');row.textContent=new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})+' · '+text;$('activity').append(row);while($('activity').children.length>100)$('activity').firstChild.remove();$('activity').scrollTop=$('activity').scrollHeight;}
  function error(text,target='chat-error'){$(target).textContent=text;}
  function preference(key,fallback){try{return localStorage.getItem('jarvis.'+key)||fallback;}catch{return fallback;}}
  function remember(key,value){try{localStorage.setItem('jarvis.'+key,value);}catch{}}
  function theme(value){const dark=value==='dark';document.documentElement.dataset.theme=dark?'dark':'light';document.querySelectorAll('.theme span').forEach(x=>x.textContent=dark?'Light mode':'Dark mode');remember('theme',value);}
  theme(preference('theme',matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'));
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
    for(const p of providers){const key=keys[p+'_key']||'';const prefix=p==='openai'?'sk-':p==='claude'?'sk-ant-':'';
      if(key.length<16||key.length>512||!key.startsWith(prefix)||!/^[!-~]+$/.test(key))errors[p]=`Enter a valid ${p==='claude'?'Claude':p==='sarvam'?'Sarvam':'OpenAI'} key${prefix?' starting with '+prefix:''}.`;
    }return errors;
  }
  function cleanInputs(){providers.forEach(p=>{$(p+'_key').value='';$(p+'_key').type='password';});$('vault-passphrase').value='';$('unlock-passphrase').value='';document.querySelectorAll('[data-reveal]').forEach(b=>{b.textContent='Show';b.setAttribute('aria-pressed','false');});}
  function showGate(message='',editing=false){
    stop();state.returnFocus=document.activeElement;document.querySelectorAll('dialog[open]').forEach(d=>d.close());closeSidebar();
    $('app').hidden=true;$('app').inert=true;$('key-gate').hidden=false;$('gate-loading').hidden=true;$('keys-form').hidden=false;$('unlock-form').hidden=true;$('cancel-keys').hidden=!editing;
    $('gate-title').textContent=editing?'Manage your API keys':'Connect your AI workspace';
    error(message,'key-error');error('','unlock-error');
    $('openai_key').focus();
  }
  function enter(keys,models){
    state.keys=Object.freeze({...keys});state.models=models;
    $('key-gate').hidden=true;$('app').hidden=false;$('app').inert=false;cleanInputs();
    status('Ready');publish();$('message').focus();log('OpenAI, Claude and Sarvam are ready.');
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
  async function verify(keys){const invalid=validate(keys);if(Object.keys(invalid).length)throw new Error(Object.values(invalid)[0]);return request('keys/verify',keys,{verification:true});}
  document.querySelectorAll('[data-reveal]').forEach(button=>{button.onclick=()=>{const input=$(button.dataset.reveal),show=input.type==='password';input.type=show?'text':'password';button.textContent=show?'Hide':'Show';button.setAttribute('aria-pressed',String(show));};});
  document.querySelectorAll('input[name=storage]').forEach(r=>r.onchange=()=>{$('passphrase-row').hidden=document.querySelector('input[name=storage]:checked').value!=='persistent';});
  $('keys-form').onsubmit=async event=>{
    event.preventDefault();if($('save-keys').disabled)return;
    const keys=Object.fromEntries(providers.map(p=>[p+'_key',$(p+'_key').value.trim()]));const invalid=validate(keys);
    providers.forEach(p=>{$(p+'-error').textContent=invalid[p]||'';$(p+'_key').setAttribute('aria-invalid',String(!!invalid[p]));});
    if(Object.keys(invalid).length){$(Object.keys(invalid)[0]+'_key').focus();return;}
    const mode=document.querySelector('input[name=storage]:checked').value,passphrase=$('vault-passphrase').value;
    if(mode==='persistent'&&passphrase.length<12){error('Use an encryption passphrase of at least 12 characters.','key-error');$('vault-passphrase').focus();return;}
    $('save-keys').disabled=true;$('cancel-keys').disabled=true;$('save-keys').textContent='Verifying your providers…';error('','key-error');
    try{const result=await verify(keys);await vault.save(keys,mode,passphrase);enter(keys,result.models);}
    catch(e){error(e.message||'Keys could not be saved. Check browser storage permissions.','key-error');}
    finally{$('save-keys').disabled=false;$('cancel-keys').disabled=false;$('save-keys').textContent='Save & start application →';}
  };
  $('unlock-form').onsubmit=async event=>{
    event.preventDefault();const button=$('unlock-form').querySelector('[type=submit]');if(button.disabled)return;button.disabled=true;$('reset-vault').disabled=true;error('','unlock-error');
    let keys;try{keys=await vault.unlock($('unlock-passphrase').value);}catch{error('Could not unlock. Check your passphrase, or enter your keys again.','unlock-error');button.disabled=false;$('reset-vault').disabled=false;return;}
    try{const result=await verify(keys);enter(keys,result.models);}catch(e){error(e.message,'unlock-error');}finally{button.disabled=false;$('reset-vault').disabled=false;$('unlock-passphrase').value='';}
  };
  $('reset-vault').onclick=()=>{vault.clear();showGate();};
  $('edit-keys').onclick=()=>{showGate('',true);providers.forEach(p=>{$(p+'_key').value=state.keys?.[p+'_key']||'';});};
  $('cancel-keys').onclick=()=>{if(state.keys)enter(state.keys,state.models);};
  $('forget-keys').onclick=()=>{state.keys=null;vault.clear();cleanInputs();publish();showGate('Saved keys have been removed.');};

  const avatar=document.querySelector('.avatar svg').cloneNode(true);
  function message(role,text){$('welcome').hidden=true;const row=document.createElement('div');row.className='message '+role;if(role==='assistant'){const icon=document.createElement('span');icon.className='avatar';icon.append(avatar.cloneNode(true));row.append(icon);}const body=document.createElement('div');body.className='message-body';body.textContent=text;row.append(body);$('feed').append(row);$('conversation').scrollTop=$('conversation').scrollHeight;while($('feed').children.length>200)$('feed').firstChild.remove();}
  function busy(value){state.busy=value;$('send').disabled=value;$('provider').disabled=value;publish();}
  function stop(){
    state.revision++;state.controller?.abort();state.controller=null;clearTimeout(state.recordTimer);
    state.recordCancelled=true;if(state.recorder&&state.recorder.state!=='inactive')state.recorder.stop();state.stream?.getTracks().forEach(t=>t.stop());state.stream=null;
    if(state.audio){state.audio.pause();state.audio.removeAttribute('src');state.audio.load();state.audio=null;}if(state.audioURL){URL.revokeObjectURL(state.audioURL);state.audioURL=null;}
    $('record').textContent='◉ Voice';busy(false);status(state.keys?'Ready':'Setup required');
  }
  $('interrupt').onclick=()=>{stop();log('Stopped.');};
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('key-gate').hidden)return;if(e.key==='Escape')stop();});
  async function send(text){
    if(!state.keys||state.busy||!text.trim())return;
    stop();const revision=state.revision;state.controller=new AbortController();const signal=state.controller.signal;
    text=text.trim().slice(0,16000);message('user',text);state.messages.push({role:'user',content:text});state.messages=state.messages.slice(-23);$('message').value='';error('');busy(true);status('Thinking…');
    const provider=$('provider').value;log(`Sending to ${provider==='claude'?'Claude':'OpenAI'}.`);
    try{
      const data=await request('chat',{provider,key:state.keys[provider+'_key'],messages:state.messages,document:state.document},{signal});
      if(revision!==state.revision)return;
      message('assistant',data.text);state.messages.push({role:'assistant',content:data.text.slice(0,16000)});status('Ready');
      if($('spoken').checked){
        status('Preparing speech…');const speech=await request('speech',{key:state.keys.sarvam_key,text:data.text.slice(0,2500),language:$('language').value},{signal});
        if(revision!==state.revision)return;
        for(const encoded of speech.audios||[]){
          if(revision!==state.revision)break;
          const bytes=Uint8Array.from(atob(encoded),c=>c.charCodeAt(0));state.audioURL=URL.createObjectURL(new Blob([bytes],{type:'audio/wav'}));
          state.audio=new Audio(state.audioURL);if($('speaker').value&&state.audio.setSinkId)await state.audio.setSinkId($('speaker').value);
          status('Speaking…');await new Promise((resolve,reject)=>{state.audio.onended=resolve;state.audio.onerror=()=>reject(new Error('Audio playback failed.'));signal.addEventListener('abort',resolve,{once:true});state.audio.play().catch(()=>reject(new Error('Reply is ready. Enable playback in your browser or turn off spoken replies.')));});
          if(state.audioURL){URL.revokeObjectURL(state.audioURL);state.audioURL=null;}
        }
      }
    }catch(e){if(e.name!=='AbortError'&&revision===state.revision){error(e.message);log('Request could not complete.');}}
    finally{if(revision===state.revision){busy(false);status('Ready');}}
  }
  $('send').onclick=()=>send($('message').value);
  $('message').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();send($('message').value);}};
  $('new-chat').onclick=()=>{stop();state.messages=[];state.document='';$('file-name').textContent='';$('feed').replaceChildren();$('welcome').hidden=false;error('');closeSidebar();$('message').focus();};
  $('provider').value=preference('provider','openai')==='claude'?'claude':'openai';$('provider').onchange=()=>{remember('provider',$('provider').value);publish();};
  $('spoken').checked=preference('spoken','true')==='true';$('spoken').onchange=()=>remember('spoken',String($('spoken').checked));
  $('language').value=preference('language','en-IN');if(!$('language').value)$('language').value='en-IN';$('language').onchange=()=>remember('language',$('language').value);
  $('file').onchange=async e=>{const file=e.target.files[0];if(!file)return;e.target.value='';if(file.size>96000){error('Attach a text document under 96 KB (up to 24,000 characters).');return;}const text=await file.text();if(text.length>24000){error('This document is too long. Attach an excerpt of up to 24,000 characters.');return;}state.document=text;$('file-name').textContent=file.name;log('Text document attached.');};

  async function listDevices(){
    if(!navigator.mediaDevices?.enumerateDevices){error('Audio device selection is unavailable in this browser.','audio-error');return;}
    try{const devices=await navigator.mediaDevices.enumerateDevices();for(const [id,kind] of [['microphone','audioinput'],['speaker','audiooutput']]){const selected=$(id).value||preference(id,'');$(id).replaceChildren(new Option('System default',''));let n=0;devices.filter(d=>d.kind===kind).forEach(d=>$(id).add(new Option(d.label||`${id} ${++n}`,d.deviceId)));$(id).value=selected;if(!$(id).value)$(id).value='';}}
    catch{error('Could not list audio devices. Check browser permissions.','audio-error');}
  }
  $('speaker').disabled=typeof HTMLMediaElement.prototype.setSinkId!=='function';$('speaker-note').textContent=$('speaker').disabled?'This browser uses your system-default speaker.':'';
  ['microphone','speaker'].forEach(id=>$(id).onchange=()=>remember(id,$(id).value));
  $('refresh-devices').onclick=async()=>{try{const stream=await navigator.mediaDevices.getUserMedia({audio:true});stream.getTracks().forEach(t=>t.stop());await listDevices();error('','audio-error');}catch{error('Microphone access was denied or no device is available. Check browser permissions.','audio-error');}};
  $('record').onclick=async()=>{
    if(!state.keys)return;if(state.recorder?.state==='recording'){state.recorder.stop();return;}
    stop();const revision=state.revision;error('');
    if(!navigator.mediaDevices?.getUserMedia||typeof MediaRecorder==='undefined'){error('Voice recording is unavailable in this browser. You can still type.');return;}
    try{
      const device=$('microphone').value||preference('microphone','');const stream=await navigator.mediaDevices.getUserMedia({audio:{...(device?{deviceId:{exact:device}}:{}),echoCancellation:true,noiseSuppression:true}});
      if(revision!==state.revision){stream.getTracks().forEach(t=>t.stop());return;}
      state.stream=stream;state.recordCancelled=false;const chunks=[];
      const mime=['audio/webm','audio/mp4','audio/ogg'].find(t=>MediaRecorder.isTypeSupported(t));
      state.recorder=new MediaRecorder(stream,mime?{mimeType:mime}:{});const recorder=state.recorder;
      recorder.ondataavailable=e=>{if(e.data.size)chunks.push(e.data);};
      recorder.onstop=async()=>{
        clearTimeout(state.recordTimer);stream.getTracks().forEach(t=>t.stop());$('record').textContent='◉ Voice';
        if(state.recordCancelled||revision!==state.revision)return;
        const blob=new Blob(chunks,{type:recorder.mimeType});if(!blob.size||blob.size>3500000){error('Recording is empty or too large. Try a shorter message.');busy(false);status('Ready');return;}
        const form=new FormData();form.append('key',state.keys.sarvam_key);form.append('audio',blob,'recording');state.controller=new AbortController();busy(true);status('Transcribing…');
        try{const data=await request('transcribe',form,{form:true,signal:state.controller.signal});if(revision!==state.revision)return;busy(false);if(data.text)await send(data.text);else {status('Ready');error('No speech detected. Check your microphone and try again.');}}
        catch(e){if(e.name!=='AbortError'&&revision===state.revision){busy(false);status('Ready');error(e.message);}}
      };
      recorder.start();busy(true);status('Recording…');$('record').textContent='■ Send recording';state.recordTimer=setTimeout(()=>{if(recorder.state==='recording')recorder.stop();},30000);
    }catch{error('Could not open the microphone. Check Audio settings and browser permissions.');status('Ready');}
  };

  async function initialize(){
    try{if(!vault)throw new Error('The secure-storage module did not load. Reload this page.');
      const keys=await vault.loadSession();
      if(keys){const result=await verify(keys);enter(keys,result.models);return;}
      if(vault.hasPersistent()){$('gate-loading').hidden=true;$('unlock-form').hidden=false;$('unlock-passphrase').focus();return;}
      showGate();
    }catch(e){showGate(e.message||'Saved keys could not be loaded. Enter your keys again.');}
  }
  initialize();
})();
