# Fonctionnalités détaillées — Plateforme Foy'z & Bar

Comportements visibles et décisions produit qui n'ont pas leur place dans le
[README](../README.md).

## Comptes et identifiants

Trois champs d'identité distincts :

- **`users.username`** — identifiant de connexion `prenom.nom` (unique, minuscule
  sans accents, séparateurs ramenés à des points), utilisé *exclusivement* pour
  la connexion ; la saisie tolère casse, accents et espaces ;
- **`users.name`** — nom réel complet, base de l'affichage ;
- **`users.nickname`** — surnom d'usage (facultatif, non unique), intégré à
  l'affichage (« Prénom Nom (Surnom) ») et aux recherches, mais **pas** au login.

Les comptes sont créés par l'administrateur (onglet Comptes) ou importés par le
module de migration ([MIGRATION.md](MIGRATION.md)).

## PWA installable, encaissement en ligne uniquement

- Manifeste `/manifest.webmanifest` : l'application est installable sur mobile et
  tablette (icônes, raccourcis caisse/prix).
- **Pas d'encaissement hors ligne** (file P10-6 retirée le 16/10/2026, décision
  produit) : en cas de panne réseau, la caisse affiche « encaissement impossible » et
  conserve le panier pour réessayer ; aucune vente n'est mise en file sur le poste.
  La clé d'idempotence côté serveur reste : un double clic ou une retentative
  réseau ne débite jamais deux fois.
- **Aucun service worker** : `/sw.js` et son fichier ont été supprimés le
  5/11/2026, aucun ancien poste ne demandant plus de mise à jour. L'application
  n'utilise plus de cache ni de stockage local côté navigateur.

## Décor de Noël

- Du 1er au 31 décembre (par défaut), les pages chargent `santa-deco.js` : un Père
  Noël traverse le bas de l'écran et des flocons tombent devant la page. L'overlay
  est en `pointer-events: none` : boutons, liens et formulaires restent utilisables ;
  `prefers-reduced-motion` fige la scène.
- Les dates sont réglables dans **Module dev → Décor de Noël**, réservé au compte
  `admin` (superadmin). Elles sont annuelles (`MM-JJ`, l'année est ignorée) et
  peuvent chevaucher le Nouvel An ; vider les deux champs désactive le décor.

## Statistiques de consommation

- L'onglet **Statistiques** est organisé en trois vues : *Vue d'ensemble*,
  *Articles* et *Étudiants*. Les filtres (période, campus, catégorie, promotion,
  équipe, soirées) restent communs aux trois vues et sont conservés dans l'URL.
- Périodes rapides : 7 jours, 30 jours, mois courant, mois dernier, 6 mois,
  12 mois, année civile, **tout l'historique**, dernier événement, personnalisé.
  Le campus de session est sélectionné par défaut ; avec plusieurs campus actifs,
  « Tous les campus » est explicite. En production mono-campus, le sélecteur est
  masqué et les agrégats ne portent que sur Brest.
  *Tout l'historique* démarre à la première transaction conservée en base et ignore
  le plafond d'accès `max_history_days` : c'est la vue de bilan complet, à réserver
  aux usages qui en ont besoin.
- Quatre indicateurs (recettes, articles, panier moyen, étudiants uniques) sont
  comparés à la période précédente de même durée. La courbe des ventes agrège
  automatiquement par jour (jusqu'à 62 jours), semaine (jusqu'à 240 jours) ou mois
  au-delà ; les jours sans vente restent à zéro et les soirées sont repérées par
  une ligne verticale en vue journalière.
- La répartition par catégorie est un tableau avec barres de proportion, cliquable
  vers les articles de la catégorie. Sur les périodes de trois mois ou plus, un
  graphique empilé montre l'évolution du volume par catégorie mois par mois.
- Les tableaux Articles et Étudiants sont triables, paginés et cherchables. Chaque
  vue s'exporte en CSV (UTF-8 avec BOM, séparateur `;`), filtres et recherche
  courants compris ; la vue d'ensemble propose en plus l'export de la série
  temporelle agrégée (jour, semaine ou mois). Aucun schéma de base n'est nécessaire.
- La case **Masquer les blagues** retire les articles de type « Blague » des
  recettes, volumes, catégories, graphiques, tableaux et exports. Elle est
  cochée par défaut ; une puce « Sans blagues » rappelle le filtre actif et
  permet de réafficher les blagues.
