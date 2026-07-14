# -*- coding: utf-8 -*-

import threading
import time
import traceback
import sys
import re
import os
import json
import requests

from collections import OrderedDict
from . import source_utils
from .source_utils import tools
from .third_party.cloudscraper import cloudscraper
from .common_types import UrlParts

# ── curl_cffi optional import (added v2.99.108) ──────────────────────────────
# Uses chrome120 TLS impersonation to bypass Cloudflare bot detection (CF v1/v2/v3+).
# Requires the separate script.module.curl_cffi Kodi addon.
# Falls back silently to cloudscraper v1.2.60 when not available (Kodi 21,
# Android without Kodi 22, or module not installed).
#
# Two-stage import: first try a direct import (works when Seren's addon.xml
# declares the dependency or Kodi has already added the module to sys.path);
# second stage uses xbmcaddon to locate and inject the lib/ path manually —
# works even when the optional dependency is NOT declared in addon.xml.
_curl_cffi_available = False
try:
    import curl_cffi.requests as _curl_cffi_requests
    _curl_cffi_available = True
except ImportError:
    try:
        import xbmcaddon as _xbmcaddon
        import sys as _sys
        import os as _os
        _ccfi_lib = _os.path.join(
            _xbmcaddon.Addon('script.module.curl_cffi').getAddonInfo('path'), 'lib')
        if _ccfi_lib not in _sys.path:
            _sys.path.insert(0, _ccfi_lib)
        import curl_cffi.requests as _curl_cffi_requests
        _curl_cffi_available = True
    except Exception:
        pass


class _CurlCffiSession(object):
    """Wraps curl_cffi.Session with the same interface as cloudscraper so
    _request_core / _get / post can call it without modification."""

    _STRIP_HEADERS = frozenset(['x-domain', 'cookie'])
    IMPERSONATE = 'chrome120'

    def __init__(self):
        self._session = _curl_cffi_requests.Session()

    @property
    def cookies(self):
        return self._session.cookies

    def _clean_headers(self, headers):
        """Remove internal-only / session-conflicting headers before passing
        to curl_cffi (X-Domain is a4kScrapers bookkeeping; Cookie is managed
        by the curl_cffi session itself)."""
        if not headers:
            return {}
        return {k: v for k, v in headers.items()
                if k.lower() not in self._STRIP_HEADERS}

    def request(self, method, url, headers=None, timeout=10,
                allow_redirects=True, **kwargs):
        return self._session.request(
            method, url,
            headers=self._clean_headers(headers),
            timeout=timeout,
            allow_redirects=allow_redirects,
            impersonate=self.IMPERSONATE,
        )

    def post(self, url, data=None, headers=None, timeout=10, **kwargs):
        return self._session.post(
            url,
            data=data,
            headers=self._clean_headers(headers),
            timeout=timeout,
            impersonate=self.IMPERSONATE,
        )


from .utils import database, cache_get, cache_save
from requests.compat import urlparse, urlunparse

_head_checks = {}

# ── Per-domain circuit breaker (added v2.99.60) ─────────────────────────────
# Goal: when a host (e.g. animetosho.org) is unreachable, every scraper firing
# multiple queries against it pays the full TCP-retry cost on each call,
# blocking the rest of the scraping pipeline. After _CB_FAILURE_THRESHOLD
# consecutive failures within this Python process, mark the domain dead for
# _CB_COOLDOWN_SECONDS — subsequent calls return a synthetic 503 in
# microseconds. Counter resets on the first successful response.
_CB_FAILURE_THRESHOLD = 3
_CB_COOLDOWN_SECONDS  = 600   # 10 minutes
_cb_lock              = threading.Lock()
_cb_failures          = {}    # domain -> consecutive failure count
_cb_dead_until        = {}    # domain -> wall-clock timestamp when cooldown ends
_cb_logged_trip       = set() # domains we have already logged the trip for (avoid log spam)


def _circuit_is_open(domain):
    """True if domain is currently in cooldown (calls should fast-fail)."""
    if not domain:
        return False
    with _cb_lock:
        until = _cb_dead_until.get(domain, 0)
        if until and time.time() < until:
            return True
        if until and time.time() >= until:
            # Cooldown expired — half-open: clear so next request gets a chance
            _cb_dead_until.pop(domain, None)
            _cb_failures[domain] = 0
            _cb_logged_trip.discard(domain)
        return False


def _circuit_record_failure(domain):
    """Increment failure counter; trip breaker when threshold reached."""
    if not domain:
        return
    with _cb_lock:
        _cb_failures[domain] = _cb_failures.get(domain, 0) + 1
        if _cb_failures[domain] >= _CB_FAILURE_THRESHOLD:
            _cb_dead_until[domain] = time.time() + _CB_COOLDOWN_SECONDS
            if domain not in _cb_logged_trip:
                tools.log(
                    'circuit_breaker: %s tripped after %d failures, cooling down %ds' % (
                        domain, _cb_failures[domain], _CB_COOLDOWN_SECONDS),
                    'notice')
                _cb_logged_trip.add(domain)


