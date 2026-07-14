# -*- coding: utf-8 -*-
#
# AIOStreams scraper — aggregates Comet + MediaFusion + Meteor through a single API.
# If AIOStreams is down, falls back to querying Comet, MediaFusion, and Meteor directly.
# MediaFusion fallback requires the user's personal D- token stored in Seren setting
# 'mf.token'.  If no token is configured, the MediaFusion fallback path is silently
# skipped.  Community/public MF instances are no longer used.

import re
import base64
from providerModules.a4kScrapers import core
from providerModules.a4kScrapers.source_utils import tools

# Package classification: detect multi-season ranges, season packs, and episode markers.
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

# ── AIOStreams endpoints ──
AIOSTREAMS_BASES = [
    'https://aiostreams.stremio.ru',
    'https://aiostreamsfortheweebsstable.midnightignite.me',
]

# Base64-encoded config that enables Comet (with P2P), MediaFusion, and Meteor
_AIOSTREAMS_HEADER = (
    'eyJwcmVzZXRzIjpbeyJ0eXBlIjoiY29tZXQiLCJpbnN0YW5jZUlkIjoiZjdiIiwiZW5hYmxlZCI6'
    'dHJ1ZSwib3B0aW9ucyI6eyJuYW1lIjoiQ29tZXQiLCJ0aW1lb3V0Ijo2NTAwLCJyZXNvdXJjZXMi'
    'Olsic3RyZWFtIl0sImluY2x1ZGVQMlAiOnRydWUsInJlbW92ZVRyYXNoIjpmYWxzZX19LHsidHlw'
    'ZSI6Im1lZGlhZnVzaW9uIiwiaW5zdGFuY2VJZCI6IjQ1MCIsImVuYWJsZWQiOnRydWUsIm9wdGlv'
    'bnMiOnsibmFtZSI6Ik1lZGlhRnVzaW9uIiwidGltZW91dCI6NjUwMCwicmVzb3VyY2VzIjpbInN0'
    'cmVhbSJdLCJ1c2VDYWNoZWRSZXN1bHRzT25seSI6dHJ1ZSwiZW5hYmxlV2F0Y2hsaXN0Q2F0YWxv'
    'Z3MiOmZhbHNlLCJkb3dubG9hZFZpYUJyb3dzZXIiOmZhbHNlLCJjb250cmlidXRvclN0cmVhbXMi'
    'OmZhbHNlLCJjZXJ0aWZpY2F0aW9uTGV2ZWxzRmlsdGVyIjpbXSwibnVkaXR5RmlsdGVyIjpbXX19'
    'LHsidHlwZSI6Im1ldGVvciIsImluc3RhbmNlSWQiOiJtMSIsImVuYWJsZWQiOnRydWUsIm9wdGlv'
    'bnMiOnsibmFtZSI6Ik1ldGVvciIsInRpbWVvdXQiOjY1MDAsInJlc291cmNlcyI6WyJzdHJlYW0i'
    'XSwibWVkaWFUeXBlcyI6W119fV0sImZvcm1hdHRlciI6eyJpZCI6InRvcnJlbnRpbyIsImRlZmlu'
    'aXRpb24iOnsibmFtZSI6IiIsImRlc2NyaXB0aW9uIjoiIn19LCJzb3J0Q3JpdGVyaWEiOnsiZ2xv'
    'YmFsIjpbXX0sImRlZHVwbGljYXRvciI6eyJlbmFibGVkIjpmYWxzZSwia2V5cyI6WyJmaWxlbmFt'
    'ZSIsImluZm9IYXNoIl0sIm11bHRpR3JvdXBCZWhhdmlvdXIiOiJhZ2dyZXNzaXZlIiwiY2FjaGVk'
    'Ijoic2luZ2xlX3Jlc3VsdCIsInVuY2FjaGVkIjoicGVyX3NlcnZpY2UiLCJwMnAiOiJzaW5nbGVf'
    'cmVzdWx0IiwiZXhjbHVkZUFkZG9ucyI6W119fQ=='
)

