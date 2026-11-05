"""Tests du projet (package pour imports relatifs)."""

import os

# Les suites historiques couvrent le comportement bi-campus. Le mode
# mono-campus est testé explicitement par tests.test_single_campus, qui écrase
# cette valeur après l'import du package mais avant celui de `app`. La valeur
# est imposée (et non pas seulement définie par défaut) pour qu'une variable
# `FOYZ_ACTIVE_CAMPUSES=brest` présente dans le shell ne fasse pas échouer les
# suites bi-campus.
os.environ["FOYZ_ACTIVE_CAMPUSES"] = "brest,paris"

# Les tests ne mesurent pas la robustesse du KDF : scrypt est volontairement
# lent et représentait une part importante du temps de la suite (chaque compte
# de test et chaque tentative de connexion en calcule un). Le format Werkzeug
# reste le même, mais les comptes de test utilisent un PBKDF2 léger.
# FOYZ_TEST_REAL_HASH=1 rétablit le scrypt réel, par exemple pour un test dédié
# au hachage.
if os.environ.get("FOYZ_TEST_REAL_HASH") != "1":
    import werkzeug.security

    _real_generate_password_hash = werkzeug.security.generate_password_hash
    _test_password_method = os.environ.get("FOYZ_TEST_PASSWORD_METHOD", "pbkdf2:sha256:1000")

    def _fast_password_hash(password, method=_test_password_method, salt_length=16):
        return _real_generate_password_hash(password, method=method, salt_length=salt_length)

    werkzeug.security.generate_password_hash = _fast_password_hash
