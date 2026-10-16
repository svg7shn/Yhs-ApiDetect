// 导航滚动收缩：页面顶部展开，下滚超过阈值后由 style.css 的 .is-scrolled 收窄居中。
(function () {
  var THRESHOLD = 24;
  var header = document.querySelector('.site-header');
  if (!header) return;

  var scheduled = false;
  var applied = null;

  function apply() {
    scheduled = false;
    var scrolled = window.scrollY > THRESHOLD;
    if (scrolled === applied) return;
    applied = scrolled;
    header.classList.toggle('is-scrolled', scrolled);
  }

  function request() {
    if (scheduled) return;
    scheduled = true;
    window.requestAnimationFrame(apply);
  }

  window.addEventListener('scroll', request, { passive: true });
  window.addEventListener('resize', request, { passive: true });
  apply();
})();
