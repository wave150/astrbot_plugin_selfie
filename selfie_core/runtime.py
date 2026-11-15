"""Injectable runtime boundaries with legacy standard-library defaults.

SystemClock uses the host's local timezone, exactly like time. No timezone
configuration, TZ mutation or timezone conversion is implemented here. Inject a
time-compatible source for a simulated clock; implement all methods used by the
consumer, including localtime()/strftime() without explicit timestamps. A fake
source owns those no-argument semantics; this adapter never consults real time
on its behalf. Errors from sources propagate unchanged.

RandomSource defaults to the *module* random, preserving random.seed() and the
existing shared random sequence. Construction neither seeds nor consumes it.
Inject random.Random(seed) for an isolated reproducible stream or a fake object
with the methods your consumer uses. These are simulation PRNGs, not security
randomness. Neither adapter eagerly validates or calls an injected source.
"""
import random as _random
import time as _time


class SystemClock:
    """Small time-compatible facade; optional source is duck-typed."""

    def __init__(self, source=None):
        self._source = _time if source is None else source

    def time(self):
        return self._source.time()

    def monotonic(self):
        return self._source.monotonic()

    def localtime(self, seconds=None):
        if seconds is None:
            return self._source.localtime()
        return self._source.localtime(seconds)

    def gmtime(self, seconds=None):
        if seconds is None:
            return self._source.gmtime()
        return self._source.gmtime(seconds)

    def strftime(self, format, t=None):
        if t is None:
            return self._source.strftime(format)
        return self._source.strftime(format, t)

    def strptime(self, text, format="%a %b %d %H:%M:%S %Y"):
        return self._source.strptime(text, format)

    def mktime(self, t):
        return self._source.mktime(t)


class RandomSource:
    """Random-compatible facade; omit source to preserve global RNG behavior."""

    def __init__(self, source=None):
        self._source = _random if source is None else source

    def choice(self, seq):
        return self._source.choice(seq)

    def random(self):
        return self._source.random()

    def uniform(self, a, b):
        return self._source.uniform(a, b)

    def randint(self, a, b):
        return self._source.randint(a, b)
