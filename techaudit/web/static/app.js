/* Графики и мелкие помощники интерфейса. Chart.js подключён локально (static/chart.umd.js). */
(function () {
  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const charts = {};

  function applyTheme() {
    if (!window.Chart) return;
    Chart.defaults.color = css('--muted');
    Chart.defaults.borderColor = css('--line');
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  }

  function runNote(r) {
    if (!r) return '';
    if (r.kind === 'crawl') return 'краул Screaming Frog: ' + r.rows + ' URL' + (r.title ? ' · ' + r.title : '') + (r.source_file ? ' · ' + r.source_file : '');
    const bits = [];
    bits.push(r.limit ? 'выборка: до ' + r.limit + ' URL на тип' : 'весь справочник');
    if (r.variants && r.variants.length) bits.push('варианты: ' + r.variants.join(', '));
    if (r.types) bits.push('типы: ' + r.types);
    return bits.join(' · ');
  }

  /**
   * spec: {id, runs, datasets:[{label, data, color:'--good', type:'line'|'bar', stack, axis:'y'|'y1', dash}],
   *        stacked, unit, unit1, max}
   */
  function draw(spec) {
    const el = document.getElementById(spec.id);
    if (!el || !window.Chart) return;
    if (charts[spec.id]) charts[spec.id].destroy();
    // обрезаем пустое начало: график начинается с первой точки, где есть хоть какое-то значение
    let from = spec.runs.length;
    spec.datasets.forEach((d) => { const i = d.data.findIndex((x) => x != null); if (i >= 0 && i < from) from = i; });
    if (from > 0 && from < spec.runs.length) {
      spec = Object.assign({}, spec, { runs: spec.runs.slice(from), datasets: spec.datasets.map((d) => Object.assign({}, d, { data: d.data.slice(from) })) });
    }
    const labels = spec.runs.map((r) => r.label);
    const ds = spec.datasets.map((d) => ({
      type: d.type || 'line', label: d.label, data: d.data, yAxisID: d.axis || 'y',
      borderColor: css(d.color), backgroundColor: d.type === 'bar' ? css(d.color) : css(d.color) + '33',
      borderWidth: 2, tension: 0.25, spanGaps: true,
      pointStyle: spec.runs.map((r) => (r.kind === 'crawl' ? 'rectRot' : 'circle')),
      pointRadius: spec.runs.map((r) => (r.kind === 'crawl' ? 6 : (spec.runs.length > 40 ? 0 : 3))),
      stack: d.stack, borderDash: d.dash ? [5, 4] : [], order: d.type === 'bar' ? 2 : 1,
    }));
    const stackX = !!(spec.stacked || spec.stacked1);
    const scales = {
      x: { stacked: stackX, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 10 }, grid: { display: false } },
      y: { stacked: !!spec.stacked, beginAtZero: true, max: spec.max, title: { display: !!spec.unit, text: spec.unit || '' } },
    };
    if (spec.datasets.some((d) => d.axis === 'y1')) {
      scales.y1 = { position: 'right', beginAtZero: true, max: spec.max1, stacked: !!spec.stacked1, grid: { drawOnChartArea: false },
        title: { display: !!spec.unit1, text: spec.unit1 || '' } };
    }
    charts[spec.id] = new Chart(el, {
      data: { labels, datasets: ds },
      options: {
        responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
        scales,
        plugins: {
          legend: { position: 'bottom', labels: { boxWidth: 12 } },
          tooltip: { callbacks: { afterBody: (items) => runNote(spec.runs[items[0].dataIndex]) } },
        },
        onClick: (_e, els) => { if (els.length && spec.onRun) spec.onRun(spec.runs[els[0].index]); },
      },
    });
  }

  /** Горизонтальные столбики «категория → значение, %» с цветом по порогам (красный / жёлтый / зелёный). */
  function drawBars(spec) {
    const el = document.getElementById(spec.id);
    if (!el || !window.Chart) return;
    if (charts[spec.id]) charts[spec.id].destroy();
    const col = (v) => css(v == null ? '--na' : v >= 90 ? '--good' : v >= 60 ? '--warn' : '--bad');
    charts[spec.id] = new Chart(el, {
      type: 'bar',
      data: { labels: spec.labels.map((l, i) => l + ' · ' + (spec.values[i] == null ? '—' : String(spec.values[i]).replace('.', ',') + '%')), datasets: [{ data: spec.values, backgroundColor: spec.values.map(col), borderRadius: 4, barPercentage: 0.7 }] },
      options: { indexAxis: 'y', responsive: true, maintainAspectRatio: false,
        scales: { x: { min: 0, max: 100, title: { display: true, text: spec.unit || '' } }, y: { grid: { display: false } } },
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => ' ' + String(c.parsed.x).replace('.', ',') + ' ' + (spec.unit || '') } } } },
    });
  }

  /** Спарклайн: <span class="spark" data-spark="[1,2,null,3]" data-unit="%"> → встроенный SVG без зависимостей. */
  function sparks(root) {
    (root || document).querySelectorAll('.spark[data-spark]').forEach((el) => {
      let v; try { v = JSON.parse(el.dataset.spark).filter((x) => x != null); } catch (e) { return; }
      const n = v.length, nums = v.filter((x) => x != null);
      if (n < 2 || nums.length < 2) { el.textContent = ''; return; }
      const W = +el.dataset.w || 84, H = +el.dataset.h || 24, pad = 3;
      let lo = Math.min(...nums), hi = Math.max(...nums);
      if (el.dataset.unit === '%') { lo = Math.min(lo, 0); hi = 100; }
      if (hi === lo) { hi = lo + 1; }
      const X = (i) => pad + (i * (W - 2 * pad)) / (n - 1), Y = (x) => H - pad - ((x - lo) / (hi - lo)) * (H - 2 * pad);
      let d = '', pen = false;
      v.forEach((x, i) => { if (x == null) { pen = false; return; } d += (pen ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(x).toFixed(1); pen = true; });
      const last = v.map((x, i) => [x, i]).filter((a) => a[0] != null).pop();
      el.innerHTML = '<svg width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '"><path d="' + d +
        '" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/><circle cx="' +
        X(last[1]).toFixed(1) + '" cy="' + Y(last[0]).toFixed(1) + '" r="2.4" fill="currentColor"/></svg>';
    });
  }

  function clear() { Object.keys(charts).forEach((k) => { charts[k].destroy(); delete charts[k]; }); }
  window.TA = { draw, drawBars, sparks, css, applyTheme, clear };
  document.addEventListener('DOMContentLoaded', applyTheme);

  // подтверждение для обычных форм: <form data-confirm="Текст">
  document.addEventListener('submit', (e) => {
    const t = e.target.closest('[data-confirm]');
    if (t && !confirm(t.getAttribute('data-confirm'))) e.preventDefault();
  });
  // «выбрать все» в таблице: <input type=checkbox data-all="имя">
  document.addEventListener('change', (e) => {
    const a = e.target.closest('[data-all]');
    if (a) document.querySelectorAll('input[name="' + a.dataset.all + '"]').forEach((c) => { c.checked = a.checked; });
  });
  // после HTMX-подмен снова вешаем подтверждения на кнопки удаления (hx-confirm работает сам)
})();

// долгие запросы (выгрузка из Вебмастера, сравнение): <form data-busy="Текст"> — блокируем кнопку и показываем, что идёт работа
document.addEventListener('submit', function (e) {
  const f = e.target.closest && e.target.closest('form[data-busy]');
  if (!f || e.defaultPrevented) return;
  const b = f.querySelector('button[type=submit]');
  if (b) { b.disabled = true; b.textContent = f.getAttribute('data-busy'); }
});
