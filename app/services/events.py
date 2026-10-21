"""Événements en cours, fenêtre commune à la passerelle et à la caisse."""

from sqlalchemy import select

from app.extensions import db
from app.models import Event
from app.models.event import GATEWAY_MARGIN, GATEWAY_MAX_DURATION
from app.utils import utcnow


def running_events(campus):
    """Événements accessibles sur un campus, avec la même fenêtre que la
    passerelle : de 12 h avant le début jusqu'à la fin (plafonnée à 72 h)."""
    now = utcnow()
    return list(
        db.session.scalars(
            select(Event)
            .where(
                Event.campus == campus,
                Event.closed.is_(False),
                Event.starts_at <= now + GATEWAY_MARGIN,
                Event.ends_at >= now,
                Event.starts_at >= now - GATEWAY_MAX_DURATION,
            )
            .order_by(Event.starts_at, Event.id)
        )
    )


def running_event_ids(campus):
    return {event.id for event in running_events(campus)}
