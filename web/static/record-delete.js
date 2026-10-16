/* Shared deletion: persistence succeeds before the page is refreshed. */
(function () {
  'use strict';
  const icon = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/></svg>';
  const deleted = new Set();
  function attach(node) {
    if (deleted.has(node.dataset.deleteUrl)) { node.remove(); return; }
    if (node.dataset.deleteReady) return;
    node.dataset.deleteReady = '1';
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'record-delete-button';
    button.title = '删除记录及本地文件'; button.setAttribute('aria-label', '删除记录及本地文件');
    button.innerHTML = icon + (node.tagName === 'TR' ? '' : '<span>删除</span>');
    let host = node;
    if (node.tagName === 'TR') {
      const cell = document.createElement('td'); cell.className = 'record-delete-cell';
      node.prepend(cell); cell.append(button);
    } else {
      host = document.createElement('div'); host.className = 'record-swipe';
      node.before(host); host.append(button, node); node.classList.add('record-swipe-content');
      node.tabIndex = 0; node.setAttribute('aria-description', '向左滑动显示删除，或按 Delete 键');
      let start = null, moved = false, suppressClick = false;
      const toggle = open => { host.classList.toggle('is-open', open); };
      button.addEventListener('focus', () => toggle(true));
      host.addEventListener('keydown', e => {
        if (e.target !== node && e.target !== button) return;
        if (e.key === 'Escape' || e.key === 'ArrowRight') {
          e.preventDefault(); toggle(false); node.focus();
        } else if (e.target === node && (e.key === 'Delete' || e.key === 'ArrowLeft')) {
          e.preventDefault(); toggle(true); button.focus();
        }
      });
      node.addEventListener('pointerdown', e => {
        if (e.button !== 0 || e.target.closest('button,a,input,select')) return;
        start = {x:e.clientX,y:e.clientY,id:e.pointerId}; moved = false;
      });
      node.addEventListener('pointermove', e => {
        if (!start || start.id !== e.pointerId) return;
        const dx = e.clientX-start.x, dy = e.clientY-start.y;
        if (!moved && Math.abs(dy)>12 && Math.abs(dy)>Math.abs(dx)) { start=null; return; }
        if (Math.abs(dx)>18 && Math.abs(dx)>Math.abs(dy)*1.3) {
          moved=true; node.setPointerCapture(e.pointerId); toggle(dx<0); e.preventDefault();
        }
      });
      node.addEventListener('pointerup', () => { suppressClick=moved; start=null; setTimeout(() => { suppressClick=false; }, 0); });
      node.addEventListener('pointercancel', () => { start=null; moved=false; });
      node.addEventListener('click', e => { if (suppressClick) { e.preventDefault(); e.stopPropagation(); } }, true);
    }
    button.addEventListener('click', async () => {
      if (button.disabled || !window.confirm('确定删除这条检测记录？本地保存的记录及关联报告也会删除，无法撤销。')) return;
      button.disabled = true; host.setAttribute('aria-busy','true');
      try {
        const response = await fetch(node.dataset.deleteUrl, {method:'DELETE',credentials:'same-origin',headers:{'X-Requested-With':'XMLHttpRequest'}})
          .catch(() => { throw new Error('网络异常，无法确认本地删除结果。请刷新记录核对后再重试。'); });
        if (!response.ok) {
          let detail = '删除未确认成功（HTTP ' + response.status + '）。可能已部分清理，请刷新记录核对后再重试。';
          try { const data = await response.json(); if (typeof data.detail === 'string') detail = data.detail; else if (data.error && typeof data.error.message === 'string') detail = data.error.message; } catch (_) {}
          throw new Error(detail);
        }
        const url = node.dataset.deleteUrl;
        deleted.add(url);
        document.querySelectorAll('[data-delete-url]').forEach(item => {
          if (item.dataset.deleteUrl === url) (item.closest('.record-swipe') || item).remove();
        });
        document.dispatchEvent(new CustomEvent('record-deleted', {detail:{url}}));
        const list = document.querySelector('.history-list');
        if (list && !list.querySelector('[data-delete-url]')) list.textContent = '暂无本地检测记录。';
      } catch (error) { window.alert(error.message || '无法确认删除结果，请刷新记录核对后再重试。'); button.disabled=false; host.removeAttribute('aria-busy'); }
    });
  }
  function scan() { document.querySelectorAll('[data-delete-url]:not([data-delete-ready])').forEach(attach); }
  scan(); new MutationObserver(scan).observe(document.body,{childList:true,subtree:true});
})();
