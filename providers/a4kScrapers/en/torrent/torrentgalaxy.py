# -*- coding: utf-8 -*-

from providerModules.a4kScrapers import core

# TorrentGalaxy: public JSON search API (ported from POV 6.10.20). Each row already carries the
# info hash ('h'), name ('n'), size in bytes ('s') and seeders ('se'), so no detail page is needed.


class sources(core.DefaultSources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, **kwargs)

    def _search_request(self, url, query):
        if not query:
            return []
        response = self._request.get((url.base + url.search) % core.quote_plus(query))
        if response is None or response.status_code != 200:
            return None
        try:
            rows = core.json.loads(response.text).get('results') or []
        except Exception:
            return None

        results = []
        for row in rows:
            infohash = row.get('h') or ''
            if len(infohash) != 40:
                continue
            results.append({
                'title': row.get('n') or '',
                'hash': infohash.upper(),
                'size_mb': int((row.get('s') or 0) / 1048576),
                'seeds': row.get('se') or 0,
            })
        return results

    def _soup_filter(self, response):
        return response

    def _title_filter(self, el):
        return el.get('title', '')

    def _info(self, el, url, torrent):
        torrent['hash'] = el.get('hash', '')
        torrent['size'] = el.get('size_mb', 0)
        torrent['seeds'] = el.get('seeds', 0)
        return torrent
