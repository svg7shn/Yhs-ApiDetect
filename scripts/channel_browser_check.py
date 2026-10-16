"""Offline Chrome check of production channel UI with mocked API, no server or keys."""
import json
import re
import subprocess
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/channel-check'
OUT.mkdir(parents=True, exist_ok=True)
from sys import path

path.insert(0, str(ROOT))
from web.channel_probe import SOURCES

html = Environment(loader=FileSystemLoader(ROOT/'web/templates')).get_template('channel.html').render(request={"url": {"path": "/channel"}})
html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.DOTALL)
html = re.sub(r'<link\b[^>]*>', '', html)
# Restrict the rendered fixture to inline resources; never load remote analytics/assets.
html = html.replace('<head>', '''<head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:">''')
css = (ROOT/'web/static/style.css').read_text(encoding='utf-8') + (ROOT/'web/static/channel.css').read_text(encoding='utf-8')
models = [{'id':'claude-test','family':'claude'},{'id':'gpt-test','family':'gpt'},{'id':'gemini-test','family':'gemini'}]
# 记录列表夹具：一条新口径（含 category / weight / id）与一条旧口径（缺这三个字段）。
EVIDENCE_NEW = [
 {'name':'消息 ID 前缀','status':'pass','detail':'消息 ID 应以 msg_ 前缀，实际：msg_bdrk_01ab','id':'message_id_prefix','category':'protocol','weight':1.5,'pass':True},
 {'name':'响应类型','status':'pass','detail':'顶层 type 应为 message','id':'response_type','category':'protocol','weight':1.0,'pass':True},
 {'name':'停止原因','status':'pass','detail':'stop_reason 应属 Anthropic 合法取值，实际：end_turn','id':'stop_reason_valid','category':'protocol','weight':1.0,'pass':True},
 {'name':'Claude 来源溯源','status':'pass','detail':'消息 ID 为 Bedrock 前缀，符合 AWS Bedrock 来源','id':'claude_source_provenance','category':'residue','weight':1.5,'pass':True},
 {'name':'计量结构','status':'pass','detail':'计量字段结构检查，不验证上游账单','id':'usage_schema','category':'protocol','weight':1.5,'pass':True},
 {'name':'能力探针','status':'pass','detail':'能力探针正确率 3/3：题目由程序生成','id':'capability_probe','category':'capability','weight':1.0,'pass':True},
 {'name':'自报身份','status':'pass','detail':'自报包含声称模型族（弱信号，可诱导）','id':'self_identity','category':'behavior','weight':1.0,'pass':True},
 {'name':'回显模型','status':'pass','detail':'仅比较模型族，不采信回显为来源证明','id':'echoed_model','category':'protocol','weight':0.5,'pass':True},
 {'name':'网关软件指纹','status':'unknown','detail':'未观察到网关软件特征','id':'gateway_software','category':'gateway','weight':0.0,'pass':False},
]
RECORDS = [
 {'id':'a'*32,'base_url':'https://relay.example.com','model':'claude-opus-4-7','family':'claude',
  'claimed_source':'claude_bedrock','source_label':'Amazon Bedrock','verdict':'authentic','score':100.0,
  'capability_accuracy':1.0,'created_at':'2026-10-14T07:00:00+00:00','duration_ms':4032,
  'summary':'协议与行为指纹与声称来源一致（非加密级，为概率判定）；未验证订阅身份或账单归属。','evidence':EVIDENCE_NEW},
 {'id':'b'*32,'base_url':'https://legacy.example.com','model':'claude-legacy-3-5','family':'claude',
  'claimed_source':'claude_official','source_label':'Anthropic 官方','verdict':'inconclusive','score':None,
  'created_at':'2026-10-13T07:00:00+00:00','duration_ms':2100,
  'summary':'仅获得有限线索或输出不完整，不能证实实际来源。',
  'evidence':[{'name':'协议结构','status':'pass','detail':'旧口径证据，没有新字段'},
              {'name':'计量结构','status':'pass','detail':'旧口径证据，没有新字段'}]},
]
mock = 'const SOURCES='+json.dumps(SOURCES)+';const MODELS='+json.dumps(models)+';const RECORDS='+json.dumps(RECORDS)+';const RECORDS_BY_ID='+json.dumps({r['id']:r for r in RECORDS})+''';
window.fetch = async (url,options={}) => ({ok:true,status:200,json:async()=> {
 if(url.endsWith('/sources')) return {sources:SOURCES};
 if(url.endsWith('/models')) return {ok:true,base_url:'https://relay.example.com',models:MODELS};
 if(url.endsWith('/run')) {window.runCount=(window.runCount||0)+1;return {ok:true,job_id:'offline'};}
 if(url.includes('/jobs/')) return {id:'offline',status:'completed',completed:3,total:3,records:[]};
 if(url.includes('/records/')) return RECORDS_BY_ID[decodeURIComponent(url.split('/records/')[1])];
 if(url.includes('/records')) return {records:RECORDS};
 return {records:[]};
}});
'''
check = '''
(async()=>{
const $=id=>document.getElementById(id); const checks=[];
const assert=(v,msg)=>{if(!v)throw Error(msg);checks.push(msg)};
const tick=()=>new Promise(r=>setTimeout(r,50));
try {
$('ch-record-refresh').click(); await new Promise(r=>setTimeout(r,250));
 const cards=document.querySelectorAll('#ch-records .ch-record');
 assert(cards.length===2,'two mocked records rendered');
 assert(cards[0].querySelector('.ch-badge').textContent==='来源一致','authentic badge reads 来源一致');
 const meta=[...cards[0].querySelectorAll('.ch-meta')].map(n=>n.textContent);
 assert(meta.join('|')==='证据 协议级|得分 100|能力 100.0%','record meta chips');
 cards[0].querySelector('button').click(); await new Promise(r=>setTimeout(r,250));
 const ths=[...document.querySelectorAll('#ch-detail-content .ch-table th')].map(n=>n.textContent);
 assert(ths.join(',')==='信号,类别,结果,权重,说明','five column headers');
 const rows=document.querySelectorAll('#ch-detail-content .ch-table tbody tr');
 assert(rows.length===9,'nine evidence rows');
 assert(rows[0].querySelector('.ch-sig-id').textContent==='message_id_prefix','signal id shown under label');
 assert(rows[0].querySelector('.ch-weight').textContent==='1.5','weight column value');
 assert(rows[8].querySelector('.ch-badge').textContent==='信息','zero-weight row reads 信息');
 if(location.hash==='#records'){document.body.dataset.checks=JSON.stringify(checks);return;}
 $('ch-detail-close').click();
 cards[1].querySelector('button').click(); await new Promise(r=>setTimeout(r,250));
 const legacy=[...document.querySelectorAll('#ch-detail-content .ch-table tbody tr')];
 assert(legacy.length===2,'legacy rows rendered');
 assert(legacy[0].querySelector('.ch-weight').textContent==='—','legacy weight shows placeholder');
 assert(!legacy[0].querySelector('.ch-sig-id'),'legacy has no signal id line');
 $('ch-detail-close').click();
 assert($('ch-configure').disabled,'initial configure disabled');
$('ch-url').value='https://relay.example.com';$('ch-key').value='offline-dummy';
$('ch-connect-form').requestSubmit(); await tick();
assert(document.querySelectorAll('.ch-chip input').length===3,'three fetched models rendered');
assert(document.querySelectorAll('.ch-chip input:checked').length===0,'models unselected by default');
$('ch-all').click();$('ch-configure').click();await tick();
assert($('ch-parameters').open,'native dialog opens');
assert($('ch-parameters').contains(document.activeElement),'focus enters dialog');
$('ch-tab-claude').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));
assert(document.activeElement.id==='ch-tab-gpt','arrow key moves family tab focus');
assert($('ch-start').disabled,'source required');
for(const [family,source] of [['claude','claude_official'],['gpt','gpt_official'],['gemini','gemini_official']]){
 $('ch-tab-'+family).click();$('ch-bulk-source').value=source;$('ch-bulk-source').dispatchEvent(new Event('change'));
}
assert(!$('ch-start').disabled,'three family sources valid');
assert($('ch-bulk-source').value==='', 'bulk source resets without misleading selection');
assert(document.documentElement.scrollWidth<=innerWidth,'no horizontal page overflow');
const rect=$('ch-parameters').getBoundingClientRect();
assert(rect.left>=0 && rect.right<=innerWidth,'dialog within actual viewport');
assert($('ch-parameters').scrollWidth<=$('ch-parameters').clientWidth,'dialog content without horizontal overflow');
checks.push('measured viewport '+innerWidth+' x '+innerHeight);
if(location.hash==='#modal') {document.body.dataset.checks=JSON.stringify(checks);return;}
$('ch-start').click();await tick();
assert(!$('ch-parameters').open,'start closes dialog');
assert(window.runCount===1,'one run request');
assert($('ch-progress').value===3,'progress completed');
assert(!$('ch-url').disabled,'configuration unlocks');
$('ch-wire').value='claude';$('ch-wire').dispatchEvent(new Event('change'));
assert($('ch-configure').disabled,'wire change invalidates connection');
$('ch-connect-form').requestSubmit();await tick();
assert(document.querySelectorAll('.ch-chip input').length===1,'native wire filters mismatched families');
$('ch-url').dispatchEvent(new Event('input'));
assert($('ch-configure').disabled,'changed credentials invalidate selection');
document.body.dataset.checks=JSON.stringify(checks);
} catch(e){document.body.dataset.failure=e.message;}
})();
'''
js = (ROOT/'web/static/channel.js').read_text(encoding='utf-8')
html = html.replace('</head>','<style>'+css+'</style></head>')
html = html.replace('</body>','<script>'+mock+js+check+'</script></body>')
page=OUT/'offline.html';page.write_text(html,encoding='utf-8')
chrome='C:/Program Files/Google/Chrome/Application/chrome.exe'
for name,size,fragment in [('desktop','1440,1500',''),('parameters','1440,1100','#modal'),('mobile','540,1500','#modal'),('records','1440,1800','#records')]:
 result=subprocess.run([chrome,'--headless','--disable-gpu','--hide-scrollbars','--no-first-run','--disable-background-networking','--virtual-time-budget=3000','--window-size='+size,'--screenshot='+str(OUT/(name+'.png')),'--dump-dom',page.as_uri()+fragment],capture_output=True,encoding='utf-8',errors='replace',timeout=40)
 dom=result.stdout
 (OUT/(name+'.dom.html')).write_text(dom,encoding='utf-8')
 if result.returncode or 'data-failure=' in dom or 'data-checks=' not in dom: raise RuntimeError(name+': browser check failed; inspect saved DOM')
 print(name+': '+re.search(r'data-checks="([^"]*)"',dom).group(1))
