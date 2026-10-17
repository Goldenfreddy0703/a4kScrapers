# -*- coding: utf-8 -*-
"""
TsukiHime scraper — public REST API (https://api.tsukihime.org/v1), no key needed.

TsukiHime is an anime release index (successor candidate to AnimeTosho after
AnimeTosho froze updates on 2026-05-09).  The API returns the infohash (btih)
directly, so no HTML parsing is needed.

Endpoints used:
  /search/torrents?q=<text>&limit=100   token (AND) text search over release names
  /animes/anidb/<aid>                   AniDB id  -> TsukiHime anime id
  /animes/<id>?limit=100&offset=N       releases for one anime, newest first

Notes:
  * The API has no seeder counts, so every result is returned with seeds=0;
    Seren's cross-scraper seed backfill fills them in when the same hash is
    seen elsewhere.
  * limit is capped at 100 server-side (>100 returns HTTP 422).
  * Episode-level releases are findable by text ("<title> S02E02", "<title> 1100"),
    but episode_no is only populated for some of them, so episode matching is
    done locally with the shared anime classifier.
  * Anime only: returns [] immediately for non-anime content.
"""

import re
import threading as _threading
import time as _time

from providerModules.a4kScrapers import core
from providerModules.a4kScrapers import scraper_health as _health
from providerModules.a4kScrapers.request import _circuit_is_open as _cb_open

_SCRAPER_NAME   = 'tsukihime'
_SCRAPER_DOMAIN = 'https://api.tsukihime.org'

_BASE            = 'https://api.tsukihime.org/v1'
_PAGE_LIMIT      = 100   # server maximum
_ANIME_PAGES     = 2     # newest 200 releases of the matched anime
_MAX_CONCURRENT  = 3
_STAGGER_MS      = 100


def _is_ascii(s):
    try:
        s.encode('ascii')
        return True
    except (UnicodeEncodeError, UnicodeDecodeError):
        return False