def _circuit_record_success(domain):
    """Reset failure counter on successful response."""
    if not domain:
        return
    with _cb_lock:
        if _cb_failures.get(domain, 0) > 0:
            _cb_failures[domain] = 0
        _cb_dead_until.pop(domain, None)
        _cb_logged_trip.discard(domain)


def _make_circuit_breaker_response(url):
    """Build a synthetic 503 Response object the request layer can return
    immediately when the breaker is open. Matches the shape that callers
    of _request_core / get / head expect."""
    resp = lambda: None
    resp.status_code = 503
    resp.url = url
    resp.text = ''
    resp.headers = {}
    resp.content = b''
    return resp


def _request_cache_save(key, cache):
    data = cache = OrderedDict(sorted(cache.items()))
    cache_save(key, data)

def _update_request_options(request_options):
    domain = _get_domain(request_options['url'])
    headers = cache_get(domain)
    if not headers:
        headers = {}
    headers['X-Domain'] = domain
    request_options.setdefault('headers', {})
    request_options['headers'].update(headers)

def _save_cf_cookies(cfscrape, response):
    cookies = ''

    set_cookie = response.headers.get('Set-Cookie', '')
    cf_cookies = re.findall(r'(PHPSESSID|__cf.*?|cf.*?)=(.*?);', set_cookie)
    cookies_dict = {key: value for (key, value) in cf_cookies}

    cf_cookies = re.findall(r'(PHPSESSID|__cf.*?|cf.*?)=(.*?);', response.request.headers.get('Cookie', ''))
    original_cookies = {key: value for (key, value) in cf_cookies}

    for key in original_cookies.keys():
            if cookies_dict.get(key, None) is None:
                cookies_dict[key] = original_cookies[key]

    try:
        cf_cookies = cfscrape.cookies.items()
        for (key, value) in cf_cookies:
            cookies_dict[key] = value
    except: pass

    cookies_dict = OrderedDict(sorted(cookies_dict.items()))
    for key in cookies_dict.keys():
        cookies += '%s=%s; ' % (key, cookies_dict[key])

    cookies = cookies.strip()
    if cookies == '':
        return

    headers = {
        'User-Agent': response.request.headers['User-Agent'],
        'Cookie': cookies.strip()
    }

    cache_key = response.request.headers['X-Domain']
    _request_cache_save(cache_key, headers)

def _get(cfscrape, url, headers, timeout, allow_redirects, update_options_fn):
    request_options = {
        'method': 'GET',
        'url': url,
        'headers': headers,
        'timeout': timeout,
        'allow_redirects': allow_redirects,
    }

    if update_options_fn is not None:
        update_options_fn(request_options)

    return cfscrape.request(**request_options)

def _is_cloudflare_iuam_challenge(resp, allow_empty_body=False):
    try:
        return (
            resp.headers.get('Server', '').startswith('cloudflare')
            and resp.status_code in [429, 503]
            and (allow_empty_body or re.search(
                r'action="/.*?__cf_chl_jschl_tk__=\S+".*?name="jschl_vc"\svalue=.*?',
                resp.text,
                re.M | re.DOTALL
            ))
        )
    except AttributeError:
        pass

    return False

def _get_domain(url): 
    parsed_url = urlparse(url)
    scheme = parsed_url.scheme if parsed_url.scheme != '' else 'https'
    return "%s://%s" % (scheme, parsed_url.netloc)

def _get_head_check(url):
    result = _head_checks.get(url, None)
    if isinstance(result, bool):
        return (url, result)
    elif result is not None:
        return _get_head_check(result)

    return (url, None)

