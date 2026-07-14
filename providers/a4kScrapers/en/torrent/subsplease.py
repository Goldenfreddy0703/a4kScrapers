# -*- coding: utf-8 -*-
"""
SubsPlease scraper — JSON API against subsplease.org.

SubsPlease is the de-facto default for current-season simulcasts. Their
public search API returns 720p/1080p magnet links per episode in clean JSON.
Older anime / completed series typically aren't carried — those go through
AniDex and Nyaa. This scraper is a focused complement, not a replacement.

API endpoint (no auth required):
  GET https://subsplease.org/api/?f=search&tz=UTC&s=<query>

Response shape (when matches exist):
  {
    "<key>": {
      "show":         "Show Title",
      "episode":      "12",
      "release_date": "...",
      "downloads": [
        {"res": "720",  "magnet": "magnet:?xt=urn:btih:...&dn=..."},
        {"res": "1080", "magnet": "magnet:?xt=urn:btih:...&dn=..."},
        ...
      ],
      "page":      "...",
      "image_url": "..."
    },
    ...
  }

When no shows match, the API returns an empty list `[]` instead of an
empty dict — we handle both shapes.

Patch: Uses alternative_season / alternative_episode from Seren's anime
       enrichment so the search hits the right cour for split-cour series.
"""

import json
import re
import threading as _threading
import time as _time

from providerModules.a4kScrapers import core
from providerModules.a4kScrapers import scraper_health as _health
from providerModules.a4kScrapers.request import _circuit_is_open as _cb_open

_SCRAPER_NAME   = 'subsplease'
_SCRAPER_DOMAIN = 'https://subsplease.org'

# Throttle: SubsPlease's API is fast and unauth'd, but be polite — 0.5s gap
_sp_lock              = _threading.Lock()
_sp_last_request_time = [0.0]
_SP_MIN_GAP           = 0.5   # seconds

_API_URL = 'https://subsplease.org/api/?f=search&tz=UTC&s=%s'


# ── Helpers ──────────────────────────────────────────────────────────────
def _is_ascii(s):
    try:
        s.encode('ascii')
        return True
    except (UnicodeEncodeError, UnicodeDecodeError):
        return False


def _throttled_get(request, url):
    """Rate-limited HTTP GET for SubsPlease API."""
    with _sp_lock:
        elapsed = _time.time() - _sp_last_request_time[0]
        if elapsed < _SP_MIN_GAP:
            _time.sleep(_SP_MIN_GAP - elapsed)
        _sp_last_request_time[0] = _time.time()
    return request.get(url)


def _hash_from_magnet(magnet):
    """Extract 40-char btih hash from magnet URI (or 32-char base32)."""
    if not magnet:
        return None
    m = re.search(r'btih:([a-fA-F0-9]{40})', magnet)
    if m:
        return m.group(1).lower()
    # Some clients emit base32 hashes — normalise to hex if possible
    m2 = re.search(r'btih:([a-zA-Z2-7]{32})', magnet)
    if m2:
        try:
            import base64, binascii
            raw = base64.b32decode(m2.group(1).upper())
            return binascii.hexlify(raw).decode('ascii').lower()
        except Exception:
            return None
    return None


def _resolution_quality(res):
    """Normalise SubsPlease's 'res' field (e.g. '720', '1080', '480')."""
    res = (res or '').strip()
    if res.startswith('1080'):
        return '1080p'
    if res.startswith('720'):
        return '720p'
    if res.startswith('480'):
        return 'SD'
    if res.startswith('2160') or res.startswith('4K'):
        return '4K'
    return 'Variable'


def _fetch(request, query):
    """Fire one SubsPlease search and return parsed torrent dicts.

    Returns a flat list — each (show, episode, resolution) becomes one
    torrent entry so downstream filters can score them independently.
    """
    url = _API_URL % core.quote_plus(query)
    try:
        resp = _throttled_get(request, url)
        if resp is None or getattr(resp, 'status_code', 0) != 200:
            sc = getattr(resp, 'status_code', 'no-resp')
            core.tools.log(
                'a4kScrapers.subsplease: %s response %s' % (url[:80], sc),
                'notice')
            return []
        body = resp.text or ''
    except Exception:
        return []

    # SubsPlease returns "[]" when nothing matches; otherwise a JSON object.
    try:
        data = json.loads(body)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []

    results = []
    for entry in data.values():
        try:
            show     = (entry.get('show') or '').strip()
            episode  = (entry.get('episode') or '').strip()
            page     = (entry.get('page') or '').strip()
            downloads = entry.get('downloads') or []
            if not show or not downloads:
                continue

            # SubsPlease release titles aren't included in the API
            # response — synthesise the canonical fansub naming so our
            # anime classifier and episode filter can score them
            # exactly as if they came off Nyaa.
            for dl in downloads:
                magnet = (dl.get('magnet') or '').strip()
                if not magnet:
                    continue
                h = _hash_from_magnet(magnet)
                if not h:
                    continue
                res = (dl.get('res') or '').strip()
                # Build the title in the official SubsPlease format:
                #   [SubsPlease] Show - 01 (1080p)
                title = '[SubsPlease] %s - %s (%sp)' % (show, episode, res or '720')

                results.append({
                    'title':   title,
                    'hash':    h,
                    'size':    0,        # API does not expose size; debrid will fill
                    'seeds':   0,        # API does not expose seeds; debrid/cross-fill
                    'magnet':  magnet,
                    'quality': _resolution_quality(res),
                    '_page':   page,
                })
        except Exception:
            continue
    return results


