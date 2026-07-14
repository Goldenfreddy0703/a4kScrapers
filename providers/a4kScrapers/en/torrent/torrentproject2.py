# -*- coding: utf-8 -*-

import re
from urllib.parse import unquote
from providerModules.a4kScrapers import core

class sources(core.DefaultExtraQuerySources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, request_timeout=5, **kwargs)

    def _soup_filter(self, response):
        """Return the result <div> rows from #similarfiles.

        torrentproject2 uses <div> rows (not <tr>) for its results, so the
        default GenericExtraQueryTorrentScraper.soup_filter (find_all('tr'))
        yields nothing.  We locate #similarfiles and return only the children
        that carry a real torrent link (href containing 'torrent.html').
        """
        soup = core.beautifulSoup(response)
        container = soup.find('div', id='similarfiles')
        if container is None:
            return []
        return [div for div in container.find_all('div', recursive=False)
                if div.find('a', href=lambda h: h and 'torrent.html' in h)]

    def _parse_magnet(self, row):
        """Extract magnet from individual torrent pages.

        torrentproject2 embeds the magnet link URL-encoded inside a
        mylink.cloud redirect (href="https://mylink.cloud/?url=magnet%3A%3F…").
        A single urllib.parse.unquote pass converts %3A→: and %26→& so the
        standard regex can find the literal 'magnet:?xt=urn:btih:…' string.
        """
        decoded = unquote(row)
        match = re.search(r'(magnet:\?xt=urn:btih:[A-Fa-f0-9]+.*?)(?:&tr=|")', decoded, re.I)
        if match:
            return match.group(1)
        # Fallback: grab hash from the download_magnet span title attribute
        hash_match = re.search(r"download_magnet[^>]+title=['\"]([0-9a-fA-F]{40})", decoded, re.I)
        if hash_match:
            return 'magnet:?xt=urn:btih:%s' % hash_match.group(1).upper()
        return None

    def _find_title(self, el):
        try:
            links = el.find_all('a')
            for link in links:
                href = link.get('href', '')
                if 'torrent.html' in href:
                    return link.text.strip()
        except:
            pass
        return el.text.split('\n')[1] if len(el.text.split('\n')) > 1 else ''

    def _find_url(self, el):
        try:
            links = el.find_all('a')
            for link in links:
                href = link.get('href', '')
                if 'torrent.html' in href:
                    return href
        except:
            pass
        return ''

    def _find_seeds(self, el):
        """Extract seeds from the second top-level <span> of the result row.

        Each result div has the structure:
          <span>title + sub-spans</span> <span>seeds</span> <span>leechers</span>
          <span>age</span> <span>size</span>
        The previous regex approach (last digits in el.text) hit the size
        column (e.g. '1.9 GB') and returned '0'.  Using recursive=False
        targets only direct-child spans so index 1 is reliably the seed count.
        """
        try:
            spans = el.find_all('span', recursive=False)
            if len(spans) >= 2:
                return spans[1].text.strip()
        except:
            pass
        return '0'
