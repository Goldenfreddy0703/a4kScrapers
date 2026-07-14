# -*- coding: utf-8 -*-

from providerModules.a4kScrapers import core

class sources(core.DefaultSources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, single_query=True, **kwargs)
        self._filter = core.Filter(fn=self._filter_fn, type='single')

    def _filter_fn(self, title, clean_title):
        if self.is_movie_query():
            return False
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
        all_results = []

        if self.is_movie_query():
            param_sets = ['ImdbId=%s' % self._imdb]
        else:
            param_sets = [
                'ImdbId=%s&Season=%s&Episode=%s' % (self._imdb, self.scraper.season_x, self.scraper.episode_x),  # episode
                'ImdbId=%s&Season=%s' % (self._imdb, self.scraper.season_x),  # season pack
            ]
            # Alternative numbering for anime cour splits
            alt_s = getattr(self.scraper, 'alt_season_x', '')
            alt_e = getattr(self.scraper, 'alt_episode_x', '')
            if alt_s and alt_e:
                param_sets.append('ImdbId=%s&Season=%s&Episode=%s' % (self._imdb, alt_s, alt_e))
                if alt_s != self.scraper.season_x:
                    param_sets.append('ImdbId=%s&Season=%s' % (self._imdb, alt_s))

        seen_hashes = set()
        any_success = False
        for params in param_sets:
            request_url = url.base + url.search + '?' + params
            response = self._request.get(request_url)
            if response is None or response.status_code != 200:
                continue
            any_success = True
            try:
                results = core.json.loads(response.text)
            except:
                continue
            for item in (results or []):
                h = item.get('info_hash', item.get('infoHash', '')).lower()
                if h and h not in seen_hashes:
                    seen_hashes.add(h)
                    all_results.append(item)

        if not any_success:
            self._request.exc_msg = ''
            return None
        return all_results

    def _soup_filter(self, response):
        return response

    def _title_filter(self, el):
        return el.get('raw_title', el.get('rawTitle', ''))

    def _info(self, el, url, torrent):
        torrent['hash'] = el.get('info_hash', el.get('infoHash', '')).upper()
        try:
            torrent['size'] = int(el.get('size', 0)) / 1024 / 1024
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
