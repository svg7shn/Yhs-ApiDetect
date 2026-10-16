
const root=document.documentElement;const header=document.getElementById('header');const dialog=document.getElementById('notice');let language='zh';
function scrollNav(){header.classList.toggle('stage1',scrollY>90);header.classList.toggle('stage2',scrollY>360)}addEventListener('scroll',scrollNav,{passive:true});scrollNav();
// Theme initialization and persistence are owned by theme.js.
(function initMagnet(){
 const area=document.querySelector('.heroart'),word=area?.querySelector('.wordmark');
 if(!word)return;
 const reduced=matchMedia('(prefers-reduced-motion: reduce)');
 let frame=0,last=0,angle=0,x=0,y=0,pointer=null,visible=true;
 function release(){pointer=null;area.dataset.magnet='auto';}
 function limits(){
  const r=area.getBoundingClientRect(),w=word.offsetWidth,h=word.offsetHeight;
  const rotatedW=w*.9903+h*.1392,rotatedH=h*.9903+w*.1392;
  return {r,rx:Math.max(0,(r.width-rotatedW)/2-12),ry:Math.max(0,(r.height-rotatedH)/2-12)};
 }
 area.addEventListener('pointermove',e=>{
  if(e.pointerType!=='mouse'||reduced.matches)return;
  const {r,rx,ry}=limits(),dx=e.clientX-r.left-r.width/2,dy=e.clientY-r.top-r.height/2;
  if(Math.hypot(dx/(r.width*.46),dy/(r.height*.46))>1){release();return;}
  const k=Math.max(1,Math.hypot(dx/Math.max(rx,1),dy/Math.max(ry,1)));
  pointer={x:dx/k,y:dy/k};area.dataset.magnet='following';
 },{passive:true});
 area.addEventListener('pointerleave',release);area.addEventListener('pointercancel',release);
 addEventListener('blur',release);addEventListener('scroll',release,{passive:true});addEventListener('resize',release);
 function tick(now){
  frame=0;if(reduced.matches||document.hidden||!visible)return;
  const dt=Math.min((now-last)||16,50);last=now;
  const {rx,ry}=limits();
  const radius=Math.min(Math.max(16,innerWidth*.03),40,rx,ry);
  if(!pointer)angle+=dt*Math.PI*2/60000;
  const target=pointer||{x:Math.cos(angle)*radius,y:Math.sin(angle)*radius};
  const blend=1-Math.exp(-dt/140);
  x+=(target.x-x)*blend;y+=(target.y-y)*blend;
  x=Math.max(-rx,Math.min(rx,x));y=Math.max(-ry,Math.min(ry,y));
  word.style.transform='translate('+x+'px,'+y+'px) rotate(-8deg)';
  frame=requestAnimationFrame(tick);
 }
 function sync(){
  cancelAnimationFrame(frame);frame=0;release();last=0;
  if(reduced.matches){word.style.removeProperty('animation');word.style.removeProperty('transform');return;}
  word.style.animation='none';
  if(!document.hidden&&visible)frame=requestAnimationFrame(tick);
 }
 reduced.addEventListener('change',sync);document.addEventListener('visibilitychange',sync);
 if('IntersectionObserver' in window)new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;sync();}).observe(area);
 sync();
})();
// Content remains visible if JavaScript or IntersectionObserver is unavailable.
(function initReveal(){
 const motion=matchMedia('(prefers-reduced-motion: reduce)');
 if(motion.matches || !('IntersectionObserver' in window))return;
 const items=[...document.querySelectorAll('.hero > div:first-child > *, .heroart, .scrollcue, .platform, main section .sectionhead, main section .demo, main section article, main section details, .yhs-footer')];
 const observer=new IntersectionObserver(entries=>{
  entries.forEach(({target,isIntersecting})=>{if(isIntersecting){target.classList.remove('is-pending');observer.unobserve(target);}});
 },{threshold:0,rootMargin:'0px 0px -32px 0px'});
 items.forEach(el=>{
  // Observe first-screen content too; reveal each item only once.
  const siblings=[...el.parentElement.children];
  el.style.setProperty('--reveal-delay',Math.min(siblings.indexOf(el),3)*90+'ms');
  el.classList.add('home-reveal','is-pending');observer.observe(el);
 });
 motion.addEventListener('change',e=>{if(e.matches){observer.disconnect();items.forEach(el=>el.classList.remove('is-pending'));}});
 document.addEventListener('focusin',e=>{const el=e.target.closest('.home-reveal');if(el){el.classList.remove('is-pending');observer.unobserve(el);}});
})();
document.getElementById('language').onclick=()=>{language=language==='zh'?'en':'zh';root.dataset.lang=language;root.lang=language==='zh'?'zh-CN':'en';document.getElementById('language').textContent=language==='zh'?'EN':'中';document.querySelectorAll('.navitem').forEach(el=>el.setAttribute('aria-label',el.querySelector('.navlabel .'+language).textContent));};
function notice(title,body){document.getElementById('noticeTitle').textContent=title;document.getElementById('noticeBody').textContent=body;dialog.showModal()}
// Replace empty destinations with partner URLs when actual placements are supplied.
const adDestinations={A:'',B:'',C:'',D:''};document.querySelectorAll('[data-ad]').forEach(b=>b.onclick=()=>{const url=adDestinations[b.dataset.ad];if(url&&/^https?:\/\//.test(url)){window.open(url,'_blank','noopener,noreferrer')}else notice(language==='zh'?'广告位待配置':'Ad placement not configured',language==='zh'?'此处将打开合作方的独立广告地址。暂未设置广告链接。':'This card will open its own partner URL. No advertising destination is configured.');});
document.querySelector('.dialogclose').onclick=()=>dialog.close();document.getElementById('noticeOk').onclick=()=>dialog.close();dialog.addEventListener('click',e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close()}});
let step=0;function tick(){document.querySelectorAll('.demostep').forEach((el,i)=>{el.classList.toggle('active',i===step);el.lastElementChild.textContent=i<=step?'✓':'·'});step=(step+1)%4}document.getElementById('replay').onclick=()=>{step=0;tick()};tick();if(!matchMedia('(prefers-reduced-motion: reduce)').matches)setInterval(()=>{if(!document.hidden)tick()},1500);
