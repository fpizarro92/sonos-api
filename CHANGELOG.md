# Changelog

## [2.4.2](https://github.com/fpizarro92/sonos-api/compare/v2.4.1...v2.4.2) (2026-09-28)


### Bug Fixes

* **stream:** remove invalid yt-dlp --extract-flat option causing 502 Bad Gateway ([1466f2c](https://github.com/fpizarro92/sonos-api/commit/1466f2c9977a70e0abcbb7e401a8f5a02970b524))

## [2.4.1](https://github.com/fpizarro92/sonos-api/compare/v2.4.0...v2.4.1) (2026-09-28)


### Bug Fixes

* persist youtube stream tokens in sqlite and delegate mcp to http api ([b06a078](https://github.com/fpizarro92/sonos-api/commit/b06a07843e95706098a97e2fa7b351e7d94ed884))

## [2.4.0](https://github.com/fpizarro92/sonos-api/compare/v2.3.0...v2.4.0) (2026-09-28)


### Features

* resilient youtube playlist resolution, 20-track limits, and infinite queue feeder ([143448b](https://github.com/fpizarro92/sonos-api/commit/143448bfe796bd33a005ac556338d4b6c214f358))

## [2.3.0](https://github.com/fpizarro92/sonos-api/compare/v2.2.0...v2.3.0) (2026-09-28)


### Features

* auto-detect youtube playlist urls and resolve tracks across all modes ([e7a6427](https://github.com/fpizarro92/sonos-api/commit/e7a64270409ed626d65ecd5e31aa66f41564d076))


### Bug Fixes

* prevent yt-dlp timeout on youtube music album and artist resolution ([d6ca0cf](https://github.com/fpizarro92/sonos-api/commit/d6ca0cf710525b46c0b1d7ee537bad7f4cfde726))

## [2.2.0](https://github.com/fpizarro92/sonos-api/compare/v2.1.0...v2.2.0) (2026-09-27)


### Features

* add track search mode, album and live version filters, and sonos_search_music MCP tool ([be659e9](https://github.com/fpizarro92/sonos-api/commit/be659e91a67644368e210ab5db80958042d1b50b))


### Bug Fixes

* add stream preheating, url normalization, playback confirmation, and auto-retry on stopped ([9835878](https://github.com/fpizarro92/sonos-api/commit/9835878ae37011a9e9143384697fe0a35970e2f1))

## [2.1.0](https://github.com/fpizarro92/sonos-api/compare/v2.1.0...v2.1.0) (2026-09-27)


### Features

* enforce artist filter in track resolution and Samba library search ([8f48df9](https://github.com/fpizarro92/sonos-api/commit/8f48df9badf6cf3b7e91897e6b37aaa892519300))
* initial commit for sonos-api with MCP server, SQLite caching and UPnP playback controls ([3a162e2](https://github.com/fpizarro92/sonos-api/commit/3a162e2a8d3f5f78671408eedb4b89481b10a42a))


### Miscellaneous Chores

* release 2.1.0 ([34c100d](https://github.com/fpizarro92/sonos-api/commit/34c100d219893e1edbab9fb02e760589bc39b99e))
