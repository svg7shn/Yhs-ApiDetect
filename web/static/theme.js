(() => {
  const root = document.documentElement;
  const system = matchMedia('(prefers-color-scheme: dark)');
  const key = 'yhs-home-theme';
  let selected;
  try { selected = localStorage.getItem(key); } catch (_) {}
  if (selected !== 'dark' && selected !== 'light') selected = null;
  const apply = () => {
    root.dataset.theme = selected || (system.matches ? 'dark' : 'light');
    const button = document.getElementById('theme');
    if (button) button.textContent = root.dataset.theme === 'dark' ? '☼' : '☾';
  };
  apply();
  system.addEventListener('change', () => { if (!selected) apply(); });
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    const button = document.getElementById('theme');
    if (button) button.addEventListener('click', () => {
      selected = root.dataset.theme === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem(key, selected); } catch (_) {}
      apply();
    });
  });
})();
