# -*- coding: utf-8 -*-

import re
from providerModules.a4kScrapers import core

class sources(core.DefaultSources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, **kwargs)

    def _search_request(self, url, query):
        if not query:
            return []
        query = core.quote_plus(query)
        any_success = False
        results = []
        for page_suffix in ['', '&p=2']:
            request_url = url.base + (url.search % query) + page_suffix
            response = self._request.get(request_url)
            if response.status_code != 200:
                continue
            any_success = True
            try:
                rows = re.findall(r'<tr[^>]*>(.*?)</tr>', response.text, re.DOTALL)
            except:
                continue
            for row in rows:
                try:
                    if '<th' in row or 'nofollow' in row:
                        continue
                    columns = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                    if len(columns) < 4:
                        continue
                    link_match = re.search(r'href\s*=\s*["\']/(.*?)["\']>', columns[0], re.I)
                    if not link_match:
                        continue
                    link_parts = link_match.group(1).split('/')
                    if len(link_parts) < 2:
                        continue
                    hash_val = link_parts[0]
                    title = link_parts[1].replace('&amp;', '&')
                    size_text = re.sub(r'<[^>]+>', '', columns[2]).strip() if len(columns) > 2 else ''
                    try:
                        seeds = int(re.sub(r'<[^>]+>', '', columns[3]).strip().replace(',', ''))
                    except:
                        seeds = 0
                    results.append({
                        'title': title,
                        'hash': hash_val,
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
