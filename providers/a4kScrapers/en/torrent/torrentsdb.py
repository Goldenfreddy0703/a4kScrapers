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
        all_streams = []

        if self.is_movie_query():
            queries = [self._imdb]
        else:
            queries = [
                self._imdb + ':' + self.scraper.season_x + ':' + self.scraper.episode_x,  # episode
                self._imdb + ':' + self.scraper.season_x,  # season pack
                self._imdb,  # show pack
            ]
            # Alternative numbering for anime cour splits
            alt_s = getattr(self.scraper, 'alt_season_x', '')
            alt_e = getattr(self.scraper, 'alt_episode_x', '')
            if alt_s and alt_e:
                queries.insert(1, self._imdb + ':' + alt_s + ':' + alt_e)
                if alt_s != self.scraper.season_x:
                    queries.insert(2, self._imdb + ':' + alt_s)

        any_success = False
        seen_hashes = set()
        for q in queries:
            request_url = url.base + (url.search % q)
            response = self._request.get(request_url)
            if response is None or response.status_code != 200:
                continue
            any_success = True
            try:
                results = core.json.loads(response.text)
            except:
                continue
            streams = results.get('streams', [])
            for s in (streams or []):
                h = s.get('infoHash', '').lower()
                if h and h not in seen_hashes:
                    seen_hashes.add(h)
                    all_streams.append(s)

        if not any_success:
            self._request.exc_msg = ''
            return None
        return all_streams

    def _soup_filter(self, response):
        return response

    def _title_filter(self, el):
        title = el.get('title', '')
        parts = title.split('\n')
        return parts[0] if parts else ''

    def _info(self, el, url, torrent):
        title = el.get('title', '')
        torrent['hash'] = el.get('infoHash', '').upper()
        torrent['size'] = core.source_utils.de_string_size(
            self.genericScraper.parse_size(title))
        try:
            torrent['seeds'] = int(self.genericScraper.parse_seeds_multi(title, el.get('name', ''), el.get('description', '')))
        except:
            torrent['seeds'] = 0
        if '\n' in torrent['release_title']:
            torrent['release_title'] = torrent['release_title'].split('\n')[0]
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
