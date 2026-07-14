# -*- coding: utf-8 -*-
"""
AniRena scraper — RSS-based search against anirena.com.

AniRena (anirena.com) is one of the oldest public anime-dedicated torrent
indexes, originally launched as Shockerz.net in 2003.

v2.99.83 fixes:
  AniRena: if api_key is set but _get_bearer_token() fails, return [] immediately.
    Previously the scraper silently fell back to RSS when the AniRena key was
    configured but the token exchange failed — obscuring auth errors and using an
    inferior path.  Fix: log the failure and return [] so the user sees no results
    rather than stale/incomplete RSS output.
  AniRena: restructure per-query dispatch to `if api_key / elif RSS-early-exit /
    else RSS`.  Previously the early-exit guard ran even in API mode (it was gated
    on `not bearer_token`, which is always False after a successful exchange, but
    logically belongs only in the RSS path).  Fix: check api_key first so API-only
    mode is explicit and the early-exit guard is unreachable in that path.
  AnimeTosho: drop abs_zfill from the OR query when anidb_id is set (Otaku parity).
    The previous code emitted "- 55"|"- 02"|"S02E02" for Frieren S2E2 — the
    abs_zfill "55" adds noise against the AniDB-filtered Sphinx index, causing
    0-result episode queries.  Fix: when anidb_id is present, use ep_zfill only,
    matching Otaku's query exactly: "- 02"|"S02E02".

v2.99.74 fix: early-exit after targeted SxxExx queries.
  "sousou no frieren 02" matches every episode with "02" anywhere in the
  title — 33 torrent downloads. "sousou no frieren" matches everything — 59
  downloads. Total: 73s, user cancels.  The SxxExx query already found the
  correct 2 releases in ~3s.  Fix: once all n_targeted SxxExx queries have
  run, skip remaining broad queries if all_raw is non-empty.  Expected: 2
  torrent downloads (~4s) for a typical episode search.

v2.99.73 audit fixes (5 bugs):
  Bug 1 (High): _fetch_torrent_hash called request.get() directly, bypassing
    the 0.5 s throttle — rapid-fire downloads for multi-result queries.
    Fix: route through _throttled_get().
  Bug 2 (High): same .torrent URL re-downloaded across overlapping queries.
    Fix: module-level _torrent_url_cache dict (URL → hash, session-scoped).
  Bug 3 (Medium): page_url + '.torrent' lacked trailing-slash strip.
    Fix: page_url.rstrip('/') + '.torrent'.
  Bug 4 (Medium): season queries used abs_zfill (e.g. 55) which never matches
    AniRena's S02Exx naming — all returned 0, wasting HTTP requests and
    triggering redundant torrent downloads.
    Fix: replaced all abs_zfill season variants with ep_zfill; removed the
    duplicate nth+abs variant.
  Bug 5 (Low): 'import hashlib' inside _torrent_info_hash — works due to
    sys.modules cache but wrong style.  Fix: moved to module-level imports.

v2.99.72 fix: .torrent download + bencode SHA-1 hash extraction.
  The /magnet redirect approach failed silently (Cloudflare blocks raw HTTP
  HEAD requests to that endpoint).  The correct approach is the <enclosure>
  tag in the RSS which gives a direct .torrent file URL.  Downloading that
  file and SHA-1 hashing the bencoded info-dict gives the btih hash.
  Also strips AniRena's category prefix from RSS item titles so the anime
  filter classifier sees clean torrent names.

v2.99.71 fix: Follow /magnet redirect (did not work — see v2.99.72).
  All queries returned 0 results because the scraper appended bare episode
  numbers (e.g. "sousou no frieren 55" or "sousou no frieren 02") but
  AniRena titles use SxxExx notation ("Sousou no Frieren 2nd Season - S02E02").
  AniRena's RSS requires word-level AND matches, so "02" never matched "S02E02".

  Fix: Insert "Title S02E02" format queries first for anime, then keep the
  existing abs/ep numeric queries as fallbacks.  Also adds "Title 2nd Season 02"
  variant to catch releases that omit the S02Exx prefix.

  Additionally, _SCRAPER_DOMAIN is changed to the RSS endpoint so the
  framework's availability HEAD check hits a URL that returns 200 rather
  than the root page (which returns 404 for HEAD under Cloudflare).

v2.99.69 fix: Switch from HTML parsing to RSS feed.
  The 2026 AniRena redesign no longer embeds magnet URIs or btih hashes in
  its search-results HTML.  Torrent rows use UUID-based
  <a href="/torrents/UUID/magnet"> redirect links (not direct magnet:?xt=...
  URIs), so the old HTML parser returned 0 results for every query.

  The RSS endpoint (/rss?q=QUERY) returns a standard Nyaa-compatible XML
  feed that includes the complete magnet URI (with btih hash) in each <item>.

  Handles both 40-char hex hashes and 32-char base32 hashes.
  Handles CDATA-wrapped and plain text XML fields.

v2.99.67 fix: Individual keyword queries (AniRena rejects Nyaa |-OR syntax).
v2.99.64: AniRena replaces AniDex.

RSS URL:  https://www.anirena.com/rss?q=QUERY
"""

