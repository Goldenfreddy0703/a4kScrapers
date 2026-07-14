# -*- coding: utf-8 -*-

import re
from urllib.parse import unquote
from providerModules.a4kScrapers import core

class sources(core.DefaultSources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, **kwargs)

    def _search_request(self, url, query):
        if not query:
            return []
        query = core.quote_plus(query)
        if self.is_movie_query():
            search_path = '/usearch/%s%%20category:movies/?field=size&sorder=desc' % query
        else:
            search_path = '/usearch/%s%%20category:tv/?field=size&sorder=desc' % query
        any_success = False
        results = []
        for page_suffix in ['', '/2/']:
            request_url = url.base + search_path + page_suffix
            response = self._request.get(request_url)
            if response.status_code != 200:
                continue
            any_success = True
            try:
                rows = re.findall(r'<tr[^>]*id="torrent_latest_torrents"[^>]*>(.*?)</tr>', response.text, re.DOTALL)
            except:
                continue
            for row in rows:
                try:
                    columns = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                    if len(columns) < 4:
                        continue
                    raw = unquote(columns[0].replace('&amp;', '&'))
                    magnet_match = re.search(r'(magnet:.+?)(?:&tr=|")', raw, re.I)
                    if not magnet_match:
                        continue
                    magnet_url = magnet_match.group(1).replace(' ', '.')
                    hash_match = re.search(r'btih:(.*?)&', magnet_url, re.I)
                    if not hash_match:
                        continue
                    dn_match = re.search(r'&dn=(.*?)(?:&|$)', magnet_url)
                    title = dn_match.group(1) if dn_match else ''
                    size_text = re.sub(r'<[^>]+>', '', columns[1]).strip() if len(columns) > 1 else ''
                    try:
                        seeds = int(re.sub(r'<[^>]+>', '', columns[3]).strip().replace(',', ''))
                    except:
                        seeds = 0
                    results.append({
                        'title': title,
                        'hash': hash_match.group(1),
                        'size_text': size_text,
                        'seeds': seeds,
                    })
                except:
                    continue
        if not any_success:
            self._request.exc_msg = ''
            return None
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
