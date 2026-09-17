"""Aides d'affichage console du rapport d'audit."""


def line(text=""):
    print(text)


def section(title):
    print()
    print(f"=== {title} " + "=" * max(0, 66 - len(title)))


def kv(label, value):
    print(f"  {label:<44} {value}")


def money(label, cents):
    from .util import fmt_euros

    kv(label, fmt_euros(cents))