import re
import base64
import hashlib
import json as _json
import threading as _threading
import time as _time

try:
    from urllib.request import Request as _UrllibRequest, urlopen as _urlopen
    from urllib.error import HTTPError as _HTTPError
except ImportError:
    _UrllibRequest = _urlopen = _HTTPError = None

from providerModules.a4kScrapers import core
from providerModules.a4kScrapers import scraper_health as _health
from providerModules.a4kScrapers.request import _circuit_is_open as _cb_open

_SCRAPER_NAME   = 'anirena'
# Use the RSS endpoint for availability checks — the root page returns 404
# for HEAD requests under Cloudflare, which falsely marks the circuit as open.
_SCRAPER_DOMAIN = 'https://www.anirena.com/rss'

_anirena_lock              = _threading.Lock()
_anirena_last_request_time = [0.0]
_ANIRENA_MIN_GAP           = 0.5

_MAX_CONSECUTIVE_HTTP_FAILURES = 2

_BASE_URL = 'https://www.anirena.com'

_ar_debug_logged = [False]

# Session-scoped cache: torrent_url -> 40-char hex hash.
# Avoids re-downloading the same .torrent file when multiple queries return
# the same item (common when title-only and SxxExx queries overlap).
_torrent_url_cache = {}

# AniRena JSON API — bearer token cache (rotates per X-New-Token response header)
_anirena_bearer_token   = [None]   # current token string
_anirena_bearer_expires = [0.0]    # unix timestamp when token expires
_anirena_bearer_key     = [None]   # api_key that produced the cached token
_anirena_bearer_lock    = _threading.Lock()
_anirena_api_key_cache  = [None]   # session-scoped key cache for seed-backfill reuse


def _is_ascii(s):
    try:
        s.encode('ascii')
        return True
    except (UnicodeEncodeError, UnicodeDecodeError):
        return False


