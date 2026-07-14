# -*- coding: utf-8 -*-
"""
AnimeTosho scraper — Otaku-style Sphinx search with AniDB server-side filtering.

Query pattern (Sphinx full-text with qx=1):
  Primary:  show "- 01"|"S01E01"           sort=downloads + aids=<anidb_id>
  Batch:    show "Batch"|"Complete"         sort=seeders   + aids=<anidb_id>
            (episode-range formats 01-N only for completed shows — Otaku pattern)
  Alias:    romaji_title "- 01"|"S01E01"    sort=downloads + aids=<anidb_id>
  Broad:    show title only                 sort=seeders   + aids=<anidb_id>
  Fallback: primary query, no sort params   (Sphinx relevance ranking — different result set)
"""

import re
import base64
import threading as _threading
import time as _time

from providerModules.a4kScrapers import core

_BASE_URL = 'https://animetosho.org'
_SEARCH   = '/search'

# ── Throttle: max 3 concurrent requests to avoid rate-limiting ────────────
_MAX_CONCURRENT = 3
_STAGGER_MS     = 150  # ms between thread launches


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
    """Fallback classifier for fansub releases: '[Group] Title - 02 (1080p)'.

    Returns True if *raw_title* contains a known show title AND a target
    episode number in a position consistent with fansub naming.
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
            if re.search(r'(?:^|\s)0*' + re.escape(ep_int) + r'(?:\s|v\d|$)', remainder):
                return True
    return False


def _ordinal(n):
    """Return ordinal string: 1->'1st', 2->'2nd', 3->'3rd', etc."""
    n = int(n)
    if 11 <= (n % 100) <= 13:
        return '%dth' % n
    return '%d%s' % (n, {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th'))


def _sphinx_clean(query):
    """Escape Sphinx special characters (mirrors Otaku's _sphinx_clean)."""
    return re.sub(r'[!@#$%^&*()\[\]{};:\'\",<>/?\\|`~]', ' ', query).strip()


def _fetch(request, query, anidb_id=None, sort='downloads'):
    """Fire one Sphinx search and return a list of parsed torrent dicts.

    Uses BeautifulSoup for HTML parsing (resilient to structure changes).
    Passes aids=<anidb_id> for server-side series filtering when available.
    sort: 'downloads', 'seeders', or None (no sort — Sphinx relevance ranking).
    """
    # Build URL; omit sort params when sort=None so Sphinx uses relevance ranking
    params_str = 'q=%s&qx=1' % core.quote_plus(query)
    if sort:
        params_str += '&s=%s&o=desc' % sort
    if anidb_id:
        params_str += '&aids=%s' % str(anidb_id)
    url = _BASE_URL + _SEARCH + '?' + params_str

    try:
        resp = request.get(url)
        if resp.status_code != 200:
            return []
        html = resp.text
    except Exception:
        return []

    # ── BeautifulSoup parser (replaces fragile regex) ─────────────────────
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')
    except ImportError:
        # Fallback: use html.parser directly if bs4 not available
        return _fetch_regex_fallback(html)

    content_div = soup.find('div', id='content')
    if not content_div:
        return []

    entries = content_div.find_all('div', class_='home_list_entry')
    if not entries:
        return []

    results = []
    for entry in entries:
        try:
            # Title — from the link div's anchor
            link_div = entry.find('div', class_='link')
            if not link_div:
                continue
            title_a = link_div.find('a')
            if not title_a:
                continue
            title = title_a.get_text(strip=True)
            if not title:
                title = title_a.get('title', '')
            if not title:
                continue

            # Magnet — find href starting with 'magnet:'
            magnet_a = entry.find('a', href=re.compile(r'^magnet:\?'))
            if not magnet_a:
                continue
            magnet = magnet_a.get('href', '')
            if not magnet:
                continue

            # Hash — extract from magnet link
            hm = re.search(r'btih:([a-fA-F0-9]{40})', magnet)
            if hm:
                hash_val = hm.group(1).lower()
            else:
                hm32 = re.search(r'btih:([A-Za-z2-7]{32})', magnet)
                if hm32:
                    try:
                        hash_val = base64.b16encode(
                            base64.b32decode(hm32.group(1).upper())
                        ).decode().lower()
                    except Exception:
                        continue
                else:
                    # Try torrent download link hash extraction
                    dl_a = entry.find('a', class_='dllink')
                    if dl_a:
                        dl_href = dl_a.get('href', '')
                        hm_dl = re.search(r'/storage/torrent/([a-fA-F0-9]{40})', dl_href)
                        if hm_dl:
                            hash_val = hm_dl.group(1).lower()
                        else:
                            continue
                    else:
                        continue

            # Size — from the size div
            size_div = entry.find('div', class_='size')
            size_text = size_div.get_text(strip=True) if size_div else ''
            try:
                size = core.source_utils.de_string_size(size_text)
            except Exception:
                size = 0

            # Seeds — from Seeders tooltip span
            seeds = 0
            seed_span = entry.find('span', title=re.compile(r'Seeders'))
            if seed_span:
                seed_match = re.match(r'Seeders:\s*(\d+)', seed_span.get('title', ''))
                if seed_match:
                    seeds = int(seed_match.group(1))

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


def _fetch_regex_fallback(html):
    """Regex-based HTML parser — only used if BeautifulSoup import fails."""
    entries = re.findall(
        r'<div\s+class="home_list_entry"[^>]*>(.*?)(?=<div\s+class="home_list_entry"|<div\s+id="page_nav"|$)',
        html, re.DOTALL,
    )
    if not entries:
        entries = re.split(r'<div\s+class="home_list_entry"[^>]*>', html)[1:]

    results = []
    for entry in entries:
        try:
            tm = (re.search(r'class="link"[^>]*>.*?<a[^>]*>([^<]+)</a>', entry, re.DOTALL)
                  or re.search(r'<a[^>]*title="([^"]+)"', entry))
            if not tm:
                continue
            title = tm.group(1).strip()
            mm = re.search(r'href="(magnet:\?[^"]+)"', entry)
            if not mm:
                continue
            magnet = mm.group(1)
            hm = re.search(r'btih:([a-fA-F0-9]{40})', magnet)
            if hm:
                hash_val = hm.group(1).lower()
            else:
                hm32 = re.search(r'btih:([A-Za-z2-7]{32})', magnet)
                if hm32:
                    try:
                        hash_val = base64.b16encode(
                            base64.b32decode(hm32.group(1).upper())
                        ).decode().lower()
                    except Exception:
                        continue
                else:
                    continue
            sm = re.search(r'class="size"[^>]*>([^<]+)<', entry)
            size_text = sm.group(1).strip() if sm else ''
            try:
                size = core.source_utils.de_string_size(size_text)
            except Exception:
                size = 0
            seedm = re.search(r'Seeders:\s*(\d+)', entry)
            seeds = int(seedm.group(1)) if seedm else 0
            results.append({
                'title': title, 'hash': hash_val, 'size': size or 0,
                'seeds': seeds, 'magnet': magnet,
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

        # Initialise request object
        scraper = self._get_scraper(simple_info.get('show_title', ''))
        if isinstance(scraper, core.NoResultsScraper):
            core.tools.log('a4kScrapers.episode.animetosho: 0', 'notice')
            core.tools.log('a4kScrapers.episode.animetosho: took 0 ms', 'notice')
            return []

        results = self._fetch_episode(simple_info)

        elapsed = int((_time.time() - start) * 1000)
        core.tools.log('a4kScrapers.episode.animetosho: %d' % len(results), 'notice')
        core.tools.log('a4kScrapers.episode.animetosho: took %d ms' % elapsed, 'notice')
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

        # ── AniDB ID for server-side filtering ────────────────────────────
        anidb_id = simple_info.get('anidb_id')

        # ── Anime cour correction: prefer alternative season/episode ─────
        alt_season  = str(simple_info.get('alternative_season', '') or '')
        alt_episode = str(simple_info.get('alternative_episode', '') or '')

        if is_anime and alt_season and alt_episode:
            season  = alt_season
            episode = alt_episode
        else:
            season  = str(simple_info.get('season_number', '1'))
            episode = str(simple_info.get('episode_number', '1'))

        # Determine effective AniDB ID for aids= filter.
        # anidb_id from AniList/MAL maps to Season 1's AniDB entry.
        # For multi-cour shows (alt_season > 1) Season 2+ content lives under
        # a different AniDB ID — using S1's aids= returns only S1 results.
        # Skip aids= for alt_season > 1 and rely on full-text search instead.
        use_anidb_id = anidb_id
        if anidb_id and alt_season and int(alt_season) > 1:
            use_anidb_id = None
            core.tools.log(
                'a4kScrapers.animetosho: alt_season=%s>1, skipping aids=%s (full-text mode)' % (alt_season, anidb_id),
                'notice'
            )
        elif anidb_id:
            core.tools.log(
                'a4kScrapers.animetosho: using AniDB aids=%s for server-side filter' % anidb_id,
                'notice'
            )

        ep_zfill   = episode.zfill(2)
        ss_zfill   = season.zfill(2)

        # Absolute number (fansub style)
        abs_num = str(simple_info.get('absolute_number', '') or '')
        if not abs_num or abs_num == '':
            abs_num = episode

        # Build filter functions
        ep_filter = core.source_utils.get_filter_single_episode_fn(simple_info)
        s_filter  = core.source_utils.get_filter_season_pack_fn(simple_info)
        sh_filter = core.source_utils.get_filter_show_pack_fn(simple_info)

        # ── Build OR queries (Otaku pattern) ──────────────────────────────
        abs_zfill = str(abs_num).zfill(2)
        if use_anidb_id:
            # Otaku parity: aids= already scopes the series; use ep_zfill only.
            # Including abs_zfill in the OR term adds noise against the AniDB-
            # filtered Sphinx index and causes 0-result episode queries.
            ep_or = '"- %s"|"S%sE%s"' % (ep_zfill, ss_zfill, ep_zfill)
        elif abs_zfill != ep_zfill:
            # No AniDB anchor — keep both numbers for fansub-style absolute ep.
            ep_or = '"- %s"|"- %s"|"S%sE%s"' % (abs_zfill, ep_zfill, ss_zfill, ep_zfill)
        else:
            # abs == ep (season 1, no cour correction needed)
            ep_or = '"- %s"|"S%sE%s"' % (ep_zfill, ss_zfill, ep_zfill)

        primary_query = '%s %s' % (_sphinx_clean(show_title), ep_or)

        # Batch/pack query — episode-range formats only for completed shows
        # (Otaku pattern: airing shows won't have 01-02 batches yet)
        batch_parts = ['"Batch"', '"Complete Series"', '"Season %s"' % season]
        status = str(simple_info.get('status', '') or '').upper()
        show_finished = status in ('FINISHED', 'FINISHED AIRING', 'ENDED', 'COMPLETED')
        if is_anime and show_finished:
            batch_parts.extend([
                '"01-%s"' % ep_zfill,
                '"01~%s"' % ep_zfill,
                '"01 - %s"' % ep_zfill,
                '"01 ~ %s"' % ep_zfill,
            ])
        batch_query = '%s %s' % (_sphinx_clean(show_title), '|'.join(batch_parts))

        # Best alias query — prefer ASCII romaji (e.g. "Sousou no Frieren"),
        # skip accented variants and English re-wordings that share the same
        # first word as show_title.
        alias_queries = []
        best_romaji = None
        show_first_word = show_title.split()[0] if show_title else ''

        # Pass 1: ASCII alias with different first word
        for a in aliases:
            if not _is_ascii(a):
                continue
            ca = core.source_utils.clean_title(a)
            if ca and ca.lower() != show_title.lower() and len(ca) > 4:
                ca_first = ca.split()[0] if ca else ''
                if is_anime and ca_first == show_first_word:
                    continue
                best_romaji = ca
                alias_queries.append('%s %s' % (_sphinx_clean(ca), ep_or))
                break
        # Pass 2 fallback: any different first word
        if not best_romaji:
            for a in aliases:
                ca = core.source_utils.clean_title(a)
                if ca and ca.lower() != show_title.lower() and len(ca) > 4:
                    ca_first = ca.split()[0] if ca else ''
                    if is_anime and ca_first == show_first_word:
                        continue
                    best_romaji = ca
                    alias_queries.append('%s %s' % (_sphinx_clean(ca), ep_or))
                    break

        # ── Anime season-qualified queries ────────────────────────────────
        if is_anime and alt_season and int(alt_season) > 1:
            s_num = int(alt_season)
            base_titles = [best_romaji] if best_romaji else []
            base_titles.append(show_title)
            for bt in base_titles[:2]:
                alias_queries.append('%s season %d %s' % (_sphinx_clean(bt), s_num, ep_or))
                alias_queries.append('%s %s season %s' % (_sphinx_clean(bt), _ordinal(s_num), ep_or))

        # ── Fire queries with throttled parallelism ────────────────────────
        # Max 3 concurrent threads with 150ms stagger between launches.
        # Matches Otaku's approach — avoids AnimeTosho rate-limiting.
        all_raw = []
        lock    = _threading.Lock()

        def run(q, sort):
            items = _fetch(self._request, q, anidb_id=use_anidb_id, sort=sort)
            core.tools.log('a4kScrapers.animetosho: query "%s"%s → %d' % (
                q[:60],
                ' (aids=%s)' % use_anidb_id if use_anidb_id else '',
                len(items),
            ), 'notice')
            with lock:
                all_raw.extend(items)

        # (query, sort) tuples — sort: 'downloads', 'seeders', or None (unsorted)
        queries = [
            (primary_query, 'downloads'),
            (batch_query,   'seeders'),
        ]
        for aq in alias_queries:
            queries.append((aq, 'downloads'))

        # Show-level broad search (Otaku's get_show_sources pattern):
        # title only, no episode filter — catches batch packs and complete
        # series.  Sort by seeders so highest-seeded packs surface first.
        if is_anime:
            if best_romaji:
                queries.append((_sphinx_clean(best_romaji), 'seeders'))
            queries.append((_sphinx_clean(show_title), 'seeders'))

        # True unsorted fallback: Sphinx relevance ranking (no s=/o= params)
        # returns a different result set than sorted queries — matches Otaku.
        queries.append((primary_query, None))

        # Throttled launch: max _MAX_CONCURRENT threads, stagger between starts
        semaphore = _threading.Semaphore(_MAX_CONCURRENT)
        threads = []

        def throttled_run(q, sort):
            with semaphore:
                run(q, sort)

        for i, (q, sort) in enumerate(queries):
            t = _threading.Thread(target=throttled_run, args=(q, sort))
            threads.append(t)
            t.start()
            # Stagger launches to avoid burst
            if i < len(queries) - 1:
                _time.sleep(_STAGGER_MS / 1000.0)

        for t in threads:
            t.join()

        # ── Deduplicate and classify ──────────────────────────────────────────
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

    # ── Legacy methods for movie() path via core ──────────────────────────

    def _search_request(self, url, query):
        if not query:
            return []
        if isinstance(query, bytes):
            query = query.decode('utf-8')
        request_url = url.base + (url.search % core.quote_plus(query))
        response = self._request.get(request_url)
        if response.status_code != 200:
            self._request.exc_msg = ''
            return None
        return _fetch(self._request, query)

    def _soup_filter(self, response):
        return response

    def _title_filter(self, el):
        return el.get('title', '')

    def _info(self, el, url, torrent):
        torrent['hash']  = el.get('hash', '')
        torrent['size']  = el.get('size', 0)
        torrent['seeds'] = el.get('seeds', 0)
        return torrent
