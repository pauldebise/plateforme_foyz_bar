# Compte rendu de réalisation — Plateforme informatique unifiée des Foy'z & Bar

**Projet :** Système informatique unifié des Foy'z/Bar — ENSTA Bretagne & ENSTA Paris
**Référence :** Cahier des charges v1.0 (`docs/main.tex`)
**Date :** 29 août 2026

---

## 1. Synthèse

La plateforme décrite dans le cahier des charges est **réalisée et fonctionnelle**. Il s'agit
d'une application web client-serveur en Python/Flask avec base de données relationnelle
(SQLAlchemy — SQLite en développement, PostgreSQL en production), organisée en trois espaces :

1. **Interface publique** — accessible sans authentification ;
2. **Interface équipe** — réservée aux membres et anciens membres, avec choix du campus ;
3. **Interface administrateur** — réservée aux membres de mandat ;
4. **Passerelle événement** — encaissement simplifié pour les associations organisatrices.

La monnaie du système est **fictive** : les soldes sont des compteurs en centimes d'euro,
sans lien avec la trésorerie réelle de l'organisme (chaque étudiant possède un portefeuille
**Brest** et un portefeuille **Paris**, strictement séparés).

Toutes les exigences fonctionnelles du cahier des charges ont été implémentées ; les points
d'interprétation et les choix techniques sont listés en §6.

## 2. Couverture du cahier des charges

### 2.1 Interface publique (§2 du CDC)

| Exigence | Implémentation | Route |
|---|---|---|
| Page d'accueil : événements par campus avec affiches | Événements à venir/en cours, affiche téléversée | `/` |
| Notes publiques des équipes | Post-its publics sur l'accueil (publication : équipe uniquement) | `/` |
| Prix standards par campus | Catalogue public groupé par type, prix standard et équipe | `/catalogue` |
| Règlement intérieur (Brest/Paris) | PDF téléversés dans le Module développement, affichage + iframe | `/reglement` |
| Liens utiles configurables | Liens gérés dans le Module développement (ex : plateformes VSS) | `/liens` |
| Trombinoscopes des deux équipes | Membres de mandat par campus, photo + fonction éditables | `/trombinoscope` |
| Page d'identification | Connexion identifiant/mot de passe + **lieu de connexion** | `/connexion` |

### 2.2 Interface équipe (§3 du CDC)

L'accès exige un compte équipe ; le **campus de connexion** est choisi à l'authentification et
détermine le portefeuille utilisé, les prix affichés et le rattachement des bilans.

