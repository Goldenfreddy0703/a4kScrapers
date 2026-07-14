# -*- coding: utf-8 -*-
"""
NekoBT scraper — uses NekoBT's structured media/episode API (Otaku approach).

Instead of text-search queries (which trigger Cloudflare 429s), we:
  1. /api/v1/media/search?query=<title>          → resolve media_id
  2. /api/v1/media/<media_id>                    → resolve episode_ids
  3. /api/v1/torrents/search?media_id=&episode_ids= → get torrents (server-side filtered)
  4. Fallback: /api/v1/torrents/search?query=    → title text search if no media_id

This matches Otaku Testing 5.2.x's nekobt.py architecture.

Patch: Uses alternative_season / alternative_episode from Seren's anime
       enrichment so the API lookups use the correct cour-aware S/E numbers.
"""

import re
import threading as _threading
import time as _time

from providerModules.a4kScrapers import core
from providerModules.a4kScrapers import scraper_health as _health
from providerModules.a4kScrapers.request import _circuit_is_open as _cb_open

_SCRAPER_NAME   = 'nekobt'
_SCRAPER_DOMAIN = 'https://nekobt.to'

_BASE = 'https://nekobt.to/api/v1'
_TIMEOUT = 20
_GROUP_PARAMS = {
    'group_secondary': 'true',
    'group_parents': 'true',
    'uploader_contributions': 'true',
}


# ── Anime fansub fallback helpers ────────────────────────────────────────
def _clean_for_match(title):
    """Lowercase + strip non-alphanumeric to spaces."""
    t = title.lower()
    t = re.sub(r'[^a-z0-9]+', ' ', t)
    return t.strip()


def _is_ascii(s):
    """Return True if string contains only ASCII characters."""
    try:
        s.encode('ascii')
        return True
    except (UnicodeEncodeError, UnicodeDecodeError):
        return False


def _anime_fansub_match(raw_title, show_title_variants, target_eps):
    """Fallback classifier for fansub releases: '[Group] Title - 02 (1080p)'."""
    rt = _clean_for_match(raw_title)
    for title in show_title_variants:
        if not title or len(title) < 4:
            continue
        title_c = _clean_for_match(title)
        if title_c not in rt:
            continue
        remainder = rt[rt.index(title_c) + len(title_c):]
        for ep in target_eps:
            if not ep or ep in ('', '0', '00'):
                continue
            ep_int = str(int(ep))
            if re.search(r'(?:^|\s)0*' + re.escape(ep_int) + r'(?:\s|v\d|$)', remainder):
                return True
    return False


def _get(request, path, params=None):
    """Fire a GET against the NekoBT API; return parsed JSON data dict or None."""
    try:
        url = _BASE + path
        if params:
            qs = '&'.join('%s=%s' % (k, core.quote_plus(str(v))) for k, v in params.items())
            url = url + '?' + qs
        resp = request.get(url)
        if resp.status_code != 200:
            return None
        data = core.json.loads(resp.text)
        if data.get('error') or 'data' not in data:
            return None
        return data['data']
    except Exception:
        return None


def _resolve_media_id(request, titles):
    """Search /media/search for each title; return the first match's media_id."""
    for title in titles:
        if not title or len(title) < 3:
            continue
        data = _get(request, '/media/search', {'query': title, 'limit': 10})
        if not data:
            continue
        results = data.get('results', [])
        if results:
            media_id = results[0].get('id')
            if media_id:
                core.tools.log('a4kScrapers.nekobt: media_id=%s via title="%s"' % (media_id, title), 'notice')
                return media_id
    return None


def _resolve_episode_ids(request, media_id, season, episode):
    """Fetch /media/<id> episode list and return matching episode IDs."""
    data = _get(request, '/media/%s' % media_id)
    if not data:
        return []
    episodes = data.get('episodes', [])
    ep_int = int(episode) if str(episode).isdigit() else 1
    s_int  = int(season)  if str(season).isdigit()  else 1

    # Strategy 1: exact season + episode
    matched = [str(ep['id']) for ep in episodes
               if ep.get('season') == s_int and ep.get('episode') == ep_int]
    # Strategy 2: season=0 absolute numbering
    if not matched:
        matched = [str(ep['id']) for ep in episodes
                   if ep.get('season') == 0 and ep.get('episode') == ep_int]
    # Strategy 3: episode number only
    if not matched:
        matched = [str(ep['id']) for ep in episodes if ep.get('episode') == ep_int]

    core.tools.log('a4kScrapers.nekobt: episode_ids=%s for S%sE%s' % (matched, season, episode), 'notice')
    return matched


