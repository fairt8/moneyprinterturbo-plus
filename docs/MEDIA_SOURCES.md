# Stock media sources

Checked on 31 October 2026 against the live pages linked below. This note is not legal advice. Pixabay stays the primary stock source. Openverse and Wikimedia Commons are fallbacks, and this project keeps only CC0 and public-domain files from those two. Unsplash is not integrated.

The Docker stack searches in the order set by `VIDEO_SOURCES` (default `pixabay,openverse,wikimedia`). `VIDEO_SOURCE` names one provider and moves it to the front of that list. A provider is skipped when its request errors or when the clips found so far do not cover the narration. Each finished task writes `storage/tasks/<task_id>/sources.json` next to the video, with the provider, page URL, creator, and license of every clip. The existing retention job deletes the whole task folder, including that file.

Still images from the fallbacks are turned into short clips with an FFmpeg Ken Burns zoom and pan. `MPT_LOW_MEMORY=true` (the Docker default) renders those clips at 720p with one encode thread.

## Pixabay

- API: <https://pixabay.com/api/docs/>
- Content License summary: <https://pixabay.com/service/license-summary/>
- Full terms, including the Content License: <https://pixabay.com/service/terms/>
- Privacy: <https://pixabay.com/service/privacy/>

The video search used here is `GET https://pixabay.com/api/videos/`. The docs say the API returns royalty-free media under the Pixabay Content License. That license is not CC0. The summary allows free commercial use and adaptation, and says attribution is appreciated but not required. It forbids selling or distributing the file on a standalone basis, using recognizable brands as if they were yours, and misleading or unlawful use. The terms also say some files, including CC0 files, can still carry trademarks, privacy rights, or similar rights.

The API docs (retrieved 31 October 2026) set the default limit at 100 requests per 60 seconds per API key, not per IP address. Response headers are `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset`. Over the limit, the API returns HTTP 429. The same page asks clients to cache responses for 24 hours and says systematic mass downloads are not allowed. This app already caches stock searches for 24 hours.

The privacy policy says Pixabay logs requests, may infer location from the IP address, and uses cookies and similar technologies on the website. API calls from this app send the search term and the API key. The key is a query parameter, so it is redacted from logs.

## Openverse

- API reference, including Register and Authenticate: <https://api.openverse.org/v1/>
- Image search: `GET https://api.openverse.org/v1/images/`
- Privacy: <https://wordpress.org/openverse/privacy/>
- CC0 1.0 deed: <https://creativecommons.org/publicdomain/zero/1.0/>
- Public Domain Mark 1.0 deed: <https://creativecommons.org/publicdomain/mark/1.0/>

The image-search parameters documented on the API page include `license`. The accepted license values include `cc0` and `pdm`. This app sends `license=cc0,pdm` and drops any hit whose `license` field is not exactly `cc0` or `pdm`. `cc0` is recorded as CC0. `pdm` is recorded as Public domain. CC BY, CC BY-SA, non-commercial, and no-derivatives results are discarded even if the server returns them. Results whose `source` is `unsplash` are discarded. Audio search is not used.

The API page says anonymous and registered clients are rate limited, that a registered application gets a higher limit after the email address is verified, and that scraping is disallowed by the Terms of Service. It tells clients to read `X-RateLimit-*` headers. A request to `GET https://api.openverse.org/v1/images/` on 31 October 2026 returned:

- `X-RateLimit-Limit-anon_burst: 20/min`
- `X-RateLimit-Limit-anon_sustained: 200/day`

Those anonymous numbers can change. HTTP 429 means the limit was hit. Optional `OPENVERSE_CLIENT_ID` and `OPENVERSE_CLIENT_SECRET` are exchanged at `POST https://api.openverse.org/v1/auth_tokens/token/` with `grant_type=client_credentials`. The access token is sent as `Authorization: Bearer`. The client secret is not written into `sources.json` or info logs.

The privacy page says Openverse follows the WordPress.org privacy policy, uses strictly necessary cookies on the website, and uses anonymous analytics that do not identify a person. API searches from this app send the query string and a User-Agent that contains `STOCK_MEDIA_CONTACT` or the project URL.

CC0 waives copyright to the extent the law allows. It does not waive trademarks, publicity, or privacy rights, and it does not promise that the dedication is valid. The Public Domain Mark identifies a work as free of known copyright restrictions. It is not a license, it may not hold in every country, and moral rights can remain. Do not imply that the creator endorses the video.

## Wikimedia Commons

- User-Agent policy: <https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy>
- API etiquette: <https://www.mediawiki.org/wiki/API:Etiquette>
- Rate limits: <https://www.mediawiki.org/wiki/Wikimedia_APIs/Rate_limits>
- Imageinfo, including `extmetadata`: <https://www.mediawiki.org/wiki/API:Imageinfo>
- Reusing content: <https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia>
- Licensing policy: <https://commons.wikimedia.org/wiki/Commons:Licensing>
- Privacy policy: <https://foundation.wikimedia.org/wiki/Policy:Privacy_policy>

Search uses the MediaWiki Action API on `https://commons.wikimedia.org/w/api.php` with `generator=search` in the File namespace and `prop=imageinfo`. Images and `webm` / `ogv` videos are eligible. A file is kept only when `extmetadata.LicenseShortName` is CC0 (including a trailing version such as "CC0 1.0") or Public domain (including "Public domain" followed by more words, such as a mark). CC BY, CC BY-SA, GFDL, and other short names are dropped. The file description page is stored as the page URL. The Artist value, with HTML removed, is stored as the creator.

The User-Agent policy requires a descriptive agent with contact information, and it says generic agents such as `python-requests` may be blocked. This app sends `MoneyPrinterTurbo/1.3.8 (<contact>) bot`. Set `STOCK_MEDIA_CONTACT` to an email address or a page you control. Do not copy a browser User-Agent.

API etiquette says to send requests one after another, to use `maxlag` for non-interactive work, and to ask for several titles in one request. `extmetadata` is documented as expensive, so each search asks for at most 8 files and only `LicenseShortName`, `LicenseUrl`, and `Artist`. Requests are spaced at least 1.1 seconds apart and include `maxlag=5`.

The rate-limit page, current as of 2026 and subject to change, lists unidentified clients at 10 requests per minute and unauthenticated clients with a compliant User-Agent at 200 requests per minute. HTTP 429 or 503 responses should be retried only after `Retry-After`, or after at least five seconds when that header is missing. This app treats those responses as a provider failure and moves to the next source instead of retrying in a tight loop.

The reuse page says almost all Commons files are free to reuse, but each file has its own attribution and license terms, and the Foundation does not warrant the copyright status. Public-domain files may not legally require attribution, and the page still recommends attribution for provenance. Personality rights, trademarks, and moral rights can still apply. Confirm the file page before you publish a video of an identifiable person.

The privacy policy treats the IP address and the User-Agent as personal information that Wikimedia receives automatically when a site or API is used. Searches send the query and the User-Agent. They do not send an account cookie.

## What is written next to the video

`sources.json` looks like this:

```json
{
  "clips": [
    {
      "provider": "openverse",
      "page_url": "https://example.org/photos/1",
      "creator": "Example Creator",
      "license": "CC0",
      "local_file": "vid-abc.mp4"
    }
  ]
}
```

Pixabay, Pexels, and Coverr clips are labeled with those providers' own license names. Openverse and Wikimedia clips use `CC0` or `Public domain`. Download URLs and API keys are not stored.
