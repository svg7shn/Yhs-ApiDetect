"""Offline evidence badge layout regression using production template, JS and CSS."""
import json
import re
import subprocess
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/channel-badge-check'
OUT.mkdir(parents=True, exist_ok=True)
html = Environment(loader=FileSystemLoader(ROOT/'web/templates')).get_template('channel.html').render(request={'url': {'path': '/channel'}})
html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S)
html = re.sub(r'<link\b[^>]*>', '', html)
html = html.replace('<head>', '''<head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:">''')
css = '\n'.join((ROOT/'web/static'/f).read_text(encoding='utf-8') for f in ['style.css','channel.css','page-theme.css'])
js = (ROOT/'web/static/channel.js').read_text(encoding='utf-8')
record = dict(id='badge-fixture', model='离线排版验证', base_url='https://example.invalid', source_label='模拟记录', evidence=[dict(id='message_id_prefix', name='消息 ID 前缀', category='protocol', status=s, weight=0 if s=='unknown' else 1, detail='仅用于验证结果标签排版，说明文字保持自动换行。'*3) for s in ['pass','fail','unknown','error']])
mock = 'const record='+json.dumps(record)+';window.fetch=async(url)=>({ok:true,json:async()=>String(url).includes("/records/badge-fixture")?record:{records:[record],sources:[]}});'
check = '''
setTimeout(()=>document.querySelector('#ch-records button').click(),100);
setTimeout(()=>{try {
const assert=(v,m)=>{if(!v)throw Error(m)};
const badges=[...document.querySelectorAll('.ch-table .ch-badge')];
assert(badges.length===4,'badge count');
for(const badge of badges){const range=document.createRange();range.selectNodeContents(badge);
assert(range.getClientRects().length===1,'wrapped '+badge.textContent);
assert(getComputedStyle(badge).whiteSpace==='nowrap','nowrap');
assert(badge.getBoundingClientRect().right<=badge.parentElement.getBoundingClientRect().right,'cell overflow');}
const pass=badges[0], before=getComputedStyle(pass), colors=[before.color,before.backgroundColor,before.borderRadius];
const style=document.createElement('style');style.textContent='.ch-table .ch-badge{white-space:normal}';document.head.append(style);
const after=getComputedStyle(pass);assert(JSON.stringify(colors)===JSON.stringify([after.color,after.backgroundColor,after.borderRadius]),'palette changed');style.remove();
assert(getComputedStyle(document.querySelector('.ch-detail')).whiteSpace!=='nowrap','description wrapping');
document.body.dataset.checks='PASS: single-line, cell fit, preserved colors and description wrapping';
}catch(e){document.body.dataset.failure=e.message}},400);
'''
html=html.replace('</head>','<style>'+css+'</style></head>').replace('</body>','<script>'+mock+js+check+'</script></body>')
chrome='C:/Program Files/Google/Chrome/Application/chrome.exe'
results=[]
for theme,width in [('dark',901),('light',901),('dark',540)]:
    name=theme+'-'+str(width)
    page=OUT/(name+'.html')
    page.write_text(re.sub(r'<html\b[^>]*>', '<html lang="zh-CN" data-theme="'+theme+'">', html, count=1),encoding='utf-8')
    result=subprocess.run([chrome,'--headless','--disable-gpu','--hide-scrollbars','--no-first-run','--disable-background-networking','--virtual-time-budget=1500','--window-size='+str(width)+',879','--screenshot='+str(OUT/(name+'.png')),'--dump-dom',page.as_uri()],capture_output=True,encoding='utf-8',errors='replace',timeout=40)
    (OUT/(name+'.dom.html')).write_text(result.stdout,encoding='utf-8')
    assert result.returncode==0 and 'data-checks=' in result.stdout and 'data-failure=' not in result.stdout, name+' failed; inspect DOM'
    results.append(name+': PASS')
    print(results[-1])
(OUT/'results.txt').write_text('\n'.join(results),encoding='utf-8')
