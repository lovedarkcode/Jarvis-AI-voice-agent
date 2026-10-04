/* Browser-only encrypted credential storage. No passphrase is persisted. */
(() => {
  'use strict';
  const SESSION = 'jarvis.credentials.session.v1', PERSISTENT = 'jarvis.credentials.vault.v1';
  const enc = new TextEncoder(), dec = new TextDecoder();
  const b64 = bytes => btoa(String.fromCharCode(...new Uint8Array(bytes)));
  const unb64 = value => Uint8Array.from(atob(value), c => c.charCodeAt(0));
  function cryptoReady() {
    if (!globalThis.crypto?.subtle) throw new Error('Secure storage needs HTTPS or localhost in a modern browser.');
  }
  async function derive(passphrase, salt) {
    const material = await crypto.subtle.importKey('raw', enc.encode(passphrase), 'PBKDF2', false, ['deriveKey']);
    return crypto.subtle.deriveKey({name:'PBKDF2', salt, iterations:310000, hash:'SHA-256'}, material, {name:'AES-GCM', length:256}, false, ['encrypt','decrypt']);
  }
  async function decrypt(record, key) {
    if (record.version !== 1 || typeof record.data !== 'string' || record.data.length > 12000) throw new Error('Saved keys are unreadable. Enter them again.');
    return JSON.parse(dec.decode(await crypto.subtle.decrypt({name:'AES-GCM',iv:unb64(record.iv)}, key, unb64(record.data))));
  }
  async function save(keys, mode, passphrase='') {
    cryptoReady();
    let key, material;
    const iv=crypto.getRandomValues(new Uint8Array(12));
    const record={version:1,iv:b64(iv)};
    if(mode==='persistent') {
      if(passphrase.length<12) throw new Error('Use an encryption passphrase of at least 12 characters.');
      const salt=crypto.getRandomValues(new Uint8Array(16));
      record.salt=b64(salt); key=await derive(passphrase,salt);
    } else {
      material=crypto.getRandomValues(new Uint8Array(32));
      key=await crypto.subtle.importKey('raw',material,'AES-GCM',false,['encrypt','decrypt']);
      record.material=b64(material);
    }
    record.data=b64(await crypto.subtle.encrypt({name:'AES-GCM',iv},key,enc.encode(JSON.stringify(keys))));
    // Write the new record before clearing the old one so a quota error doesn't lose keys.
    if(mode==='persistent') {localStorage.setItem(PERSISTENT,JSON.stringify(record));sessionStorage.removeItem(SESSION);}
    else {sessionStorage.setItem(SESSION,JSON.stringify(record));localStorage.removeItem(PERSISTENT);}
  }
  async function loadSession() {
    const saved=sessionStorage.getItem(SESSION);if(!saved)return null;
    cryptoReady(); const record=JSON.parse(saved);
    const key=await crypto.subtle.importKey('raw',unb64(record.material),'AES-GCM',false,['decrypt']);
    return decrypt(record,key);
  }
  async function unlock(passphrase) {
    cryptoReady(); const record=JSON.parse(localStorage.getItem(PERSISTENT)||'null');
    if(!record) throw new Error('No saved keys found.');
    return decrypt(record,await derive(passphrase,unb64(record.salt)));
  }
  function clear() {
    try{sessionStorage.removeItem(SESSION);}catch{}
    try{localStorage.removeItem(PERSISTENT);}catch{}
  }
  window.JarvisVault=Object.freeze({save,loadSession,unlock,clear,hasPersistent:()=>!!localStorage.getItem(PERSISTENT)});
})();
