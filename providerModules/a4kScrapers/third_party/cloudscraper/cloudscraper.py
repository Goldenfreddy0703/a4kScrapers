# Compatibility shim: re-exports so that
#   from .third_party.cloudscraper import cloudscraper
#   cloudscraper.create_scraper(interpreter='native')
# continues to work after the v1.2.42 -> v1.2.60 restructure.
from . import (
    CloudScraper,
    CipherSuiteAdapter,
    create_scraper,
    get_tokens,
    get_cookie_string,
)