def _search_torrents(request, params):
    """Call /torrents/search with merged group params; return raw result list."""
    merged = dict(params)
    merged.update(_GROUP_PARAMS)
    data = _get(request, '/torrents/search', merged)
    if data:
        return data.get('results', [])
    return []


def _parse_results(raw_list):
    """Convert raw API results to a4kScrapers torrent dicts."""
    items = []
    for item in raw_list:
        try:
            infohash = item.get('infohash', '')
            magnet   = item.get('magnet') or item.get('private_magnet', '')
            if not infohash and magnet:
                m = re.search(r'btih:([a-fA-F0-9]{40})', magnet)
                infohash = m.group(1) if m else ''
            if not infohash:
                continue
            title = item.get('title', '')
            if not title:
                continue
            filesize = int(item.get('filesize', 0) or 0)
            seeders  = int(item.get('seeders',  0) or 0)
            items.append({
                'title':  title,
                'hash':   infohash.lower(),
                'size':   filesize / 1024 / 1024 if filesize else 0,
                'seeds':  seeders,
                'magnet': magnet or ('magnet:?xt=urn:btih:%s&' % infohash.lower()),
            })
        except Exception:
            continue
    return items


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

        # Initialise the request object (find_url, etc.)
        scraper = self._get_scraper(simple_info.get('show_title', ''))
        if isinstance(scraper, core.NoResultsScraper):
            core.tools.log('a4kScrapers.episode.nekobt: 0', 'notice')
            core.tools.log('a4kScrapers.episode.nekobt: took 0 ms', 'notice')
            return []

        results = self._fetch_episode(simple_info)

        _health.record(_SCRAPER_NAME, had_http_error=_cb_open(_SCRAPER_DOMAIN))
        elapsed = int((_time.time() - start) * 1000)
        core.tools.log('a4kScrapers.episode.nekobt: %d' % len(results), 'notice')
        core.tools.log('a4kScrapers.episode.nekobt: took %d ms' % elapsed, 'notice')
        return results

    def movie(self, title, year, imdb_id=None, auto_query=True, **kwargs):
        """Movies: fall back to core title-search behaviour."""
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
        is_anime   = simple_info.get('isanime', False)

        # ── Anime cour correction: prefer alternative season/episode ─────
        # NekoBT uses anime-native season numbering (like MAL/AniList), so
        # Trakt's flat S01E30 won't match.  The cour-corrected S02E02 will.
        alt_season  = str(simple_info.get('alternative_season', '') or '')
        alt_episode = str(simple_info.get('alternative_episode', '') or '')

        if is_anime and alt_season and alt_episode:
            season  = alt_season
            episode = alt_episode
        else:
            season  = simple_info.get('season_number', '1')
            episode = simple_info.get('episode_number', '1')

        # Build candidate titles: cleaned show + up to 4 unique aliases.
        # Prefer ASCII aliases — accented titles like "Sōsō no Furīren"
        # clean to "soso no furiren" which NekoBT's API won't match.
        titles = [show_title]
        seen   = {show_title.lower()}
        ascii_aliases = []
        other_aliases = []
        for a in aliases:
            ca = core.source_utils.clean_title(a)
            if ca and ca.lower() not in seen and len(ca) > 2:
                if _is_ascii(a):
                    ascii_aliases.append(ca)
                else:
                    other_aliases.append(ca)
                seen.add(ca.lower())
        for ca in (ascii_aliases + other_aliases)[:4]:
            titles.append(ca)

        # Build filter functions for package-type classification
        ep_filter  = core.source_utils.get_filter_single_episode_fn(simple_info)
        s_filter   = core.source_utils.get_filter_season_pack_fn(simple_info)
        sh_filter  = core.source_utils.get_filter_show_pack_fn(simple_info)

        # ── Step 1: resolve media_id ─────────────────────────────────────
        media_id   = _resolve_media_id(self._request, titles)

        # ── Step 2: resolve episode_ids (with corrected season/episode) ──
        episode_ids = []
        if media_id:
            episode_ids = _resolve_episode_ids(self._request, media_id, season, episode)

            # Fallback: if cour-corrected lookup failed, try with original
            # Trakt numbers in case the media uses flat numbering
            if not episode_ids and alt_season and alt_episode:
                orig_season  = simple_info.get('season_number', '1')
                orig_episode = simple_info.get('episode_number', '1')
                episode_ids = _resolve_episode_ids(
                    self._request, media_id, orig_season, orig_episode)

        # ── Step 3: build search tasks ───────────────────────────────────
        ep_zfill = str(episode).zfill(2)
        ss_zfill = str(season).zfill(2)
        ep_hint  = 'S%sE%s' % (ss_zfill, ep_zfill)

        abs_num = str(simple_info.get('absolute_number', '') or '')
        abs_zfill = str(abs_num).zfill(2) if abs_num else ep_zfill

        tasks = []
        if media_id:
            if episode_ids:
                tasks.append({'media_id': media_id,
                              'episode_ids': ','.join(episode_ids),
                              'episode_match_any': 'true',
                              'sort_by': 'seeders', 'limit': 100})
            tasks.append({'media_id': media_id, 'query': ep_hint,
                          'sort_by': 'seeders', 'limit': 100})
            tasks.append({'media_id': media_id, 'query': 'batch',
                          'sort_by': 'seeders', 'limit': 100})
            if episode_ids:
                tasks.append({'media_id': media_id,
                              'episode_ids': ','.join(episode_ids),
                              'episode_match_any': 'true',
                              'sort_by': 'latest', 'limit': 100})
            # Broad media_id search (Otaku pattern): no episode filter,
            # no query hint — catches batch packs, complete series, and
            # releases without episode-level metadata in NekoBT's DB.
            tasks.append({'media_id': media_id,
                          'sort_by': 'seeders', 'limit': 100})
        else:
            # No media_id — text search fallback
            for t in titles[:3]:
                tasks.append({'query': '%s %s' % (t, ep_hint),
                              'sort_by': 'seeders', 'limit': 50})
                tasks.append({'query': t, 'sort_by': 'seeders', 'limit': 50})

        # ── Step 4: execute tasks in parallel ────────────────────────────
        raw_combined = []
        lock = _threading.Lock()

        def run(params):
            items = _search_torrents(self._request, params)
            with lock:
                raw_combined.extend(items)

        threads = [_threading.Thread(target=run, args=(p,)) for p in tasks]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # ── Step 5: parse, deduplicate, classify ─────────────────────────
        seen_hashes = set()
        results = []
        for item in _parse_results(raw_combined):
            h = item['hash']
            if h in seen_hashes:
                continue
            seen_hashes.add(h)

            rt = item['title']
            # Pass clean_tags() output (dashes preserved) to anime classifier;
            # fully-normalised cleaned_rt is still needed for non-anime path.
            tags_rt    = core.source_utils.clean_tags(rt)
            cleaned_rt = core.source_utils.clean_title(tags_rt)
            pkg = None

            if is_anime:
                # Otaku-style regex classifier: extracts episode/season/part
                # from the title and returns the correct package type, or None
                # to discard (confirmed wrong episode/season/part).
                pkg = core.source_utils.anime_filter_sources(
                    tags_rt, simple_info, ep_zfill, ss_zfill, abs_zfill
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

    # ── Legacy _search_request kept for movie() path via core ────────────

    def _search_request(self, url, query):
        if not query:
            return []
        if isinstance(query, bytes):
            query = query.decode('utf-8')
        params = {'query': query, 'sort_by': 'seeders', 'limit': 100}
        params.update(_GROUP_PARAMS)
        qs = '&'.join('%s=%s' % (k, core.quote_plus(str(v))) for k, v in params.items())
        request_url = url.base + url.search + '?' + qs
        response = self._request.get(request_url)
        if response.status_code != 200:
            self._request.exc_msg = ''
            return None
        try:
            data = core.json.loads(response.text)
        except Exception:
            return []
        if data.get('error') or 'data' not in data:
            return []
        return _parse_results(data['data'].get('results', []))

    def _soup_filter(self, response):
        return response

    def _title_filter(self, el):
        return el.get('title', '')

    def _info(self, el, url, torrent):
        torrent['hash']  = el.get('hash', '')
        torrent['size']  = el.get('size', 0)
        torrent['seeds'] = el.get('seeds', 0)
        return torrent
