import hashlib
import hmac
import re
import threading
import time
from collections import defaultdict, deque

from flask import (
    Blueprint,
    current_app,
    flash,
    g,
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
from app.services import passwords, totp
from app.utils import CAMPUSSES, client_ip, is_safe_target, login_required, slug_username, utcnow

bp = Blueprint("auth", __name__)

# Délai laissé pour saisir le code MFA après un mot de passe valide.
_MFA_TIMEOUT_SECONDS = 300
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
        user = (
            db.session.scalars(select(User).where(User.username == slug)).first() if slug else None
        )
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
                    user.display_name,
                    user.id,
                )
        if ok and user and user.disabled:
            ok = False
            reason = "Accès refusé : compte désactivé."
        elif ok and user and (not user.is_team or user.blacklist):
            ok = False
            reason = (
                "Accès refusé : compte blacklisté."
                if user.blacklist
                else "Accès réservé aux membres de l'équipe."
            )
        else:
            reason = "Identifiants incorrects."

        if ok and user and user.totp_enabled:
            # Mot de passe valide : second facteur requis avant d'ouvrir la
            # session. L'état « en attente » est volontairement minimal.
            session.clear()
            session["mfa_user_id"] = user.id
            session["mfa_campus"] = campus
            session["mfa_since"] = time.time()
            target = request.args.get("next")
            if is_safe_target(target):
                session["mfa_next"] = target
            return redirect(url_for("auth.mfa"))

        db.session.add(
            LoginLog(
                user_id=user.id if user else None,
                name=identifiant[:120],  # LoginLog.name = VARCHAR(120)
                campus=campus,
                ip=ip,
                success=bool(ok),
            )
        )
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


def _complete_login(user, campus):
    """Ouvre la session applicative après authentification complète."""
    session.clear()
    session["user_id"] = user.id
    session["campus"] = campus if campus in CAMPUSSES else "brest"
    session["last_activity"] = time.time()
    session.permanent = True


@bp.route("/connexion/verification", methods=["GET", "POST"])
def mfa():
    """Second facteur (TOTP ou code de secours) après mot de passe valide."""
    user = db.session.get(User, session.get("mfa_user_id") or 0)
    if user is None or not user.totp_enabled:
        session.clear()
        return redirect(url_for("auth.login"))
    if time.time() - session.get("mfa_since", time.time()) > _MFA_TIMEOUT_SECONDS:
        session.clear()
        flash("Vérification expirée, reconnectez-vous.", "warning")
        return redirect(url_for("auth.login", expired=1))

    if request.method == "POST":
        ip = client_ip()
        if _rate_limited(f"mfa:{ip}"):
            flash("Trop de tentatives. Réessayez dans quelques minutes.", "danger")
            return render_template("auth/mfa.html"), 429
        submitted_code = (request.form.get("code") or "").strip()
        submitted_recovery = (request.form.get("recovery_code") or "").strip()
        counter = None
        used_recovery = False
        if submitted_recovery:
            remaining, used_recovery = totp.consume_recovery_code(
                user.totp_recovery, submitted_recovery
            )
            if used_recovery:
                user.totp_recovery = remaining
        else:
            counter = totp.verify(
                user.totp_secret, submitted_code, last_counter=user.totp_last_counter
            )
        campus = session.get("mfa_campus", "brest")
        name = (user.username or "")[:120]
        if counter is None and not used_recovery:
            db.session.add(
                LoginLog(user_id=user.id, name=name, campus=campus, ip=ip, success=False)
            )
            db.session.commit()
            flash("Code invalide.", "danger")
            return render_template("auth/mfa.html"), 401
        if counter is not None:
            user.totp_last_counter = counter
        db.session.add(LoginLog(user_id=user.id, name=name, campus=campus, ip=ip, success=True))
        db.session.commit()
        target = session.get("mfa_next")
        _complete_login(user, campus)
        _cleanup_old_logs()
        if used_recovery:
            remaining = totp.remaining_recovery_codes(user.totp_recovery)
            flash(
                f"Code de secours utilisé : {remaining} restant(s). Régénérez-en dès que possible.",
                "warning",
            )
        if user.blacklist_alcohol:
            flash("Rappel : ce compte porte le statut « blacklist alcool ».", "warning")
        return redirect(target if is_safe_target(target) else url_for("team.payment"))
    return render_template("auth/mfa.html")


