# -*- coding: utf-8 -*-
#
# MediaFusion scraper — ElfHosted (primary, user's personal D- token) with
# Midnight public instance fallback (mediafusionfortheweebs.midnightignite.me).
# Primary: requires 'mf.token' in Seren settings; URL: {base}/{token}/stream/{type}/{id}.json
# Fallback fires when: no user token set, ElfHosted network error/timeout, or ElfHosted
# returns the invalid-config sentinel (expired/wrong token).
# Obtain your ElfHosted token from the plugin.video.mediafusion addon (Secret String)
# or the ElfHosted configure UI at mediafusion.elfhosted.com.

import re
from providerModules.a4kScrapers import core
from providerModules.a4kScrapers.source_utils import tools

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


# MediaFusion ElfHosted instance — user must supply their own personal D- token.
# Token is obtained from the plugin.video.mediafusion Kodi addon (Secret String field)
# or via the ElfHosted configure UI at mediafusion.elfhosted.com.
# Format: {base}/{token}/stream/{type}/{id}.json
_MF_BASES = [
    'https://mediafusion.elfhosted.com',       # 0 — Production (default)
    'https://mediafusion-dev.elfhosted.com',   # 1 — Dev / Beta
]
# Midnight public MF instance — torrent-only fallback (no debrid service configured).
# Used when the user has no personal ElfHosted token, or ElfHosted is unreachable/expired.
# Token sourced from plugin.video.pov (public; does NOT provide debrid-cached streams).
_MF_MIDNIGHT_BASE  = 'https://mediafusionfortheweebs.midnightignite.me'
_MF_MIDNIGHT_TOKEN = (
    'D-h5mpsX35oygOGFiHutl66dLAPiXzjQTODPXKQuKBaQOLjwNBbVkSPi7TJPr0gdykpCFREq8JOh'
    'DHZcvoS_UNZsWpsbjscCAwzgqc9VvP0S3Wt9lz5blcPT8lU6fcHdAHYctp_yde6nWKtSQ1O9Tjeh'
    'GNwajH9TjGZwn6rOybPFmoMpccXfTkB3Xwe9xRhT9O-bKzoYnGnlG8fCDxlNGdzrnlythePc3C7O'
    'phF8b5GyhuSnvBhxD7dTfkI77Dbay8_k_wqS-me9euZQ-oyOJBNTOIsO8HiWQhLGCC8m9rYsqJT6'
    'QF1Xhn-2bNzlukfbSYh_X1kOFdi6Y-YkBeEYokDlQHzzU45qmrj2b1Nz-GALcJHjNDJEMF3h9Eyx'
    '7UcmGWT1qvTpv_tcXjAX37ceqrWH-e_EqwVkvQDjNnmpjOhBWhuUW2R-0KbvxKUn1s5d2jZjLBxC'
    'bMotHIC-G2SrVCLgC_KV0OUainevUHKOKTe0CQmWz1HKV1ju52CFZFZYAWkOAX5cw55qzNnWl_nQ'
    'RnLyngrW_P6aYqghbYyyrAvQ6hCrIbSnVj4GsMFIelcMETvGW4jIXdwZGZA1L8gCzmyCbI9vAqPv'
    'dZxRWb7roc2EnB7gaSYdFtTP9gGoFKKkQ-9aircUEiPXjkP4QWO7lVI4GZri7KKCKjBM7-hWf4nm'
    'ttY7lJS_4Te_H80BeR_qpqeYQ6V0gpVwihARA6cIsZFbWmQXtoYNO16jt1ZqeVztwR6L1IQQnAsH'
    'ANyR5kF7ovGCOnhWlDDxO3nk8fhm3s0k7XewrMisZHy1zNsivTjvJW6KoVwghLn8-QCTf9PEPoPj'
    's6tW5KjciaRvbMg5-mbhpAhYOmPisB4ZyW63vWY6TeU1OBJV0T_fkHtgbvgiTEX5RFoRVDLnhaof'
    '-xHVw2oCc2AdXmBDVROmFjY8x9KEyZ91QfNjHnrTFmGetelcHE'
)


