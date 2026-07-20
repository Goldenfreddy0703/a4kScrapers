# -*- coding: utf-8 -*-

import html as _html
import re

from providerModules.a4kScrapers import core
from bs4 import BeautifulSoup

RE_SEEDERS = re.compile(r'fa-arrow-up[^"]*"></i>\s*<span[^>]*>\s*(\d+)\s*</span>\s*<span>seeders</span>', re.I)

class sources(core.DefaultSources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, **kwargs)

    def _soup_filter(self, response):
        soup = BeautifulSoup(response.text, 'html.parser')
        rows = soup.find_all('div', class_=lambda c: c and 'items-start' in c)
        results = []
        for row in rows:
            row_html = _html.unescape(str(row))
            torrent = self.genericScraper._parse_torrent(row_html, '<div')
            if torrent is None:
                continue
            seeds_match = RE_SEEDERS.search(row_html)
            if seeds_match:
                torrent.seeds = seeds_match.group(1)
            results.append(torrent)
        return results
