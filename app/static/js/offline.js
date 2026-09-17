/* File d'attente hors ligne des encaissements (P10-6).
 *
 * - stocke chaque vente dans IndexedDB avec sa clé d'idempotence (générée par
 *   la caisse) : le serveur déduplique un éventuel double envoi ;
 * - rejoue automatiquement au retour du réseau, ou sur demande via le bouton
 *   « Synchroniser » de la bannière ;
 * - une vente refusée par le serveur (solde, blacklist, session expirée)
 *   reste listée en erreur : l'équipe doit la traiter manuellement ;
 * - enregistre le service worker (coquille hors ligne).
 */
(function () {
  'use strict';

  const DB_NAME = 'foyz-offline';
  const STORE = 'sales';
  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const supported = typeof indexedDB !== 'undefined';

  function openDb() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, 1);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(STORE)) {
          db.createObjectStore(STORE, { keyPath: 'id', autoIncrement: true });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  function run(mode, action) {
    return openDb().then(
      (db) =>
        new Promise((resolve, reject) => {
          const transaction = db.transaction(STORE, mode);
          const store = transaction.objectStore(STORE);
          let result;
          action(store, (value) => {
            result = value;
          });
          transaction.oncomplete = () => {
            db.close();
            resolve(result);
          };
          transaction.onerror = () => {
            db.close();
            reject(transaction.error);
          };
        })
    );
  }

  const api = {
    supported,

    queueSale(url, payload) {
      if (!supported) return Promise.resolve(null);
      const entry = { url, payload, createdAt: Date.now(), error: null };
      return run('readwrite', (store, done) => {
        const request = store.add(entry);
        request.onsuccess = () => {
          done(request.result);
          notifyChange();
        };
      });
    },

    all() {
      if (!supported) return Promise.resolve([]);
      return run('readonly', (store, done) => {
        const request = store.getAll();
        request.onsuccess = () => done(request.result || []);
      });
    },

    count() {
      return api.all().then((entries) => entries.length);
    },

    remove(id) {
      return run('readwrite', (store) => store.delete(id));
    },

    markError(id, message) {
      return run('readwrite', (store) => {
        const request = store.get(id);
        request.onsuccess = () => {
          const entry = request.result;
          if (entry) {
            entry.error = message;
            store.put(entry);
          }
        };
      });
    },

    async flush() {
      if (!supported || !navigator.onLine) return { sent: 0, failed: 0 };
      const entries = await api.all();
      let sent = 0;
      let failed = 0;
      for (const entry of entries) {
        try {
          const response = await fetch(entry.url, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-CSRFToken': csrfMeta ? csrfMeta.content : '',
            },
            body: JSON.stringify(entry.payload),
          });
          if (response.ok) {
            await api.remove(entry.id);
            sent += 1;
            continue;
          }
          const data = await response.json().catch(() => ({}));
          if (response.status === 401 || response.status === 403) {
            await api.markError(entry.id, 'Session expirée : reconnectez-vous puis synchronisez.');
            failed += 1;
            break;
          }
          await api.markError(entry.id, data.error || 'Refusé par le serveur (' + response.status + ')');
          failed += 1;
        } catch (error) {
          failed += 1;
          break;
        }
      }
      notifyChange();
      return { sent, failed };
    },
  };

  function notifyChange() {
    document.dispatchEvent(new CustomEvent('foyz-offline-change'));
  }

  function updateBanner() {
    const banner = document.getElementById('offline-banner');
    if (!banner) return;
    api.all().then((entries) => {
      const pending = entries.filter((entry) => !entry.error).length;
      const errors = entries.filter((entry) => entry.error);
      const text = document.getElementById('offline-banner-text');
      const list = document.getElementById('offline-errors');
      banner.classList.remove('d-none');
      banner.classList.toggle('alert-warning', navigator.onLine);
      banner.classList.toggle('alert-danger', !navigator.onLine);
      if (!navigator.onLine) {
        text.textContent =
          'Hors ligne : les ventes sont enregistrées sur ce poste' +
          (pending ? ' (' + pending + ' en attente).' : '.');
      } else if (pending) {
        text.textContent = pending + ' vente(s) en attente de synchronisation.';
      } else if (errors.length) {
        text.textContent = errors.length + ' vente(s) refusée(s) : à traiter manuellement.';
      } else {
        banner.classList.add('d-none');
      }
      list.textContent = '';
      errors.forEach((entry) => {
        const item = document.createElement('li');
        item.textContent =
          'Vente du ' + new Date(entry.createdAt).toLocaleString('fr-FR') + ' : ' + entry.error;
        list.appendChild(item);
      });
    });
  }

  window.FoyzOffline = api;

  document.addEventListener('foyz-offline-change', updateBanner);
  window.addEventListener('online', () => {
    updateBanner();
    api.flush().then(updateBanner);
  });
  window.addEventListener('offline', updateBanner);
  document.addEventListener('click', (event) => {
    if (event.target.closest('[data-offline-flush]')) {
      api.flush().then(updateBanner);
    }
  });

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
  updateBanner();
})();
