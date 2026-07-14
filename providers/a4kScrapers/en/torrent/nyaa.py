# -*- coding: utf-8 -*-
"""
Nyaa scraper — Otaku-style OR queries with per-request throttling.

Uses the same OR query format as Otaku Testing 5.2.x:
  Primary:  show "- 01"|"S01E01"  (sorted by downloads)
  Batch:    show "Batch"|"Complete Series"|... (richer terms for finished shows)
  Alias:    romaji_title "- 01"|"S01E01"
  Fallback: primary query with no sort — different Nyaa relevance result set

A module-level lock spaces requests ≥ _NYAA_MIN_GAP seconds apart to
avoid triggering Cloudflare's rate-limiter.  Otaku avoids 429s by making
fewer total requests via OR syntax — we do the same here.

Patch: Uses alternative_season / alternative_episode from Seren's anime
       enrichment to build correct queries for multi-cour anime.
"""

import re
import threading as _threading
import time as _time

from providerModules.a4kScrapers import core
from providerModules.a4kScrapers import scraper_health as _health
from providerModules.a4kScrapers.request import _circuit_is_open as _cb_open

_SCRAPER_NAME   = 'nyaa'
_SCRAPER_DOMAIN = 'https://nyaa.si'

# Throttle: space all Nyaa requests from this process ≥ 1.0 s apart
_nyaa_lock              = _threading.Lock()
_nyaa_last_request_time = [0.0]
_NYAA_MIN_GAP           = 1.0   # seconds

# Optional Google-translate mirror (mirrors Otaku's provider.nyaaalt toggle)
_BASE_URL = 'https://nyaa.si'


# ── Anime fansub fallback helpers ────────────────────────────────────────
def _clean_for_match(title):
    """Lowercase + strip non-alphanumeric to spaces (like source_utils.clean_title)."""
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
    """Fallback classifier for fansub releases: '[Group] Title - 02 (1080p)'.

    Returns True if *raw_title* contains a known show title AND a target
    episode number in a position consistent with fansub naming (i.e. after
    the title, not inside a resolution like 1080).
    """
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
            # Match standalone episode number (not part of '1080', '264', etc.)
            if re.search(r'(?:^|\s)0*' + re.escape(ep_int) + r'(?:\s|v\d|$)', remainder):
                return True
    return False


def _throttled_get(request, url):
    """Rate-limited HTTP GET for Nyaa (shared across all query threads)."""
    with _nyaa_lock:
        elapsed = _time.time() - _nyaa_last_request_time[0]
        if elapsed < _NYAA_MIN_GAP:
            _time.sleep(_NYAA_MIN_GAP - elapsed)
        _nyaa_last_request_time[0] = _time.time()
    return request.get(url)


def _fetch(request, query, sort='downloads'):
    """Fire one Nyaa search and return parsed torrent dicts."""
    params = 'f=0&c=1_0&q=%s' % core.quote_plus(query)
    if sort:
        params += '&s=%s&o=desc' % sort
    url = _BASE_URL + '/?' + params
    try:
        resp = _throttled_get(request, url)
        if resp.status_code != 200:
            core.tools.log(
                'a4kScrapers.nyaa: %s response %d' % (url[:80], resp.status_code), 'notice')
            return []
        html = resp.text
    except Exception:
        return []

    results = []
    # Parse table rows: danger = remake/banned, default = normal, success = trusted
    rows = re.findall(
        r'<tr\s+class="(?:danger|default|success)"[^>]*>(.*?)</tr>',
        html, re.DOTALL)
    for row in rows:
        try:
            # Title — second <a> without class attribute
            links = re.findall(r'<a(?:\s+[^>]*)?>([^<]+)</a>', row)
            title_candidates = [l.strip() for l in links if l.strip() and len(l.strip()) > 4]
            if not title_candidates:
                continue
            # The torrent title is typically the longest anchor text
            title = max(title_candidates, key=len)

            # Magnet
            mm = re.search(r'href="(magnet:\?[^"]+)"', row)
            if not mm:
                continue
            magnet = mm.group(1)

            # Hash
            hm = re.search(r'btih:([a-fA-F0-9]{40})', magnet)
            if not hm:
                continue
            hash_val = hm.group(1).lower()

            # Size — first plain-text td.text-center (Nyaa date now uses <span>, reducing matches from 5 to 4)
            tds = re.findall(r'<td[^>]*class="text-center"[^>]*>([^<]+)</td>', row)
            size_text = tds[0].strip() if tds else ''
            try:
                size = core.source_utils.de_string_size(size_text.replace('i', ''))
            except Exception:
                size = 0

            # Seeders — 3rd-from-last td
            seeds = int(tds[-3]) if len(tds) >= 3 and tds[-3].isdigit() else 0

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