_SIZE_RE = re.compile(r'((?:\d+[,.]?\d*)\s*(?:GB|GiB|MB|MiB))', re.I)
_HASH_RE = re.compile(r'\b([a-fA-F0-9]{40})\b')
_EMOJI_STRIP_RE = re.compile(r'[📺🎞️💾👤📂\s]')

_SEEDS_PATTERNS = [
    re.compile(r'Seeders?:?\s*(\d+)', re.I),
    re.compile(r'Seed:?\s*(\d+)', re.I),
    re.compile(r'👤\s*(\d+)'),
]


def _parse_seeds(text):
    for pattern in _SEEDS_PATTERNS:
        m = pattern.search(text)
        if m and m.group(1) != '0':
            return int(m.group(1))
    return 0


_QUALITY_MAP = [
    (['2160', '4k'], '4K'),
    (['1080'],       '1080p'),
    (['720'],        '720p'),
]


def _extract_quality(text):
    t = text.lower()
    for keywords, quality in _QUALITY_MAP:
        if any(kw in t for kw in keywords):
            return quality
    return None


def _log(msg):
    try:
        tools.log('a4kScrapers.mediafusion: %s' % msg, 'notice')
    except Exception:
        pass


_UUID_RE = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)


def _is_invalid_sentinel(streams):
    """Return True when MF returns its known invalid-token placeholder.
    When a D- token is wrong or expired MF returns exactly one stream:
      - 'url' ends with 'invalid_config.mp4'
      - 'description' contains 'Invalid MediaFusion configuration'
    Caught at the fetch level to trigger the Midnight fallback.
    """
    if len(streams) != 1:
        return False
    s = streams[0]
    return ('invalid_config' in (s.get('url') or '')
            or 'Invalid MediaFusion configuration' in (s.get('description') or ''))


def _get_mf_token():
    """
    Return the user's MediaFusion secret token from Seren settings, or None if unset.
    Reads from plugin.video.seren setting 'mf.token'.
    Handles: full URL paste, D-/U- prefixed token, or bare UUID (auto-prepends U-).
    """
    try:
        import xbmcaddon as _xbmcaddon
        raw = _xbmcaddon.Addon('plugin.video.seren').getSetting('mf.token') or ''
        raw = raw.strip()
        if not raw:
            return None
        # Extract D-/U- token from a full URL or prefixed string
        m = re.search(r'([DU]-[A-Za-z0-9_\-]+)', raw)
        if m:
            return m.group(1)
        # Bare UUID (copied from the MF dashboard without the U- prefix) — add it
        if _UUID_RE.match(raw):
            return 'U-' + raw
        return raw
    except Exception as e:
        _log('_get_mf_token failed: %s' % str(e)[:80])
    return None


def _get_mf_base():
    """Return the MF base URL based on Seren setting 'mf.instance' (0=Production, 1=Dev/Beta)."""
    try:
        import xbmcaddon as _xbmcaddon
        idx = int(_xbmcaddon.Addon('plugin.video.seren').getSetting('mf.instance') or 0)
        return _MF_BASES[idx] if 0 <= idx < len(_MF_BASES) else _MF_BASES[0]
    except Exception:
        pass
    return _MF_BASES[0]


def _http_get(url, headers=None, timeout=12):
    try:
        from urllib.request import Request, urlopen
        import json as _json
        req = Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0')
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        return _json.loads(urlopen(req, timeout=timeout).read().decode('utf-8'))
    except Exception as e:
        _log('http_get failed (%s): %s' % (url[:80], str(e)[:120]))
    return None