@bp.route("/compte/securite", methods=["GET", "POST"])
@login_required
def securite():
    """Sécurité du compte : MFA TOTP, codes de secours, mot de passe."""
    from app.services import audit as A

    user = g.current_user
    reveal_secret = None
    recovery_codes = None
    if request.method == "POST":
        action = request.form.get("action", "")
        submitted_password = request.form.get("password", "")
        code_value = (request.form.get("code") or "").strip()
        password_ok = bool(submitted_password) and check_password_hash(
            user.password_hash or "", submitted_password
        )
        if action == "start":
            if not password_ok:
                flash("Mot de passe incorrect.", "danger")
            else:
                if not user.totp_secret:
                    user.totp_secret = totp.generate_secret()
                user.totp_enabled = False
                user.totp_last_counter = None
                db.session.commit()
                reveal_secret = user.totp_secret
                flash(
                    "Saisissez le secret dans votre application d'authentification, "
                    "puis validez avec le code affiché.",
                    "info",
                )
        elif action == "confirm":
            counter = totp.verify(user.totp_secret, code_value) if user.totp_secret else None
            if counter is None:
                flash("Code invalide : réessayez (vérifiez l'heure du téléphone).", "danger")
                reveal_secret = user.totp_secret
            else:
                recovery_codes = totp.generate_recovery_codes()
                user.totp_enabled = True
                user.totp_last_counter = counter
                user.totp_recovery = totp.hash_recovery_codes(recovery_codes)
                A.record("compte.mfa_activation", target=user.username)
                db.session.commit()
                flash(
                    "MFA activé : notez les codes de secours ci-dessous, ils ne seront "
                    "plus affichés.",
                    "success",
                )
        elif action == "disable":
            counter = (
                totp.verify(user.totp_secret, code_value, last_counter=user.totp_last_counter)
                if user.totp_secret
                else None
            )
            if not password_ok:
                flash("Mot de passe incorrect.", "danger")
            elif counter is None:
                flash("Code d'authentification invalide.", "danger")
            else:
                user.totp_secret = None
                user.totp_enabled = False
                user.totp_recovery = None
                user.totp_last_counter = None
                A.record("compte.mfa_desactivation", target=user.username)
                db.session.commit()
                flash("MFA désactivé.", "success")
        elif action == "regenerate":
            counter = (
                totp.verify(user.totp_secret, code_value, last_counter=user.totp_last_counter)
                if user.totp_secret
                else None
            )
            if not password_ok:
                flash("Mot de passe incorrect.", "danger")
            elif counter is None:
                flash("Code d'authentification invalide.", "danger")
            else:
                recovery_codes = totp.generate_recovery_codes()
                user.totp_recovery = totp.hash_recovery_codes(recovery_codes)
                user.totp_last_counter = counter
                A.record("compte.mfa_codes", target=user.username)
                db.session.commit()
                flash("Nouveaux codes de secours : les anciens ne fonctionnent plus.", "success")
        elif action == "password":
            problem = passwords.validate(
                request.form.get("new_password", ""),
                username=user.username or "",
                name=user.name or "",
            )
            if not password_ok:
                flash("Mot de passe actuel incorrect.", "danger")
            elif problem:
                flash(problem, "danger")
            else:
                user.password_hash = generate_password_hash(request.form.get("new_password", ""))
                A.record("compte.mot_de_passe", target=user.username)
                db.session.commit()
                flash("Mot de passe modifié.", "success")
    return render_template(
        "auth/securite.html",
        u=user,
        reveal_secret=reveal_secret,
        recovery_codes=recovery_codes,
        provisioning_uri=(
            totp.provisioning_uri(user.totp_secret, user.username)
            if user.totp_secret and not user.totp_enabled
            else None
        ),
        recovery_remaining=totp.remaining_recovery_codes(user.totp_recovery),
    )


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
    db.session.query(LoginLog).filter(
        LoginLog.created_at < utcnow() - timedelta(days=days)
    ).delete()
    db.session.commit()


@bp.route("/deconnexion", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("public.home"))