def _ordinal(n):
    n = int(n)
    if 11 <= (n % 100) <= 13:
        return '%dth' % n
    return '%d%s' % (n, {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th'))


def _throttled_get(request, url):
    with _anirena_lock:
        elapsed = _time.time() - _anirena_last_request_time[0]
        if elapsed < _ANIRENA_MIN_GAP:
            _time.sleep(_ANIRENA_MIN_GAP - elapsed)
        _anirena_last_request_time[0] = _time.time()
    return request.get(url)


def _parse_size(text):
    try:
        return core.source_utils.de_string_size(text.replace('i', ''))
    except Exception:
        return 0


def _b32_to_hex(b32):
    """Convert a base32-encoded info-hash to 40-char lowercase hex."""
    try:
        pad = (8 - len(b32) % 8) % 8
        raw = base64.b32decode(b32.upper() + '=' * pad)
        return raw.hex()
    except Exception:
        return None


def _extract_hash(text):
    """Extract a btih hash (hex or base32) from any string containing it.
    Returns 40-char lowercase hex, or None.
    """
    hm = re.search(r'btih:([a-fA-F0-9]{40})', text, re.IGNORECASE)
    if hm:
        return hm.group(1).lower()
    # 32-char base32
    hm = re.search(r'btih:([A-Za-z2-7]{32})(?:[^A-Za-z2-7]|$)', text)
    if hm:
        return _b32_to_hex(hm.group(1))
    # 52-char base32 (SHA-256 variant)
    hm = re.search(r'btih:([A-Za-z2-7]{52})(?:[^A-Za-z2-7]|$)', text)
    if hm:
        return _b32_to_hex(hm.group(1))
    return None


def _cdata_or_text(s):
    """Strip CDATA wrapper if present."""
    m = re.search(r'<!\[CDATA\[(.*?)\]\]>', s, re.DOTALL)
    return m.group(1).strip() if m else s.strip()


# ---------------------------------------------------------------------------
# .torrent → info-hash  (AniRena 2026: hash only obtainable via torrent file)
# ---------------------------------------------------------------------------

def _bencode_skip(d, i):
    """Return the index after the bencoded value starting at position i.
    Returns -1 on any parse error.
    """
    if i >= len(d):
        return -1
    b = d[i]
    if b == 100:          # b'd' — dict
        i += 1
        while i < len(d) and d[i] != 101:   # b'e'
            i = _bencode_skip(d, i)          # key
            if i < 0:
                return -1
            i = _bencode_skip(d, i)          # value
            if i < 0:
                return -1
        return i + 1 if i < len(d) else -1
    elif b == 108:        # b'l' — list
        i += 1
        while i < len(d) and d[i] != 101:
            i = _bencode_skip(d, i)
            if i < 0:
                return -1
        return i + 1 if i < len(d) else -1
    elif b == 105:        # b'i' — integer
        e = d.find(b'e', i + 1)
        return e + 1 if e >= 0 else -1
    elif 48 <= b <= 57:   # b'0'–b'9' — byte string
        c = d.find(b':', i)
        if c < 0:
            return -1
        try:
            return c + 1 + int(d[i:c])
        except Exception:
            return -1
    return -1


def _torrent_info_hash(data):
    """Return SHA-1 info-hash (40-char hex) from raw .torrent bytes, or None."""
    try:
        idx = data.find(b'4:info')
        if idx < 0:
            return None
        start = idx + 6                     # skip b'4:info'
        end   = _bencode_skip(data, start)
        if end > start:
            return hashlib.sha1(data[start:end]).hexdigest()
    except Exception:
        pass
    return None


def _fetch_torrent_hash(torrent_url, request):
    """Download .torrent and return SHA-1 info-hash hex, or None.

    Uses the module-level _torrent_url_cache to avoid re-downloading the same
    file when multiple queries return the same torrent.  Respects the shared
    0.5 s inter-request throttle via _throttled_get so torrent downloads do
    not bypass the rate limiter.
    """
    if torrent_url in _torrent_url_cache:
        return _torrent_url_cache[torrent_url]
    try:
        resp = _throttled_get(request, torrent_url)
        if resp and getattr(resp, 'status_code', 0) == 200 and resp.content:
            h = _torrent_info_hash(resp.content)
            if h:
                _torrent_url_cache[torrent_url] = h
            return h
    except Exception:
        pass
    return None


def _api_post(url, auth_header, payload=None, timeout=15):
    """POST to AniRena JSON API. Returns (response_dict_or_None, x_new_token_or_None)."""
    if _UrllibRequest is None:
        return None, None
    try:
        body = _json.dumps(payload or {}).encode('utf-8')
        req  = _UrllibRequest(url, data=body, method='POST')
        req.add_header('Content-Type',  'application/json')
        req.add_header('Accept',        'application/json')
        req.add_header('Authorization', auth_header)
        req.add_header('User-Agent',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) '
            'Gecko/20100101 Firefox/115.0')
        with _urlopen(req, timeout=timeout) as resp:
            new_token = resp.headers.get('X-New-Token') or None
            data = _json.loads(resp.read().decode('utf-8'))
            return data, new_token
    except _HTTPError as exc:
        core.tools.log(
            'a4kScrapers.anirena: API HTTP %d: %s' % (exc.code, url), 'notice')
        if exc.code == 401:
            # Token rejected/consumed — clear cache so next call re-exchanges.
            # Written without the lock (safe under CPython GIL for list-slot
            # writes); the lock's purpose is to guard concurrent exchanges, not
            # individual slot nullifications.
            _anirena_bearer_token[0]   = None
            _anirena_bearer_expires[0] = 0.0
            _anirena_bearer_key[0]     = None
    except Exception as exc:
        core.tools.log(
            'a4kScrapers.anirena: API error: %s' % str(exc)[:100], 'notice')
    return None, None


def _get_bearer_token(api_key):
    """Exchange API key for bearer token. Cached with ≥60 s validity buffer."""
    with _anirena_bearer_lock:
        if (_anirena_bearer_token[0]
                and _anirena_bearer_key[0] == api_key
                and _time.time() < _anirena_bearer_expires[0] - 60):
            return _anirena_bearer_token[0]
        data, _ = _api_post(_BASE_URL + '/api/v1/auth/token', 'ApiKey %s' % api_key)
        if data and isinstance(data, dict):
            token = data.get('token') or data.get('access_token')
            if token:
                expires_in = int(data.get('expires_in', 3600) or 3600)
                _anirena_bearer_token[0]   = token
                _anirena_bearer_expires[0] = _time.time() + expires_in
                _anirena_bearer_key[0]     = api_key
                return token
        _anirena_bearer_key[0] = None
        core.tools.log('a4kScrapers.anirena: API key exchange failed', 'notice')
    return None


def _api_search(query, bearer_token, per_page=250):
    """Search via AniRena JSON API. Returns (results_or_None, new_token_or_None).

    Returns None results on HTTP failure — mirrors _fetch() so the consecutive-
    failure counter works identically for both code paths.
    Token rotation: caller must update its bearer_token from new_token when non-None.
    """
    data, new_token = _api_post(
        _BASE_URL + '/api/v1/torrents/search',
        'Bearer %s' % bearer_token,
        {'q': query, 'category': 'anime', 'sort': 'seeders',
         'order': 'desc', 'per_page': per_page},
    )
    if data is None:
        return None, new_token

    results  = []
    torrents = data.get('torrents', []) if isinstance(data, dict) else []
    for t in torrents:
        try:
            title    = (t.get('title') or '').strip()
            hash_val = (t.get('info_hash_v1') or '').strip().lower()
            if not title or not hash_val:
                continue
            magnet = t.get('magnet') or (
                'magnet:?xt=urn:btih:%s&dn=%s' % (hash_val, core.quote_plus(title)))
            size   = _parse_size(t.get('size_fmt') or '')
            seeds  = int(t.get('seeders') or 0)
            results.append({
                'title':  title,
                'hash':   hash_val,
                'size':   size,
                'seeds':  seeds,
                'magnet': magnet,
            })
        except Exception:
            continue
    return results, new_token


def _fetch(request, query):
    """Fire one AniRena RSS search and return parsed torrent dicts.

    AniRena 2026 RSS items contain NO btih hash.  The <link> is a UUID-based
    torrent-page URL and the <enclosure> is a .torrent file download URL.
    We download the .torrent file and derive the info-hash via SHA-1.

    Returns:
      None  - HTTP failure (non-200 / timeout). Triggers abort counter.
      []    - Valid 200 response, 0 results.
      list  - Parsed results.
    """
    url = _BASE_URL + '/rss?q=%s' % core.quote_plus(query)

    try:
        resp = _throttled_get(request, url)
        if resp is None or getattr(resp, 'status_code', 0) != 200:
            sc = getattr(resp, 'status_code', 'no-resp')
            core.tools.log(
                'a4kScrapers.anirena: %s response %s' % (url[:80], sc), 'notice')
            return None
        xml = resp.text or ''
    except Exception as e:
        core.tools.log(
            'a4kScrapers.anirena: exception on %s - %s' % (url[:80], str(e)[:60]),
            'notice')
        return None

    if not _ar_debug_logged[0]:
        _ar_debug_logged[0] = True
        sample = xml[200:2000].replace('\n', ' ').replace('\r', '')
        core.tools.log(
            'a4kScrapers.anirena RSS debug (offset 200): %s' % sample, 'notice')

    results = []
    items = re.findall(r'<item>(.*?)</item>', xml, re.DOTALL)
    if not items:
        items = re.findall(r'<item\b[^>]*>(.*?)</item>', xml, re.DOTALL)

    for item in items:
        try:
            # --- Title ---
            tm = re.search(r'<title[^>]*>(.*?)</title>', item, re.DOTALL)
            if not tm:
                continue
            title = _cdata_or_text(tm.group(1))
            if not title:
                continue
            # Strip AniRena category prefix e.g. "[Anime > Subtitle(s)...] "
            title = re.sub(r'^\[[^\]]*\]\s*', '', title).strip()
            if not title:
                continue

            # --- Hash: try standard XML paths first ---
            hash_val    = None
            magnet      = None
            torrent_url = None    # <enclosure> .torrent URL
            page_url    = None    # <link> UUID page URL

            lm = re.search(r'<link[^>]*>(.*?)</link>', item, re.DOTALL)
            if lm:
                link_text = _cdata_or_text(lm.group(1))
                if link_text.startswith('magnet:'):
                    hash_val = _extract_hash(link_text)
                    if hash_val:
                        magnet = link_text
                elif 'anirena.com/torrents/' in link_text:
                    page_url = link_text          # UUID-based page URL
                else:
                    hash_val = _extract_hash(link_text)

            if not hash_val:
                eu = re.search(r'<enclosure[^>]+url=["\']([^"\']+)["\']', item)
                if eu:
                    enc_url = eu.group(1)
                    if enc_url.endswith('.torrent'):
                        torrent_url = enc_url     # save for fallback
                    else:
                        hash_val = _extract_hash(enc_url)
                        if enc_url.startswith('magnet:') and hash_val:
                            magnet = enc_url

            if not hash_val:
                hash_val = _extract_hash(item)
                if hash_val:
                    mm = re.search(r'(magnet:\?[^\s<"\'&]+)', item)
                    if mm:
                        magnet = mm.group(1)

            # --- Hash: AniRena 2026 fallback — download .torrent and parse ---
            if not hash_val:
                # Prefer the explicit enclosure URL; fall back to constructing it
                dl_url = torrent_url or (
                    page_url.rstrip('/') + '.torrent' if page_url else None)
                if dl_url:
                    hash_val = _fetch_torrent_hash(dl_url, request)
                    if hash_val:
                        magnet = 'magnet:?xt=urn:btih:%s' % hash_val
                        core.tools.log(
                            'a4kScrapers.anirena: torrent hash OK %s' % hash_val[:12],
                            'notice')
                    else:
                        core.tools.log(
                            'a4kScrapers.anirena: torrent hash FAIL %s' % (dl_url[:60],),
                            'notice')

            if not hash_val:
                continue
            if not magnet:
                magnet = 'magnet:?xt=urn:btih:%s' % hash_val

            # --- Seeders ---
            seeds = 0
            sm = re.search(
                r'<(?:[a-zA-Z]+:)?seeders[^>]*>(\d+)</(?:[a-zA-Z]+:)?seeders>',
                item)
            if sm:
                try:
                    seeds = int(sm.group(1))
                except Exception:
                    seeds = 0

            # --- Size: parse from description "Size: X.X GB" ---
            size = 0
            szm = re.search(r'Size:\s*([\d.]+\s*(?:GB|MB|TB|KB|GiB|MiB|TiB|KiB))',
                             item, re.IGNORECASE)
            if szm:
                size = _parse_size(szm.group(1))
            if not size:
                szm2 = re.search(
                    r'<(?:[a-zA-Z]+:)?size[^>]*>([^<]+)</(?:[a-zA-Z]+:)?size>',
                    item)
                if szm2:
                    size = _parse_size(_cdata_or_text(szm2.group(1)))

            results.append({
                'title':  title,
                'hash':   hash_val,
                'size':   size or 0,
                'seeds':  seeds,
                'magnet': magnet,
            })
        except Exception:
            continue

    return results


class sources(core.DefaultSources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, **kwargs)
        self._anirena_api_key = None

    def episode(self, simple_info, all_info,
                auto_query=True, query_seasons=True, query_show_packs=True, **kwargs):
        self.query_type = 'episode'
        start = _time.time()

        if _health.is_blacklisted(_SCRAPER_NAME):
            core.tools.log('a4kScrapers.episode.anirena: 0', 'notice')
            core.tools.log('a4kScrapers.episode.anirena: took 0 ms', 'notice')
            return []

        scraper = self._get_scraper(simple_info.get('show_title', ''))
        if isinstance(scraper, core.NoResultsScraper):
            core.tools.log('a4kScrapers.episode.anirena: 0', 'notice')
            core.tools.log('a4kScrapers.episode.anirena: took 0 ms', 'notice')
            return []

        # API key passed from Seren's Accounts → AniRena API Key setting
        apikeys = kwargs.get('apikeys') or {}
        self._anirena_api_key = apikeys.get('anirena') or None

        # Cache at module level: seed-backfill calls may not receive apikeys kwargs.
        # Note: if the user removes their key, the stale cache persists until restart —
        # token exchange will fail and return [], which is correct (no silent RSS fallback).
        if self._anirena_api_key:
            _anirena_api_key_cache[0] = self._anirena_api_key

        results = self._fetch_episode(simple_info)

        _health.record(_SCRAPER_NAME, had_http_error=_cb_open(_SCRAPER_DOMAIN))
        elapsed = int((_time.time() - start) * 1000)
        core.tools.log('a4kScrapers.episode.anirena: %d' % len(results), 'notice')
        core.tools.log('a4kScrapers.episode.anirena: took %d ms' % elapsed, 'notice')
        return results

    def movie(self, title, year, imdb_id=None, auto_query=True, **kwargs):
        self.query_type = 'movie'
        return []

    def _fetch_episode(self, simple_info):
        show_title = core.source_utils.clean_title(simple_info.get('show_title', ''))
        aliases    = simple_info.get('show_aliases', [])
        is_anime   = simple_info.get('isanime', False)

        alt_season  = str(simple_info.get('alternative_season', '') or '')
        alt_episode = str(simple_info.get('alternative_episode', '') or '')

        if is_anime and alt_season and alt_episode:
            season  = alt_season
            episode = alt_episode
        else:
            season  = str(simple_info.get('season_number', '1'))
            episode = str(simple_info.get('episode_number', '1'))

        ep_zfill = episode.zfill(2)
        ss_zfill = season.zfill(2)

        abs_num = str(simple_info.get('absolute_number', '') or '')
        if not abs_num:
            abs_num = episode
        abs_zfill = str(abs_num).zfill(2)

        ep_filter = core.source_utils.get_filter_single_episode_fn(simple_info)
        s_filter  = core.source_utils.get_filter_season_pack_fn(simple_info)
        sh_filter = core.source_utils.get_filter_show_pack_fn(simple_info)

        # Best ASCII romaji alias
        best_romaji     = None
        show_first_word = show_title.split()[0] if show_title else ''

        for a in aliases:
            if not _is_ascii(a):
                continue
            ca = core.source_utils.clean_title(a)
            if ca and ca.lower() != show_title.lower() and len(ca) > 4:
                if is_anime and (ca.split() or [''])[0] == show_first_word:
                    continue
                best_romaji = ca
                break

        if not best_romaji:
            for a in aliases:
                ca = core.source_utils.clean_title(a)
                if ca and ca.lower() != show_title.lower() and len(ca) > 4:
                    if is_anime and (ca.split() or [''])[0] == show_first_word:
                        continue
                    best_romaji = ca
                    break

        # Individual keyword queries (AniRena rejects Nyaa |-OR syntax).
        # Query priority for anime:
        #   1. SxxExx format  — "sousou no frieren S02E02"  (AniRena primary naming)
        #   2. Nth Season ep  — "sousou no frieren 2nd Season 02"  (alternate naming)
        #   3. Abs/ep numeric — legacy fallbacks
        queries    = []
        n_targeted = 0   # number of high-specificity queries at the front

        # --- 1. SxxExx format (highest priority for anime) ---
        if is_anime:
            sxxexx = 'S%sE%s' % (ss_zfill, ep_zfill)
            if best_romaji:
                queries.append('%s %s' % (best_romaji, sxxexx))
            queries.append('%s %s' % (show_title, sxxexx))
            n_targeted = len(queries)   # mark end of targeted block

        # --- 2. Absolute / plain episode number queries ---
        if best_romaji:
            queries.append('%s %s' % (best_romaji, abs_zfill))
        queries.append('%s %s' % (show_title, abs_zfill))

        if abs_zfill != ep_zfill:
            if best_romaji:
                queries.append('%s %s' % (best_romaji, ep_zfill))
            queries.append('%s %s' % (show_title, ep_zfill))

        # --- 3. Batch queries ---
        if best_romaji:
            queries.append('%s Batch' % best_romaji)
        queries.append('%s Batch' % show_title)

        # --- 4. Season-N variants ---
        if is_anime and alt_season and int(alt_season) > 1:
            s_num = int(alt_season)
            nth   = _ordinal(s_num)
            # Use ep_zfill (cour-corrected) not abs_zfill — AniRena uses
            # S02E02 / "2nd Season" naming, never absolute episode numbers.
            if best_romaji:
                queries.append('%s %s Season %s' % (best_romaji, nth, ep_zfill))
                queries.append('%s Season %d %s' % (best_romaji, s_num, ep_zfill))
            queries.append('%s %s Season %s' % (show_title, nth, ep_zfill))
            queries.append('%s Season %d %s' % (show_title, s_num, ep_zfill))

        # --- 5. Title-only broad sweep (filtered by classifier) ---
        # Both romaji and English title included — catches batch packs and
        # releases that use either naming convention.
        if best_romaji:
            queries.append(best_romaji)
        queries.append(show_title)

        # API key path: exchange key for bearer token once per session.
        # If a key is configured and the token exchange fails we return []
        # immediately — RSS is never used as a silent fallback for a key that
        # the user explicitly set.
        api_key      = getattr(self, '_anirena_api_key', None) or _anirena_api_key_cache[0]
        bearer_token = None
        if api_key:
            bearer_token = _get_bearer_token(api_key)
            if bearer_token:
                core.tools.log('a4kScrapers.anirena: JSON API active (key configured)', 'notice')
            else:
                core.tools.log(
                    'a4kScrapers.anirena: API key set but token exchange failed — no RSS fallback',
                    'notice')
                return []

        # Fire queries with consecutive HTTP failure early-abort
        all_raw = []
        consecutive_http_failures = 0

        for i, q in enumerate(queries):
            if self._cancellation_token.is_cancellation_requested:
                break

            if consecutive_http_failures >= _MAX_CONSECUTIVE_HTTP_FAILURES:
                remaining = len(queries) - i
                core.tools.log(
                    'a4kScrapers.anirena: %d consecutive HTTP failures - '
                    'aborting %d remaining queries' % (
                        consecutive_http_failures, remaining),
                    'notice')
                break

            if api_key:
                # API-only path: never fall back to RSS, all queries run for
                # maximum coverage (JSON queries are fast — no .torrent downloads).
                raw, new_token = _api_search(q, bearer_token)
                if new_token:
                    bearer_token = new_token
                    _anirena_bearer_token[0] = new_token
            elif is_anime and i >= n_targeted and all_raw:
                # RSS early-exit guard (only when no API key is configured).
                # "sousou no frieren 02" returns 33+ results per RSS query,
                # each requiring a .torrent download — 70s wasted when the
                # SxxExx queries already found the correct releases in ~3s.
                core.tools.log(
                    'a4kScrapers.anirena: %d raw from targeted queries, '
                    'skipping %d remaining (RSS mode)' % (len(all_raw), len(queries) - i),
                    'notice')
                break
            else:
                raw = _fetch(self._request, q)

            if raw is None:
                consecutive_http_failures += 1
                core.tools.log(
                    'a4kScrapers.anirena: HTTP failure %d/%d on query "%s"' % (
                        consecutive_http_failures,
                        _MAX_CONSECUTIVE_HTTP_FAILURES, q[:60]),
                    'notice')
                continue

            consecutive_http_failures = 0
            core.tools.log(
                'a4kScrapers.anirena: query "%s" -> %d' % (q[:70], len(raw)),
                'notice')
            all_raw.extend(raw)

        # Deduplicate and classify
        seen_hashes = set()
        results     = []
        for item in all_raw:
            h = item['hash']
            if h in seen_hashes:
                continue
            seen_hashes.add(h)

            rt         = item['title']
            tags_rt    = core.source_utils.clean_tags(rt)
            cleaned_rt = core.source_utils.clean_title(tags_rt)
            pkg        = None

            if is_anime:
                pkg = core.source_utils.anime_filter_sources(
                    tags_rt, simple_info, ep_zfill, ss_zfill, abs_zfill)
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
