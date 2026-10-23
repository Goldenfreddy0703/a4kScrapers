# -*- coding: utf-8 -*-

import xml.etree.ElementTree as ET

from providerModules.a4kScrapers import core

# Torlock: public Torznab (RSS/XML) search endpoint (ported from POV 6.10.20). The HTML pages sit
# behind Cloudflare, the /torznab/api route does not. Each <item> carries the info hash and seeders
# as torznab:attr entries and the size in bytes in <size>.
_TORZNAB_NS = {'torznab': 'http://torznab.com/schemas/2015/feed'}


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
            root = ET.fromstring(response.text)
        except Exception:
            return None

        results = []
        for item in root.iter('item'):
            try:
                attrs = {}
                for attr in item.findall('torznab:attr', _TORZNAB_NS):
                    attrs[attr.get('name')] = attr.get('value')
                infohash = attrs.get('infohash') or ''
                if len(infohash) != 40:
                    continue
                results.append({
                    'title': item.find('title').text or '',
                    'hash': infohash.upper(),
                    'size_mb': int(int(item.find('size').text or 0) / 1048576),
                    'seeds': int(attrs.get('seeders') or 0),
                })
            except Exception:
                continue
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