class sources(core.DefaultSources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, **kwargs)

    # ── Public entry point ────────────────────────────────────────────────

    def episode(self, simple_info, all_info,
                auto_query=True, query_seasons=True, query_show_packs=True, **kwargs):
        self.query_type = 'episode'
        start = _time.time()

        if _health.is_blacklisted(_SCRAPER_NAME):
            return []

        scraper = self._get_scraper(simple_info.get('show_title', ''))
        if isinstance(scraper, core.NoResultsScraper):
            core.tools.log('a4kScrapers.episode.subsplease: 0', 'notice')
            core.tools.log('a4kScrapers.episode.subsplease: took 0 ms', 'notice')
            return []

        results = self._fetch_episode(simple_info)

        _health.record(_SCRAPER_NAME, had_http_error=_cb_open(_SCRAPER_DOMAIN))
        elapsed = int((_time.time() - start) * 1000)
        core.tools.log('a4kScrapers.episode.subsplease: %d' % len(results), 'notice')
        core.tools.log('a4kScrapers.episode.subsplease: took %d ms' % elapsed, 'notice')
        return results

    def movie(self, title, year, imdb_id=None, auto_query=True, **kwargs):
        # SubsPlease only carries airing TV anime. No movies.
        self.query_type = 'movie'
        return []

    # ── Core fetch logic ──────────────────────────────────────────────────

    def _fetch_episode(self, simple_info):
        show_title = core.source_utils.clean_title(simple_info.get('show_title', ''))
        aliases    = simple_info.get('show_aliases', [])
        is_anime   = simple_info.get('isanime', False)

        # Anime cour correction — SubsPlease numbers by absolute episode for
        # multi-cour series, so we lean on alternative_episode when present.
        alt_season  = str(simple_info.get('alternative_season', '') or '')
        alt_episode = str(simple_info.get('alternative_episode', '') or '')

        if is_anime and alt_season and alt_episode:
            episode = alt_episode
        else:
            episode = str(simple_info.get('episode_number', '1'))

        ep_zfill = episode.zfill(2)

        # Absolute number (fansub style) — SubsPlease releases use absolute
        # numbering across cour boundaries (e.g. Frieren ep 30, not S2E2).
        abs_num = str(simple_info.get('absolute_number', '') or '')
        if not abs_num:
            abs_num = simple_info.get('episode_number', episode)
        abs_zfill = str(abs_num).zfill(2)

        # SubsPlease's search is title-based — episode filtering happens on
        # the result side. Build a small set of title queries: primary title
        # + best ASCII romaji alias. The API matches partial words.
        queries = []
        if show_title and len(show_title) >= 3:
            queries.append(show_title)

        best_romaji = None
        show_first_word = show_title.split()[0] if show_title else ''
        for a in aliases:
            if not _is_ascii(a):
                continue
            ca = core.source_utils.clean_title(a)
            if ca and ca.lower() != show_title.lower() and len(ca) > 4:
                ca_first = ca.split()[0] if ca else ''
                if is_anime and ca_first == show_first_word:
                    continue
                best_romaji = ca
                queries.append(ca)
                break

        # ── Fire queries sequentially (throttled) ─────────────────────────
        ep_filter = core.source_utils.get_filter_single_episode_fn(simple_info)
        s_filter  = core.source_utils.get_filter_season_pack_fn(simple_info)
        sh_filter = core.source_utils.get_filter_show_pack_fn(simple_info)

        all_raw = []
        for q in queries:
            if self._cancellation_token.is_cancellation_requested:
                break
            items = _fetch(self._request, q)
            core.tools.log(
                'a4kScrapers.subsplease: query "%s" → %d' % (q[:70], len(items)),
                'notice')
            all_raw.extend(items)

        # ── Deduplicate and classify ──────────────────────────────────────
        seen_hashes = set()
        results     = []
        for item in all_raw:
            h = item['hash']
            if h in seen_hashes:
                continue
            seen_hashes.add(h)

            rt = item['title']
            tags_rt    = core.source_utils.clean_tags(rt)
            cleaned_rt = core.source_utils.clean_title(tags_rt)
            pkg = None

            if is_anime:
                pkg = core.source_utils.anime_filter_sources(
                    tags_rt, simple_info, ep_zfill, str(simple_info.get('season_number', '1')).zfill(2), abs_zfill
                )
                if pkg is None:
                    continue
            else:
                if ep_filter(cleaned_rt):
                    pkg = 'single'
                elif s_filter(cleaned_rt):
                    pkg = 'season'
                elif sh_filter(cleaned_rt):
                    pkg = 'show'

            if not pkg:
                continue

            results.append({
                'scraper':       self._caller_name,
                'hash':          h,
                'package':       pkg,
                'release_title': rt,
                'size':          item['size'],
                'seeds':         item['seeds'],
                'magnet':        item['magnet'],
            })

        return results
