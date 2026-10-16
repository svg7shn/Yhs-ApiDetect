"""Offline grade rendering checks: production JS/CSS, no network or credentials."""
import json
import re
import subprocess
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/channel-grade-check'
OUT.mkdir(parents=True, exist_ok=True)
html = Environment(loader=FileSystemLoader(ROOT/'web/templates')).get_template('channel.html').render(request={'url': {'path': '/channel'}})
html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S)
html = re.sub(r'<link\b[^>]*>', '', html)
html = html.replace('<head>', '''<head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:">''')
css = '\n'.join((ROOT/'web/static'/f).read_text(encoding='utf-8') for f in ['style.css','channel.css','page-theme.css'])
js = (ROOT/'web/static/channel.js').read_text(encoding='utf-8')
values = [100,90,89,75,74,60,59,0,None,-1,101]
tones = ['excellent','excellent','good','good','fair','fair','poor','poor','neutral','neutral','neutral']
records = [dict(id=str(i),model='等级示例 '+str(v),score=v,capability_accuracy=v/100 if v is not None else None,evidence=[dict(category='protocol' if i%3==0 else 'behavior',status='pass')] if i%3!=2 else []) for i,v in enumerate(values)]
mock = 'const records='+json.dumps(records)+';window.fetch=async()=>({ok:true,json:async()=>({records,sources:[]})});'
check = '''
setTimeout(()=>{try {
const cards=[...document.querySelectorAll('#ch-records .ch-record')];
const expected=TONES;
const assert=(v,m)=>{if(!v)throw Error(m)};
assert(cards.length===expected.length,'record count');
const colors=new Set();
cards.forEach((card,i)=>{const chips=card.querySelectorAll('.ch-meta');assert(chips.length===3,'three badges');
for(const n of [chips[1],chips[2]]) assert(n.classList.contains('ch-grade-'+expected[i]),'grade boundary '+i);
assert(chips[0].classList.contains('ch-grade-'+['good','fair','neutral'][i%3]),'evidence independence');
const s=getComputedStyle(chips[1]);colors.add(s.color);assert(s.color!==s.backgroundColor,'visible text');
assert(chips[2].getBoundingClientRect().right<=innerWidth,'badge overflow');});
assert(colors.size===5,'five distinct grade colors');
assert(getComputedStyle(cards[0].querySelectorAll('.ch-meta')[1]).color === (document.documentElement.dataset.theme==='dark' ? 'rgb(128, 226, 182)' : 'rgb(18, 107, 75)'), 'theme palette');
document.querySelectorAll('.channel-page > :not(section)').forEach(n=>n.hidden=true);
document.querySelectorAll('.ch-panel').forEach(n=>{if(!n.contains(document.getElementById('ch-records')))n.hidden=true;});
document.querySelectorAll('header,footer,nav').forEach(n=>n.hidden=true);
document.body.dataset.checks='PASS: boundaries, missing/invalid, evidence independence, five colors, layout';
}catch(e){document.body.dataset.failure=e.message}},200);
'''.replace('TONES',json.dumps(tones))
html=html.replace('</head>','<style>'+css+'</style></head>').replace('</body>','<script>'+mock+js+check+'</script></body>')
chrome='C:/Program Files/Google/Chrome/Application/chrome.exe'
for theme,width in [('dark',1100),('light',1100),('dark',540)]:
    name=theme+'-'+str(width)
    page=OUT/(name+'.html')
    themed=html
    themed=re.sub(r'<html\b[^>]*>', '<html lang="zh-CN" data-theme="'+theme+'">', themed, count=1)
    page.write_text(themed,encoding='utf-8')
    result=subprocess.run([chrome,'--headless','--disable-gpu','--hide-scrollbars','--no-first-run','--disable-background-networking','--virtual-time-budget=1500','--window-size='+str(width)+',1300','--screenshot='+str(OUT/(name+'.png')),'--dump-dom',page.as_uri()],capture_output=True,encoding='utf-8',errors='replace',timeout=40)
    (OUT/(name+'.dom.html')).write_text(result.stdout,encoding='utf-8')
    assert result.returncode==0 and 'data-checks=' in result.stdout and 'data-failure=' not in result.stdout, name+' failed; inspect DOM'
    print(name+': PASS')