def _ordinal(n):
    """1->'1st', 2->'2nd', 3->'3rd', 11->'11th'."""
    n = int(n)
    if 11 <= (n % 100) <= 13:
        return '%dth' % n
    return '%d%s' % (n, {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th'))


_RE_SEP = r'[-~]'
_RE_SE_EP  = re.compile(r'(?<![a-z0-9])s(\d{1,2})\s*e\d{1,4}', re.I)
_RE_S_RNG  = re.compile(r'(?<![a-z0-9])s(\d{1,2})\s*' + _RE_SEP + r'\s*s(\d{1,2})(?![a-z0-9])', re.I)
_RE_S_ONLY = re.compile(r'(?<![a-z0-9])s(\d{1,2})(?![a-z0-9])', re.I)
_RE_SEASON = re.compile(r'(?<![a-z0-9])season\s*(\d{1,2})(?![a-z0-9])', re.I)
_RE_ORD    = re.compile(r'(?<![a-z0-9])(\d{1,2})(?:st|nd|rd|th)\s+season', re.I)


def _title_seasons(title):
    """Seasons a release title explicitly claims (S02E05, S2, S01-S02, Season 2, 2nd Season)."""
    found = set()
    for m in _RE_S_RNG.finditer(title):
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo <= hi <= lo + 30:
            found.update(range(lo, hi + 1))
    for rx in (_RE_SE_EP, _RE_S_ONLY, _RE_SEASON, _RE_ORD):
        for m in rx.finditer(title):
            found.add(int(m.group(1)))
    return found


def _get(request, path, params=None):
    """GET <_BASE><path>; return the parsed JSON dict or None on any failure."""
    try:
        url = _BASE + path
        if params:
            qs = '&'.join('%s=%s' % (k, core.quote_plus(str(v))) for k, v in params.items())
            url = url + '?' + qs
        resp = request.get(url)
        if resp is None or resp.status_code != 200:
            return None
        data = core.json.loads(resp.text)
        if not isinstance(data, dict) or data.get('error'):
            return None
        return data
    except Exception:
        return None


def _parse_results(raw_list):
    """Convert API torrent rows to a4kScrapers torrent dicts."""
    items = []
    for item in raw_list or []:
        try:
            if item.get('is_adult'):
                continue
            h = (item.get('btih') or '').strip().lower()
            if not re.match(r'^[0-9a-f]{40}$', h):
                continue
            title = item.get('name') or ''
            if not title:
                continue
            total = int(item.get('totalsize', 0) or 0)
            items.append({
                'title':  title,
                'hash':   h,
                'size':   total / 1024 / 1024 if total else 0,   # MB, like nekobt
                'seeds':  0,
                'magnet': 'magnet:?xt=urn:btih:%s&dn=%s' % (h, core.quote_plus(title)),
                'anime_id': (item.get('anime') or {}).get('id'),
            })
        except Exception:
            continue
    return items


def _search(request, query):
    data = _get(request, '/search/torrents', {'q': query, 'limit': _PAGE_LIMIT})
    return _parse_results(data.get('results')) if data else []


def _resolve_anime_id(request, anidb_id):
    """AniDB id -> TsukiHime anime id (None when unknown)."""
    meta = _get(request, '/animes/anidb/%s' % anidb_id)
    return meta.get('id') if meta else None


def _anime_releases(request, anime_id):
    """Releases for one TsukiHime anime id (newest first)."""
    items = []
    for page in range(_ANIME_PAGES):
        data = _get(request, '/animes/%s' % anime_id,
                    {'limit': _PAGE_LIMIT, 'offset': page * _PAGE_LIMIT})
        rows = (data or {}).get('results') or []
        for it in _parse_results(rows):
            it['anime_id'] = anime_id
            items.append(it)
        if len(rows) < _PAGE_LIMIT:
            break
    return items


class sources(core.DefaultSources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, **kwargs)

    # ── Public entry point ────────────────────────────────────────────────

    def episode(self, simple_info, all_info,
                auto_query=True, query_seasons=True, query_show_packs=True, **kwargs):
        self.query_type = 'episode'
        start = _time.time()

        # Anime-only index: skip everything else outright.
        if not simple_info.get('isanime', False):
            return []

        if _health.is_blacklisted(_SCRAPER_NAME):
            return []

        scraper = self._get_scraper(simple_info.get('show_title', ''))
        if isinstance(scraper, core.NoResultsScraper):
            core.tools.log('a4kScrapers.episode.tsukihime: 0', 'notice')
            core.tools.log('a4kScrapers.episode.tsukihime: took 0 ms', 'notice')
            return []

        results = self._fetch_episode(simple_info)

        _health.record(_SCRAPER_NAME, had_http_error=_cb_open(_SCRAPER_DOMAIN))
        elapsed = int((_time.time() - start) * 1000)
        core.tools.log('a4kScrapers.episode.tsukihime: %d' % len(results), 'notice')
        core.tools.log('a4kScrapers.episode.tsukihime: took %d ms' % elapsed, 'notice')
        return results

    def movie(self, title, year, imdb_id=None, auto_query=True, **kwargs):
        """Movies: core title-search behaviour via _search_request below."""
        self.query_type = 'movie'
        return self._get_scraper(title).movie_query(
            title, year, imdb_id,
            caller_name=self._caller_name,
            auto_query=auto_query,
            single_query=self._single_query,
        )

    # ── Core fetch logic ─────────────────────────────────────────────────

    def _fetch_episode(self, simple_info):
        show_title = core.source_utils.clean_title(simple_info.get('show_title', ''))
        aliases    = simple_info.get('show_aliases', [])
        anidb_id   = simple_info.get('anidb_id')

        # Anime cour correction: prefer alternative season/episode.
        alt_season  = str(simple_info.get('alternative_season', '') or '')
        alt_episode = str(simple_info.get('alternative_episode', '') or '')
        if alt_season and alt_episode:
            season  = alt_season
            episode = alt_episode
        else:
            season  = str(simple_info.get('season_number', '1'))
            episode = str(simple_info.get('episode_number', '1'))

        ep_zfill = episode.zfill(2)
        ss_zfill = season.zfill(2)
        abs_num  = str(simple_info.get('absolute_number', '') or '') or episode
        abs_zfill = abs_num.zfill(2)
        s_int = int(season) if season.isdigit() else 1

        # Best ASCII romaji alias with a different first word (e.g. "Sousou no Frieren").
        best_romaji = None
        first_word = show_title.split()[0] if show_title else ''
        for a in aliases:
            if not _is_ascii(a):
                continue
            ca = core.source_utils.clean_title(a)
            if ca and ca.lower() != show_title.lower() and len(ca) > 4:
                if (ca.split() or [''])[0] == first_word:
                    continue
                best_romaji = ca
                break

        # ── Build tasks ──────────────────────────────────────────────────
        queries = [
            '%s S%sE%s' % (show_title, ss_zfill, ep_zfill),     # SxxEyy style
            '%s %s' % (show_title, abs_zfill),                  # fansub "- 05" / absolute style
            '%s batch' % show_title,
            '%s S%s' % (show_title, ss_zfill),                  # season pack
        ]
        if s_int > 1:
            queries.append('%s %s season %s' % (show_title, _ordinal(s_int), ep_zfill))
        if best_romaji:
            queries.append('%s S%sE%s' % (best_romaji, ss_zfill, ep_zfill))
            queries.append('%s %s' % (best_romaji, abs_zfill))
            queries.append('%s batch' % best_romaji)

        # AniDB id maps to Season 1's entry for multi-cour shows (same caveat as
        # animetosho.py), so only use the per-anime listing for season 1.
        use_listing = bool(anidb_id) and not (alt_season and s_int > 1)
        target_anime_id = _resolve_anime_id(self._request, anidb_id) if use_listing else None

        raw_combined = []
        lock = _threading.Lock()
        semaphore = _threading.Semaphore(_MAX_CONCURRENT)

        def run(fn, *args):
            with semaphore:
                items = fn(self._request, *args)
            with lock:
                raw_combined.extend(items)

        jobs = [(_search, (q,)) for q in queries]
        if target_anime_id:
            jobs.append((_anime_releases, (target_anime_id,)))

        threads = []
        for i, (fn, args) in enumerate(jobs):
            t = _threading.Thread(target=run, args=(fn,) + args)
            threads.append(t)
            t.start()
            if i < len(jobs) - 1:
                _time.sleep(_STAGGER_MS / 1000.0)
        for t in threads:
            t.join()

        # ── Scope to the right season ────────────────────────────────────
        # Text search spans every season of a franchise and the shared anime
        # classifier is lenient about season, so pin the anime entry here.
        # No AniDB-resolved entry (multi-cour / unknown): take the entry most
        # of the explicitly-season-marked rows belong to.
        if not target_anime_id:
            counts = {}
            for item in raw_combined:
                if item.get('anime_id') and s_int in _title_seasons(item['title']):
                    counts[item['anime_id']] = counts.get(item['anime_id'], 0) + 1
            if counts:
                best = max(counts, key=counts.get)
                if counts[best] >= 3:
                    target_anime_id = best

        # Trakt-flat numbering is still accepted when the release spells it out.
        orig_s = str(simple_info.get('season_number', '') or '')
        orig_e = str(simple_info.get('episode_number', '') or '')
        flat_rx = None
        if (alt_season and alt_episode and orig_s.isdigit() and orig_e.isdigit()
                and (orig_s, orig_e) != (season, episode)):
            flat_rx = re.compile(r'(?<![a-z0-9])s0*%s\s*e0*%s(?![0-9])' % (orig_s, orig_e), re.I)

        seen_hashes = set()
        results = []
        for item in raw_combined:
            h = item['hash']
            if h in seen_hashes:
                continue
            seen_hashes.add(h)

            if target_anime_id and item.get('anime_id') and item['anime_id'] != target_anime_id:
                continue

            rt = item['title']
            seasons = _title_seasons(rt)
            if seasons and s_int not in seasons and not (flat_rx and flat_rx.search(rt)):
                continue
            tags_rt = core.source_utils.clean_tags(rt)
            pkg = core.source_utils.anime_filter_sources(
                tags_rt, simple_info, ep_zfill, ss_zfill, abs_zfill
            )
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

    # ── Legacy _search_request kept for movie() path via core ────────────

    def _search_request(self, url, query):
        if not query:
            return []
        if isinstance(query, bytes):
            query = query.decode('utf-8')
        data = _get(self._request, '/search/torrents', {'q': query, 'limit': _PAGE_LIMIT})
        if data is None:
            self._request.exc_msg = ''
            return None
        return _parse_results(data.get('results'))

    def _soup_filter(self, response):
        return response

    def _title_filter(self, el):
        return el.get('title', '')

    def _info(self, el, url, torrent):
        torrent['hash']  = el.get('hash', '')
        torrent['size']  = el.get('size', 0)
        torrent['seeds'] = el.get('seeds', 0)
        return torrent