class sources(core.DefaultSources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, single_query=True, **kwargs)

    # ------------------------------------------------------------------ #
    #  Fetch helpers                                                       #
    # ------------------------------------------------------------------ #

    def _fetch_streams(self, path):
        """Fetch streams: ElfHosted (primary) with Midnight public instance fallback.

        Fallback fires when:
          (a) no user token set in Seren settings,
          (b) ElfHosted returns a network error / timeout, or
          (c) ElfHosted returns the invalid-config sentinel (expired/wrong token).
        Valid ElfHosted responses — including empty {streams:[]} — bypass the fallback
        so a genuine "no results" does not incorrectly trigger Midnight.
        Note: Midnight is torrent-only (no debrid service configured); it provides
        additional torrent sources that Seren cache-checks independently.
        """
        token = _get_mf_token()
        if token:
            base = _get_mf_base()
            _log('ElfHosted: token prefix=%s len=%d base=%s path=%s'
                 % (token[:6], len(token), base.split('//')[1][:30], path))
            url = '%s/%s%s' % (base, token, path)
            data = _http_get(url)
            if data is not None:
                streams = data.get('streams', [])
                if not _is_invalid_sentinel(streams):
                    _log('elfhosted -> %d streams' % len(streams))
                    return streams  # valid response (may be empty) — no fallback
                _log('ElfHosted token invalid or expired — falling back to Midnight')
            else:
                _log('ElfHosted request failed — falling back to Midnight')
        else:
            _log('no mf.token configured — using Midnight public instance')

        # Midnight fallback — torrent sources only, no debrid cache markers
        url = '%s/%s%s' % (_MF_MIDNIGHT_BASE, _MF_MIDNIGHT_TOKEN, path)
        _log('Midnight: %s%s' % (_MF_MIDNIGHT_BASE, path))
        data = _http_get(url)
        if data and data.get('streams'):
            _log('midnight -> %d streams' % len(data['streams']))
            return data['streams']
        _log('Midnight returned no streams')
        return []

    # ------------------------------------------------------------------ #
    #  Stream parser                                                       #
    # ------------------------------------------------------------------ #

    def _parse_stream(self, stream):
        # Detect the known invalid-token error placeholder returned by MF.
        # When the D- token is wrong/expired, MF returns exactly one stream with
        # no infoHash, a .mp4 URL ending in 'invalid_config.mp4', and a description
        # telling the user to reconfigure.  Catch it early with a clear log message.
        _url_f = stream.get('url', '')
        _desc_f = stream.get('description', '')
        if 'invalid_config' in _url_f or 'Invalid MediaFusion configuration' in _desc_f:
            _log('MF token is invalid or expired — reconfigure at mediafusion.elfhosted.com '
                 '(desc: %s)' % _desc_f[:120])
            return None

        h = stream.get('infoHash', '')
        if not h and 'url' in stream:
            m = _HASH_RE.search(stream['url'])
            if m:
                h = m.group(1)
        if not h:
            _log('parse rejected (no hash) keys=%s name=%r url=%r desc=%r bh=%r' % (
                list(stream.keys()),
                (stream.get('name') or '')[:80],
                (_url_f)[:120],
                (_desc_f)[:120],
                stream.get('behaviorHints')))
            return None

        desc = stream.get('description', stream.get('title', ''))
        desc_clean = desc.replace('\u2508\u27a4', '\n').replace('\U0001F4C2 - ', '').replace('\U0001F4C2 ', '')
        desc_clean = re.sub(r'www.*? - ', '', desc_clean)
        lines = desc_clean.split('\n')

        # Level 0: behaviorHints['filename'] \u2014 actual torrent filename, most reliable (POV parity).
        # Strip container extension if present, then apply standard emoji/space cleaning.
        release_title = ''
        _bh = stream.get('behaviorHints') or {}
        _fn = (_bh.get('filename') or '').strip()
        if _fn:
            _fn = re.sub(r'\.(mkv|mp4|avi|m2ts|ts|mov|wmv|flv|rmvb)$', '', _fn, flags=re.I)
            _fn = re.sub(r'[\U0001F300-\U0001F9FF\u2508\u27a4\u2764]+', '', _fn).strip()
            _fn = re.sub(r'\s{2,}', ' ', _fn).strip(' .-')
            if len(_fn) > 2:
                release_title = _fn

        # Level 1: first description line with >5 meaningful chars (after stripping emojis/spaces)
        if not release_title:
            for line in lines:
                line = line.strip()
                cleaned = _EMOJI_STRIP_RE.sub('', line)
                if len(cleaned) > 5:
                    release_title = line
                    break

        # Level 2: accept shorter content (>1 char)
        if not release_title:
            for line in lines:
                line = line.strip()
                cleaned = _EMOJI_STRIP_RE.sub('', line)
                if len(cleaned) > 1:
                    release_title = line
                    break

        # Level 3: name field second line
        if not release_title:
            name_lines = stream.get('name', '').strip().split('\n')
            for nl in name_lines[1:]:
                nl = nl.strip()
                if nl and nl.lower() not in ('mediafusion', 'media fusion', ''):
                    release_title = nl
                    break

        if not release_title:
            _log('parse rejected (no title) hash=%s name=%r desc=%r' % (
                h[:10], (stream.get('name') or '')[:80], (_desc_f)[:120]))
            return None

        release_title = re.sub(r'[\U0001F300-\U0001F9FF\u2508\u27a4\u2764]+', '', release_title).strip()
        release_title = re.sub(r'\s{2,}', ' ', release_title).strip(' .-')
        if not release_title:
            _log('parse rejected (title empty after clean) hash=%s' % h[:10])
            return None

        size = 0
        m = _SIZE_RE.search(desc)
        if m:
            size = core.source_utils.de_string_size(m.group(1).replace('GiB', 'GB').replace('MiB', 'MB'))
        seeds = _parse_seeds(desc)

        result = {
            'scraper': 'mediafusion', 'hash': h.lower(),
            'magnet': 'magnet:?xt=urn:btih:%s&dn=%s' % (h.lower(), release_title),
            'package': _classify_package(release_title), 'release_title': release_title,
            'size': size, 'seeds': seeds,
        }

        name = stream.get('name', '')
        name_quality = None
        for line in name.strip().split('\n'):
            line = line.strip()
            if not line or line.lower() in ('mediafusion', 'media fusion', 'torrentio', ''):
                continue
            if name_quality is None:
                name_quality = _extract_quality(line)
            if 'tracker' not in result:
                result['tracker'] = line

        quality = name_quality or _extract_quality(desc)
        if quality:
            result['quality'] = quality

        return result

    # ------------------------------------------------------------------ #
    #  Public entry points                                                 #
    # ------------------------------------------------------------------ #

    def movie(self, title, year, imdb=None, **kwargs):
        if not imdb:
            return []
        path = '/stream/movie/%s.json' % imdb
        streams = self._fetch_streams(path)
        results = [r for r in (self._parse_stream(s) for s in streams) if r]
        _log('movie() %d streams -> %d results' % (len(streams), len(results)))
        return results

    def episode(self, simple_info, all_info, **kwargs):
        imdb = all_info.get('info', {}).get('tvshow.imdb_id', None)
        if imdb is None:
            imdb = all_info.get('info', {}).get('imdb_id', None)
        if imdb is None:
            imdb = all_info.get('showInfo', {}).get('ids', {}).get('imdb', None)
        if not imdb:
            return []

        season = simple_info.get('season_number', '')
        episode_num = simple_info.get('episode_number', '')
        path = '/stream/series/%s:%s:%s.json' % (imdb, season, episode_num)
        streams = self._fetch_streams(path)

        # Cour-split anime: also query alternative season/episode numbers and merge.
        alt_season = simple_info.get('alternative_season', '')
        alt_episode = simple_info.get('alternative_episode', '')
        if alt_season and alt_episode:
            alt_path = '/stream/series/%s:%s:%s.json' % (imdb, alt_season, alt_episode)
            alt_streams = self._fetch_streams(alt_path)
            seen = {s.get('infoHash', '') for s in streams if s.get('infoHash')}
            streams.extend(s for s in alt_streams if s.get('infoHash', '') not in seen)

        results = [r for r in (self._parse_stream(s) for s in streams) if r]
        _log('episode() %d streams -> %d results' % (len(streams), len(results)))
        return results
