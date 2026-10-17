# -*- coding: utf-8 -*-

from providerModules.a4kScrapers import core

# torrents-csv.com: open-source DHT torrent index with a public JSON search API
# (no Cloudflare). Each row already carries the info hash, so no detail page is needed.
_PAGE_SIZE = 50


class sources(core.DefaultSources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, **kwargs)

    def _search_request(self, url, query):
        if not query:
            return []
        request_url = (url.base + url.search) % core.quote_plus(query)
        response = self._request.get(request_url)
        if response is None or response.status_code != 200:
            return None
        try:
            rows = core.json.loads(response.text).get('torrents') or []
        except Exception:
            return None

        results = []
        for row in rows:
            infohash = row.get('infohash') or ''
            if len(infohash) != 40:
                continue
            results.append({
                'title': row.get('name') or '',
                'hash': infohash.upper(),
                'size_mb': int((row.get('size_bytes') or 0) / 1048576),
                'seeds': row.get('seeders') or 0,
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
