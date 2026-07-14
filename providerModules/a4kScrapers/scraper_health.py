# -*- coding: utf-8 -*-
"""
Per-scraper video-level health tracker (added v2.99.63).

Purpose
-------
The per-domain circuit breaker in request.py stops individual HTTP retries
fast, but it resets after 10 minutes and has no memory across videos or
Kodi restarts. This module adds a *video-level* layer on top: if a scraper
suffers HTTP errors on 3 consecutive video scrapes it is blacklisted for
3 days, completely skipping it until the site recovers.

"HTTP error" definition (deliberately narrow)
---------------------------------------------
A video scrape counts as an HTTP failure ONLY if the circuit breaker is
open for the scraper's primary domain at the end of the scrape — meaning
at least 3 HTTP-level errors occurred against that domain (502, timeout,
connection refused, etc.).  Content misses (site is reachable but returned
no matching torrents) do NOT count as failures, so scrapers like SubsPlease
that legitimately return 0 results for older anime are never penalised.

Persistence
-----------
Health state is stored via database.cache_insert / cache_get, the same
mechanism used by urls.py for domain priority storage.  Data survives
Kodi restarts.  Key format: "a4k_health_<scraper_name>".

Configuration
-------------
_FAILURE_THRESHOLD  = 3  consecutive video HTTP failures before blacklist
_BLACKLIST_DAYS     = 3  days the blacklist lasts once triggered
"""

import json
import time
import threading

from .utils import database
from .source_utils import tools

_FAILURE_THRESHOLD = 3
_BLACKLIST_SECONDS = 3 * 24 * 3600   # 3 days

_lock = threading.Lock()
_MEM  = {}   # in-process cache: scraper → {"fails": N, "until": float}


# ── Internal helpers ──────────────────────────────────────────────────────

def _key(name):
    return 'a4k_health_%s' % name


def _load(name):
    """Load state from in-process cache, falling back to DB."""
    with _lock:
        if name in _MEM:
            return dict(_MEM[name])

    try:
        raw = database.cache_get(_key(name))
        if raw and isinstance(raw, dict) and 'fails' in raw:
            state = {'fails': int(raw.get('fails', 0)),
                     'until': float(raw.get('until', 0))}
        elif raw and isinstance(raw, str):
            parsed = json.loads(raw)
            state = {'fails': int(parsed.get('fails', 0)),
                     'until': float(parsed.get('until', 0))}
        else:
            state = {'fails': 0, 'until': 0.0}
    except Exception:
        state = {'fails': 0, 'until': 0.0}

    with _lock:
        _MEM[name] = state
    return dict(state)


def _save(name, state):
    """Persist state to in-process cache and DB."""
    with _lock:
        _MEM[name] = dict(state)
    try:
        database.cache_insert(_key(name), json.dumps(state))
    except Exception:
        pass


# ── Public API ────────────────────────────────────────────────────────────

def is_blacklisted(name):
    """Return True if this scraper is currently in a 3-day blacklist period.

    If the blacklist period has expired, clears it automatically so the
    scraper gets a fresh start the next time it's called.
    """
    state = _load(name)
    until = state.get('until', 0)
    if until and time.time() < until:
        remaining_h = (until - time.time()) / 3600
        tools.log(
            'scraper_health: %s is blacklisted for %.1fh more, skipping' % (
                name, remaining_h),
            'notice')
        return True
    if until and time.time() >= until:
        # Expired — reset fully so the scraper gets a clean slate
        _save(name, {'fails': 0, 'until': 0.0})
        tools.log('scraper_health: %s blacklist expired, reset' % name, 'notice')
    return False


def record(name, had_http_error):
    """Record the outcome of one video scrape for this scraper.

    Parameters
    ----------
    name           : scraper name, e.g. 'anidex'
    had_http_error : True  → HTTP-level failure (circuit breaker was open)
                     False → scraper responded normally (even if 0 results)
    """
    state = _load(name)

    if not had_http_error:
        # Success: reset consecutive failure counter
        if state['fails'] > 0:
            tools.log('scraper_health: %s recovered, resetting fail count' % name,
                      'notice')
            _save(name, {'fails': 0, 'until': 0.0})
        return

    # HTTP failure
    state['fails'] = state.get('fails', 0) + 1
    tools.log(
        'scraper_health: %s HTTP failure %d/%d' % (
            name, state['fails'], _FAILURE_THRESHOLD),
        'notice')

    if state['fails'] >= _FAILURE_THRESHOLD:
        until = time.time() + _BLACKLIST_SECONDS
        state['until'] = until
        tools.log(
            'scraper_health: %s blacklisted for %d days (until %s)' % (
                name, _BLACKLIST_SECONDS // 86400,
                time.strftime('%Y-%m-%d %H:%M', time.localtime(until))),
            'notice')

    _save(name, state)


def reset(name):
    """Manually clear the health record for a scraper (e.g. from settings)."""
    _save(name, {'fails': 0, 'until': 0.0})
    tools.log('scraper_health: %s manually reset' % name, 'notice')
