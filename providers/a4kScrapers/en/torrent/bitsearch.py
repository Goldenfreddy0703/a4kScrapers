# -*- coding: utf-8 -*-

import html as _html

from providerModules.a4kScrapers import core
from bs4 import BeautifulSoup

class sources(core.DefaultSources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, **kwargs)

    def _soup_filter(self, response):
        soup = BeautifulSoup(response.text, 'html.parser')
        rows = soup.find_all('div', class_=lambda c: c and 'flex' in c and 'items-start' in c)
        results = []
        for row in rows:
            torrent = self.genericScraper._parse_torrent(_html.unescape(str(row)), '<div')
            if torrent is None:
                continue
            seeds_span = row.find('span', class_=lambda c: c and 'text-green-600' in c)
            if seeds_span:
                num_span = seeds_span.find('span', class_=lambda c: c and 'font-medium' in c)
                if num_span:
                    torrent.seeds = num_span.get_text(strip=True)
            results.append(torrent)
        return results
