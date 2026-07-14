# -*- coding: utf-8 -*-
#
# Meteor scraper — queries Meteor Stremio addon directly.
# Rewritten in Session 3.3.2 from URL-based DefaultSources to direct HTTP,
# matching the comet.py/mediafusion.py pattern.
#
# Root cause of the old 0-results bug: DefaultSources._filter_fn returned
# False for all movies, and the URL-based mechanism doesn't correctly handle
# the Stremio JSON stream format for the episode package types.
# The external_cache meteor_check_cache() already queries this same endpoint
# successfully, confirming the URL works — the issue was purely in the scraper.

import re
from providerModules.a4kScrapers import core
from providerModules.a4kScrapers.source_utils import tools

# ── Package classification ──
_PKG_MULTI_SEASON_RE = re.compile(r's\d{1,2}[\s.\-]+s\d{1,2}', re.I)
_PKG_SHOW_PACK_RE = re.compile(
    r'\b(?:complete[\s._]series|complete[\s._]collection|all[\s._]seasons|'
    r'full[\s._]series|seasons?[\s._]\d|the[\s._]complete[\s._]series)\b', re.I
)
_PKG_SEASON_RE = re.compile(
    r'\b(?:s\d{1,2}|season[\s._]\d{1,2})[\s._]?(?:complete|1080p|720p|480p|bluray|web|$)',
    re.I
)
_PKG_EPISODE_RE = re.compile(r's\d{1,2}[\s._]?e\d{1,2}|\s-\s\d{2,3}\s', re.I)
# Multi-episode range detector — must fire BEFORE _PKG_EPISODE_RE so that
# releases like S01E01-E08, S01E01E02E03, or S01E01.E08 are classified as
# 'season' packs instead of 'single'.  Ported from Umbrella scrape_utils
# check_title() range_regex guard; patterns refined to eliminate false
# positives on genuine single-episode titles (e.g. S01E05.BluRay).
_PKG_RANGE_RE = re.compile(
    r's\d{1,3}e\d{1,3}[-.]e\d{1,3}'                               # S01E01-E08 / S01E01.E08
    r'|s\d{1,3}e\d{1,3}e\d{1,3}'                                   # S01E01E02E03 (no separator)
    r'|s\d{1,3}[-.]e\d{1,3}[-.]e\d{1,3}'                          # S01-E01-E08
    r'|season[.\-]?\d{1,3}[.\-]?ep[.\-]?\d{1,3}[-.\s]ep[.\-]?\d{1,3}'
    r'|season[.\-]?\d{1,3}[.\-]?episode[.\-]?\d{1,3}[-.\s]episode[.\-]?\d{1,3}',
    re.I
)


def _classify_package(release_title):
    """Return 'show', 'season', or 'single' based on the release title."""
    if _PKG_MULTI_SEASON_RE.search(release_title) or _PKG_SHOW_PACK_RE.search(release_title):
        return 'show'
    if _PKG_RANGE_RE.search(release_title):
        return 'season'  # multi-episode range pack, not a single episode
    if _PKG_EPISODE_RE.search(release_title):
        return 'single'
    if _PKG_SEASON_RE.search(release_title):
        return 'season'
    return 'single'


# ── Meteor endpoint ──
_METEOR_BASE = 'https://meteorfortheweebs.midnightignite.me'

_SIZE_RE = re.compile(r'((?:\d+[,.]?\d*)\s*(?:GB|GiB|MB|MiB))', re.I)
_HASH_RE = re.compile(r'\b([a-fA-F0-9]{40})\b')
_HASH16_RE = re.compile(r'\b([a-fA-F0-9]{16,})\b')
_EMOJI_STRIP_RE = re.compile(r'[📺🎞️💾👤📂\s]')

# Multi-pattern seed extraction — mirrors a4kScrapers scrapers.py parse_seeds cascade
_SEEDS_PATTERNS = [
    re.compile(r'Seeders?:?\s*(\d+)', re.I),
    re.compile(r'Seed:?\s*(\d+)', re.I),
    re.compile(r'👤\s*(\d+)'),
]


def _parse_seeds(text):
    """Extract seed count using the same cascade as a4kScrapers scrapers.py."""
    for pattern in _SEEDS_PATTERNS:
        m = pattern.search(text)
        if m and m.group(1) != '0':
            return int(m.group(1))
    return 0


def _log(msg):
    try:
        tools.log('a4kScrapers.meteor: %s' % msg, 'notice')
    except Exception:
        pass


def _http_get(url, timeout=12):
    try:
        from urllib.request import Request, urlopen
        import json as _json
        req = Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0')
        return _json.loads(urlopen(req, timeout=timeout).read().decode('utf-8'))
    except Exception as e:
        _log('http_get failed (%s): %s' % (url[:80], str(e)[:120]))
    return None


