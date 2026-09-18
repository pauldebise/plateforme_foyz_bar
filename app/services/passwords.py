"""Politique de mot de passe (P10-5).

Longueur minimale, diversité des caractères, refus des mots de passe les plus
courants et des mots de passe contenant l'identifiant ou le nom du compte.
Utilisée par la gestion d'équipe (politique allégée : 8 caractères, sans
diversité imposée) et par le mot de passe administrateur (politique complète
par défaut) ; la connexion reste inchangée (les mots de passe existants
continuent de fonctionner).
"""

import re

MIN_LENGTH = 12
TEAM_MIN_LENGTH = 8

# Les plus courants (fuites publiques) en français et en anglais. Liste
# volontairement courte : elle bloque l'évident sans imposer de dépendance.
COMMON_PASSWORDS = {
    "123456",
    "123456789",
    "1234567890",
    "123456789012",
    "azerty",
    "azertyuiop",
    "adminadmin",
    "administrateur",
    "anniversaire",
    "bonjour",
    "chocolate",
    "foyzbar",
    "football",
    "hellohello",
    "iloveyou",
    "liverpool",
    "loulou",
    "motdepasse",
    "motdepasse123",
    "motdepasse1234",
    "nintendoo",
    "password",
    "password123",
    "password1234",
    "princesse",
    "qwerty",
    "qwerty123",
    "soleil",
    "superman",
    "supporter",
    "tartiflette",
    "welcome",
    "welcome123",
}


def _classes(password):
    return sum(
        (
            any(ch.islower() for ch in password),
            any(ch.isupper() for ch in password),
            any(ch.isdigit() for ch in password),
            any(not ch.isalnum() for ch in password),
        )
    )


def validate(password, username="", name="", min_length=MIN_LENGTH, require_diversity=True):
    """Retourne un message d'erreur en français, ou None si acceptable."""
    password = password or ""
    if len(password) < min_length:
        return f"Mot de passe trop court ({min_length} caractères minimum)."
    if password.lower() in COMMON_PASSWORDS:
        return "Mot de passe trop courant : choisissez une phrase difficile à deviner."
    if require_diversity and _classes(password) < 3:
        return (
            "Mot de passe trop simple : mélangez majuscules, minuscules, chiffres "
            "et caractères spéciaux."
        )
    lowered = password.lower()
    tokens = {username or ""}
    tokens.update(re.split(r"[^a-z0-9]+", (name or "").lower()))
    for token in tokens:
        token = token.strip()
        if len(token) >= 4 and token in lowered:
            return "Le mot de passe ne doit pas contenir l'identifiant ou le nom."
    return None
