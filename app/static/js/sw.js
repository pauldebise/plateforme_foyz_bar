/* Service worker : coquille hors ligne de la caisse (P10-6).
   - pré-cache des assets statiques et de la page /hors-ligne ;
   - pages HTML de caisse : réseau d'abord, copie en cache pour la reprise
     hors ligne (les autres pages authentifiées ne sont jamais mises en
     cache) ;
   - les POST (encaissements) ne sont jamais interceptés : la file d'attente
     est gérée par offline.js avec la clé d'idempotence côté serveur. */
'use strict';

const VERSION = 'foyz-v1';
const OFFLINE_URL = '/hors-ligne';
const PRECACHE = [
  OFFLINE_URL,
  '/static/css/app.css',
  '/static/js/base.js',
  '/static/js/app.js',
  '/static/js/payment.js',
  '/static/js/offline.js',
  '/static/vendor/bootstrap/bootstrap.min.css',
  '/static/vendor/bootstrap/bootstrap.bundle.min.js',
  '/static/vendor/bootstrap-icons/bootstrap-icons.min.css',
  '/static/favicon.svg',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(VERSION)
      .then((cache) => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== VERSION).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

function isCaisse(path) {
  return path.startsWith('/equipe/paiement') || path.startsWith('/passerelle/');
}

async function networkFirst(request, cacheable) {
  const cache = await caches.open(VERSION);
  try {
    const response = await fetch(request);
    if (cacheable && response && response.ok) {
      cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    const cached = await cache.match(request);
    if (cached) return cached;
    const fallback = await cache.match(OFFLINE_URL);
    return fallback || Response.error();
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(VERSION);
  const cached = await cache.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response && response.ok && new URL(request.url).pathname.startsWith('/static/')) {
    cache.put(request, response.clone());
  }
  return response;
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/uploads/')) return;
  const accept = request.headers.get('accept') || '';
  if (accept.includes('text/html')) {
    event.respondWith(networkFirst(request, isCaisse(url.pathname)));
    return;
  }
  event.respondWith(cacheFirst(request));
});