# ── Comet fallback endpoints ──
COMET_BASES = ['https://comet.stremio.ru', 'https://comet.feels.legal', 'https://cometfortheweebs.midnightignite.me']
_COMET_DEFAULT_PARAMS = (
    'eyJtYXhSZXN1bHRzUGVyUmVzb2x1dGlvbiI6MCwibWF4U2l6ZSI6MCwiY2FjaGVkT25seSI6ZmFsc2Us'
    'InJlbW92ZVRyYXNoIjp0cnVlLCJyZXN1bHRGb3JtYXQiOlsidGl0bGUiLCJtZXRhZGF0YSIsInNpemUi'
    'LCJsYW5ndWFnZXMiXSwiZGVicmlkU2VydmljZSI6InRvcnJlbnQiLCJkZWJyaWRBcGlLZXkiOiIiLCJk'
    'ZWJyaWRTdHJlYW1Qcm94eVBhc3N3b3JkIjoiIiwibGFuZ3VhZ2VzIjp7InJlcXVpcmVkIjpbXSwiZXhj'
    'bHVkZSI6W10sInByZWZlcnJlZCI6W119LCJyZXNvbHV0aW9ucyI6e30sIm9wdGlvbnMiOnsicmVtb3Zl'
    'X3JhbmtzX3VuZGVyIjotMTAwMDAwMDAwMDAsImFsbG93X2VuZ2xpc2hfaW5fbGFuZ3VhZ2VzIjpmYWxz'
    'ZSwicmVtb3ZlX3Vua25vd25fbGFuZ3VhZ2VzIjpmYWxzZX19'
)
_DEBRID_MAP = {'pm': 'premiumize', 'rd': 'realdebrid', 'ad': 'alldebrid', 'tb': 'torbox', 'dl': 'debridlink'}

# ── MediaFusion fallback — ElfHosted instance with user's personal D- token ──
# Obtain your token from the plugin.video.mediafusion Kodi addon (Secret String)
# or from the ElfHosted configure UI at mediafusion.elfhosted.com.
# If no token is configured in Seren settings, MediaFusion fallback is skipped.
_MF_BASES = [
    'https://mediafusion.elfhosted.com',       # 0 — Production (default)
    'https://mediafusion-dev.elfhosted.com',   # 1 — Dev / Beta
]


_UUID_RE = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)


def _get_mf_base():
    """Return the MF base URL based on Seren setting 'mf.instance' (0=Production, 1=Dev/Beta)."""
    try:
        import xbmcaddon as _xbmcaddon
        idx = int(_xbmcaddon.Addon('plugin.video.seren').getSetting('mf.instance') or 0)
        return _MF_BASES[idx] if 0 <= idx < len(_MF_BASES) else _MF_BASES[0]
    except Exception:
        pass
    return _MF_BASES[0]


def _get_mf_token():
    """
    Return the user's MediaFusion secret token from Seren settings, or None if unset.
    Handles: full URL paste, D-/U- prefixed token, or bare UUID (auto-prepends U-).
    """
    try:
        import xbmcaddon as _xbmcaddon
        raw = _xbmcaddon.Addon('plugin.video.seren').getSetting('mf.token') or ''
        raw = raw.strip()
        if not raw:
            return None
        m = re.search(r'([DU]-[A-Za-z0-9_\-]+)', raw)
        if m:
            return m.group(1)
        # Bare UUID (copied from MF dashboard without the U- prefix) — add it
        if _UUID_RE.match(raw):
            return 'U-' + raw
        return raw
    except Exception as e:
        _log('_get_mf_token failed: %s' % str(e)[:80])
    return None


# ── Meteor fallback endpoint ──
METEOR_BASES = ['https://meteorfortheweebs.midnightignite.me']

# ── Shared regex ──
_SIZE_RE = re.compile(r'((?:\d+[,.]?\d*)\s*(?:GB|GiB|MB|MiB))', re.I)
_SEEDS_RE = re.compile(r'👤\s*(\d+)')
_HASH_RE = re.compile(r'\b([a-fA-F0-9]{40})\b')
_HASH16_RE = re.compile(r'\b([a-fA-F0-9]{16,})\b')


