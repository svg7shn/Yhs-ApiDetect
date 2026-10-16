"""Offline Chrome deletion checks; no server or user records are accessed."""
import html
import re
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/record-delete-check'
OUT.mkdir(parents=True,exist_ok=True)
css=(ROOT/'web/static/record-delete.css').read_text(encoding='utf-8')
js=(ROOT/'web/static/record-delete.js').read_text(encoding='utf-8')
check=r"""
(async()=>{
const tick=()=>new Promise(r=>setTimeout(r,50)), checks=[];
const assert=(v,m)=>{if(!v)throw Error(m);checks.push(m)};
let calls=0, allow=false, fail=true, alert='';
window.confirm=()=>allow;window.alert=m=>alert=m;
window.fetch=async(u,o)=>{calls++;assert(o.method==='DELETE','DELETE method');assert(o.headers['X-Requested-With']==='XMLHttpRequest','CSRF header');return {ok:!fail,status:500,json:async()=>({detail:'模拟写入失败'})}};
try{
await tick();assert(document.querySelectorAll('.record-swipe').length===3,'three swipe cards');
const row=document.querySelector('tr');assert(row.firstElementChild.className==='record-delete-cell','leftmost trash icon');
const card=document.querySelector('article');card.setPointerCapture=()=>{};
card.dispatchEvent(new PointerEvent('pointerdown',{button:0,clientX:200,clientY:50,pointerId:1}));
card.dispatchEvent(new PointerEvent('pointermove',{clientX:90,clientY:52,pointerId:1}));
assert(card.parentElement.classList.contains('is-open'),'left swipe reveals delete');
card.dispatchEvent(new PointerEvent('pointerup',{pointerId:1}));await tick();
const b=card.parentElement.querySelector('button');b.click();await tick();assert(calls===0,'cancel sends no request');
allow=true;b.click();await tick();assert(card.isConnected&&!b.disabled&&alert,'failure preserves row and enables retry');
fail=false;b.click();await tick();assert(!card.isConnected,'success removes record');
const other=document.querySelector('article');other.dispatchEvent(new KeyboardEvent('keydown',{key:'Delete',bubbles:true}));assert(other.parentElement.classList.contains('is-open'),'keyboard reveals delete');
for (const key of ['Escape','ArrowRight']) {
 document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key,bubbles:true}));
 assert(!other.parentElement.classList.contains('is-open')&&document.activeElement===other,'keyboard closes and restores focus '+key);
 other.dispatchEvent(new KeyboardEvent('keydown',{key:'Delete',bubbles:true}));
}
const savedFetch=window.fetch, trash=row.querySelector('button');
window.fetch=async()=>{throw new TypeError('Failed to fetch')};trash.click();await tick();
assert(row.isConnected&&!trash.disabled&&alert.includes('无法确认')&&!alert.includes('已保留'),'network uncertainty preserves UI without false promise');
window.fetch=async()=>({ok:false,status:502,json:async()=>{throw Error('invalid JSON')}});trash.click();await tick();
assert(row.isConnected&&!trash.disabled&&alert.includes('可能已部分清理'),'HTTP fallback warns about partial cleanup');
window.fetch=async()=>({ok:false,status:500,json:async()=>({error:{message:'本地删除失败，可能仍有残留'}})});trash.click();await tick();
assert(alert==='本地删除失败，可能仍有残留','backend failure message preserved');
window.fetch=savedFetch;trash.click();await tick();assert(!row.isConnected,'trash icon successfully deletes after retry');
const stale=document.createElement('article');stale.dataset.deleteUrl='/api/history/test';document.body.append(stale);await tick();assert(!stale.isConnected,'stale refresh cannot resurrect deleted record');
await new Promise(r=>setTimeout(r,250));
assert(other.getBoundingClientRect().left < other.parentElement.getBoundingClientRect().left-80,'delete area visibly exposed');
assert(document.documentElement.scrollWidth<=innerWidth,'no page overflow');
document.body.dataset.checks=JSON.stringify(checks);
}catch(e){document.body.dataset.failure=e.message}
})();
"""
body=''.join('<article data-delete-url="'+u+'" style="padding:24px;min-height:80px"><strong>模型检测记录</strong><p>本地记录 · 左滑删除</p></article>' for u in ['/api/history/test','/api/channel/records/test','/api/authenticity/records/test'])
page=OUT/'offline.html'
page.write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><style>body{margin:20px;max-width:780px;font-family:sans-serif}table{width:100%}'+css+'</style>'+body+'<table><tr data-delete-url="/api/image/history/test"><td>2026-10-13</td><td>图片检测记录</td></tr></table><script>'+js+check+'</script>',encoding='utf-8')
for name,size in [('desktop','1200,850'),('mobile','540,850')]:
 r=subprocess.run(['C:/Program Files/Google/Chrome/Application/chrome.exe','--headless','--force-prefers-reduced-motion','--disable-gpu','--no-first-run','--disable-background-networking','--virtual-time-budget=3000','--window-size='+size,'--screenshot='+str(OUT/(name+'.png')),'--dump-dom',page.as_uri()],capture_output=True,encoding='utf-8',errors='replace',timeout=45)
 (OUT/(name+'.dom.html')).write_text(r.stdout,encoding='utf-8')
 if r.returncode or 'data-failure=' in r.stdout or 'data-checks=' not in r.stdout: raise RuntimeError(name+': '+r.stdout[-4000:])
 print(name+': '+html.unescape(re.search(r'data-checks="([^"]*)"',r.stdout).group(1)))
