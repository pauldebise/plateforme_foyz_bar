import hashlib
import hmac
import re
import time
from math import ceil

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import select
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models import LoginLog, User
from app.services import loginthrottle as LT
from app.utils import CAMPUSSES, client_ip, is_safe_target, slug_username

bp = Blueprint("auth", __name__)

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


_dummy_hash = None


def _burn_password_check(password):
    """Calcule un hash pour un identifiant inconnu : le temps de réponse ne
    révèle pas si le compte existe."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = generate_password_hash("identifiant-inconnu")
    check_password_hash(_dummy_hash, password)


def reset_throttle(app):
    """Efface les compteurs d'échecs (tests, déblocage manuel)."""
    with app.app_context():
        LT.reset_all()


def _wait_message(seconds):
    if seconds < 60:
        return f"Trop de tentatives. Réessayez dans {seconds} seconde(s)."
    return f"Trop de tentatives. Réessayez dans {ceil(seconds / 60)} minute(s)."


@bp.route("/connexion", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = client_ip()
        identifiant = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        campus = request.form.get("campus")
        if campus not in CAMPUSSES:
            campus = "brest"

        # Le login ne reconnaît que l'identifiant prenom.nom : la saisie est
        # normalisée comme à l'import (casse, accents et séparateurs tolérés).
        slug = slug_username(identifiant)
        acct_key, addr_key = LT.account_key(slug), LT.ip_key(ip)
        wait = LT.remaining_seconds(acct_key, addr_key)
        if wait:
            current_app.logger.warning(
                "Connexion différée (%s s) pour %s depuis %s", wait, acct_key, ip
            )
            flash(_wait_message(wait), "danger")
            response = render_template("auth/login.html", campus=campus)
            return response, 429
        user = (
            db.session.scalars(select(User).where(User.username == slug)).first() if slug else None
        )
        ok = False
        if user and user.password_hash:
            ok = check_password_hash(user.password_hash, password)
        elif not user:
            _burn_password_check(password)
        if not ok and user and user.legacy_password:
            ok = _check_legacy_password(user.legacy_password, password)
            if ok:
                # Conversion au format cible : le mot de passe hérité ne sert plus.
                user.password_hash = generate_password_hash(password)
                user.legacy_password = None
                current_app.logger.info(
                    "Mot de passe legacy converti pour %s (compte %s).",
                    user.display_name,
                    user.id,
                )
        password_ok = bool(ok)
        if ok and user and user.disabled:
            ok = False
            reason = "Accès refusé : compte désactivé."
        elif ok and user and (not (user.is_team or user.is_super_admin) or user.blacklist):
            ok = False
            reason = (
                "Accès refusé : compte blacklisté."
                if user.blacklist
                else "Accès réservé aux membres de l'équipe."
            )
        else:
            reason = "Identifiants incorrects."

        db.session.add(
            LoginLog(
                user_id=user.id if user else None,
                # Jamais la saisie brute (un mot de passe tapé dans le mauvais
                # champ s'y retrouverait) : identifiant du compte, sinon empreinte.
                name=user.username if user else f"inconnu:{LT.fingerprint(slug)[:8]}",
                campus=campus,
                ip=ip,
                success=bool(ok),
            )
        )
        db.session.commit()
        if password_ok:
            LT.reset(acct_key)
        else:
            LT.record_failure(acct_key, addr_key)

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


@bp.route("/deconnexion", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("public.home"))
