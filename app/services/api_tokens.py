"""Gestion des jetons d'API externe.

Le jeton en clair n'est jamais stocké ni journalisé : seule son empreinte
SHA-256 est conservée. La limite d'une requête par seconde est appliquée par
une mise à jour atomique de `last_request_at`, partagée entre les workers
Gunicorn (contrairement au limiteur en mémoire utilisé pour la caisse).
"""

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update

from app.extensions import db
from app.models import (
    ApiToken,
    ApiTokenArticle,
    ApiTokenKeg,
    Article,
    Event,
    Keg,
    KegPrice,
    Tap,
)
from app.utils import CAMPUSSES, clamp_text, new_token, paris_to_utc, utcnow

TOKEN_PREFIX = "fz_"
TOKEN_PREFIX_LENGTH = 12
API_MAX_PRICE_CENTS = 10_000
API_MAX_BATCH = 200
API_RATE_LIMIT_SECONDS = 1


def bearer_token(header):
    """Valeur du jeton dans un en-tête Authorization: Bearer <jeton>."""
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def generate_token():
    """Nouveau jeton en clair, son empreinte et son préfixe d'affichage."""
    raw = TOKEN_PREFIX + new_token()
    return raw, hash_token(raw), raw[:TOKEN_PREFIX_LENGTH]


