const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {webcrypto}=require('node:crypto');
function storage(){const map=new Map();return {getItem:k=>map.get(k)||null,setItem:(k,v)=>map.set(k,v),removeItem:k=>map.delete(k),map};}
function setup(local=storage(),session=storage()){
  const c=vm.createContext({window:{},crypto:webcrypto,TextEncoder,TextDecoder,btoa,atob,localStorage:local,sessionStorage:session});
  vm.runInContext(fs.readFileSync('web/vault.js','utf8'),c);
  return {vault:c.window.JarvisVault,local,session};
}
const keys={openai_key:'sk-openai-test-only',claude_key:'sk-ant-test-only',sarvam_key:'sarvam-test-only'};
test('session storage is encrypted and reloadable within the same tab',async()=>{
 const {vault,local,session}=setup();await vault.save(keys,'session');
 assert(![...session.map.values()].join('').includes(keys.openai_key));assert.equal(local.map.size,0);
 assert.deepEqual(JSON.parse(JSON.stringify(await setup(local,session).vault.loadSession())),keys);
 assert.equal(await setup(local).vault.loadSession(),null);
});
test('persistent vault requires passphrase, never stores it, and survives sessions',async()=>{
 const {vault,local,session}=setup();const passphrase='a long test passphrase';await vault.save(keys,'persistent',passphrase);
 const stored=[...local.map.values()].join('');assert(!stored.includes(passphrase));assert(!stored.includes(keys.claude_key));assert.equal(session.map.size,0);
 const next=setup(local).vault;assert(next.hasPersistent());await assert.rejects(next.unlock('wrong passphrase'));
 assert.deepEqual(JSON.parse(JSON.stringify(await next.unlock(passphrase))),keys);
});
test('switching persistence and forgetting removes obsolete secrets',async()=>{
 const {vault,local,session}=setup();await vault.save(keys,'persistent','long passphrase 123');await vault.save(keys,'session');
 assert.equal(local.map.size,0);vault.clear();assert.equal(session.map.size,0);
});
test('tampering and blocked storage fail closed',async()=>{
 const {vault,session}=setup();await vault.save(keys,'session');const key=[...session.map.keys()][0];const record=JSON.parse(session.getItem(key));record.data='AAAA';session.setItem(key,JSON.stringify(record));await assert.rejects(vault.loadSession());
 const broken=storage();broken.setItem=()=>{throw Error('Storage disabled');};await assert.rejects(setup(storage(),broken).vault.save(keys,'session'));
});