class sources(core.DefaultSources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, single_query=True, **kwargs)

    def _fetch_streams(self, imdb, season=None, episode=None):
        """Fetch all relevant streams from Meteor for an IMDB title."""
        all_streams = []
        seen = set()

        if season and episode:
            # Episode: query episode, season pack, and show pack
            queries = [
                '%s:%s:%s' % (imdb, season, episode),
                '%s:%s' % (imdb, season),
                imdb,
            ]
            # Alternative numbering for anime cour splits
            if hasattr(self, '_alt_season') and self._alt_season and self._alt_episode:
                queries.insert(1, '%s:%s:%s' % (imdb, self._alt_season, self._alt_episode))
                if self._alt_season != season:
                    queries.insert(2, '%s:%s' % (imdb, self._alt_season))
            cat = 'series'
        else:
            queries = [imdb]
            cat = 'movie'

        for q in queries:
            url = '%s/stream/%s/%s.json' % (_METEOR_BASE, cat, q)
            data = _http_get(url)
            if not data:
                continue
            for s in (data.get('streams') or []):
                h = s.get('infoHash', '').lower()
                if h and h not in seen:
                    seen.add(h)
                    all_streams.append(s)

        return all_streams

    def _parse_stream(self, stream):
        """Parse a Meteor stream dict into an a4kScrapers result dict."""
        h = stream.get('infoHash', '')
        if not h and 'url' in stream:
            m = _HASH_RE.search(stream['url'])
            if m:
                h = m.group(1)
        if not h and 'url' in stream:
            m = _HASH16_RE.search(stream['url'])
            if m:
                h = m.group(1)
        if not h:
            return None

        desc = stream.get('description', stream.get('title', ''))
        desc_clean = desc.replace('\u2508\u27a4', '\n').replace('\U0001F4C2 - ', '').replace('\U0001F4C2 ', '')
        desc_clean = re.sub(r'www.*? - ', '', desc_clean)
        lines = desc_clean.split('\n')

        # Level 1: substantial content
        release_title = ''
        for line in lines:
            line = line.strip()
            cleaned = _EMOJI_STRIP_RE.sub('', line)
            if len(cleaned) > 5:
                release_title = line
                break

        # Level 2: accept shorter content rather than produce blank title
        if not release_title:
            for line in lines:
                line = line.strip()
                cleaned = _EMOJI_STRIP_RE.sub('', line)
                if len(cleaned) > 1:
                    release_title = line
                    break

        # Level 3: fall back to name field second line
        if not release_title:
            name_lines = stream.get('name', '').strip().split('\n')
            for nl in name_lines[1:]:
                nl = nl.strip()
                if nl and nl.lower() not in ('meteor', 'torrentio', ''):
                    release_title = nl
                    break

        if not release_title:
            return None

        release_title = re.sub(r'[\U0001F300-\U0001F9FF\u2508\u27a4\u2764]+', '', release_title).strip()
        release_title = re.sub(r'\s{2,}', ' ', release_title).strip(' .-')
        if not release_title:
            return None

        size = 0
        m = _SIZE_RE.search(desc)
        if m:
            size = core.source_utils.de_string_size(m.group(1).replace('GiB', 'GB').replace('MiB', 'MB'))
        seeds = _parse_seeds(desc)

        result = {
            'scraper': 'meteor', 'hash': h.lower(),
            'magnet': 'magnet:?xt=urn:btih:%s&dn=%s' % (h.lower(), release_title),
            'package': _classify_package(release_title), 'release_title': release_title,
            'size': size, 'seeds': seeds,
        }

        # Extract underlying tracker from name second line
        name = stream.get('name', '')
        for line in name.strip().split('\n'):
            line = line.strip()
            if line and line.lower() not in ('meteor', 'torrentio', ''):
                result['tracker'] = line
                break

        return result

    def _get_all_results(self, imdb, season=None, episode=None):
        streams = self._fetch_streams(imdb, season, episode)
        parsed = []
        seen = set()
        for s in streams:
            r = self._parse_stream(s)
            if r and r['hash'] not in seen:
                seen.add(r['hash'])
                parsed.append(r)
        _log('%d raw → %d parsed' % (len(streams), len(parsed)))
        return parsed

    def movie(self, title, year, imdb=None, **kwargs):
        if not imdb:
            return []
        results = self._get_all_results(imdb)
        _log('movie() → %d results' % len(results))
        return results

    def episode(self, simple_info, all_info, **kwargs):
        imdb = all_info.get('info', {}).get('tvshow.imdb_id', None)
        if imdb is None:
            imdb = all_info.get('info', {}).get('imdb_id', None)
        if imdb is None:
            imdb = all_info.get('showInfo', {}).get('ids', {}).get('imdb', None)
        if not imdb:
            return []
        self._alt_season = simple_info.get('alternative_season', '')
        self._alt_episode = simple_info.get('alternative_episode', '')
        season = simple_info.get('season_number', '')
        episode_num = simple_info.get('episode_number', '')
        results = self._get_all_results(imdb, season, episode_num)
        _log('episode() → %d results' % len(results))
        return results
