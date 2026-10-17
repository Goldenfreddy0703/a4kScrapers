# -*- coding: utf-8 -*-

import re
import threading
from providerModules.a4kScrapers import core

# MagnetDL's result list no longer carries magnet links: each row only links to a
# /single/<id> detail page that shows the info hash. Fetch the detail pages of the
# best-seeded rows in parallel and hand the hashes to the generic scraper.
_MAX_DETAIL = 8


class sources(core.DefaultSources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, **kwargs)

    def _search_request(self, url, query):
        if not query:
            return None
        query_str = core.quote_plus(query)
        search_url = url.base + url.search % query_str
        response = self._request.get(search_url, headers={'Accept': 'text/html'})
        if response is None or response.status_code != 200:
            return response

        text = re.sub(r'<!--.*?-->', '', response.text, flags=re.DOTALL)
        rows = []
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', text, re.DOTALL):
            link = re.search(r'/single/(\d+)["\'][^>]*>([^<]+)</a>', row)
            if not link:
                continue
            cells = [re.sub(r'<[^>]+>', '', c).strip() for c in re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)]
            try:
                seeds = int(cells[5].replace(',', ''))
            except (IndexError, ValueError):
                seeds = 0
            rows.append({'id': link.group(1),
                         'title': link.group(2).strip(),
                         'size_text': cells[4] if len(cells) > 4 else '',
                         'seeds': seeds,
                         'hash': ''})

        rows.sort(key=lambda r: r['seeds'], reverse=True)
        rows = rows[:_MAX_DETAIL]

        def fetch(row):
            try:
                if self._cancellation_token.is_cancellation_requested:
                    return
                detail = self._request.get(url.base + '/single/' + row['id'], headers={'Accept': 'text/html'})
                if detail is None or detail.status_code != 200:
                    return
                found = re.search(r'Info Hash:</dt>\s*<dd>\s*([0-9A-Fa-f]{40})', detail.text)
                if found:
                    row['hash'] = found.group(1).lower()
            except Exception:
                pass

        threads = [threading.Thread(target=fetch, args=(r,)) for r in rows]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        results = [r for r in rows if r['hash']]
        if not results:
            self._request.exc_msg = ''
        return results

    def _soup_filter(self, response):
        return response if isinstance(response, list) else []

    def _title_filter(self, el):
        return el.get('title', '')

    def _info(self, el, url, torrent):
        torrent['hash'] = el.get('hash', '')
        try:
            torrent['size'] = core.source_utils.de_string_size(el.get('size_text', ''))
        except Exception:
            torrent['size'] = 0
        torrent['seeds'] = el.get('seeds', 0)
        return torrent
