// Hashes keep old bookmarks and post-save redirects working.
(() => {
  const groups = {networks: 'sites', 'new-net': 'sites', external: 'integrations', ai: 'integrations',
    thresholds: 'rules', misc: 'rules', access: 'system', ua: 'system', requests: 'system', import: 'system'};
  function select() {
    const id = location.hash.slice(1) || 'networks';
    if (id.startsWith('speed-') || id.startsWith('net-')) {
      const name = id.slice(id.indexOf('-') + 1);
      const url = new URL('/sites/' + encodeURIComponent(name), location.origin);
      url.search = location.search;
      url.hash = id;
      location.replace(url);
      return;
    }
    const group = groups[id] || (id.startsWith('net-') || id.startsWith('speed-') ? 'sites' : 'sites');
    document.querySelectorAll('[data-settings-section]').forEach(el => { el.hidden = el.dataset.settingsSection !== group; });
    document.querySelectorAll('.settings-nav a[href^="#"]').forEach(el => {
      const active = groups[el.hash.slice(1)] === group;
      el.classList.toggle('on', active);
      if (active) el.setAttribute('aria-current', 'page'); else el.removeAttribute('aria-current');
    });
    const target = document.getElementById(id);
    if (target && location.hash) {
      const primary = ['networks', 'external', 'thresholds', 'requests'].includes(id);
      (primary ? document.querySelector('.settings-nav') : target).scrollIntoView({block: 'start'});
    }
  }
  window.addEventListener('hashchange', select);
  select();
})();
