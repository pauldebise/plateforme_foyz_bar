// Recherche locale dans la carte « Fûts enregistrés » : filtre les lignes par
// nom, sans accents ni majuscules, sans requête serveur.
(() => {
  const input = document.getElementById('keg-search');
  if (!input) return;

  const rows = Array.from(document.querySelectorAll('[data-keg-row]'));
  const empty = document.getElementById('keg-search-empty');

  // Même règle que fold_text (app/utils.py) et fold() de payment.js.
  const FOLD_EXTRA = { œ: 'oe', æ: 'ae', ø: 'o', ł: 'l', đ: 'd', ı: 'i' };
  const fold = (text) => String(text || '')
    .toLowerCase()
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[œæøłđı]/g, (c) => FOLD_EXTRA[c]);

  function applyFilter() {
    const tokens = fold(input.value).split(/\s+/).filter(Boolean);
    let visible = 0;
    rows.forEach((row) => {
      const text = fold(row.dataset.search);
      const matches = tokens.every((token) => text.includes(token));
      row.hidden = !matches;
      if (matches) visible += 1;
    });
    if (empty) empty.hidden = !(tokens.length && visible === 0);
  }

  input.addEventListener('input', applyFilter);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && input.value) {
      input.value = '';
      applyFilter();
    }
  });
})();
