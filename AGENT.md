# Instructions pour l'agent de déploiement (OpenCode)

Ce document définit les règles d'intervention, les contraintes d'infrastructure et les procédures d'exécution pour le déploiement de l'environnement de développement.

---

## 1. Contexte et accès d'infrastructure

- **Cible distante** : Serveur VPS Hetzner.
- **Accès SSH** : Utiliser l'alias configuré localement `hetzner-site`.
  - Utilisateur distant : `deploy` (accès `root` strictement interdit).
  - Exécution distante : `ssh hetzner-site "<commande>"`.
  - Transfert de fichiers : `rsync -avz ... hetzner-site:<chemin>`.
- **Nom de domaine ciblé** : `dev.foyz.fr` (zone Cloudflare : `foyz.fr`).
- **Variables d'environnement disponibles localement** :
  - `CLOUDFLARE_API_TOKEN` : jeton d'API avec droits DNS, Cache et Rules.
  - `CLOUDFLARE_ZONE_ID` : identifiant de la zone Cloudflare `foyz.fr`.

---

## 2. Règles strictes de sécurité et gardes-fous

1. **Isolation DNS** :
   - Modifier ou créer **uniquement** l'enregistrement DNS de type A pour le sous-domaine `dev` (`dev.foyz.fr`).
   - Ne jamais altérer, remplacer ou supprimer l'enregistrement racine (`@` / `foyz.fr`) ni aucun autre enregistrement DNS existant.
2. **Droits système sur le VPS** :
   - Agir exclusivement sous l'utilisateur `deploy`.
   - Ne pas modifier les fichiers système protégés (`/etc/ssh/`, `/etc/sudoers`, etc.).
   - Gérer les conteneurs avec Docker sans élévation de privilèges (l'utilisateur `deploy` appartient déjà au groupe `docker`).
3. **Gestion des secrets** :
   - Ne jamais logger ou afficher en clair le contenu de `$CLOUDFLARE_API_TOKEN`.
   - Ne jamais commiter ni téléverser de fichiers `.env` non prévus pour le suivi Git.

---

## 3. Procédure standard de déploiement

Exécuter méthodiquement les étapes suivantes :

### Étape 1 : Contrôle des prérequis et de la connectivité
```bash
ssh hetzner-site "docker --version && docker compose version && uptime"
```

### Étape 2 : Déploiement applicatif
1. Synchroniser le code source vers le répertoire cible sur le serveur (ex. `/home/deploy/app/`) en excluant les dossiers temporaires (`.git`, `node_modules`, `__pycache__`, `.env.local`).
2. Reconstruire et relancer les conteneurs :
   ```bash
   ssh hetzner-site "cd /home/deploy/app && docker compose up -d --build"
   ```

### Étape 3 : Contrôle de santé local sur le VPS
Vérifier que le service répond localement avant de basculer le trafic externe :
```bash
ssh hetzner-site "curl -sI [http://127.0.0.1:8000](http://127.0.0.1:8000) | head -n 1"
```

---

## 4. Configuration Cloudflare (API REST)

Toutes les interactions avec Cloudflare doivent s'effectuer via des requêtes cURL utilisant `$CLOUDFLARE_API_TOKEN` et `$CLOUDFLARE_ZONE_ID`.

### 1. Vérifier si l'enregistrement `dev.foyz.fr` existe
```bash
curl -s -X GET "[https://api.cloudflare.com/client/v4/zones/$CLOUDFLARE_ZONE_ID/dns_records?name=dev.foyz.fr](https://api.cloudflare.com/client/v4/zones/$CLOUDFLARE_ZONE_ID/dns_records?name=dev.foyz.fr)" \
     -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
     -H "Content-Type: application/json"
```

### 2. Créer l'enregistrement A (si inexistant)
```bash
curl -s -X POST "[https://api.cloudflare.com/client/v4/zones/$CLOUDFLARE_ZONE_ID/dns_records](https://api.cloudflare.com/client/v4/zones/$CLOUDFLARE_ZONE_ID/dns_records)" \
     -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
     -H "Content-Type: application/json" \
     --data '{"type":"A","name":"dev","content":"IP_DU_VPS","ttl":1,"proxied":true}'
```

### 3. Mettre à jour l'enregistrement A (si existant)
```bash
curl -s -X PUT "[https://api.cloudflare.com/client/v4/zones/$CLOUDFLARE_ZONE_ID/dns_records/](https://api.cloudflare.com/client/v4/zones/$CLOUDFLARE_ZONE_ID/dns_records/)<RECORD_ID>" \
     -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
     -H "Content-Type: application/json" \
     --data '{"type":"A","name":"dev","content":"IP_DU_VPS","ttl":1,"proxied":true}'
```

### 4. Purger le cache après mise à jour
```bash
curl -s -X POST "[https://api.cloudflare.com/client/v4/zones/$CLOUDFLARE_ZONE_ID/purge_cache](https://api.cloudflare.com/client/v4/zones/$CLOUDFLARE_ZONE_ID/purge_cache)" \
     -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
     -H "Content-Type: application/json" \
     --data '{"purge_everything":true}'
```

---

## 5. Livrables attendus en fin d'exécution

À la fin de chaque session de travail, restituer un récapitulatif compact :
1. **Statut de l'application** : état des conteneurs (`docker compose ps`).
2. **Statut DNS** : validation du pointage de `dev.foyz.fr` vers le proxy Cloudflare.
3. **Validation HTTP publique** : code de retour obtenu en interrogeant `[https://dev.foyz.fr](https://dev.foyz.fr)`.
