# -*- coding: utf-8 -*-

import re
from providerModules.a4kScrapers import core
from providerModules.a4kScrapers.source_utils import tools

BASE = 'https://torrentio.strem.fun'
_TIO_OPTIONS = 'debridoptions=nodownloadlinks,nocatalog'
_TIO_RD_FALLBACK = 'realdebrid=T2iZoymNCCD1T5c2sX5u8tIZVcgcFWlCsCJ72rCmrU2mDdmvgieM'
_SIZE_RE = re.compile(r'((?:\d+[,.]?\d*)\s*(?:GB|GiB|MB|MiB))', re.I)
_SEEDS_RE = re.compile(u'\U0001F464\\s*(\\d+)')
_HASH_RE = re.compile(r'\b([a-fA-F0-9]{40})\b')

# Package classification
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


def _log(msg):
    try: tools.log('a4kScrapers.torrentio: %s' % msg, 'notice')
    except: pass


def _http_get(url, headers=None, timeout=10):
    try:
        from urllib.request import Request, urlopen
        import json
        req = Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0')
        if headers:
            for k, v in headers.items(): req.add_header(k, v)
        return json.loads(urlopen(req, timeout=timeout).read().decode('utf-8'))
    except Exception as e:
        _log('http_get failed: %s' % str(e)[:200])
    return None


