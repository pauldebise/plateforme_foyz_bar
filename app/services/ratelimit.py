import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    """Limiteur en mémoire à fenêtre glissante, borné en nombre de clés.

    La purge périodique et l'éviction des clés les moins récentes empêchent
    une croissance illimitée de la mémoire quand la clé provient d'un client
    (IP, jeton de passerelle). Chaque worker Gunicorn possède son propre
    compteur : la limite effective est donc multipliée par le nombre de
    workers, ce qui reste suffisant pour freiner un abus applicatif.
    """

    def __init__(self, window_seconds, max_requests, max_keys=10_000, sweep_interval=60):
        self.window_seconds = window_seconds
        self.max_requests = max_requests
        self.max_keys = max_keys
        self.sweep_interval = sweep_interval
        self._lock = threading.Lock()
        self._hits = defaultdict(deque)
        self._last_sweep = 0.0

    def _sweep(self, now):
        cutoff = now - self.window_seconds
        expensive = len(self._hits) > self.max_keys
        if not expensive and now - self._last_sweep < self.sweep_interval:
            return
        self._last_sweep = now
        for key in [k for k, q in self._hits.items() if not q or q[-1] <= cutoff]:
            del self._hits[key]
        if len(self._hits) > self.max_keys:
            excess = len(self._hits) - self.max_keys
            for key in sorted(self._hits, key=lambda k: self._hits[k][-1])[:excess]:
                del self._hits[key]

    def limited(self, key):
        now = time.time()
        with self._lock:
            self._sweep(now)
            hits = self._hits[key]
            while hits and now - hits[0] > self.window_seconds:
                hits.popleft()
            if len(hits) >= self.max_requests:
                return True
            hits.append(now)
            return False
