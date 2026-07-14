# -*- coding: utf-8 -*-

import ctypes
import math
import random
import time
from providerModules.a4kScrapers import core

def _calc_value_alg(t, n, const):
    temp = t ^ n
    t = ctypes.c_long((temp * const)).value
    t4 = ctypes.c_long(t << 5).value
    x32 = t & 0xFFFFFFFF
    t5 = ctypes.c_long(x32 >> 27).value
    return t4 | t5

def _slice(e, t):
    a = math.floor(len(e) / 2)
    s, n = e[0:a], e[a:]
    i, o = t[0:a], t[a:]
    l = ""
    for idx in range(0, a):
        l += s[idx] + i[idx]
    return l + (o[::-1] + n[::-1])

def _generate_hash(e):
    t = ctypes.c_long(int(3735928559) ^ int(len(e))).value
    a = 1103547991 ^ len(e)
    for s in range(len(e)):
        n = ord(e[s])
        t = _calc_value_alg(t, n, 2654435761)
        a = _calc_value_alg(a, n, 1597334677)
    t = ctypes.c_long(t + ctypes.c_long(a * 1566083941).value | 0).value
    a = ctypes.c_long(a + ctypes.c_long(t * 2024237689).value | 0).value
    return (ctypes.c_long(t ^ a).value & 0xFFFFFFFF) >> 0

def _get_secret():
    ran = random.randrange(10**80)
    e = ("%064x" % ran)[:8]
    t = int(time.time())
    a = str(e) + '-' + str(t)
    s = hex(_generate_hash(a)).replace('0x', '')
    n = hex(_generate_hash("debridmediamanager.com%%fe7#td00rA3vHz%VmI-" + e)).replace('0x', '')
    i = _slice(s, n)
    return a, i


class sources(core.DefaultSources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, single_query=True, **kwargs)
        self._filter = core.Filter(fn=self._filter_fn, type='single')

    def _filter_fn(self, title, clean_title):
        if self.is_movie_query():
            return True
        if self.scraper.filter_single_episode.fn(title, clean_title):
            self._filter.type = self.scraper.filter_single_episode.type
            return True
        if self.scraper.filter_show_pack.fn(title, clean_title):
            self._filter.type = self.scraper.filter_show_pack.type
            return True
        if self.scraper.filter_season_pack.fn(title, clean_title):
            self._filter.type = self.scraper.filter_season_pack.type
            return True
        return False

    def _get_scraper(self, title):
        return super(sources, self)._get_scraper(title, custom_filter=self._filter)

    def _search_request(self, url, query):
        imdb = self._imdb
        all_results = []

        # Collect season numbers to query (original + alternative for anime cour)
        # For movie queries, season_x doesn't exist — use a single dummy iteration
        if self.is_movie_query() or not hasattr(self.scraper, 'season_x'):
            seasons_to_query = [None]
        else:
            seasons_to_query = [self.scraper.season_x]
            alt_season = getattr(self.scraper, 'alt_season_x', '')
            if alt_season and alt_season != self.scraper.season_x:
                seasons_to_query.append(alt_season)

        for season_num in seasons_to_query:
            season_results = []

            for page in range(2):
                if page > 0:
                    if not season_results:
                        break  # page 0 was empty — nothing to page through
                    time.sleep(2)  # wider gap to stay under Cloudflare rate limit
                dmm_key, solution = _get_secret()
                if self.is_movie_query():
                    api_path = '/api/torrents/movie?imdbId=%s&page=%s' % (imdb, page)
                else:
                    api_path = '/api/torrents/tv?imdbId=%s&seasonNum=%s&page=%s' % (
                        imdb, season_num, page)
                request_url = url.base + api_path + '&dmmProblemKey=%s&solution=%s' % (
                    core.quote_plus(dmm_key), core.quote_plus(solution))
                try:
                    response = self._request.get(request_url)
                except Exception:
                    break
                if response is None or response.status_code == 429:
                    break  # rate limited — keep what we have from page 0
                if response.status_code != 200:
                    break
                try:
                    data = core.json.loads(response.text)
                except Exception:
                    break
                season_results.extend(data.get('results', []))

            all_results.extend(season_results)
        return all_results

    def _soup_filter(self, response):
        return response

    def _title_filter(self, el):
        return el.get('title', '')

    def _info(self, el, url, torrent):
        torrent['hash'] = el.get('hash', '').upper()
        try:
            torrent['size'] = float(el.get('fileSize', 0))
        except:
            torrent['size'] = 0
        torrent['seeds'] = 0
        return torrent

    def movie(self, title, year, imdb=None, **kwargs):
        self._imdb = imdb
        super(sources, self).movie(title, year, imdb, auto_query=False)
        return self.scraper._get_movie_results()

    def episode(self, simple_info, all_info, **kwargs):
        self._imdb = all_info.get('info', {}).get('tvshow.imdb_id', None)
        if self._imdb is None:
            self._imdb = all_info.get('info', {}).get('imdb_id', None)
        if self._imdb is None:
            self._imdb = all_info.get('showInfo', {}).get('ids', {}).get('imdb', None)
        return super(sources, self).episode(simple_info, all_info)
