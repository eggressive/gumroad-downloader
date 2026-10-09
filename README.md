# gumroad-downloader

Download every file of a Gumroad purchase from its `gumroad.com/d/<token>` link, videos included.

Gumroad's `/d/` links are not direct downloads: they redirect to an email confirmation page, and
video products are only exposed as signed HLS renditions. This tool handles both, then proves every
file it wrote actually plays.

```console
$ gumroad-dl https://gumroad.com/d/abcdefababcdefababcdefababcdefab --email buyer@example.com --out ~/Downloads/my-purchase
2 file(s), 418.6 MB declared on the product page
  [ 0] pdf     59.0 MB  Pattern.pdf
  [ 1] video  359.4 MB  01 chin.mp4
start: Pattern.pdf
  pdf validated: 19 pages
start: 01 chin.mp4
  rendition 1920x1080 (playlist title '01 chin')
  video validated: 231s, h264,aac
wrote /home/you/Downloads/my-purchase/SHA256SUMS.txt (2 entries)
wrote /home/you/Downloads/my-purchase/results.jsonl
```

## Install

```console
pip install git+https://github.com/eggressive/gumroad-downloader
```

or from a checkout:

```console
pip install .
```

Requirements:

- Python 3.9+
- `ffmpeg` and `ffprobe` on PATH (video products only)
- `pdfinfo` (poppler-utils) optional, used to report page counts

## Usage

```
gumroad-dl LINK --email EMAIL [options]

  -e, --email      email address used to buy the product (required)
  -o, --out        output directory (default: current directory)
  -l, --list       print the purchase contents and exit
  -n, --dry-run    resolve everything, download nothing
      --only N     download only this content index, repeatable
      --keep-going continue after a per-file failure
      --json       print one JSON result record per downloaded file
      --version
```

`LINK` accepts the full `/d/<token>` URL, any `/s/<token>/...` URL from the same purchase, or the
bare 32 character token.

### Where do I get the token?

The token is the purchase redirect id inside the download link. A buyer finds it in:

- the Gumroad receipt email, via the **View content** button (the link is `gumroad.com/d/<token>`)
- the product page, when Gumroad recognises you as a past buyer (the "you already own this" card)
- [gumroad.com/library](https://gumroad.com/library), with an account created from the buying address
- [gumroad.com/license-key-lookup](https://gumroad.com/license-key-lookup), which re-emails the receipt
- any `/r/<token>/...` or `/s/<token>/...` URL from the same purchase

It cannot be derived from the purchase id, so copy it out of one of those links. Treat it as a
credential: the confirm page checks only the token plus the buyer email, so a link posted publicly
is access posted publicly.

Each run writes two artefacts next to the files:

- `results.jsonl`, one record per file with size, sha256, duration or page count, and status
- `SHA256SUMS.txt`, ready for `sha256sum -c`

Rerunning is safe: a file that already validates is skipped, and a stray `.part` file is resumed
with a Range request instead of being downloaded again.

Exit codes: `0` success, `1` Gumroad or download failure, `2` bad arguments.

## What it does under the hood

1. **Authorise.** `GET /confirm?destination=download_page&id=<token>`, read the Inertia props for the
   `authenticity_token`, then `POST /confirm-redirect` with the buyer email. A confirmation email
   click is not needed, the purchase token plus the matching address is enough. The resulting
   session cookies are what authorise the download endpoints.
2. **Enumerate.** Re-fetch `/d/<token>` with `X-Inertia: true` and read
   `props.content.content_items[]`.
3. **Download files.** Each `download_url` 302s to a signed `files.gumroad.com` object.
4. **Download videos.** Each `stream_url` (`/s/<token>/<file_id>`) renders a player page whose
   `props.playlist[]` lists every video of the purchase. The entry whose `external_id` matches the
   wanted file id is selected, its master playlist is read with the authorised session, and the
   highest bandwidth rendition (1080p when present) is copied into MP4 with `ffmpeg -c copy`.
   No re-encode, so the output is H.264/AAC and plays everywhere.
5. **Validate.** Videos must have a video stream and a duration matching the duration Gumroad
   declares for the item (3 percent tolerance). PDFs must start with `%PDF` and carry a trailing
   `%%EOF`. A record is only written after validation passes.

## Limitations worth knowing

**Videos are Gumroad's streaming renditions, not the original uploads.** The original objects
(`files.gumroad.com/.../original/*.mp4`) answer `403 Invalid token` for every combination of
cookies, referer and user agent, and `/r/<token>/product_files` returns 404 for video ids. The HLS
renditions are the maximum Gumroad exposes, so expect roughly a third of the byte size shown on the
product page (4.5 Mbps instead of about 13 Mbps).

**Stamped PDFs never match the declared size.** Gumroad rewrites purchased PDFs with a personal
digital stamp, so the downloaded byte count is larger than `file_size`. That is why validation uses
page count and the EOF trailer rather than a byte comparison.

**`--email` must match the purchase.** A different address silently re-renders the confirm page;
the tool reports this instead of pretending to succeed.

Only download products you actually bought. The tool uses the purchase link plus the buying
address, which is exactly the access Gumroad grants the buyer.

## Development

```console
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/ruff check .
```

The test suite is fully offline: network entry points are stubbed, so no purchase, token or
credential is needed to run it. Real Gumroad mechanics are exercised through recorded response
shapes (Inertia payloads, master playlists, redirects).

## License

MIT. See [LICENSE](LICENSE).
