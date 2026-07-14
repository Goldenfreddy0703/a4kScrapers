# a4kScrapers

Expanded torrent provider pack for **[Prism](https://github.com/Goldenfreddy0703/Prism)**, [Seren](https://github.com/nixgates/plugin.video.seren), and [a4kStreaming](https://github.com/a4k-openproject/a4kStreaming).

This is a maintained fork of the original [a4kScrapers](https://github.com/a4k-openproject/a4kScrapers) project, updated with additional scrapers, anime-focused providers, and Stremio-style aggregators for use with **Prism**, Seren, and a4kStreaming.

**Current version:** `2.99.126`

## Install

In Prism, Seren, or a4kStreaming, set the a4kScrapers provider URL to:

```
https://api.github.com/repos/Goldenfreddy0703/a4kScrapers/zipball
```

No GitHub Pages or build step required — pushing to `master` is the release.

## Scrapers



### Stremio / aggregators

`aiostreams`, `comet`, `dmm`, `mediafusion`, `meteor`, `torrentio`, `torrentsdb`, `torz`, `zilean`, `bitmagnet`

### General torrent sites

`bitsearch`, `eztv`, `kickass`, `kickass2`, `knaben`, `leet` (1337x), `magnetdl`, `piratebay`, `torrentdownload`, `torrentproject2`, `torrentz2`, `yts`

### Anime

`animetosho`, `anirena`, `nekobt`, `nyaa`, `subsplease`

### Other

`cached`, `showrss`

## Updating

When you change scrapers:

1. Edit files in `providers/` or `providerModules/`
2. Bump `version` in `meta.json`
3. Commit and push to `master`

Prism, Seren, and a4kStreaming check `remote_meta` for the new version and pull updates automatically.

## Repo structure

```
providers/          # Individual scraper modules (one file per site)
providerModules/    # Shared framework (core, request, urls, etc.)
meta.json           # Version and update URLs
```



## Credits

- Original project: [a4k-openproject/a4kScrapers](https://github.com/a4k-openproject/a4kScrapers)
- Updated scrapers and framework: [minhgi](https://github.com/minhgi)
- Maintained by [Goldenfreddy0703](https://github.com/Goldenfreddy0703) for [Prism](https://github.com/Goldenfreddy0703/Prism)

