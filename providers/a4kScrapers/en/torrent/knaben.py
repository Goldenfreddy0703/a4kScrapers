# -*- coding: utf-8 -*-

import re
from providerModules.a4kScrapers import core

class sources(core.DefaultSources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, **kwargs)

    def _search_request(self, url, query):
        if not query:
            return []
        probe = self._request.get(url.base + '/')
        title_match = re.search(r'<title[^>]*>([^<]*)</title>', probe.text, re.I)
        if probe.status_code != 200 or not title_match or 'Knaben' not in title_match.group(1):
            self._request.exc_msg = ''
            return None
        query = core.quote_plus(query)
        cat = '003000000' if self.is_movie_query() else '002000000'
        request_url = url.base + '/search/index.php?cat=%s&q=%s&search=fast' % (cat, query)
        response = self._request.get(request_url)
        if response.status_code != 200:
            return []
        try:
            rows = re.findall(r'<tr[^>]*class="text-nowrap border-start"[^>]*>(.*?)</tr>', response.text, re.DOTALL)
        except:
            return []
        results = []
        for row in rows:
            try:
                columns = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                if len(columns) < 5:
                    continue
                magnet_raw = columns[1]
                magnet_match = re.search(r'(magnet:.+?)(?:&tr=|")', magnet_raw, re.I)
                if not magnet_match:
                    continue
                magnet_url = magnet_match.group(1).replace('&amp;', '&').replace(' ', '.')
                dn_match = re.search(r'&dn=(.+?)(?:&|$)', magnet_url)
                title = dn_match.group(1).replace('+', ' ') if dn_match else ''
                hash_match = re.search(r'btih:(.*?)&', magnet_url, re.I)
                if not hash_match:
                    continue
                size_text = re.sub(r'<[^>]+>', '', columns[2]).strip() if len(columns) > 2 else ''
                try:
                    seeds = int(re.sub(r'<[^>]+>', '', columns[4]).strip().replace(',', ''))
                except:
                    seeds = 0
                results.append({
                    'title': title,
                    'hash': hash_match.group(1),
                    'size_text': size_text,
                    'seeds': seeds,
                    'magnet': magnet_url
                })
            except:
                continue
        return results

    def _soup_filter(self, response):
        return response

    def _title_filter(self, el):
        return el.get('title', '')

    def _info(self, el, url, torrent):
        torrent['hash'] = el.get('hash', '')
        try:
            torrent['size'] = core.source_utils.de_string_size(el.get('size_text', ''))
        except:
            torrent['size'] = 0
        torrent['seeds'] = el.get('seeds', 0)
        return torrent
