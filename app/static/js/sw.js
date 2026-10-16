/* Pierre tombale de l'ancienne coquille hors ligne de la caisse (P10-6,
   retirée le 16/10/2026 : l'encaissement exige désormais un accès réseau).
   Ce fichier n'est plus enregistré nulle part ; il reste servi à /sw.js pour
   que les postes ayant installé l'ancien service worker le remplacent, purgent
   ses caches et se désenregistrent à la prochaine visite. Ne pas supprimer
   tant que d'anciens postes peuvent demander une mise à jour. */
'use strict';

self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(keys.map((key) => caches.delete(key)));
      await self.registration.unregister();
    })()
  );
});
