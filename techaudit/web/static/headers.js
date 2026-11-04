document.querySelectorAll('.header-editor').forEach(editor => {
  const body = editor.querySelector('tbody');
  editor.addEventListener('click', event => {
    if (event.target.closest('[data-clear-token]')) {
      editor.querySelector('[name=clear_token]').value = '1';
      editor.querySelector('[name=token]').value = '';
      editor.querySelector('[data-token-status]').textContent = 'Токен сайта будет удалён после сохранения. Общий токен, если задан, остаётся запасным.';
    }
    if (event.target.closest('[data-remove-header]')) event.target.closest('tr').remove();
    if (event.target.closest('[data-add-header]')) {
      const row = document.createElement('tr');
      row.dataset.headerRow = '';
      row.innerHTML = '<td><input type="checkbox" checked aria-label="Включить заголовок"></td><td><input data-name list="header-names" required aria-label="Имя заголовка"></td><td><input data-value type="password" autocomplete="new-password" aria-label="Значение заголовка"></td><td><button type="button" class="btn sm danger" data-remove-header>Удалить</button></td>';
      body.append(row);
      row.querySelector('[data-name]').focus();
    }
  });
  editor.querySelector('[name=token]').addEventListener('input', () => {
    editor.querySelector('[name=clear_token]').value = '';
    editor.querySelector('[data-token-status]').textContent = 'Новое значение будет сохранено вместе с сайтом';
  });
  editor.closest('form').addEventListener('submit', () => {
    editor.querySelector('[name=headers]').value = JSON.stringify(
      [...body.querySelectorAll('[data-header-row]')].map(row => ({
        name: row.querySelector('[data-name]').value,
        value: row.querySelector('[data-value]').value,
        enabled: row.querySelector('[type=checkbox]').checked,
        keep: row.dataset.keep === '1'
      }))
    );
  });
});
