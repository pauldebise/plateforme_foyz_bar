// Onglet API : filtrage de la sélection et copie du jeton affiché une fois.
(() => {
  const campus = document.getElementById('api-campus');
  const search = document.getElementById('api-selection-search');
  const rows = Array.from(document.querySelectorAll('.api-select-row'));

  const fold = (text) => String(text || '')
    .toLowerCase()
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '');

  function applyFilter() {
    const campusValue = campus ? campus.value : '';
    const needle = fold(search ? search.value.trim() : '');
    rows.forEach((row) => {
      const campusOk = !campusValue || row.dataset.campus === campusValue;
      const searchOk = !needle || fold(row.dataset.search).includes(needle);
      row.hidden = !(campusOk && searchOk);
    });
  }

  if (campus) {
    campus.addEventListener('change', () => {
      // Une sélection de l'autre campus deviendrait invisible mais resterait
      // soumise : on la décoche au changement de campus.
      rows.forEach((row) => {
        if (row.dataset.campus !== campus.value) {
          const input = row.querySelector('input[type="checkbox"]');
          if (input) input.checked = false;
        }
      });
      applyFilter();
    });
  }
  if (search) search.addEventListener('input', applyFilter);
  applyFilter();

  function legacyCopy(text) {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand('copy');
    } finally {
      area.remove();
    }
  }

  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-copy-text]');
    if (!button) return;
    const input = document.querySelector(button.getAttribute('data-copy-text'));
    if (!input) return;
    const done = () => {
      const icon = button.querySelector('i');
      if (!icon) return;
      const previous = icon.className;
      icon.className = 'bi bi-check-lg';
      setTimeout(() => { icon.className = previous; }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(input.value)
        .then(done)
        .catch(() => { legacyCopy(input.value); done(); });
    } else {
      legacyCopy(input.value);
      done();
    }
  });
})();
