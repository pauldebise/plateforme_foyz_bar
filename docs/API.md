# API externe — Foy'z & Bar

API destinée aux logiciels tiers autorisés (par exemple un script qui adapte
les prix pendant une soirée). Elle permet de lire des **agrégats de
consommation** et de modifier les **prix standards**. Elle ne permet jamais
d'encaisser une vente : la caisse de la plateforme reste le seul point de
vente.

## Principes

- Un jeton est créé et géré uniquement depuis **Administrateur → API**, par le
  compte `admin` (super administrateur). Les membres de l'équipe n'y ont pas
  accès.
- Un jeton est limité à **un campus**, à une **fenêtre de lecture** et à une
  **sélection précise** d'articles standards, d'articles d'événement et de
  fûts. Un fût donne accès à ses trois tailles : demi, pinte et pot. Un fût
  **non monté** peut être sélectionné s'il a des prix sur le campus ; ses prix
  s'appliquent lorsqu'il est installé sur une tireuse.
- La sélection, les bornes et l'expiration peuvent être modifiées à tout
  moment ; le changement est immédiat, sans régénérer le jeton.
- Le jeton en clair n'est affiché **qu'une seule fois**, à la création. Seule
  son empreinte SHA-256 est conservée.
- **Aucune donnée nominative** n'est exposée : ni nom d'étudiant, ni solde, ni
  blacklist. Les agrégats portent sur les articles sélectionnés.
- Une requête par seconde au maximum, toutes requêtes confondues.

## Authentification

Base : `https://foyz.fr/api/v1`

En-tête obligatoire :

```http
Authorization: Bearer fz_…
```

Une requête sans jeton valide reçoit `401`. Un jeton expiré ou révoqué est
refusé immédiatement. La limite d'une requête par seconde renvoie `429` avec
`Retry-After: 1`.

## Unités et dates

- Prix et recettes : **centimes entiers** (`price_std_cents`, `revenue_cents`).
- Volumes : `volume_cl` en centilitres dans le catalogue, `volume_ml` en
  millilitres dans les agrégats.
- Dates : ISO 8601. Les dates sans fuseau sont interprétées en UTC ; les dates
  avec décalage sont converties. Les réponses utilisent le suffixe `Z`.

## `GET /catalog`

Retourne les articles et les fûts autorisés, avec leurs prix standards actuels.

```bash
curl -H "Authorization: Bearer $JETON" https://foyz.fr/api/v1/catalog
```

```json
{
  "ok": true,
  "token": {
    "label": "Script prix soirée",
    "campus": "brest",
    "scopes": ["consommation:lecture", "prix:ecriture"],
    "expires_at": "2026-10-25T02:00:00Z",
    "history_from": "2026-10-24T18:00:00Z",
    "history_to": "2026-10-25T02:00:00Z"
  },
  "articles": [
    {
      "id": 12,
      "name": "Pinte de tireuse 1 (IPA)",
      "article_type": "biere",
      "volume_cl": 50,
      "is_alcohol": true,
      "is_tap": true,
      "tap_number": 1,
      "keg_id": 4,
      "event_id": null,
      "active": true,
      "price_std_cents": 400
    }
  ],
  "kegs": [
    {
      "id": 4,
      "name": "IPA",
      "volume_l": 30.0,
      "remaining_l": 19.75,
      "active": true,
      "price_half_std_cents": 200,
      "price_pint_std_cents": 400,
      "price_pot_std_cents": 300
    }
  ]
}
```

Les articles d'événement portent en plus `event_name`. Les articles de
tireuse sont présents à travers le fût sélectionné (les trois tailles).

## `GET /consumption`

Agrégats des ventes des articles autorisés, sur une sous-fenêtre des bornes du
jeton.

Paramètres :

| Paramètre | Défaut | Description |
|---|---|---|
| `from` | début de la fenêtre du jeton | début ISO 8601, inclus |
| `to` | fin de la fenêtre du jeton | fin ISO 8601, incluse |
| `group_by` | `article` | `article` ou `category` |
| `bucket_seconds` | absent | tranches de temps (secondes), par exemple `300` pour 5 min |

```bash
curl -H "Authorization: Bearer $JETON" \
  "https://foyz.fr/api/v1/consumption?group_by=article&bucket_seconds=300"
```