def hash_token(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def resolve_token(raw):
    if not raw:
        return None
    token = db.session.scalars(
        select(ApiToken).where(ApiToken.token_hash == hash_token(raw))
    ).first()
    if token is None or not token.is_active:
        return None
    return token


def enforce_rate_limit(token):
    """Une requête par seconde et par jeton, tous workers confondus.

    La ligne du jeton sert de verrou : la mise à jour ne réussit que si le
    dernier appel date d'au moins une seconde. Le commit est immédiat pour que
    la limite soit visible des autres workers.
    """
    now = utcnow()
    cutoff = now - timedelta(seconds=API_RATE_LIMIT_SECONDS)
    result = db.session.execute(
        update(ApiToken)
        .where(
            ApiToken.id == token.id,
            or_(ApiToken.last_request_at.is_(None), ApiToken.last_request_at <= cutoff),
        )
        .values(last_request_at=now, last_used_at=now)
        .execution_options(synchronize_session=False)
    )
    db.session.commit()
    return result.rowcount == 1


def iso_utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_api_datetime(value):
    """Date ISO 8601 d'une requête API -> datetime naïf UTC.

    Une date sans fuseau est interprétée comme UTC (la documentation le
    précise) ; une date avec décalage est convertie.
    """
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("Date invalide (format ISO 8601 attendu).") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def parse_form_datetime(value, label):
    """datetime-local (heure de Paris) d'un formulaire -> datetime naïf UTC."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError(f"{label} requise.")
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise ValueError(f"{label} invalide.") from exc
    return paris_to_utc(parsed)


def _parse_ids(values):
    ids = set()
    for value in values or []:
        raw = str(value).strip()
        if not raw or not raw.isascii() or not raw.isdigit() or len(raw) > 9:
            raise ValueError("Sélection d'articles ou de fûts invalide.")
        ids.add(int(raw))
    return ids


def selection_objects(campus, article_values, keg_values):
    """Valide la sélection d'un jeton et renvoie les objets correspondants."""
    if campus not in CAMPUSSES:
        raise ValueError("Campus invalide.")
    article_ids = _parse_ids(article_values)
    keg_ids = _parse_ids(keg_values)
    if len(article_ids) > 500 or len(keg_ids) > 100:
        raise ValueError("Sélection trop importante (500 articles et 100 fûts maximum).")

    articles = (
        db.session.scalars(select(Article).where(Article.id.in_(article_ids))).all()
        if article_ids
        else []
    )
    if {article.id for article in articles} != article_ids:
        raise ValueError("Un article sélectionné est introuvable.")
    for article in articles:
        if article.campus != campus:
            raise ValueError("Un article sélectionné appartient à l'autre campus.")
        if article.is_tap:
            raise ValueError("Les articles de tireuse se sélectionnent via leur fût.")
        if article.event_id is not None:
            event = db.session.get(Event, article.event_id)
            if event is None or event.campus != campus:
                raise ValueError("Un article d'événement sélectionné est incohérent.")

    kegs = db.session.scalars(select(Keg).where(Keg.id.in_(keg_ids))).all() if keg_ids else []
    if {keg.id for keg in kegs} != keg_ids:
        raise ValueError("Un fût sélectionné est introuvable.")
    if keg_ids:
        attached = set(
            db.session.scalars(
                select(Tap.keg_id).where(Tap.keg_id.in_(keg_ids), Tap.campus == campus)
            )
        )
        # Un fût détaché reste modifiable s'il a déjà un prix sur ce campus :
        # une correction de borne ne doit pas être bloquée pendant une soirée.
        priced = set(
            db.session.scalars(
                select(KegPrice.keg_id).where(
                    KegPrice.keg_id.in_(keg_ids), KegPrice.campus == campus
                )
            )
        )
        if (attached | priced) != keg_ids:
            raise ValueError("Un fût sélectionné n'est pas rattaché à ce campus.")

    return articles, kegs


def validate_token_fields(campus, label, expires_at, history_from, history_to, scopes):
    """Valide les champs communs à la création et à la modification."""
    if campus not in CAMPUSSES:
        raise ValueError("Campus invalide.")
    label = clamp_text((label or "").strip(), 160)
    if not label:
        raise ValueError("Le libellé du jeton est requis.")
    if not any(scopes):
        raise ValueError("Sélectionnez au moins une portée (lecture ou écriture).")
    now = utcnow()
    if expires_at is None or expires_at <= now:
        raise ValueError("La date d'expiration doit être dans le futur.")
    if history_from is None or history_to is None:
        raise ValueError("Les bornes de l'historique sont requises.")
    if history_from >= history_to:
        raise ValueError("La borne de début doit précéder la borne de fin.")
    if history_to > expires_at:
        raise ValueError("La fin de l'historique doit précéder l'expiration du jeton.")
    return label


def create_token(
    *,
    label,
    campus,
    expires_at,
    history_from,
    history_to,
    can_read_consumption,
    can_write_prices,
    can_write_team_prices,
    can_read_team_prices,
    articles,
    kegs,
    created_by=None,
):
    """Crée un jeton et sa sélection ; l'appelant commite et journalise."""
    label = validate_token_fields(
        campus,
        label,
        expires_at,
        history_from,
        history_to,
        (
            can_read_consumption,
            can_write_prices,
            can_write_team_prices,
            can_read_team_prices,
        ),
    )
    raw, digest, prefix = generate_token()
    token = ApiToken(
        label=label,
        token_hash=digest,
        token_prefix=prefix,
        campus=campus,
        can_read_consumption=can_read_consumption,
        can_write_prices=can_write_prices,
        can_write_team_prices=can_write_team_prices,
        can_read_team_prices=can_read_team_prices,
        history_from=history_from,
        history_to=history_to,
        expires_at=expires_at,
        created_by_id=created_by.id if created_by else None,
    )
    db.session.add(token)
    db.session.flush()
    for article in articles:
        token.article_links.append(ApiTokenArticle(article_id=article.id))
    for keg in kegs:
        token.keg_links.append(ApiTokenKeg(keg_id=keg.id))
    return token, raw


def update_token(
    token,
    *,
    label,
    expires_at,
    history_from,
    history_to,
    can_read_consumption,
    can_write_prices,
    can_write_team_prices,
    can_read_team_prices,
    articles,
    kegs,
):
    """Met à jour un jeton sans changer sa valeur (effet immédiat)."""
    if token.is_revoked:
        raise ValueError("Un jeton révoqué ne peut plus être modifié.")
    label = validate_token_fields(
        token.campus,
        label,
        expires_at,
        history_from,
        history_to,
        (
            can_read_consumption,
            can_write_prices,
            can_write_team_prices,
            can_read_team_prices,
        ),
    )
    token.label = label
    token.expires_at = expires_at
    token.history_from = history_from
    token.history_to = history_to
    token.can_read_consumption = can_read_consumption
    token.can_write_prices = can_write_prices
    token.can_write_team_prices = can_write_team_prices
    token.can_read_team_prices = can_read_team_prices

    # Mise à jour en place : supprimer puis recréer les mêmes lignes dans le
    # même flush violerait la contrainte d'unicité (INSERT avant DELETE).
    wanted_articles = {article.id for article in articles}
    for link in list(token.article_links):
        if link.article_id not in wanted_articles:
            token.article_links.remove(link)
    current_articles = {link.article_id for link in token.article_links}
    for article_id in wanted_articles - current_articles:
        token.article_links.append(ApiTokenArticle(article_id=article_id))

    wanted_kegs = {keg.id for keg in kegs}
    for link in list(token.keg_links):
        if link.keg_id not in wanted_kegs:
            token.keg_links.remove(link)
    current_kegs = {link.keg_id for link in token.keg_links}
    for keg_id in wanted_kegs - current_kegs:
        token.keg_links.append(ApiTokenKeg(keg_id=keg_id))
    return token


def selected_article_ids(token):
    """Articles lisibles/modifiables : sélection directe + trois tailles des fûts."""
    direct = {
        link.article_id
        for link in token.article_links
        if link.article is not None and link.article.campus == token.campus
    }
    keg_ids = {link.keg_id for link in token.keg_links}
    taps = set()
    if keg_ids:
        taps = set(
            db.session.scalars(
                select(Article.id).where(
                    Article.keg_id.in_(keg_ids),
                    Article.is_tap.is_(True),
                    Article.campus == token.campus,
                )
            )
        )
    return direct | taps


def selected_keg_ids(token):
    return {link.keg_id for link in token.keg_links}
