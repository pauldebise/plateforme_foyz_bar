import threading
import time
from collections import defaultdict, deque

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from sqlalchemy import select
from werkzeug.security import check_password_hash

from app.extensions import db
from app.models import LoginLog, User
from app.utils import CAMPUSSES, is_safe_target, utcnow

bp = Blueprint("auth", __name__)
_limiter_lock = threading.Lock()
_attempts = defaultdict(deque)


def _rate_limited(ip):
    now = time.time()
    with _limiter_lock:
        q = _attempts[ip]
        while q and now - q[0] > 300:
            q.popleft()
        if len(q) >= 8:
            return True
        q.append(now)
        return False


@bp.route("/connexion", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?")
        if _rate_limited(ip):
            flash("Trop de tentatives. Réessayez dans quelques minutes.", "danger")
            return render_template("auth/login.html"), 429
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        campus = request.form.get("campus")
        if campus not in CAMPUSSES:
            campus = "brest"

        user = db.session.scalars(select(User).where(User.username == username)).first()
        ok = False
        if user and user.password_hash:
            ok = check_password_hash(user.password_hash, password)
        if ok and user and (not user.is_team or user.blacklist):
            ok = False
            reason = "Accès refusé : compte blacklisté." if user.blacklist else "Accès réservé aux membres de l'équipe."
        else:
            reason = "Identifiants incorrects."

        db.session.add(LoginLog(user_id=user.id if user else None, username=username, campus=campus, ip=ip, success=bool(ok)))
        db.session.commit()

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

        _cleanup_old_logs()
        target = request.args.get("next")
        return redirect(target if is_safe_target(target) else url_for("team.payment"))

    if request.args.get("expired"):
        flash("Session expirée pour inactivité, reconnectez-vous.", "warning")
    return render_template("auth/login.html", campus=session.get("campus", "brest"))


def _cleanup_old_logs():
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
