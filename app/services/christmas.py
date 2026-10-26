"""Décor de Noël annuel : Père Noël et flocons sur les pages du site.

Les dates sont mémorisées au format « MM-JJ », sans année : le décor revient
chaque année à la même période. Une fenêtre à cheval sur le Nouvel An est
acceptée (par exemple du 20 décembre au 5 janvier).
"""

from datetime import date, datetime

from app.services.settings import get_setting
from app.utils import PARIS_TZ


def _month_day(value):
    """« MM-JJ » valide -> (mois, jour), sinon None."""
    try:
        month, day = (int(part) for part in str(value).split("-", 1))
        date(2000, month, day)
    except (TypeError, ValueError):
        return None
    return month, day


def parse_form_date(value):
    """Saisie ISO du sélecteur de date -> « MM-JJ », ou None si invalide."""
    text = str(value or "").strip()
    if len(text) != 10:
        return None
    try:
        parsed = date.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return f"{parsed.month:02d}-{parsed.day:02d}"


def is_active(today=None):
    """Vrai si la date (Paris par défaut) tombe dans la fenêtre configurée."""
    start = _month_day(get_setting("christmas_deco_start"))
    end = _month_day(get_setting("christmas_deco_end"))
    if start is None or end is None:
        return False
    current = today or datetime.now(PARIS_TZ).date()
    current = (current.month, current.day)
    if start <= end:
        return start <= current <= end
    # Fenêtre à cheval sur le Nouvel An : du 20/12 au 05/01 par exemple.
    return current >= start or current <= end


def date_input_values(start, end, year=None):
    """Valeurs ISO des deux champs du module dev (l'année est ignorée)."""
    start_pair = _month_day(start)
    end_pair = _month_day(end)
    if start_pair is None or end_pair is None:
        return "", ""
    year = year or datetime.now(PARIS_TZ).year
    end_year = year + 1 if end_pair < start_pair else year
    try:
        return (
            date(year, *start_pair).isoformat(),
            date(end_year, *end_pair).isoformat(),
        )
    except ValueError:
        # 29 février d'une année non bissextile : année de référence bissextile.
        return date(2000, *start_pair).isoformat(), date(2000, *end_pair).isoformat()
