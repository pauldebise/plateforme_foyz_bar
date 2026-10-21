# Politique de confidentialité — plateforme Foy'z & Bar (modèle à adapter)

Ce document est un **modèle** destiné à être relu par les responsables de
l'association puis publié sur le site (page publique ou document lié). Il décrit
les données réellement traitées par la plateforme.

## Responsable du traitement

L'association Foy'z & Bar de l'ENSTA (Brest et Paris), représentée par son
bureau. Contact : _(adresse e-mail à compléter)_.

## Données traitées

| Donnée | Finalité | Base légale | Conservation |
|---|---|---|---|
| Identité (nom, prénom, surnom, promotion) | Gestion des comptes et des portefeuilles | Intérêt légitime associatif (exécution du service) | Durée d'adhésion + purge annuelle |
| Identifiant et mot de passe (haché) | Accès au service | Exécution du service | Jusqu'à suppression du compte |
| Soldes et historique des transactions | Comptabilité du bar, justification des soldes | Intérêt légitime / obligations comptables | **Non purgé** (pièces justificatives) |
| Verres empruntés (consignes) | Suivi du prêt de gobelets | Exécution du service | Remis à zéro à la restitution |
| Statuts (blacklist, blacklist alcool) | Sécurité et conformité | Intérêt légitime | Jusqu'à retrait du statut |
| Registre des connexions (identifiant du compte, IP, succès/échec) | Sécurité (détection d'intrusions) | Intérêt légitime (sécurité du système) | Affichage limité à 90 jours par défaut ; **pas de purge automatique** (purge manuelle par l'administrateur système) |
| Fichiers téléversés (affiches, logos, photos) | Communication de l'association | Consentement des personnes concernées | Durée de publication |
| Jetons d'API (libellé, empreinte, campus, portées, bornes, dernier appel) | Accès machine autorisé aux agrégats et aux prix | Intérêt légitime (sécurité et fonctionnement du service) | Jusqu'à révocation ou suppression du jeton ; actions conservées dans le journal d'audit |

Les données de l'ancienne plateforme (sauvegardes de migration) sont conservées
**au maximum 30 jours** après la bascule puis détruites (`ARCHIVE_RETENTION_DAYS`).

## Accès

- Seuls les membres de l'équipe du campus disposent d'un accès nominatif.
- Les données ne sont **jamais** cédées ni transmises à des tiers ; elles ne
  quittent pas le serveur de l'association. Les sauvegardes peuvent être
  chiffrées de bout en bout (GnuPG AES-256, voir README §7.1) et le stockage
  (base, téléversements) repose sur un volume chiffré.
- Chaque membre accède aux données de son campus ; l'administrateur peut agir
  sur les deux campus.
- Les jetons d'API sont créés uniquement par le compte d'administration, pour
  un campus et une durée déterminés. L'API externe ne reçoit **aucune donnée
  nominative** : uniquement des quantités, recettes et volumes agrégés sur les
  articles autorisés, ainsi que leurs prix standards.

## Sécurité

- Mots de passe hachés (werkzeug/scrypt), jamais stockés en clair.
- Connexions chiffrées (HTTPS/HSTS), cookies `Secure`/`HttpOnly`, jetons CSRF.
- Journalisation applicative sans mot de passe ; limiteur anti-force brute sur
  la connexion.
- Jetons d'API conservés sous forme d'empreinte SHA-256, jamais en clair,
  limités à une requête par seconde ; créations, modifications, révocations et
  changements de prix sont journalisés.
- Sauvegardes quotidiennes conservées 7 jours (+ 4 hebdomadaires), restauration
  testée.

## Droits des personnes

Toute personne peut demander l'accès, la rectification ou la suppression de ses
données (droit à l'effacement) en écrivant au bureau. La suppression d'un compte
est possible depuis l'administration : les données d'identité sont effacées et
l'historique comptable est conservé sous forme anonyme (`user_id` mis à NULL).

## Mise à jour

Version du _(date à compléter)_. Prévenir les personnes concernées en cas de
changement substantiel.
