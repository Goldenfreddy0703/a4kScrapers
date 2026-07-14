# -*- coding: utf-8 -*-

from providerModules.a4kScrapers import core

class sources(core.DefaultSources):
    def __init__(self, *args, **kwargs):
        super(sources, self).__init__(__name__,
                                      *args,
                                      request=core.Request(sequental=False, timeout=15),
                                      **kwargs)

    def _search_request(self, url, query):
        response = super(sources, self)._search_request(url, query)
        if response is None or response.status_code != 200:
            self._request.exc_msg = ''
            return None
        return response
