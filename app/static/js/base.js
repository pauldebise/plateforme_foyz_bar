// Comportements génériques, sans script inline (compatibles CSP).
// - data-confirm : confirmation avant soumission d'un formulaire.
// - data-copy : copie d'une URL relative dans le presse-papiers.
// - toast(message, type) : notification éphémère (remplace alert()).
(() => {
  window.toast = function toast(message, type = 'danger') {
    const container = document.getElementById('toast-container');
    if (!container) return;
    if (typeof bootstrap === 'undefined' || !bootstrap.Toast) {
      window.alert(message);
      return;
    }
    const el = document.createElement('div');
    el.className = `toast align-items-center text-bg-${type} border-0`;
    el.setAttribute('role', 'status');
    const body = document.createElement('div');
    body.className = 'd-flex';
    const text = document.createElement('div');
    text.className = 'toast-body';
    text.textContent = message;
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn-close btn-close-white me-2 m-auto';
    close.setAttribute('data-bs-dismiss', 'toast');
    close.setAttribute('aria-label', 'Fermer');
    body.appendChild(text);
    body.appendChild(close);
    el.appendChild(body);
    container.appendChild(el);
    el.addEventListener('hidden.bs.toast', () => el.remove());
    new bootstrap.Toast(el, { delay: 4000 }).show();
  };

  document.addEventListener('submit', (event) => {
    const message = event.target.getAttribute && event.target.getAttribute('data-confirm');
    if (message && !window.confirm(message)) event.preventDefault();
  }, true);

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

  // Affiche l'URL absolue du lien passerelle sans dépendre de `request.host_url`
  // côté serveur (qui lit l'en-tête Host, potentiellement forgé).
  document.querySelectorAll('[data-gateway-link]').forEach((input) => {
    input.value = new URL(input.value, window.location.origin).href;
  });

  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-copy]');
    if (!button) return;
    const url = new URL(button.getAttribute('data-copy'), window.location.origin).href;
    const done = () => {
      const icon = button.querySelector('i');
      if (!icon) return;
      const previous = icon.className;
      icon.className = 'bi bi-check-lg';
      setTimeout(() => { icon.className = previous; }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(done).catch(() => { legacyCopy(url); done(); });
    } else {
      legacyCopy(url);
      done();
    }
  });
})();
