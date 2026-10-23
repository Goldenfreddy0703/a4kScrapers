# -*- coding: utf-8 -*-

import re
from urllib.parse import unquote_plus
from providerModules.a4kScrapers import core

# TorrentDownload (ported from POV 6.10.20). Result rows link to /<40-char hash>/<release-name>, so
# the hash and the full release name come straight from the search page (the visible link text is
# truncated); size and seeders are in the row's own cells. Up to two result pages are read; the second
# only when the first came back (nearly) full - a full page holds 50 rows.
_FULL_PAGE_MIN_ROWS = 40
_ROW_RE = re.compile(
    r'href="/([0-9A-Fa-f]{40})/([^"]+)".*?'
    r'<td class="tdnormal">[^<]*</td>\s*<td class="tdnormal">([^<]*)</td>\s*<td class="tdseed">([^<]*)</td>',
    re.DOTALL)


class sources(core.DefaultSources):
    def __init__(self, *args, **kwargs):
        # The site answers parallel requests with sporadic 404s (page 1 as well as page 2), so every
        # query of one scrape goes through a single sequential request queue.
        kwargs.setdefault('request', core.Request(sequental=True, wait=0.5))
        super(sources, self).__init__(__name__, *args, **kwargs)

    def _search_request(self, url, query):
        if not query:
            return []
        request_url = (url.base + url.search) % core.quote_plus(query)
        any_success = False
        results = []
        for page in ['', '&p=2']:
            response = self._request.get(request_url + page)
            if response is None or response.status_code != 200:
                continue
            any_success = True
            rows = _ROW_RE.findall(response.text)
            for hash_, slug, size_text, seeds_text in rows:
                try:
                    seeds = int(seeds_text.replace(',', '').strip() or 0)
                except Exception:
                    seeds = 0
                results.append({
                    'title': unquote_plus(slug).replace('&amp;', '&'),
                    'hash': hash_.upper(),
                    'size_text': size_text.strip(),
                    'seeds': seeds,
                })
            if not page and len(rows) < _FULL_PAGE_MIN_ROWS:
                break
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
        except Exception:
            torrent['size'] = 0
        torrent['seeds'] = el.get('seeds', 0)
        return torrent