| Onglet | Réalisation |
|---|---|
| **Paiement** | Recherche d'étudiants (autocomplétion), sélection de **contributeurs multiples**, panier avec boutons **+ / −** par article, débit automatique du/des soldes à la validation. Option **« payer directement »** pour les visiteurs sans compte (moyen de paiement enregistré, détaillé séparément en stats/trésorerie). |
| **Commande partagée** | Le montant total (articles + consignes) est divisé équitablement entre contributeurs. |
| **Contrôles blacklist** | Tout contributeur *blacklist* ⇒ transaction refusée avec notification nominative. Un article alcoolisé + un contributeur *blacklist alcool* ⇒ refus également (statuts **mutualisés entre campus**). |
| **Découvert** | Si la part d'un contributeur dépasse son solde : confirmation par **mot de passe administrateur** (l'étudiant passe en négatif). Si le découvert résultant dépasse la **limite autorisée** (paramétrable), la transaction est refusée. |
| **Consignes** | Activables à la commande (montant paramétrable) : débit à l'emprunt, **crédit instantané au retour** ; suivi du nombre de verres non rendus par étudiant ; panneau dédié « Retour de consignes ». |
| **Rechargement** | Nom + montant + campus + moyen de paiement parmi **CB, Lydia, Espèces, HelloAsso**. |
| **Retrait** | Nom + montant + campus ; bloqué si le montant dépasse le solde disponible. |
| **Transfert** | Entre deux étudiants, portefeuilles du **même campus** ; solde du donneur vérifié. |
| **Historique** | Journal global antéchronologique : date/heure, étudiant(s), type (Achat, Retrait, Transfert, Rechargement…), somme ; **filtres** (type, campus, étudiant, période) + recherche. **Annulation** (une transaction) sur mot de passe administrateur avec recalcul des soldes. |
| **Statistiques** | Volumes et tendances : filtres période, **catégorie d'articles**, **promotion**, **campus**, acheteur équipe Foy'z/Bar ou non ; graphiques (par catégorie, tendance journalière) ; **statistiques par profil étudiant** (nombre d'achats, articles consommés, total dépensé, top 10 en graphique), commandes partagées réparties entre participants. |
| **Trésorerie** | Bilans mensuels par campus : rechargements (détail par moyen de paiement), ventes (détail par type d'article), recettes des événements ; **graphe 12 mois glissants** distinguant les **entrées réelles** (rechargements + recettes d'événements, barres empilées) de la **consommation de la monnaie virtuelle** (ventes bar, courbe) afin d'éviter tout double comptage — le « Total entrées » ne somme que les entrées réelles. **Exports CSV** : rapport mensuel (par ligne du tableau) et rapport annuel (12 mois glissants, avec totaux). |
| **Notes (Post-it)** | Espace **privé** d'équipe (publier/modifier/supprimer) et espace **public** affiché à l'accueil ; seules les notes les plus récentes sont conservées (nombre maximal paramétrable, les plus anciennes sont purgées). |

### 2.3 Administrateur (§4 du CDC)

Accès réservé aux **membres de mandat** (les anciens membres reçoivent une erreur 403).

| Sous-onglet | Réalisation |
|---|---|
| **Gestion des comptes** | Recherche globale (nom, identifiant), fiche complète : nom, promotion, soldes des deux portefeuilles, verres consignés, statuts *blacklist* / *blacklist alcool* ; profils modifiables à tout moment ; création de comptes (portefeuilles Brest + Paris initialisés). Le retrait du statut *blacklist alcool* exige le mot de passe administrateur (l'étudiant ne peut pas se le retirer seul). |
| **Équipe** | Attribution/retrait du statut équipe ; distinction **mandat** / **ancien membre** ; identifiant + mot de passe (haché) ; photo + fonction pour le trombinoscope. Un compte *blacklisté* perd tous ses accès (connexion bloquée). |
| **Articles** | Fiche : nom, prix standard + prix Foy'z/barreux **pour chaque campus**, volume, type (Bière, Vin, Cidre, Snacks, Saucisson, Évènement/Soirée) ; les articles **tireuse** et **événement** ne sont pas modifiables ici (onglets dédiés). |
| **Bières pression & tireuses** | Suivi des fûts (nom, degré, volume total, **volume restant**, prix au demi/pinte/pot standards et équipe, par campus) ; **assignation d'un fût à une tireuse** générant automatiquement les articles au format `[Demi/Pinte/Pot] de tireuse [Numéro] ["Nom du Fût"]` ; décrément du volume restant à chaque vente et désactivation automatique à sec. |
| **Événements** | Planification (nom, campus, heures début/fin, affiche) ; catalogue d'**articles temporaires** dédié ; génération d'une **passerelle à sens unique** (lien sécurisé à jeton) offrant une interface d'encaissement simplifiée aux organisateurs (ex : BDE) — limitée aux **articles temporaires de l'événement** ; retour à l'interface équipe = **nouvelle authentification**. |
| **Registre des connexions** | Traçabilité : compte, campus, IP, succès/échec, date/heure ; filtres par date et nom ; suppression automatique après la durée configurable. |
| **Module développement** | Configuration : montant du découvert autorisé, valeur de la consigne, PDF des règlements (par campus), édition de la page d'accueil, **thème de couleur + logo par campus**, trombinoscopes, durée d'accès aux transactions depuis le site (données conservées en base), délai de déconnexion d'inactivité, mot de passe administrateur, nombre maximal de post-it (privé/public), liens utiles, liens vers l'hébergeur / la base de données / le dépôt du code source. |

## 3. Architecture & organisation

```
app/
├── __init__.py      factory : CSRF, sessions, filtres Jinja, contexte, erreurs
├── config.py        configuration par environnement (variables d'environnement)
├── models/          11 tables SQLAlchemy (users, wallets, articles, kegs, keg_prices,
│                    taps, events, transactions, transaction_lines, contributions,
│                    notes, settings, login_logs, useful_links)
├── services/        logique métier pure (transactions, stats, trésorerie, paramètres,
│                    fûts/tireuses) — testable indépendamment des routes
├── routes/          6 blueprints (public, auth, team, admin, gateway, api)
├── templates/       Jinja2, un socle commun (base.html) + un dossier par espace
└── static/          CSS personnalisé + JS (caisse, recherche étudiant, opérations)
```

**Choix notables**

- **Séparation routes / services / modèles** : les règles d'encaissement (le cœur sensible)
  sont concentrées dans `app/services/transactions.py`, appelées aussi bien par l'interface
  équipe que par la passerelle événement — une seule implémentation des contrôles.
- **Traçabilité** : chaque transaction stocke ses lignes d'articles (avec prix unitaires
  figés) et ses contributions signées par étudiant avec le solde résultant — les annulations
  et la trésorerie s'appuient sur ce journal.
- **Montants entiers en centimes** partout (pas d'arithmétique flottante monétaire).
- Horodatage UTC en base, conversion Europe/Paris à l'affichage et pour les bilans mensuels.

## 4. Sécurité mise en œuvre

- Authentification : mots de passe hachés (scrypt), anti-bruteforce (8 essais / 5 min / IP),
  journal des connexions (y compris échecs) avec purge automatique.
- Sessions signées `HttpOnly`, `SameSite=Lax`, expiration d'inactivité côté serveur,
  cookies `Secure` derrière HTTPS (`HTTPS_ONLY=1`).
- **CSRF** : jeton de session vérifié sur tous les POST (formulaires et API JSON).
- Autorisations à trois niveaux : public < équipe (mandat + anciens) < admin (mandat seul),
  vérifiées avant chaque requête ; un compte *blacklist* ne peut plus se connecter.
- Mot de passe administrateur distinct, requis pour les opérations sensibles
  (découvert, annulation, retrait de « blacklist alcool »).
- Contrôles serveur systématiques (blacklist, alcool, découvert, soldes, campus, droits) —
  le JavaScript n'est qu'une facilité d'usage.
- En-têtes de sécurité HTTP ; téléversements filtrés par extension et nom sécurisé.

## 5. Vérifications effectuées (bout en bout)

- Pages publiques, connexion, expiration de session, contrôle CSRF (400 sans jeton).
- Achat simple ; **refus** pour contributeur blacklisté ; **refus** alcool + blacklist alcool ;
  **découvert** : demande de mot de passe administrateur puis passage en négatif ; **refus**
  au-delà de la limite de découvert ; paiement direct visiteur.
- Consigne : débit à l'emprunt, retour crédité, refus si plus de verres que détenus.
- Rechargement (4 moyens de paiement), retrait bloqué au-delà du solde, transfert bloqué si
  solde donneur insuffisant (solde inchangé vérifié).
- Annulation de transaction : refusée sans/avec mauvais mot de passe, correcte avec le bon
  (soldes recalculés, consignes annulées).
- Statistiques : filtres promotion / équipe / catégorie / période / campus vérifiés via API.
- Trésorerie : agrégats mensuels (rechargements par moyen, ventes par type, événements).
- Fûts : génération du catalogue tireuse au format attendu, décrément du volume restant
  (4 demis = 1 L vérifié).
- Passerelle : accès par jeton, encaissement direct, refus d'achat sans contributeur ni
  paiement direct, redirection vers l'authentification pour repasser en interface équipe.
- Droits : ancien membre ⇒ équipe OK / admin 403 ; compte sans équipe ⇒ connexion refusée ;
  API sans session ⇒ 401.
- Suite : 21 routes vérifiées en HTTP 200, journal d'erreurs vide.

## 6. Interprétations et choix assumés

Le cahier des charges laissait quelques points ouverts ; les choix suivants ont été faits :

1. **Prix équipe** : appliqué lorsque *tous* les contributeurs d'une commande sont membres
   (mandat ou ancien) de l'équipe ; sinon prix standard (cas d'une commande mixte).
2. **Consigne en commande partagée** : les verres empruntés sont portés au compte du premier
   contributeur ; la consigne est désactivée pour les paiements directs (pas de compte).
3. **Post-it** : au-delà du maximum configuré, les notes les plus anciennes sont supprimées
   (et non masquées) afin d'éviter tout encombrement.
4. **« Statut Foy'z/Bar ou non »** (filtre statistiques) : interprété comme acheteur membre
   de l'équipe ou non.
5. **Fûts** : les prix du fût sont saisis par campus ; l'assignation à une tireuse projette
   les prix du campus de la tireuse sur l'article généré.
6. **Passerelle** : le lien à jeton constitue l'authentification des organisateurs pendant la
   fenêtre de l'événement (−12 h avant le début) ; toute navigation vers l'interface équipe
   exige une connexion complète.
7. **Annulation** : le volume de fût consommé n'est pas re-crédité (physiquement bu), les
   soldes, consignes et statuts sont, eux, intégralement restitués.

## 7. Limites connues et perspectives

- L'anti-bruteforce est en mémoire par processus : derrière plusieurs workers, un front
  (Nginx/`fail2ban`) ou un stockage partagé renforcerait la limitation.
- Pas de migration de schéma automatisée (Alembic) : en cas d'évolution du modèle, recréer
  ou migrer la base — sans impact en production au premier déploiement.
- Perspectives : application mobile caisse hors-ligne, export comptable (CSV/PDF),
  notifications (stock faible des fûts), gestion fine des stocks snacks.

## 8. Déploiement

Les instructions complètes (Docker Compose avec PostgreSQL, ou serveur Gunicorn + Nginx +
systemd, variables d'environnement, sauvegardes) figurent dans le **README.md** à la racine
du projet.