class Request(object):
    def __init__(self, sequental=False, timeout=None, wait=1):
        self._request = source_utils.randomUserAgentRequests()
        if _curl_cffi_available:
            self._cfscrape = _CurlCffiSession()
            tools.log('curl_cffi: active (chrome120 impersonation)', 'debug')
        else:
            self._cfscrape = cloudscraper.create_scraper(interpreter='native')
        self._sequental = sequental
        self._wait = wait
        self._should_wait = False
        self._lock = threading.Lock()
        self._timeout = 10
        if timeout is not None:
            self._timeout = timeout
        self.exc_msg = ''
        self.skip_head = False
        self.request_time = 99

    def _verify_response(self, response):
      if response.status_code >= 400:
          self.exc_msg = 'response status code %s' % response.status_code
          if response.status_code in [429, 503]:
            self.exc_msg = '%s (probably Cloudflare)' % self.exc_msg
          raise Exception()

    def _request_core(self, request, sequental = None, cf_retries=3):
        self.exc_msg = ''

        # ── Circuit breaker fast-fail (added v2.99.60) ──────────────────────
        # If this domain has tripped the breaker, return a synthetic 503
        # immediately rather than waiting for TCP retries to complete.
        try:
            cb_url    = getattr(request, 'url', None)
            cb_domain = _get_domain(cb_url) if cb_url else None
            if cb_domain and _circuit_is_open(cb_domain):
                self.exc_msg = 'circuit breaker open'
                return _make_circuit_breaker_response(cb_url)
        except Exception:
            cb_domain = None

        if sequental is None:
            sequental = self._sequental

        response_err = lambda: None
        response_err.status_code = 501

        try:
            response = None
            if sequental is False:
                self._request_start = time.time()
                response = request(None)
                self._request_end = time.time()
                self.request_time = round(self._request_end - self._request_start)

                response_err = response
                self._verify_response(response)

                _circuit_record_success(cb_domain)
                return response

            with self._lock:
                if self._should_wait:
                    time.sleep(self._wait)
                self._should_wait = True
                self._request_start = time.time()
                response = request(_update_request_options)
                self._request_end = time.time()
                self.request_time = round(self._request_end - self._request_start)

            response_err = response
            self._verify_response(response)

            try:
                if self.exc_msg == '' and response.request.headers.get('X-Domain', None) is not None:
                    _save_cf_cookies(self._cfscrape, response)
            except: pass

            _circuit_record_success(cb_domain)
            return response
        except:
            self._request_end = time.time()
            self.request_time = round(self._request_end - self._request_start)

            if self.exc_msg == '':
              exc = traceback.format_exc(limit=1)
              if 'PreemptiveCancellation' in exc:
                raise Exception("PreemptiveCancellation")

              if cf_retries <= 0:
                  _circuit_record_failure(cb_domain)
                  return response_err

              if 'Detected the new Cloudflare challenge.' in exc and self.request_time < 2:
                  cf_retries -= 1
                  tools.log('cf_new_challenge_retry: %s' % request.url, 'notice')
                  return self._request_core(request, sequental, cf_retries)
              elif 'Cloudflare' in exc or '!!Loop Protection!!' in exc:
                  self.exc_msg = 'failed Cloudflare protection'
              elif 'Max retries exceeded with url' in exc:
                  self.exc_msg = 'Max retries exceeded'
              elif 'ConnectTimeout' in exc or 'ReadTimeout' in exc:
                  self.exc_msg = 'request timed out'
              else:
                  self.exc_msg = 'failed - %s' % exc
            elif 'response status code 500' in self.exc_msg:
                  cf_retries -= 1
                  return self._request_core(request, sequental, cf_retries)

            tools.log('%s %s' % (request.url, self.exc_msg), 'notice')

            _circuit_record_failure(cb_domain)
            return response_err

    def _check_redirect(self, src, response):
        if response.status_code in [301, 302]:
            redirect_url = response.headers['Location']
            if not redirect_url.endswith('127.0.0.1') and not redirect_url.endswith('localhost') and response.url != redirect_url:
                dest = redirect_url
                src_clean = re.sub(r'https?://', '', src)
                dest_clean = re.sub(r'https?://', '', _get_domain(dest))
                if src_clean != dest_clean or 'https://' in dest:
                  return dest
        return False

    def _head(self, url):
        global _head_checks

        if self.skip_head:
            return (url, 200)

        (url, head_check) = _get_head_check(url)
        if head_check:
            return (url, 200)
        elif head_check is False:
            return (url, 500)

        url = _get_domain(url)
        tools.log('HEAD: %s' % url, 'debug')
        request = lambda _: self._request.head(url, timeout=2, allow_redirects=True)
        request.url = url

        try:
            response = self._request_core(request, sequental=False)
            if _is_cloudflare_iuam_challenge(response, allow_empty_body=True):
                response = lambda: None
                response.url = url
                response.status_code = 200

            if response.status_code >= 400:
                response = lambda: None
                response.url = url
                response.status_code = 200

        except:
            response = lambda: None
            response.url = url
            response.status_code = 200

        try:
            head_check_key = _get_domain(response.url)
        except:
            response.url = url
            head_check_key = _get_domain(url)

        redirect_url = self._check_redirect(head_check_key, response)
        if redirect_url:
            _head_checks[head_check_key] = redirect_url
            return self._head(redirect_url)

        _head_checks[head_check_key] = response.status_code == 200

        return (response.url, response.status_code)

    def head(self, url):
        return database.get(self._head, 12, url)

    def find_url(self, urls):
        for url in urls:
            (response_url, status_code) = self.head(url.base)
            if status_code != 200:
                continue

            if response_url.endswith("/"):
                response_url = response_url[:-1]

            return UrlParts(base=response_url, search=url.search, default_search=url.default_search)

        return None

    def get(self, url, headers={}, allow_redirects=True):
        parsed_url = urlparse(url)

        response = self.head(_get_domain(url))
        if response is None:
            return None

        (url, status_code) = response
        if status_code != 200:
            return None

        resolved_url = urlparse(url)
        url = urlunparse(
            (
                resolved_url.scheme,
                resolved_url.netloc,
                parsed_url.path,
                parsed_url.params,
                parsed_url.query,
                parsed_url.fragment,
            )
        )

        tools.log('GET: %s' % url, 'debug')
        request = lambda x: _get(self._cfscrape, url, headers, self._timeout, allow_redirects, x)
        request.url = url

        return self._request_core(request)

    def post(self, url, data, headers={}):
        tools.log('POST: %s' % url, 'debug')
        request = lambda _: self._cfscrape.post(url, data, headers=headers, timeout=self._timeout)
        request.url = url
        return self._request_core(request)