```json
{
  "ok": true,
  "generated_at": "2026-10-24T22:15:00Z",
  "window": {"from": "2026-10-24T18:00:00Z", "to": "2026-10-25T02:00:00Z"},
  "group_by": "article",
  "bucket_seconds": 300,
  "groups": [
    {
      "article_id": 12,
      "name": "Pinte de tireuse 1 (IPA)",
      "article_type": "biere",
      "keg_id": 4,
      "is_tap": true,
      "quantity": 27,
      "revenue_cents": 10800,
      "volume_ml": 13500
    }
  ],
  "totals": {"quantity": 27, "revenue_cents": 10800, "volume_ml": 13500},
  "series": [
    {
      "from": "2026-10-24T22:10:00Z",
      "to": "2026-10-24T22:15:00Z",
      "article_id": 12,
      "name": "Pinte de tireuse 1 (IPA)",
      "quantity": 3,
      "revenue_cents": 1200,
      "volume_ml": 1500
    }
  ],
  "kegs": [
    {"id": 4, "name": "IPA", "volume_l": 30.0, "remaining_l": 19.75, "active": true}
  ]
}
```

- Les transactions annulées sont exclues.
- Les recettes sont les montants réellement encaissés (tarif standard ou
  équipe selon les acheteurs).
- `series` ne contient que les tranches où au moins une vente a eu lieu.
- Une fenêtre hors des bornes du jeton, un `group_by` inconnu ou trop de points
  renvoient `400`.

## `PATCH /prices`

Modifie des prix **standards** par lot, de façon atomique : si une entrée est
invalide, aucune modification n'est appliquée.

```bash
curl -X PATCH -H "Authorization: Bearer $JETON" -H "Content-Type: application/json" \
  -d '{
    "articles": [
      {"article_id": 12, "price_std_cents": 450},
      {"article_id": 18, "price_std_cents": 600}
    ],
    "kegs": [
      {"keg_id": 4, "price_half_std_cents": 250, "price_pint_std_cents": 450, "price_pot_std_cents": 350}
    ]
  }' \
  https://foyz.fr/api/v1/prices
```

- `articles` : articles standards ou d'événement sélectionnés. Les articles de
  tireuse ne se modifient pas directement : utilisez `kegs`.
- `kegs` : au moins un des trois prix (`price_half_std_cents`,
  `price_pint_std_cents`, `price_pot_std_cents`). Les trois articles de
  tireuse correspondants sont mis à jour dans la même transaction.
- Prix entre `0` et `10000` centimes (100 €), entiers uniquement.
- Les prix équipe (`price_team`) ne sont **jamais** modifiés par l'API.
- Maximum 200 modifications par requête ; pas de doublon dans un lot.

Réponse :

```json
{
  "ok": true,
  "updated": {
    "articles": [
      {"id": 12, "name": "Pinte de tireuse 1 (IPA)", "price_std_cents": 450, "previous_price_std_cents": 400}
    ],
    "kegs": [
      {
        "id": 4,
        "name": "IPA",
        "price_half_std_cents": 250,
        "price_pint_std_cents": 450,
        "price_pot_std_cents": 350,
        "previous_price_half_std_cents": 200,
        "previous_price_pint_std_cents": 400,
        "previous_price_pot_std_cents": 300
      }
    ]
  }
}
```

## Erreurs

```json
{"ok": false, "code": "forbidden", "error": "Article hors du périmètre du jeton."}
```

| Statut | Signification |
|---|---|
| `400` | requête ou valeur invalide, fenêtre hors bornes |
| `401` | jeton absent, invalide, expiré ou révoqué |
| `403` | portée absente ou article/fût hors du périmètre |
| `404` | ressource inconnue |
| `405` | méthode non autorisée |
| `429` | plus d'une requête par seconde (`Retry-After: 1`) |
| `500` | erreur interne |

## Côté caisse

- Le catalogue de la caisse se rafraîchit toutes les 8 secondes : un prix
  modifié par l'API est répercuté à l'écran sans rechargement, avec un message
  discret.
- Les articles d'événement sont vendables depuis la caisse équipe et depuis la
  passerelle uniquement pendant la fenêtre de l'événement (même règle que la
  passerelle).
- Le prix effectivement débité est celui du serveur au moment de
  l'encaissement ; un écart de quelques secondes avec l'affichage est possible
  et accepté.

## Gestion des jetons

Dans **Administrateur → API** : création, modification de la sélection, des
bornes, de l'expiration et des portées, révocation immédiate. Le jeton reste
inchangé lors d'une modification ; il suffit de révoquer et d'en recréer un en
cas de perte.

Un **guide texte prêt à transmettre** aux tiers est téléchargeable depuis cet
onglet (`/static/guide-api-externe.txt`). Il reprend l'authentification, les
endpoints, les unités, les erreurs et les règles de sécurité.