class sources(core.DefaultSources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, single_query=True, **kwargs)
        self._rd_token = None

    def _set_apikeys(self, kwargs):
        """Extract RD token from apikeys dict passed by Seren."""
        apikeys = kwargs.get('apikeys', {})
        self._rd_token = apikeys.get('rd') or None

    def _build_tio_url(self, path, rd_param=None):
        """Build Torrentio URL — bare or RD-authenticated."""
        if rd_param:
            return '%s/%s|%s/stream/%s' % (BASE, _TIO_OPTIONS, rd_param, path)
        return '%s/stream/%s' % (BASE, path)

    def _fetch_streams(self, imdb, season=None, episode=None):
        """
        Fetch streams from Torrentio.

        Always calls the bare URL for full hash coverage (Option A fallback pool).
        When an RD token is configured, ALSO calls the authenticated URL in parallel
        and extracts hashes confirmed cached on RD (identified by '+' in stream name,
        matching POV's tio_check_cache filter exactly).  These are tagged rd_cached=True
        so Seren's _realdebrid_worker can mark them CACHED without an external cache check.

        Falls back to the shared hardcoded RD key when no user token is set —
        same key POV uses in tio_check_cache.
        """
        if season and episode:
            path = 'series/%s:%s:%s.json' % (imdb, season, episode)
        else:
            path = 'movie/%s.json' % imdb

        bare_url = self._build_tio_url(path)

        # Determine RD param: user key preferred, hardcoded shared key as fallback
        rd_param = ('realdebrid=%s' % self._rd_token) if self._rd_token else _TIO_RD_FALLBACK

        # Call bare URL and authenticated URL in parallel threads
        from threading import Thread
        bare_result = [None]
        auth_result = [None]

        def _fetch_bare():
            bare_result[0] = _http_get(bare_url) or {}

        def _fetch_auth():
            auth_result[0] = _http_get(self._build_tio_url(path, rd_param)) or {}

        t_bare = Thread(target=_fetch_bare)
        t_auth = Thread(target=_fetch_auth)
        t_bare.start()
        t_auth.start()
        t_bare.join()
        t_auth.join()

        bare_streams = bare_result[0].get('streams', [])
        auth_streams = auth_result[0].get('streams', [])

        # Extract confirmed-RD-cached hashes from authenticated response.
        # '+' in stream name is Torrentio's cached indicator (e.g. "⚡ RD+").
        # Matches POV's tio_check_cache filter exactly.
        rd_cached_hashes = set()
        for s in auth_streams:
            name = s.get('name', '')
            if '+' in name:
                h = s.get('infoHash', '')
                if not h:
                    m = _HASH_RE.search(s.get('url', ''))
                    if m: h = m.group(1)
                if h:
                    rd_cached_hashes.add(h.lower())

        label = 'user key' if self._rd_token else 'hardcoded RD fallback'
        _log('bare URL returned %d streams' % len(bare_streams))
        _log('RD-authenticated (%s) returned %d streams, %d cached hashes' % (
            label, len(auth_streams), len(rd_cached_hashes)))

        return bare_streams, rd_cached_hashes

    def _parse_stream(self, stream, rd_cached_hashes=None):
        # Extract hash: try infoHash first, then extract from url field
        h = stream.get('infoHash', '')
        if not h and 'url' in stream:
            m = _HASH_RE.search(stream['url'])
            if m: h = m.group(1)
        if not h:
            return None

        title_raw = stream.get('title', '')
        lines = title_raw.split('\n')
        release_title = lines[0].strip() if lines else ''
        if not release_title:
            return None
        release_title = re.sub(u'[\U0001F300-\U0001F9FF┈➤❤]+', '', release_title).strip()
        release_title = re.sub(r'\s{2,}', ' ', release_title).strip(' .-')
        if not release_title:
            return None

        size = 0
        m = _SIZE_RE.search(title_raw)
        if m:
            size = core.source_utils.de_string_size(m.group(1).replace('GiB', 'GB').replace('MiB', 'MB'))
        seeds = 0
        m = _SEEDS_RE.search(title_raw)
        if m: seeds = int(m.group(1))

        result = {
            'scraper': 'torrentio', 'hash': h.lower(),
            'magnet': 'magnet:?xt=urn:btih:%s&dn=%s' % (h.lower(), release_title),
            'package': _classify_package(release_title),
            'release_title': release_title, 'size': size, 'seeds': seeds,
        }

        # Tag pre-verified RD-cached hashes — Seren skips external cache check for these.
        if rd_cached_hashes and h.lower() in rd_cached_hashes:
            result['rd_cached'] = True

        name = stream.get('name', '')
        if '[PM+]' in name: result['debrid'] = 'PM'
        elif '[RD+]' in name: result['debrid'] = 'RD'
        elif '[AD+]' in name: result['debrid'] = 'AD'
        elif '[TB+]' in name: result['debrid'] = 'TB'
        elif '[DL+]' in name: result['debrid'] = 'DL'

        # Extract tracker/source from name field
        tracker = ''
        name_lines = name.replace('[PM+]', '').replace('[RD+]', '').replace('[AD+]', '') \
                         .replace('[TB+]', '').replace('[DL+]', '').strip().split('\n')
        for line in name_lines:
            line = line.strip()
            if line and line.lower() not in ('torrentio', ''):
                tracker = line
                break
        if tracker:
            result['tracker'] = tracker

        return result

    def movie(self, title, year, imdb=None, **kwargs):
        if not imdb: return []
        self._set_apikeys(kwargs)
        streams, rd_cached_hashes = self._fetch_streams(imdb)
        results = [r for r in (self._parse_stream(s, rd_cached_hashes) for s in streams) if r]
        _log('movie() %d streams -> %d results' % (len(streams), len(results)))
        return results

    def episode(self, simple_info, all_info, **kwargs):
        imdb = all_info.get('info', {}).get('tvshow.imdb_id', None)
        if imdb is None:
            imdb = all_info.get('info', {}).get('imdb_id', None)
        if imdb is None:
            imdb = all_info.get('showInfo', {}).get('ids', {}).get('imdb', None)
        if not imdb: return []
        self._set_apikeys(kwargs)
        season = simple_info.get('season_number', '')
        episode_num = simple_info.get('episode_number', '')
        streams, rd_cached_hashes = self._fetch_streams(imdb, season, episode_num)

        # Also try alternative numbering for anime cour splits
        alt_season = simple_info.get('alternative_season', '')
        alt_episode = simple_info.get('alternative_episode', '')
        if alt_season and alt_episode:
            alt_streams, alt_rd_cached = self._fetch_streams(imdb, alt_season, alt_episode)
            seen_hashes = {s.get('infoHash', '') for s in streams if s.get('infoHash')}
            streams.extend(s for s in alt_streams if s.get('infoHash', '') not in seen_hashes)
            rd_cached_hashes = rd_cached_hashes | alt_rd_cached

        results = [r for r in (self._parse_stream(s, rd_cached_hashes) for s in streams) if r]
        _log('episode() %d streams -> %d results' % (len(streams), len(results)))
        return results
