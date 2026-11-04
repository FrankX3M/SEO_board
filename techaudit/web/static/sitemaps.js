document.querySelectorAll('.sitemap-editor').forEach(editor => {
  const form = editor.closest('form'), body = editor.querySelector('.sitemap-rows');
  const rows = () => [...body.querySelectorAll('.sitemap-row')];
  function validate(row) {
    const input = row.querySelector('[data-sitemap-url]'), text = input.value.trim();
    let error = '';
    try {
      const url = new URL(text, form.querySelector('[name=url]').value.replace(/\/$/, '') + '/');
      if (!text || /\s/.test(text) || !['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.hash) throw Error();
      if (rows().some(other => other !== row && other.querySelector('[data-sitemap-url]').value.trim() === text)) error = 'Карта уже есть в списке';
    } catch (_) { error = 'Введите HTTP(S)-адрес или путь без пробелов, логина и #'; }
    input.setCustomValidity(error);
    return error;
  }
  function status(row, text, bad = false) {
    const label = row.querySelector('[data-sitemap-status]');
    label.textContent = text;
    label.className = 'small ' + (bad ? 'err-inline' : 'muted');
  }
  function reset() {
    rows().forEach(row => {
      row.dataset.version = String(Number(row.dataset.version || 0) + 1);
      const error = validate(row);
      status(row, error || 'Адрес корректен · доступность ещё не проверена', !!error);
    });
  }
  async function check(row) {
    const error = validate(row);
    if (error) { status(row, error, true); return; }
    if (!editor.dataset.site) { status(row, 'Сначала сохраните новый сайт'); return; }
    if (form.querySelector('[name=url]').value.replace(/\/$/, '') !== editor.dataset.base.replace(/\/$/, '')) {
      status(row, 'Адрес сайта изменён — сначала сохраните сайт'); return;
    }
    const version = row.dataset.version, button = row.querySelector('[data-check-sitemap]');
    button.disabled = true;
    status(row, 'Проверяем доступность и XML…');
    try {
      const data = new URLSearchParams({url: row.querySelector('[data-sitemap-url]').value.trim()});
      const response = await fetch('/settings/network/' + encodeURIComponent(editor.dataset.site) + '/check-sitemap-row', {method:'POST', body:data});
      const result = await response.json();
      if (!row.isConnected || version !== row.dataset.version) return;
      if (!response.ok) { status(row, result.detail || 'Не удалось проверить карту', true); return; }
      status(row, result.message + (result.status ? ' · HTTP ' + result.status : '') + ' · ' + new Date(result.checked_at).toLocaleTimeString(), !result.available || !result.valid);
    } catch (_) { if (version === row.dataset.version) status(row, 'Не удалось получить результат проверки', true); }
    finally { button.disabled = false; }
  }
  editor.addEventListener('input', reset);
  form.querySelector('[name=url]').addEventListener('input', reset);
  editor.addEventListener('click', async event => {
    if (event.target.closest('[data-remove-sitemap]')) { event.target.closest('.sitemap-row').remove(); reset(); }
    if (event.target.closest('[data-add-sitemap]')) {
      const row = document.createElement('div'); row.className = 'sitemap-row';
      row.innerHTML = '<input type="text" data-sitemap-url aria-label="Адрес карты сайта" placeholder="/sitemap.xml"><button type="button" class="btn sm" data-check-sitemap>Проверить</button><button type="button" class="btn sm danger" data-remove-sitemap>Удалить</button><span data-sitemap-status class="small muted" role="status">Введите адрес карты</span>';
      body.append(row); row.querySelector('input').focus(); reset();
    }
    if (event.target.closest('[data-check-sitemap]')) await check(event.target.closest('.sitemap-row'));
    const all = event.target.closest('[data-check-all-sitemaps]');
    if (all) { all.disabled = true; try { for (const row of rows()) await check(row); } finally { all.disabled = false; } }
  });
  form.addEventListener('submit', event => {
    rows().forEach(validate);
    if (!form.reportValidity()) { event.preventDefault(); return; }
    editor.querySelector('[name=sitemaps]').value = rows().map(row => row.querySelector('[data-sitemap-url]').value.trim()).join('\n');
  });
  reset();
});
