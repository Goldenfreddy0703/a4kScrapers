# -*- coding: utf-8 -*-

import re
import threading
import time

from collections import deque
from providerModules.a4kScrapers import core

# 1337xx.to is a 1337x look-alike without Cloudflare, but its search matches ANY word of the query
# (so "the dark knight 2008" returns whatever contains "the") and it rate-limits hard: ~8 requests
# in ~10 s, then HTTP 429 (Retry-After 1). The real 1337x domains sit behind a Cloudflare challenge.
#
# So: search for the single most distinctive word of the query inside the Movies/TV category (sorted by
# seeders), pull a few pages of that pool, and let the core scraper's own title/year/episode filters pick
# the right releases out of it. Every request made by this scraper goes through a shared pacer so the
# limiter is never tripped (a 429 counts toward a4kScrapers' per-domain circuit breaker).

_PACE_MAX_REQUESTS = 6
_PACE_WINDOW_SECONDS = 10.5
_pace_lock = threading.Lock()
_pace_times = {}

_MAX_POOL_PAGES = 3
_ROWS_PER_PAGE = 20
_ENOUGH_MATCHES = 5
_POOL_TTL_SECONDS = 180
_POOL_MAX_ENTRIES = 40
_pool_lock = threading.Lock()
_pool = {}

_STOP_WORDS = {'the', 'and', 'of', 'a', 'an', 'in', 'on', 'to', 'for', 'at', 'by', 'vs', 'with', 'from', 'part', 'chapter', 'vol', 'volume'}
_MARKER_WORDS = {'season', 'complete', 'batch', 'pack', 'episode'}
_MARKER_RE = re.compile(r'\bs\d{1,2}e\d{1,3}\b|\bs\d{1,2}\b|\bseason\s*\d+\b|\b(?:19|20)\d{2}\b')


def _pace(url):
    # one window per host: a dead or blocked fallback domain must not eat the working domain's request budget
    host = re.sub(r'^https?://', '', url).split('/')[0].lower()

    with _pace_lock:
        times = _pace_times.setdefault(host, deque())
        now = time.time()
        while times and now - times[0] > _PACE_WINDOW_SECONDS:
            times.popleft()

        if len(times) >= _PACE_MAX_REQUESTS:
            wait = times[0] + _PACE_WINDOW_SECONDS - now
            if wait > 0:
                time.sleep(min(wait, _PACE_WINDOW_SECONDS))
            now = time.time()
            while times and now - times[0] > _PACE_WINDOW_SECONDS:
                times.popleft()

        times.append(time.time())


class _PacedRequest(core.Request):
    def get(self, url, headers={}, allow_redirects=True):
        _pace(url)
        response = super(_PacedRequest, self).get(url, headers, allow_redirects)

        if response is not None and getattr(response, 'status_code', 200) == 429:
            time.sleep(1.5)
            _pace(url)
            response = super(_PacedRequest, self).get(url, headers, allow_redirects)

        return response


def _words(query):
    query = _MARKER_RE.sub(' ', query.lower())
    return [w for w in re.findall(r'[a-z0-9]+', query) if w not in _MARKER_WORDS]


def _pick_word(query):
    words = _words(query)
    candidates = [w for w in words if w not in _STOP_WORDS and not w.isdigit()]
    if not candidates:
        candidates = [w for w in re.findall(r'[a-z0-9]+', query.lower()) if w not in _STOP_WORDS and w not in _MARKER_WORDS]
    if not candidates:
        return None

    # longest word, first one on a tie: long words are rarer, so the seeder-sorted pool holds fewer unrelated rows
    best = candidates[0]
    for word in candidates[1:]:
        if len(word) > len(best):
            best = word

    return best


def _count_matches(html, query):
    # only used to decide whether another page of the pool is worth fetching; the core scraper does the real filtering
    lowered = query.lower()
    tokens = [w for w in _words(query) if w not in _STOP_WORDS]
    tokens += re.findall(r'\b(?:19|20)\d{2}\b', lowered)
    if not tokens:
        return 0

    episode = re.search(r'\bs(\d{1,2})e(\d{1,3})\b', lowered)
    season = re.search(r'\bs(\d{1,2})\b|\bseason\s*(\d+)\b', lowered)
    if episode:
        episode = 's%02de%02d' % (int(episode.group(1)), int(episode.group(2)))
    elif season:
        season = int(season.group(1) or season.group(2))

    count = 0
    for slug in re.findall(r'href="/torrent/\d+/([^"/]+)/?"', html):
        title = re.sub(r'[^a-z0-9]+', ' ', slug.lower())
        if not all(token in title for token in tokens):
            continue
        if isinstance(episode, str) and episode not in title.replace(' ', ''):
            continue
        if isinstance(season, int) and ('s%02d' % season not in title and 'season %d' % season not in title):
            continue
        count += 1

    return count


def _page_url(url, word, page):
    search = url.search
    if page > 1:
        search = search[:-2] + '%d/' % page

    return (url.base + search) % word


def _get_page(request, url, word, page):
    key = (url.base, url.search, word, page)

    # held across the fetch on purpose: parallel query threads for the same title would otherwise
    # both miss the cache and fetch the same pool twice, doubling the load against the rate limit
    with _pool_lock:
        now = time.time()
        hit = _pool.get(key)
        if hit is not None and now - hit[0] < _POOL_TTL_SECONDS:
            return hit[1]

        response = request.get(_page_url(url, word, page))
        if response is None or getattr(response, 'status_code', None) != 200:
            return None

        if len(_pool) >= _POOL_MAX_ENTRIES:
            for stale_key in sorted(_pool, key=lambda k: _pool[k][0])[:_POOL_MAX_ENTRIES // 2]:
                _pool.pop(stale_key, None)

        _pool[key] = (now, response.text)
        return response.text


class sources(core.DefaultExtraQuerySources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, **kwargs)
        self._request = _PacedRequest(sequental=False)

    # 1337xx.to rows: an icon link first, the title link second, seeds in <td class="coll-2 seeds">
    def _find_title(self, el):
        return el.find_all('a')[1].text.strip()

    def _find_seeds(self, el):
        return el.find('td', class_='seeds').text.strip()

    def _search_request(self, url, query):
        if isinstance(query, bytes):
            query = query.decode('utf-8')

        word = _pick_word(query)
        if word is None or not url.search.endswith('1/'):
            self._request.exc_msg = ''
            return None

        pages = []
        matches = 0
        for page in range(1, _MAX_POOL_PAGES + 1):
            html = _get_page(self._request, url, word, page)
            if html is None:
                break

            pages.append(html)
            matches += _count_matches(html, query)
            if matches >= _ENOUGH_MATCHES or len(re.findall(r'href="/torrent/\d+/', html)) < _ROWS_PER_PAGE:
                break

        if not pages:
            self._request.exc_msg = ''
            return None

        FakeResponse = type('Response', (), {'status_code': 200, 'text': '\n'.join(pages)})
        return FakeResponse()
