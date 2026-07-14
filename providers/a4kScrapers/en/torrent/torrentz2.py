# -*- coding: utf-8 -*-

import re as _re
from bs4 import BeautifulSoup
from providerModules.a4kScrapers import core

TOP_N = 5

class sources(core.DefaultSources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, **kwargs)

    def _search_request(self, url, query):
        if not query:
            return []
        query = core.quote_plus(query)
        search_url = (url.base + url.search) % query
        response = self._request.get(search_url)
        if response is None or response.status_code != 200:
            return []

        soup = BeautifulSoup(response.text, 'html.parser')
        candidates = []
        seen = set()
        for a in soup.find_all('a', href=lambda h: h and _re.match(r'^/torrent/[0-9a-f]{24}$', h)):
            mongoid = a['href'].rsplit('/', 1)[-1]
            if mongoid in seen:
                continue
            seen.add(mongoid)
            title = a.get_text(strip=True)
            if not title:
                continue
            candidates.append({'mongoid': mongoid, 'title': title})
            if len(candidates) >= TOP_N:
                break

        results = []
        for candidate in candidates:
            if self._cancellation_token.is_cancellation_requested:
                break
            try:
                detail_url = url.base + '/torrent/' + candidate['mongoid']
                detail_resp = self._request.get(detail_url)
                if detail_resp is None or detail_resp.status_code != 200:
                    continue
                detail_soup = BeautifulSoup(detail_resp.text, 'html.parser')
                magnet_tag = detail_soup.find('a', href=lambda h: h and h.startswith('magnet:'))
                if not magnet_tag:
                    continue
                magnet = magnet_tag['href']

                seeds = 0
                seeds_match = _re.search(r'[Ss]eeds\D{0,5}(\d+)', detail_resp.text)
                if seeds_match:
                    try:
                        seeds = int(seeds_match.group(1))
                    except:
                        pass

                size_text = ''
                size_match = _re.search(r'(\d+(?:\.\d+)?\s*(?:GB|MB|KB|TB))', detail_resp.text, _re.I)
                if size_match:
                    size_text = size_match.group(1)

                results.append({
                    'title': candidate['title'],
                    'size_text': size_text,
                    'seeds': seeds,
                    'magnet': magnet,
                })
            except:
                continue

        return results

    def _soup_filter(self, response):
        return response

    def _title_filter(self, el):
        return el.get('title', '')

    def _info(self, el, url, torrent):
        magnet = el.get('magnet', '')
        hash_match = _re.search(r'btih:([0-9A-Fa-f]+)', magnet)
        if hash_match:
            torrent['hash'] = hash_match.group(1).upper()
        try:
            torrent['size'] = core.source_utils.de_string_size(el.get('size_text', ''))
        except:
            torrent['size'] = 0
        torrent['seeds'] = el.get('seeds', 0)
        return torrent
