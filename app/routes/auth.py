import hashlib
import hmac
import re
import threading
import time
from collections import defaultdict, deque

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from sqlalchemy import select
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models import LoginLog, User
from app.utils import CAMPUSSES, client_ip, is_safe_target, slug_username, utcnow

bp = Blueprint("auth", __name__)
_limiter_lock = threading.Lock()
_attempts = defaultdict(deque)

# Limiteur en mémoire : 8 tentatives par fenêtre glissante de 5 minutes.
# La purge régulière et la borne du nombre de clés empêchent une croissance
# illimitée de la mémoire (les clés proviennent d'IP, jamais de X-Forwarded-For).
_RATE_WINDOW_SECONDS = 300
_RATE_MAX_ATTEMPTS = 8
_RATE_MAX_KEYS = 10_000
_RATE_SWEEP_INTERVAL = 60
_last_sweep = 0.0

# Purge du registre des connexions : déclenchée à chaque tentative de
# connexion mais au plus une fois par heure (les échecs aussi déclenchent la
# purge, sinon les logs IP s'accumulent tant qu'aucune connexion ne réussit).
_LOG_CLEANUP_INTERVAL = 3600
# -interval : la première tentative déclenche toujours la purge, même si le
# serveur vient de démarrer (monotonic() peut être inférieur à l'intervalle).
_last_log_cleanup = -_LOG_CLEANUP_INTERVAL

# Hash bcrypt hérité de l'ancienne plateforme (PHP password_hash) : $2a$, $2b$, $2y$
_BCRYPT_HASH_RE = re.compile(r"^\$2[aby]\$\d{2}\$")


def _check_legacy_password(stored, password):
    """Vérifie un mot de passe migré (users.legacy_password, valeur brute source).

    Formats gérés : hash bcrypt, empreinte hexadécimale md5 (32) / sha1 (40) /
    sha256 (64), sinon comparaison directe (texte brut). True -> l'appelant
    convertit le compte au format werkzeug et vide legacy_password.
    """
    if not stored or password is None:
        return False
    if _BCRYPT_HASH_RE.match(stored):
        try:
            import bcrypt

            return bcrypt.checkpw(password.encode(), stored.encode())
        except (ImportError, ValueError):
            return False
    hex_digests = {32: "md5", 40: "sha1", 64: "sha256"}
    if len(stored) in hex_digests:
        digest = hashlib.new(hex_digests[len(stored)], password.encode()).hexdigest()
        return hmac.compare_digest(digest.encode(), stored.lower().encode())
    return hmac.compare_digest(stored.encode(), password.encode())


def _sweep_attempts(now):
    """Purge les fenêtres expirées et borne le nombre de clés suivies."""
    global _last_sweep
    cutoff = now - _RATE_WINDOW_SECONDS
    expensive = len(_attempts) > _RATE_MAX_KEYS
    if not expensive and now - _last_sweep < _RATE_SWEEP_INTERVAL:
        return
    _last_sweep = now
    for key in [k for k, q in _attempts.items() if not q or q[-1] <= cutoff]:
        del _attempts[key]
    if len(_attempts) > _RATE_MAX_KEYS:
        # éviction des entrées les moins récemment actives
        excess = len(_attempts) - _RATE_MAX_KEYS
        for key in sorted(_attempts, key=lambda k: _attempts[k][-1])[:excess]:
            del _attempts[key]


def _rate_limited(key):
    now = time.time()
    with _limiter_lock:
        _sweep_attempts(now)
        q = _attempts[key]
        while q and now - q[0] > _RATE_WINDOW_SECONDS:
            q.popleft()
        if len(q) >= _RATE_MAX_ATTEMPTS:
            return True
        q.append(now)
        return False


@bp.route("/connexion", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = client_ip()
        if _rate_limited(ip):
            flash("Trop de tentatives. Réessayez dans quelques minutes.", "danger")
            return render_template("auth/login.html"), 429
        identifiant = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        campus = request.form.get("campus")
        if campus not in CAMPUSSES:
            campus = "brest"

        # Le login ne reconnaît que l'identifiant prenom.nom : la saisie est
        # normalisée comme à l'import (casse, accents et séparateurs tolérés).
        slug = slug_username(identifiant)
        user = db.session.scalars(
            select(User).where(User.username == slug)
        ).first() if slug else None
        ok = False
        if user and user.password_hash:
            ok = check_password_hash(user.password_hash, password)
        if not ok and user and user.legacy_password:
            ok = _check_legacy_password(user.legacy_password, password)
            if ok:
                # Conversion au format cible : le mot de passe hérité ne sert plus.
                user.password_hash = generate_password_hash(password)
                user.legacy_password = None
                current_app.logger.info(
                    "Mot de passe legacy converti pour %s (compte %s).",
                    user.display_name, user.id,
                )
        if ok and user and user.disabled:
            ok = False
            reason = "Accès refusé : compte désactivé."
        elif ok and user and (not user.is_team or user.blacklist):
            ok = False
            reason = "Accès refusé : compte blacklisté." if user.blacklist else "Accès réservé aux membres de l'équipe."
        else:
            reason = "Identifiants incorrects."

        db.session.add(LoginLog(
            user_id=user.id if user else None,
            name=identifiant[:120],  # LoginLog.name = VARCHAR(120)
            campus=campus,
            ip=ip,
            success=bool(ok),
        ))
        db.session.commit()
        _cleanup_old_logs()

        if not ok:
            flash(reason, "danger")
            return render_template("auth/login.html", campus=campus), 401

        session.clear()
        session["user_id"] = user.id
        session["campus"] = campus
        session["last_activity"] = time.time()
        session.permanent = True
        if user.blacklist_alcohol:
            flash("Rappel : ce compte porte le statut « blacklist alcool ».", "warning")

        target = request.args.get("next")
        return redirect(target if is_safe_target(target) else url_for("team.payment"))

    if request.args.get("expired"):
        flash("Session expirée pour inactivité, reconnectez-vous.", "warning")
    return render_template("auth/login.html", campus=session.get("campus", "brest"))


def _cleanup_old_logs():
    global _last_log_cleanup
    now = time.monotonic()
    if now - _last_log_cleanup < _LOG_CLEANUP_INTERVAL:
        return
    _last_log_cleanup = now

    from datetime import timedelta

    from app.services.settings import int_setting

    days = int_setting("login_logs_retention_days")
    if days <= 0:
        return
    db.session.query(LoginLog).filter(LoginLog.created_at < utcnow() - timedelta(days=days)).delete()
    db.session.commit()


@bp.route("/deconnexion", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("public.home"))
