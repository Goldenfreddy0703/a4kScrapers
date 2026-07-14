# -*- coding: utf-8 -*-

import threading
from providerModules.a4kScrapers import core

class sources(core.DefaultExtraQuerySources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__, *args, **kwargs)
        # Replace sequental request so page 1+2 list fetches run truly in parallel
        self._request = core.Request(sequental=False)

    def _search_request(self, url, query):
        if isinstance(query, bytes):
            query_str = query.decode('utf-8')
        else:
            query_str = query

        if not url.search.endswith('1/'):
            # Unexpected URL format — single page only
            r = self._request.get((url.base + url.search) % query_str)
            if r is None or r.status_code != 200:
                self._request.exc_msg = ''
                return None
            FakeResponse = type('Response', (), {'status_code': 200, 'text': r.text})
            return FakeResponse()

        page1_url = (url.base + url.search) % query_str
        page2_url = (url.base + url.search[:-2] + '2/') % query_str

        texts = []
        lock = threading.Lock()

        def _fetch(req_url):
            r = self._request.get(req_url)
            if r is not None and r.status_code == 200:
                with lock:
                    texts.append(r.text)

        t1 = threading.Thread(target=_fetch, args=(page1_url,))
        t2 = threading.Thread(target=_fetch, args=(page2_url,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        if not texts:
            self._request.exc_msg = ''
            return None

        FakeResponse = type('Response', (), {'status_code': 200, 'text': '\n'.join(texts)})
        return FakeResponse()
