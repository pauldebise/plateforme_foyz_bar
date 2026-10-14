// Recherche dans le catalogue public : filtre local, sans accents ni majuscules.
(() => {
  const input = document.getElementById('catalogue-search');
  const box = document.getElementById('catalogue-search-box');
  if (!input || !box) return;

  // Même règle que fold_text (app/utils.py) et fold() de payment.js.
  const FOLD_EXTRA = { œ: 'oe', æ: 'ae', ø: 'o', ł: 'l', đ: 'd', ı: 'i' };
  const fold = (text) => String(text || '')
    .toLowerCase()
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[œæøłđı]/g, (c) => FOLD_EXTRA[c]);

  const top = document.getElementById('catalogue-top');
  const none = document.getElementById('catalogue-none');
  const cards = [...document.querySelectorAll('.catalogue-card')];
  const rows = [...document.querySelectorAll('.catalogue-row')].map((row) => ({
    row,
    text: fold(row.dataset.search),
  }));

  function filter() {
    const tokens = fold(input.value).split(/\s+/).filter(Boolean);
    rows.forEach(({ row, text }) => {
      row.hidden = !tokens.every((t) => text.includes(t));
    });
    // Pendant une recherche, la carte « les plus vendus » doublonnerait les
    // résultats des catégories : on la masque.
    if (top) top.hidden = tokens.length > 0;
    cards.forEach((card) => {
      if (card !== top) card.hidden = !card.querySelector('.catalogue-row:not([hidden])');
    });
    none.hidden = tokens.length === 0 || cards.some((card) => !card.hidden);
  }

  input.addEventListener('input', filter);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && input.value) {
      input.value = '';
      filter();
    }
  });
  box.hidden = false;
})();