def _log(msg):
    try: tools.log('a4kScrapers.aiostreams: %s' % msg, 'notice')
    except: pass


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
        self._debrid_service = None
        self._debrid_apikey = None

    def _set_apikeys(self, kwargs):
        apikeys = kwargs.get('apikeys', {})
        self._debrid_service = None
        self._debrid_apikey = None
        for key in ('pm', 'rd', 'ad', 'tb', 'dl'):
            token = apikeys.get(key)
            if token:
                self._debrid_service = _DEBRID_MAP[key]
                self._debrid_apikey = token
                break

    # ── AIOStreams primary path ──
    def _fetch_aiostreams(self, imdb, season=None, episode=None):
        if season and episode:
            params = 'type=series&id=%s:%s:%s' % (imdb, season, episode)
        else:
            params = 'type=movie&id=%s' % imdb

        for base in AIOSTREAMS_BASES:
            url = '%s/api/v1/search?%s' % (base, params)
            data = _http_get(url, headers={'x-aiostreams-user-data': _AIOSTREAMS_HEADER})
            if data:
                results = data.get('data', {}).get('results', [])
                if results:
                    _log('AIOStreams: %s returned %d results' % (base.split('//')[1][:30], len(results)))
                    return results
        return None  # None = failed (distinct from [] = success with 0 results)

    def _parse_aiostreams_result(self, file):
        h = file.get('infoHash', '')
        if not h:
            return None

        file_title = (file.get('folderName') or file.get('filename') or '').replace('┈➤', '\n').split('\n')
        release_title = file_title[0].strip() if file_title else ''
        if not release_title:
            return None
        release_title = re.sub(r'[\U0001F300-\U0001F9FF\u2508\u27a4\u2764]+', '', release_title).strip()
        release_title = re.sub(r'\s{2,}', ' ', release_title).strip(' .-')
        if not release_title:
            return None

        size = 0
        try:
            size = float(file.get('size', 0)) / 1024 / 1024
        except:
            pass

        seeds = 0
        try:
            seeds = int(file.get('seeders', 0))
        except:
            pass

        result = {
            'scraper': 'aiostreams', 'hash': h.lower(),
            'magnet': 'magnet:?xt=urn:btih:%s&dn=%s' % (h.lower(), release_title),
            'package': _classify_package(release_title), 'release_title': release_title,
            'size': size, 'seeds': seeds,
        }

        # Extract tracker/source if present in AIOStreams result
        tracker = file.get('source', file.get('indexer', file.get('addon', '')))
        if tracker:
            result['tracker'] = tracker

        # Sub-label: which AIOStreams sub-provider supplied this result.
        # The 'addon' field identifies the underlying Stremio addon (e.g. "comet",
        # "mediafusion", "stremthru-torz"). Map to a display name so Seren's
        # source select can show "AIOSTREAMS (Comet)" etc.
        _ADDON_LABELS = {
            'comet': 'Comet',
            'mediafusion': 'MediaFusion',
            'media fusion': 'MediaFusion',
            'stremthru-torz': 'StremThru',
            'stremthru': 'StremThru',
            'torz': 'Torz',
            'meteor': 'Meteor',
        }
        addon = file.get('addon', '').strip().lower()
        if addon:
            label = _ADDON_LABELS.get(addon, addon.title())
            result['sub_label'] = label

        return result

    # ── Comet fallback ──
    def _fetch_comet(self, imdb, season=None, episode=None):
        if season and episode:
            path = '/stream/series/%s:%s:%s.json' % (imdb, season, episode)
        else:
            path = '/stream/movie/%s.json' % imdb

        # Try with debrid key first
        if self._debrid_service and self._debrid_apikey:
            config = {
                "maxResultsPerResolution": 0, "maxSize": 0, "cachedOnly": False,
                "removeTrash": True, "resultFormat": ["title", "metadata", "size", "languages"],
                "debridService": self._debrid_service, "debridApiKey": self._debrid_apikey,
                "debridStreamProxyPassword": "",
                "languages": {"required": [], "exclude": [], "preferred": []},
                "resolutions": {},
                "options": {"remove_ranks_under": -10000000000, "allow_english_in_languages": False,
                            "remove_unknown_languages": False}
            }
            params = base64.b64encode(core.json.dumps(config, separators=(',', ':')).encode()).decode()
            for base in COMET_BASES:
                data = _http_get(base + '/' + params + path)
                if data and data.get('streams'):
                    _log('Comet fallback: %s returned %d streams (debrid)' % (base.split('//')[1][:20], len(data['streams'])))
                    return data['streams']

        # Fallback to P2P mode
        for base in COMET_BASES:
            data = _http_get(base + '/' + _COMET_DEFAULT_PARAMS + path)
            if data and data.get('streams'):
                _log('Comet fallback: %s returned %d streams (P2P)' % (base.split('//')[1][:20], len(data['streams'])))
                return data['streams']
        return []

    # ── MediaFusion fallback ──
    def _fetch_mediafusion(self, imdb, season=None, episode=None):
        token = _get_mf_token()
        if not token:
            _log('MediaFusion fallback: no mf.token in Seren settings — skipping')
            return []
        if season and episode:
            path = '/stream/series/%s:%s:%s.json' % (imdb, season, episode)
        else:
            path = '/stream/movie/%s.json' % imdb
        base = _get_mf_base()
        _log('MediaFusion fallback: token prefix=%s len=%d base=%s path=%s' % (token[:6], len(token), base.split('//')[1][:30], path))
        url = '%s/%s%s' % (base, token, path)
        data = _http_get(url)
        if data and data.get('streams'):
            streams = data['streams']
            if any('invalid_config' in (s.get('url') or '') or
                   'Invalid MediaFusion configuration' in (s.get('description') or '')
                   for s in streams):
                _log('MediaFusion fallback: MF token is invalid or expired — reconfigure at mediafusion.elfhosted.com')
                return []
            _log('MediaFusion fallback: elfhosted returned %d streams' % len(streams))
            return streams
        return []

    def _parse_stremio_stream(self, stream, provider_name):
        """Parse a Comet/MediaFusion stream into a result dict."""
        h = stream.get('infoHash', '')
        if not h and 'url' in stream:
            m = _HASH_RE.search(stream['url'])
            if m: h = m.group(1)
        if not h and 'url' in stream:
            m = _HASH16_RE.search(stream['url'])
            if m: h = m.group(1)
        if not h:
            return None

        desc = stream.get('description', stream.get('title', ''))
        desc_clean = desc.replace('\u2508\u27a4', '\n').replace('📂 - ', '').replace('📂 ', '')
        desc_clean = re.sub(r'www.*? - ', '', desc_clean)
        lines = desc_clean.split('\n')

        release_title = ''
        for line in lines:
            line = line.strip()
            cleaned = re.sub(r'[📺🎞️💾👤📂\s]', '', line)
            if len(cleaned) > 5:
                release_title = line
                break
        if not release_title and lines:
            release_title = lines[0].strip()
        if not release_title:
            return None

        release_title = re.sub(r'[\U0001F300-\U0001F9FF\u2508\u27a4\u2764]+', '', release_title).strip()
        release_title = re.sub(r'\s{2,}', ' ', release_title).strip(' .-')
        if not release_title:
            return None

        size = 0
        m = _SIZE_RE.search(desc)
        if m: size = core.source_utils.de_string_size(m.group(1).replace('GiB', 'GB').replace('MiB', 'MB'))
        seeds = 0
        m = _SEEDS_RE.search(desc)
        if m: seeds = int(m.group(1))

        result = {
            'scraper': 'aiostreams', 'hash': h.lower(),
            'magnet': 'magnet:?xt=urn:btih:%s&dn=%s' % (h.lower(), release_title),
            'package': _classify_package(release_title), 'release_title': release_title,
            'size': size, 'seeds': seeds,
        }

        # Extract tracker from stream name (e.g. "Comet\nThePirateBay", "MediaFusion\nYTS")
        name = stream.get('name', '')
        name_lines = name.strip().split('\n')
        for line in name_lines:
            line = line.strip()
            lower = line.lower()
            if line and lower not in ('comet', 'mediafusion', 'media fusion', 'torrentio', provider_name.lower(), ''):
                result['tracker'] = line
                break

        return result

    # ── Meteor fallback ──
    def _fetch_meteor(self, imdb, season=None, episode=None):
        if season and episode:
            path = '/stream/series/%s:%s:%s.json' % (imdb, season, episode)
        else:
            path = '/stream/movie/%s.json' % imdb

        for base in METEOR_BASES:
            data = _http_get(base + path)
            if data and data.get('streams'):
                _log('Meteor fallback: %s returned %d streams' % (base.split('//')[1][:30], len(data['streams'])))
                return data['streams']
        return []

    # ── Main entry points ──
    def _get_all_results(self, imdb, season=None, episode=None):
        """Try AIOStreams first; on failure fall back to Comet + MediaFusion + Meteor directly."""
        # Primary: AIOStreams
        aio_results = self._fetch_aiostreams(imdb, season, episode)
        if aio_results is not None:
            # Success (even if 0 results — the API responded)
            parsed = [r for r in (self._parse_aiostreams_result(f) for f in aio_results) if r]
            _log('AIOStreams primary: %d raw → %d parsed' % (len(aio_results), len(parsed)))
            return parsed

        # Fallback: Comet + MediaFusion + Meteor (all attempted, deduped by hash)
        _log('AIOStreams down, falling back to Comet + MediaFusion + Meteor')
        all_results = []
        seen_hashes = set()

        comet_streams = self._fetch_comet(imdb, season, episode)
        for s in comet_streams:
            r = self._parse_stremio_stream(s, 'comet')
            if r and r['hash'] not in seen_hashes:
                seen_hashes.add(r['hash'])
                all_results.append(r)

        mf_streams = self._fetch_mediafusion(imdb, season, episode)
        for s in mf_streams:
            r = self._parse_stremio_stream(s, 'mediafusion')
            if r and r['hash'] not in seen_hashes:
                seen_hashes.add(r['hash'])
                all_results.append(r)

        meteor_streams = self._fetch_meteor(imdb, season, episode)
        for s in meteor_streams:
            r = self._parse_stremio_stream(s, 'meteor')
            if r and r['hash'] not in seen_hashes:
                seen_hashes.add(r['hash'])
                all_results.append(r)

        _log('Fallback: Comet %d + MediaFusion %d + Meteor %d → %d unique results' % (
            len(comet_streams), len(mf_streams), len(meteor_streams), len(all_results)))
        return all_results

    def movie(self, title, year, imdb=None, **kwargs):
        self._set_apikeys(kwargs)
        if not imdb:
            return []
        results = self._get_all_results(imdb)
        _log('movie() → %d results' % len(results))
        return results

    def episode(self, simple_info, all_info, **kwargs):
        self._set_apikeys(kwargs)
        imdb = all_info.get('info', {}).get('tvshow.imdb_id', None)
        if imdb is None:
            imdb = all_info.get('info', {}).get('imdb_id', None)
        if imdb is None:
            imdb = all_info.get('showInfo', {}).get('ids', {}).get('imdb', None)
        if not imdb:
            return []
        season = simple_info.get('season_number', '')
        episode_num = simple_info.get('episode_number', '')
        results = self._get_all_results(imdb, season, episode_num)

        # Also try alternative numbering for anime cour splits
        alt_season = simple_info.get('alternative_season', '')
        alt_episode = simple_info.get('alternative_episode', '')
        if alt_season and alt_episode:
            alt_results = self._get_all_results(imdb, alt_season, alt_episode)
            seen_hashes = {r.get('hash', '') for r in results if r.get('hash')}
            results.extend(r for r in alt_results if r.get('hash', '') not in seen_hashes)

        _log('episode() → %d results' % len(results))
        return results
