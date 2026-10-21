"""API externe v1 : lecture des consommations et écriture des prix standards.

Authentification par jeton porteur (`Authorization: Bearer …`). Aucune
donnée nominative n'est exposée : les agrégats portent uniquement sur les
articles sélectionnés pour le jeton.
"""

from datetime import timedelta

from flask import Blueprint, g, jsonify, request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import Article, Event, Keg, KegPrice, Transaction, TransactionLine
from app.services import api_tokens as AT
from app.services import audit as A
from app.utils import TAP_SIZES, euros, utcnow

bp = Blueprint("api_v1", __name__)


def _error(message, status=400, code="invalid"):
    response = jsonify(ok=False, code=code, error=message)
    if status == 429:
        response.headers["Retry-After"] = "1"
    return response, status


@bp.before_request
def authenticate():
    raw = AT.bearer_token(request.headers.get("Authorization"))
    token = AT.resolve_token(raw) if raw else None
    if token is None:
        return _error("Jeton d'API absent, invalide, expiré ou révoqué.", 401, "unauthorized")
    if not AT.enforce_rate_limit(token):
        return _error("Trop de requêtes : une requête par seconde maximum.", 429, "rate_limited")
    g.api_token = token


@bp.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store, private"
    return response


def _article_payload(article, events, include_team=False):
    data = {
        "id": article.id,
        "name": article.name,
        "article_type": article.article_type,
        "volume_cl": article.volume_cl,
        "is_alcohol": article.is_alcohol,
        "is_tap": article.is_tap,
        "tap_number": article.tap_number,
        "keg_id": article.keg_id,
        "event_id": article.event_id,
        "active": article.active,
        "price_std_cents": article.price_std,
    }
    if include_team:
        data["price_team_cents"] = article.price_team
    if article.event_id is not None:
        event = events.get(article.event_id)
        data["event_name"] = event.name if event else None
    return data


def _keg_payload(keg, price=None, include_team=False):
    data = {
        "id": keg.id,
        "name": keg.name,
        "volume_l": keg.volume_l,
        "active": keg.active,
        "price_half_std_cents": price.price_half_std if price else 0,
        "price_pint_std_cents": price.price_pint_std if price else 0,
        "price_pot_std_cents": price.price_pot_std if price else 0,
    }
    if include_team:
        data["price_half_team_cents"] = price.price_half_team if price else 0
        data["price_pint_team_cents"] = price.price_pint_team if price else 0
        data["price_pot_team_cents"] = price.price_pot_team if price else 0
    return data


def _keg_prices(keg_ids, campus):
    if not keg_ids:
        return {}
    rows = db.session.scalars(
        select(KegPrice).where(KegPrice.keg_id.in_(keg_ids), KegPrice.campus == campus)
    )
    return {row.keg_id: row for row in rows}


@bp.get("/catalog")
def catalog():
    token = g.api_token
    article_ids = AT.selected_article_ids(token)
    articles = (
        db.session.scalars(select(Article).where(Article.id.in_(article_ids))).all()
        if article_ids
        else []
    )
    articles = [article for article in articles if article.campus == token.campus]
    articles.sort(key=lambda article: (not article.is_tap, article.name.lower()))

    keg_ids = AT.selected_keg_ids(token)
    kegs = db.session.scalars(select(Keg).where(Keg.id.in_(keg_ids))).all() if keg_ids else []
    kegs.sort(key=lambda keg: keg.name.lower())
    prices = _keg_prices(keg_ids, token.campus)

    event_ids = {article.event_id for article in articles if article.event_id is not None}
    events = (
        {
            event.id: event
            for event in db.session.scalars(select(Event).where(Event.id.in_(event_ids)))
        }
        if event_ids
        else {}
    )

    return jsonify(
        ok=True,
        token={
            "label": token.label,
            "campus": token.campus,
            "scopes": token.scope_labels,
            "expires_at": AT.iso_utc(token.expires_at),
            "history_from": AT.iso_utc(token.history_from),
            "history_to": AT.iso_utc(token.history_to),
        },
        articles=[
            _article_payload(article, events, token.can_read_team_prices) for article in articles
        ],
        kegs=[_keg_payload(keg, prices.get(keg.id), token.can_read_team_prices) for keg in kegs],
    )


def _parse_bucket_seconds(raw):
    if raw is None or raw == "":
        return None
    if not raw.isascii() or not raw.isdigit():
        raise ValueError("bucket_seconds invalide.")
    value = int(raw)
    if value <= 0:
        raise ValueError("bucket_seconds doit être positif.")
    return value