def _ordinal(n):
    """Return ordinal string: 1->'1st', 2->'2nd', 3->'3rd', etc."""
    n = int(n)
    if 11 <= (n % 100) <= 13:
        return '%dth' % n
    return '%d%s' % (n, {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th'))


class sources(core.DefaultSources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, **kwargs)

    # ── Public entry point ────────────────────────────────────────────────

    def episode(self, simple_info, all_info,
                auto_query=True, query_seasons=True, query_show_packs=True, **kwargs):
        self.query_type = 'episode'
        start = _time.time()

        if _health.is_blacklisted(_SCRAPER_NAME):
            core.tools.log('a4kScrapers.episode.nyaa: 0', 'notice')
            core.tools.log('a4kScrapers.episode.nyaa: took 0 ms', 'notice')
            return []

        scraper = self._get_scraper(simple_info.get('show_title', ''))
        if isinstance(scraper, core.NoResultsScraper):
            core.tools.log('a4kScrapers.episode.nyaa: 0', 'notice')
            core.tools.log('a4kScrapers.episode.nyaa: took 0 ms', 'notice')
            return []

        results = self._fetch_episode(simple_info)

        _health.record(_SCRAPER_NAME, had_http_error=_cb_open(_SCRAPER_DOMAIN))
        elapsed = int((_time.time() - start) * 1000)
        core.tools.log('a4kScrapers.episode.nyaa: %d' % len(results), 'notice')
        core.tools.log('a4kScrapers.episode.nyaa: took %d ms' % elapsed, 'notice')
        return results

    def movie(self, title, year, imdb_id=None, auto_query=True, **kwargs):
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
        # Seren's anime enrichment computes cour-corrected numbers and
        # stores them in alternative_season / alternative_episode.
        # e.g. Trakt S01E30 → anime S02E02 for Frieren 2nd cour.
        alt_season  = str(simple_info.get('alternative_season', '') or '')
        alt_episode = str(simple_info.get('alternative_episode', '') or '')

        if is_anime and alt_season and alt_episode:
            season  = alt_season
            episode = alt_episode
        else:
            season  = str(simple_info.get('season_number', '1'))
            episode = str(simple_info.get('episode_number', '1'))

        ep_zfill   = episode.zfill(2)
        ss_zfill   = season.zfill(2)

        # Absolute number for fansub queries
        abs_num = str(simple_info.get('absolute_number', '') or '')
        if not abs_num:
            abs_num = episode
        abs_zfill = str(abs_num).zfill(2)

        # Build filter functions
        ep_filter = core.source_utils.get_filter_single_episode_fn(simple_info)
        s_filter  = core.source_utils.get_filter_season_pack_fn(simple_info)
        sh_filter = core.source_utils.get_filter_show_pack_fn(simple_info)

        # ── Build OR queries (Otaku pattern) ──────────────────────────────
        # Primary: fansub dash AND standard SxxExx — Nyaa supports OR via |
        if abs_zfill != ep_zfill:
            ep_or = '"- %s"|"- %s"|"S%sE%s"' % (abs_zfill, ep_zfill, ss_zfill, ep_zfill)
        else:
            ep_or = '"- %s"|"S%sE%s"' % (ep_zfill, ss_zfill, ep_zfill)

        primary_query = '%s %s' % (show_title, ep_or)

        # Batch query (sorted by seeders) — richer episode-range terms for finished shows
        batch_parts = ['"Batch"', '"Complete Series"', '"Season %s"' % season, '"S%s"' % ss_zfill]
        status = str(simple_info.get('status', '') or '').upper()
        show_finished = status in ('FINISHED', 'FINISHED AIRING', 'ENDED', 'COMPLETED')
        if is_anime and show_finished:
            batch_parts.extend([
                '"01-%s"' % ep_zfill,
                '"01~%s"' % ep_zfill,
                '"E%s"' % ep_zfill,
                '"Episode %s"' % ep_zfill,
                '"%s-%s"' % (ss_zfill, ep_zfill),
                '"%s~%s"' % (ss_zfill, ep_zfill),
            ])
        batch_query = '%s %s' % (show_title, '|'.join(batch_parts))

        # Season pack query
        season_query = '%s "Season %s"|"S%s"' % (show_title, season, ss_zfill)

        # ── Build alias queries ──────────────────────────────────────────
        # For anime: prefer an ASCII romaji title (e.g. "Sousou no Frieren")
        # over accented variants ("Sōsō no Furīren" → "soso no furiren"
        # after clean_title, which nobody indexes).  Also skip aliases that
        # share the same first word as the English show_title.
        alias_queries = []
        best_romaji = None
        show_first_word = show_title.split()[0] if show_title else ''

        # Pass 1: ASCII alias with a different first word (ideal romaji)
        for a in aliases:
            if not _is_ascii(a):
                continue
            ca = core.source_utils.clean_title(a)
            if ca and ca.lower() != show_title.lower() and len(ca) > 4:
                ca_first = ca.split()[0] if ca else ''
                if is_anime and ca_first == show_first_word:
                    continue
                best_romaji = ca
                alias_queries.append(('%s %s' % (ca, ep_or), 'downloads'))
                break
        # Pass 2 fallback: any alias with a different first word
        if not best_romaji:
            for a in aliases:
                ca = core.source_utils.clean_title(a)
                if ca and ca.lower() != show_title.lower() and len(ca) > 4:
                    ca_first = ca.split()[0] if ca else ''
                    if is_anime and ca_first == show_first_word:
                        continue
                    best_romaji = ca
                    alias_queries.append(('%s %s' % (ca, ep_or), 'downloads'))
                    break

        # For anime with cour correction: add season-qualified title queries
        # e.g. "sousou no frieren season 2" or "sousou no frieren 2nd season"
        if is_anime and alt_season and int(alt_season) > 1:
            s_num = int(alt_season)
            base_titles = [best_romaji] if best_romaji else []
            base_titles.append(show_title)
            for bt in base_titles:
                sq1 = '%s season %d %s' % (bt, s_num, ep_or)
                sq2 = '%s %s season %s' % (bt, _ordinal(s_num), ep_or)
                alias_queries.append((sq1, 'downloads'))
                alias_queries.append((sq2, 'downloads'))
            # Also search season-qualified batch
            if best_romaji:
                alias_queries.append(('%s season %d "Batch"|"Complete Series"' % (best_romaji, s_num), 'seeders'))

        # ── Fire queries sequentially (throttled) — Otaku-style low volume ─
        # Nyaa's Cloudflare rate-limits burst traffic; sequential + throttle
        # beats parallel for reliability on this site.
        all_raw = []
        queries = [(primary_query, 'downloads'), (batch_query, 'seeders'),
                   (season_query, 'seeders')]
        queries.extend(alias_queries)

        # Show-level broad search (Otaku's get_show_sources pattern):
        # just the title, no episode filter — catches batch packs, complete
        # series, and releases without episode numbers in the title.
        if is_anime and best_romaji:
            queries.append((best_romaji, 'downloads'))
        queries.append((show_title, 'downloads'))

        # Unsorted fallback — Nyaa's relevance ranking returns a different result
        # set than sorted queries, often surfacing releases the sorted view misses.
        queries.append((primary_query, None))

        for q, sort in queries:
            if self._cancellation_token.is_cancellation_requested:
                break
            items = _fetch(self._request, q, sort)
            core.tools.log(
                'a4kScrapers.nyaa: query "%s" → %d' % (q[:70], len(items)), 'notice')
            all_raw.extend(items)

        # ── Deduplicate and classify ────────────────────────────────────────
        seen_hashes = set()
        results     = []
        for item in all_raw:
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

    # ── Legacy _parse_seeds (kept for any core movie path) ───────────────

    def _parse_seeds(self, row):
        return self.genericScraper._parse_number(row, -3)