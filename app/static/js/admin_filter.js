// Filtrage instantané des listes Comptes et Articles : la recherche, les
// filtres et les tris mettent à jour le tableau sans recharger la page.
// Même principe que la recherche locale des fûts, mais côté serveur pour
// couvrir toute la liste des comptes, au-delà de la pagination.
(() => {
  const DEBOUNCE_MS = 250;

  document.querySelectorAll('form[data-live-filter]').forEach((form) => {
    const rows = document.querySelector(form.dataset.liveRows);
    const footer = document.querySelector(form.dataset.liveFooter);
    if (!rows || !footer) return;

    let timer = null;
    let controller = null;
    let sequence = 0;

    function clearPending() {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
    }

    // URL correspondant à l'état courant du formulaire. La page est toujours
    // retirée : un changement de filtre ou de tri repart de la première page.
    function buildUrl() {
      const base = new URL(
        form.getAttribute('action') || window.location.pathname,
        window.location.origin,
      );
      const params = new URLSearchParams();
      new FormData(form).forEach((value, key) => {
        if (key === 'page') return;
        const text = String(value).trim();
        if (text) params.set(key, text);
      });
      base.search = params.toString();
      return base;
    }

    async function load(url) {
      sequence += 1;
      const seq = sequence;
      if (controller) controller.abort();
      const ctrl = new AbortController();
      controller = ctrl;
      rows.setAttribute('aria-busy', 'true');
      try {
        const response = await fetch(url, {
          headers: { 'X-Foyz-Partiel': '1', Accept: 'application/json' },
          credentials: 'same-origin',
          signal: ctrl.signal,
        });
        if (!response.ok) throw new Error(`Erreur ${response.status}`);
        const data = await response.json();
        if (seq !== sequence) return;
        rows.innerHTML = data.rows_html;
        footer.innerHTML = data.footer_html;
        if (window.initClickableRows) window.initClickableRows(rows);
        window.history.replaceState(null, '', url.toString());
      } catch (error) {
        if (error && error.name === 'AbortError') return;
        if (window.toast) window.toast('Impossible de filtrer la liste.', 'danger');
      } finally {
        if (controller === ctrl) controller = null;
        if (seq === sequence) rows.removeAttribute('aria-busy');
      }
    }

    function schedule() {
      clearPending();
      timer = setTimeout(() => {
        timer = null;
        load(buildUrl());
      }, DEBOUNCE_MS);
    }

    form.addEventListener('input', (event) => {
      if (event.target.matches('select')) return;
      schedule();
    });

    form.addEventListener('change', (event) => {
      if (!event.target.matches('select')) return;
      clearPending();
      load(buildUrl());
    });

    form.addEventListener('submit', (event) => {
      event.preventDefault();
      clearPending();
      load(buildUrl());
    });

    form.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      const field = event.target;
      if (!field.matches('input[type="search"]') || !field.value) return;
      field.value = '';
      clearPending();
      load(buildUrl());
    });

    form.addEventListener('click', (event) => {
      const reset = event.target.closest('[data-live-reset]');
      if (!reset || !form.contains(reset)) return;
      event.preventDefault();
      clearPending();
      form.querySelectorAll('input:not([type="hidden"]), select').forEach((field) => {
        if (field.tagName === 'SELECT') field.selectedIndex = 0;
        else field.value = '';
      });
      load(new URL(reset.href, window.location.origin));
    });

    // Pagination : les liens du pied de liste sont rechargés de la même façon,
    // pour rester sur la même page sans rechargement complet.
    footer.addEventListener('click', (event) => {
      const link = event.target.closest('a.page-link');
      if (!link || link.closest('.disabled')) return;
      event.preventDefault();
      if (timer !== null) {
        // Une saisie est en attente : on l'applique d'abord (page 1).
        clearPending();
        load(buildUrl());
        return;
      }
      load(new URL(link.href, window.location.origin));
    });
  });
})();