@bp.get("/consumption")
def consumption():
    token = g.api_token
    if not token.can_read_consumption:
        return _error("Ce jeton n'a pas le droit de lecture des consommations.", 403, "forbidden")

    try:
        start = AT.parse_api_datetime(request.args.get("from")) or token.history_from
        end = AT.parse_api_datetime(request.args.get("to")) or token.history_to
    except ValueError as exc:
        return _error(str(exc))
    if start > end:
        return _error("La date de début doit précéder la date de fin.")
    if start < token.history_from or end > token.history_to:
        return _error("Fenêtre demandée hors des bornes du jeton.")

    group_by = request.args.get("group_by", "article")
    if group_by not in ("article", "category"):
        return _error("group_by doit valoir « article » ou « category ».")

    try:
        bucket_seconds = _parse_bucket_seconds(request.args.get("bucket_seconds"))
    except ValueError as exc:
        return _error(str(exc))
    window_seconds = max(1, int((end - start).total_seconds()))
    bucket_count = 0
    if bucket_seconds:
        bucket_seconds = min(bucket_seconds, window_seconds)
        bucket_count = (window_seconds + bucket_seconds - 1) // bucket_seconds
        if bucket_count > 5000:
            return _error("Trop de tranches : augmentez bucket_seconds ou réduisez la fenêtre.")

    article_ids = AT.selected_article_ids(token)
    articles = (
        db.session.scalars(select(Article).where(Article.id.in_(article_ids))).all()
        if article_ids
        else []
    )
    meta = {article.id: article for article in articles if article.campus == token.campus}
    ids = set(meta)
    keg_ids = AT.selected_keg_ids(token)

    groups = {}
    series = {}
    totals = {"quantity": 0, "revenue_cents": 0, "volume_ml": 0}

    if ids:
        stmt = (
            select(
                Transaction.created_at,
                TransactionLine.article_id,
                TransactionLine.article_type,
                TransactionLine.quantity,
                TransactionLine.line_total,
            )
            .select_from(TransactionLine)
            .join(Transaction, TransactionLine.transaction_id == Transaction.id)
            .where(
                Transaction.campus == token.campus,
                Transaction.type.in_(("achat", "direct")),
                Transaction.cancelled.is_(False),
                Transaction.created_at >= start,
                Transaction.created_at <= end,
                TransactionLine.article_id.in_(ids),
            )
        )
        for created_at, article_id, line_type, quantity, line_total in db.session.execute(
            stmt
        ).yield_per(1000):
            article = meta.get(article_id)
            if article is None:
                continue
            quantity = int(quantity or 0)
            revenue = int(line_total or 0)
            # volume_cl est exprimé en centilitres ; l'API expose des millilitres.
            volume = quantity * (article.volume_cl or 0) * 10
            key = article_id if group_by == "article" else line_type
            group = groups.setdefault(key, {"quantity": 0, "revenue_cents": 0, "volume_ml": 0})
            group["quantity"] += quantity
            group["revenue_cents"] += revenue
            group["volume_ml"] += volume
            totals["quantity"] += quantity
            totals["revenue_cents"] += revenue
            totals["volume_ml"] += volume

            if bucket_seconds:
                index = int((created_at - start).total_seconds() // bucket_seconds)
                index = min(max(index, 0), bucket_count - 1)
                bucket = series.setdefault(
                    (index, key),
                    {"quantity": 0, "revenue_cents": 0, "volume_ml": 0},
                )
                bucket["quantity"] += quantity
                bucket["revenue_cents"] += revenue
                bucket["volume_ml"] += volume

    group_rows = []
    for key, values in groups.items():
        if group_by == "article":
            article = meta[key]
            group_rows.append(
                {
                    "article_id": article.id,
                    "name": article.name,
                    "article_type": article.article_type,
                    "keg_id": article.keg_id,
                    "is_tap": article.is_tap,
                    **values,
                }
            )
        else:
            group_rows.append({"category": key, **values})
    group_rows.sort(key=lambda row: (-row["quantity"], -row["revenue_cents"]))
    if len(group_rows) > 5000:
        return _error("Trop de groupes : réduisez la sélection du jeton.")

    series_rows = []
    if bucket_seconds:
        for (index, key), values in series.items():
            bucket_start = start + timedelta(seconds=index * bucket_seconds)
            bucket_end = min(bucket_start + timedelta(seconds=bucket_seconds), end)
            row = {
                "from": AT.iso_utc(bucket_start),
                "to": AT.iso_utc(bucket_end),
                **values,
            }
            if group_by == "article":
                row["article_id"] = key
                row["name"] = meta[key].name
            else:
                row["category"] = key
            series_rows.append(row)
        if len(series_rows) > 5000:
            return _error("Trop de points : augmentez bucket_seconds ou réduisez la fenêtre.")
        series_rows.sort(
            key=lambda row: (row["from"], row.get("article_id", ""), row.get("category", ""))
        )

    kegs = db.session.scalars(select(Keg).where(Keg.id.in_(keg_ids))).all() if keg_ids else []
    kegs.sort(key=lambda keg: keg.name.lower())

    return jsonify(
        ok=True,
        generated_at=AT.iso_utc(utcnow()),
        window={"from": AT.iso_utc(start), "to": AT.iso_utc(end)},
        group_by=group_by,
        bucket_seconds=bucket_seconds,
        groups=group_rows,
        totals=totals,
        series=series_rows,
        kegs=[
            {
                "id": keg.id,
                "name": keg.name,
                "volume_l": keg.volume_l,
                "active": keg.active,
            }
            for keg in kegs
        ],
    )


def _id_value(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} invalide.")
    if value <= 0 or value > 2_147_483_647:
        raise ValueError(f"{label} invalide.")
    return value


def _price_value(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} invalide (centimes entiers attendus).")
    if value < 0 or value > AT.API_MAX_PRICE_CENTS:
        raise ValueError(f"{label} hors limites (0 à {AT.API_MAX_PRICE_CENTS} centimes).")
    return value


@bp.patch("/prices")
def prices():
    token = g.api_token
    if not token.can_write_prices and not token.can_write_team_prices:
        return _error("Ce jeton n'a pas le droit d'écriture des prix.", 403, "forbidden")

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("Corps JSON objet attendu.")
    articles_payload = payload.get("articles", [])
    kegs_payload = payload.get("kegs", [])
    if not isinstance(articles_payload, list) or not isinstance(kegs_payload, list):
        return _error("Les champs articles et kegs doivent être des listes.")
    if not articles_payload and not kegs_payload:
        return _error("Aucune modification de prix fournie.")
    if len(articles_payload) + len(kegs_payload) > AT.API_MAX_BATCH:
        return _error(f"Lot trop important ({AT.API_MAX_BATCH} modifications maximum).")

    direct_ids = {link.article_id for link in token.article_links}
    keg_ids = {link.keg_id for link in token.keg_links}

    article_updates = []
    seen_articles = set()
    for item in articles_payload:
        if not isinstance(item, dict):
            return _error("Chaque entrée d'article doit être un objet.")
        try:
            article_id = _id_value(item.get("article_id"), "article_id")
        except ValueError as exc:
            return _error(str(exc))
        if article_id in seen_articles:
            return _error("Article présent plusieurs fois dans le lot.")
        seen_articles.add(article_id)

        has_std = "price_std_cents" in item
        has_team = "price_team_cents" in item
        if not has_std and not has_team:
            return _error("Aucun prix fourni pour l'article.")
        if has_std and not token.can_write_prices:
            return _error(
                "Ce jeton n'a pas le droit d'écriture des prix standards.",
                403,
                "forbidden",
            )
        if has_team and not token.can_write_team_prices:
            return _error(
                "Ce jeton n'a pas le droit d'écriture des prix équipe.",
                403,
                "forbidden",
            )

        std_price = None
        team_price = None
        if has_std:
            try:
                std_price = _price_value(item["price_std_cents"], "price_std_cents")
            except ValueError as exc:
                return _error(str(exc))
        if has_team:
            try:
                team_price = _price_value(item["price_team_cents"], "price_team_cents")
            except ValueError as exc:
                return _error(str(exc))

        article = db.session.get(Article, article_id)
        if article is None:
            return _error("Article introuvable.")
        if article_id not in direct_ids or article.campus != token.campus or article.is_tap:
            return _error("Article hors du périmètre du jeton.", 403, "forbidden")
        article_updates.append(
            {
                "article": article,
                "std": std_price,
                "team": team_price,
                "previous_std": article.price_std,
                "previous_team": article.price_team,
            }
        )

    keg_updates = []
    seen_kegs = set()
    fields = {
        "price_half_std_cents": ("price_half_std", False, "demi standard"),
        "price_pint_std_cents": ("price_pint_std", False, "pinte standard"),
        "price_pot_std_cents": ("price_pot_std", False, "pot standard"),
        "price_half_team_cents": ("price_half_team", True, "demi équipe"),
        "price_pint_team_cents": ("price_pint_team", True, "pinte équipe"),
        "price_pot_team_cents": ("price_pot_team", True, "pot équipe"),
    }
    labels = {model_field: label for _key, (model_field, _team, label) in fields.items()}
    for item in kegs_payload:
        if not isinstance(item, dict):
            return _error("Chaque entrée de fût doit être un objet.")
        try:
            keg_id = _id_value(item.get("keg_id"), "keg_id")
        except ValueError as exc:
            return _error(str(exc))
        if keg_id in seen_kegs:
            return _error("Fût présent plusieurs fois dans le lot.")
        seen_kegs.add(keg_id)
        keg = db.session.get(Keg, keg_id)
        if keg is None:
            return _error("Fût introuvable.")
        if keg_id not in keg_ids:
            return _error("Fût hors du périmètre du jeton.", 403, "forbidden")

        provided = {}
        for key, (model_field, is_team, _label) in fields.items():
            if key not in item:
                continue
            if is_team and not token.can_write_team_prices:
                return _error(
                    "Ce jeton n'a pas le droit d'écriture des prix équipe.",
                    403,
                    "forbidden",
                )
            if not is_team and not token.can_write_prices:
                return _error(
                    "Ce jeton n'a pas le droit d'écriture des prix standards.",
                    403,
                    "forbidden",
                )
            try:
                provided[model_field] = _price_value(item[key], key)
            except ValueError as exc:
                return _error(str(exc))
        if not provided:
            return _error("Aucun prix fourni pour le fût.")
        keg_updates.append((keg, provided))

    updated_articles = []
    for update in article_updates:
        article = update["article"]
        changes = []
        if update["std"] is not None and article.price_std != update["std"]:
            changes.append(f"standard {euros(article.price_std)} → {euros(update['std'])}")
            article.price_std = update["std"]
        if update["team"] is not None and article.price_team != update["team"]:
            changes.append(f"équipe {euros(article.price_team)} → {euros(update['team'])}")
            article.price_team = update["team"]
        if changes:
            A.record(
                "api.prix_article",
                target=article.name,
                details=f"Prix {', '.join(changes)} (jeton {token.label})",
                campus=token.campus,
                actor_label=f"Jeton API — {token.label}",
            )
        data = {
            "id": article.id,
            "name": article.name,
            "price_std_cents": article.price_std,
            "previous_price_std_cents": update["previous_std"],
        }
        if update["team"] is not None or token.can_read_team_prices:
            data["price_team_cents"] = article.price_team
            data["previous_price_team_cents"] = update["previous_team"]
        updated_articles.append(data)

    updated_kegs = []
    all_fields = (
        "price_half_std",
        "price_pint_std",
        "price_pot_std",
        "price_half_team",
        "price_pint_team",
        "price_pot_team",
    )
    for keg, provided in keg_updates:
        row = keg.price_row(token.campus)
        previous = {model_field: getattr(row, model_field) for model_field in all_fields}
        changes = []
        for model_field, value in provided.items():
            old = getattr(row, model_field)
            if old != value:
                setattr(row, model_field, value)
                changes.append(f"{labels[model_field]} {euros(old)} → {euros(value)}")
        if changes:
            A.record(
                "api.prix_fut",
                target=keg.name,
                details=f"Prix {', '.join(changes)} (jeton {token.label})",
                campus=token.campus,
                actor_label=f"Jeton API — {token.label}",
            )

        # Les articles de tireuse sont des copies du prix du fût : on les met à
        # jour en place pour que la caisse voie les nouveaux prix immédiatement.
        tap_articles = db.session.scalars(
            select(Article).where(
                Article.keg_id == keg.id,
                Article.is_tap.is_(True),
                Article.campus == token.campus,
            )
        ).all()
        for article in tap_articles:
            size = next(
                (key for key, (_label, volume) in TAP_SIZES.items() if volume == article.volume_cl),
                None,
            )
            prefix = {"demi": "half", "pinte": "pint", "pot": "pot"}.get(size)
            if prefix is None:
                continue
            std_field = f"price_{prefix}_std"
            team_field = f"price_{prefix}_team"
            if std_field in provided:
                article.price_std = provided[std_field]
            if team_field in provided:
                article.price_team = provided[team_field]

        data = {
            "id": keg.id,
            "name": keg.name,
            "price_half_std_cents": row.price_half_std,
            "price_pint_std_cents": row.price_pint_std,
            "price_pot_std_cents": row.price_pot_std,
            "previous_price_half_std_cents": previous["price_half_std"],
            "previous_price_pint_std_cents": previous["price_pint_std"],
            "previous_price_pot_std_cents": previous["price_pot_std"],
        }
        if any(model_field.endswith("_team") for model_field in provided) or (
            token.can_read_team_prices
        ):
            data.update(
                {
                    "price_half_team_cents": row.price_half_team,
                    "price_pint_team_cents": row.price_pint_team,
                    "price_pot_team_cents": row.price_pot_team,
                    "previous_price_half_team_cents": previous["price_half_team"],
                    "previous_price_pint_team_cents": previous["price_pint_team"],
                    "previous_price_pot_team_cents": previous["price_pot_team"],
                }
            )
        updated_kegs.append(data)

    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        return _error("Erreur interne lors de l'enregistrement des prix.", 500, "server_error")

    return jsonify(ok=True, updated={"articles": updated_articles, "kegs": updated_kegs})
