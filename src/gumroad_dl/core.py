"""Core logic for gumroad-dl.

How a Gumroad ``/d/<token>`` link works
---------------------------------------
``https://gumroad.com/d/<token>`` never serves a file. It redirects to
``/confirm?destination=download_page&id=<token>``, whose props carry
``content_unavailability_reason_code: email_confirmation_required`` until the buyer
email is submitted. Posting that email to ``/confirm-redirect`` mints an authorised
session, and the same token then renders the download page.

Files come in two flavours:

* non-video files (PDF, images) expose ``download_url``, which 302s to a signed
  ``files.gumroad.com`` URL that needs no cookies.
* video files expose only ``stream_url`` (``/s/<token>/<file_id>``). The original
  ``.../original/*.mp4`` object answers ``403 Invalid token`` for every cookie,
  referer and user agent combination, and ``/r/<token>/product_files`` returns 404
  for video ids. The signed HLS renditions (480p and 1080p, already H.264/AAC) are
  the only retrievable form.

Everything in this module is importable without network access; only the
``requests.Session`` based helpers talk to Gumroad.
"""

from __future__ import annotations

import dataclasses
import html
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

import requests

BASE = "https://gumroad.com"
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)
APP_DIV_RE = re.compile(r'<div id="app" data-page="(.*?)"></div>', re.S)
TOKEN_RE = re.compile(r"([0-9a-f]{32})", re.I)
ILLEGAL_FS = re.compile(r'[<>:"/\\|?*]')
DURATION_TOLERANCE = 0.03
DURATION_FLOOR = 5.0


class GumroadError(RuntimeError):
    """Raised when Gumroad refuses a step or returns an unexpected shape."""


class ToolMissing(GumroadError):
    """Raised when a required external binary is not on PATH."""


@dataclasses.dataclass
class Item:
    """One entry of the purchase's content list."""

    index: int
    name: str
    ext: str
    file_id: str
    size: int | None = None
    duration: float | None = None
    download_url: str | None = None
    stream_url: str | None = None

    @property
    def is_video(self) -> bool:
        return bool(self.stream_url)

    @property
    def filename(self) -> str:
        return f"{self.name}.{self.ext}" if self.ext else self.name


def extract_token(link: str) -> str:
    """Accept a full ``/d/<token>`` URL, ``/s/<token>/...`` URL, or a bare token."""
    link = link.strip()
    match = TOKEN_RE.search(link)
    if not match:
        raise GumroadError(f"no 32 hex character purchase token found in {link!r}")
    return match.group(1).lower()


def sanitize(name: str) -> str:
    """Collapse whitespace and strip characters that are illegal in file names."""
    name = re.sub(r"\s+", " ", (name or "").strip())
    name = ILLEGAL_FS.sub("_", name)
    return name.strip(" .") or "unnamed"


def parse_app_div(page_html: str) -> dict:
    """Return the Inertia page props embedded in a Gumroad HTML response."""
    match = APP_DIV_RE.search(page_html)
    if not match:
        raise GumroadError("response contains no Inertia app div")
    try:
        return json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise GumroadError(f"unparseable Inertia payload: {exc}") from exc


def items_from_content(props: dict) -> list:
    """Build :class:`Item` objects from ``props['content']['content_items']``."""
    content = (props or {}).get("content") or {}
    raw_items = content.get("content_items") or []
    items = []
    for position, raw in enumerate(raw_items):
        items.append(
            Item(
                index=position,
                name=sanitize(raw.get("file_name") or f"item{position}"),
                ext=(raw.get("extension") or "").lower(),
                file_id=raw.get("id") or "",
                size=raw.get("file_size"),
                duration=raw.get("duration"),
                download_url=raw.get("download_url"),
                stream_url=raw.get("stream_url"),
            )
        )
    if not items:
        raise GumroadError("purchase has no content items")
    return items


def choose_playlist_entry(entries: Sequence[dict], file_id: str) -> dict:
    """Pick the playlist entry for ``file_id``.

    The ``/s/`` page lists every video of the purchase, so selecting the first
    entry silently downloads the wrong video. Match on ``external_id`` first and
    only fall back to a single-entry playlist.
    """
    for entry in entries or []:
        if entry.get("external_id") == file_id:
            return entry
    if len(entries or []) == 1:
        return entries[0]
    seen = [entry.get("external_id") for entry in (entries or [])][:4]
    raise GumroadError(f"no playlist entry with external_id={file_id!r} (have {seen})")


def hls_source(entry: dict, base: str = BASE) -> str:
    """Return the absolute master playlist URL of a playlist entry."""
    for source in entry.get("sources") or []:
        if source.startswith("/") and source.endswith(".m3u8"):
            return base + source
    raise GumroadError("playlist entry has no m3u8 source")


def best_variant(master_text: str) -> tuple:
    """Return ``(bandwidth, resolution, url)`` for the highest quality rendition."""
    if not master_text.lstrip().startswith("#EXTM3U"):
        raise GumroadError("not an m3u8 master playlist")
    lines = master_text.splitlines()
    best = None
    for position, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF") and position + 1 < len(lines):
            bandwidth = int(re.search(r"BANDWIDTH=(\d+)", line).group(1))
            resolution = re.search(r"RESOLUTION=(\d+x\d+)", line)
            if best is None or bandwidth > best[0]:
                best = (bandwidth, resolution.group(1) if resolution else "?", lines[position + 1])
    if best is None:
        raise GumroadError("master playlist has no variants")
    return best


def duration_ok(actual: float, declared: float | None) -> bool:
    """True when a rendition's duration matches the declared one within tolerance."""
    if not declared:
        return actual > 0
    return abs(actual - declared) <= max(DURATION_FLOOR, DURATION_TOLERANCE * declared)


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise ToolMissing(f"{name} is required but was not found on PATH")
    return path


# --------------------------------------------------------------------------------------
# network helpers
# --------------------------------------------------------------------------------------


def new_http_session(user_agent: str = DEFAULT_UA) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {"User-Agent": user_agent, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    )
    return session


def authorize(session: requests.Session, token: str, email: str, timeout: int = 40) -> None:
    """Submit the buyer email and leave ``session`` authorised for the purchase."""
    confirm_url = f"{BASE}/confirm?destination=download_page&id={token}"
    response = session.get(confirm_url, timeout=timeout)
    props = parse_app_div(response.text).get("props", {})
    authenticity = props.get("authenticity_token")
    if not authenticity:
        raise GumroadError("confirm page carried no authenticity_token")
    posted = session.post(
        f"{BASE}/confirm-redirect",
        data={
            "utf8": "\u2713",
            "authenticity_token": authenticity,
            "id": token,
            "destination": "download_page",
            "display": "",
            "email": email,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": BASE,
            "Referer": confirm_url,
        },
        timeout=timeout,
    )
    if "content_items" not in posted.text:
        raise GumroadError(
            "Gumroad did not accept that email for this purchase "
            "(the confirm page was returned again)"
        )


def fetch_items(session: requests.Session, token: str, timeout: int = 60) -> list:
    """Return the purchase manifest for an authorised session."""
    response = session.get(
        f"{BASE}/d/{token}",
        headers={
            "X-Inertia": "true",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "text/html, application/xhtml+xml",
        },
        timeout=timeout,
    )
    payload = response.text
    if payload.lstrip().startswith("{"):
        props = json.loads(payload).get("props", {})
    else:
        props = parse_app_div(payload).get("props", {})
    if props.get("content_unavailability_reason_code"):
        raise GumroadError(
            "purchase is not accessible: " + str(props["content_unavailability_reason_code"])
        )
    return items_from_content(props)


def video_variant(session: requests.Session, item: Item, token: str) -> tuple:
    """Resolve the highest quality rendition of a video item.

    The master playlist needs the authorised session; the rendition URLs it lists
    are CloudFront signed URLs that do not.
    """
    response = session.get(
        BASE + (item.stream_url or ""),
        headers={"Referer": f"{BASE}/d/{token}"},
        timeout=60,
    )
    props = parse_app_div(response.text).get("props", {})
    entry = choose_playlist_entry(props.get("playlist") or [], item.file_id)
    master_url = hls_source(entry)
    master = session.get(master_url, headers={"Referer": f"{BASE}/d/{token}"}, timeout=60)
    bandwidth, resolution, variant = best_variant(master.text)
    return variant, resolution, bandwidth, entry.get("title")


def file_redirect(session: requests.Session, item: Item, token: str) -> str:
    """Resolve the signed download URL of a non-video file."""
    if not item.download_url:
        raise GumroadError(f"{item.filename} has no download_url")
    response = session.get(
        BASE + item.download_url,
        allow_redirects=False,
        headers={"Referer": f"{BASE}/d/{token}"},
        timeout=60,
    )
    location = response.headers.get("location")
    if not location:
        raise GumroadError(f"{item.filename}: expected a redirect, got HTTP {response.status_code}")
    return location


# --------------------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------------------


def probe_video(path: Path) -> dict:
    ffprobe = require_binary("ffprobe")
    completed = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise GumroadError(f"ffprobe failed on {path.name}: {completed.stderr[-200:]}")
    return json.loads(completed.stdout)


def validate_video(path: Path, declared_duration: float | None) -> dict:
    """Raise unless the file is a playable video whose duration matches the listing."""
    info = probe_video(path)
    streams = info.get("streams") or []
    video = [stream for stream in streams if stream.get("codec_type") == "video"]
    if not video:
        raise GumroadError(f"{path.name}: no video stream")
    duration = float((info.get("format") or {}).get("duration") or 0)
    if duration <= 0:
        raise GumroadError(f"{path.name}: zero duration")
    if not duration_ok(duration, declared_duration):
        raise GumroadError(
            f"{path.name}: duration {duration:.0f}s does not match declared {declared_duration}s"
        )
    return {
        "duration": duration,
        "resolution": f"{video[0].get('width')}x{video[0].get('height')}",
        "codecs": ",".join(stream.get("codec_name", "") for stream in streams),
    }


def validate_pdf(path: Path) -> dict:
    """Structurally validate a PDF.

    Gumroad rewrites purchased PDFs with a personal digital stamp, so the
    downloaded byte count never equals the declared ``file_size``. Page count and
    a trailing ``%%EOF`` are the reliable signals.
    """
    raw = path.read_bytes()
    if not raw.startswith(b"%PDF"):
        raise GumroadError(f"{path.name}: not a PDF")
    if b"%%EOF" not in raw[-4096:]:
        raise GumroadError(f"{path.name}: missing %%EOF trailer")
    pages = None
    if shutil.which("pdfinfo"):
        completed = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True)
        match = re.search(r"Pages:\s+(\d+)", completed.stdout)
        if match:
            pages = int(match.group(1))
    if pages is None:
        pages = len(re.findall(rb"/Type\s*/Page[^s]", raw))
    if not pages:
        raise GumroadError(f"{path.name}: no pages found")
    return {"pages": pages}


def sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------------------
# downloading
# --------------------------------------------------------------------------------------


def download_http(session: requests.Session, url: str, dest: Path, referer: str | None = None,
                  timeout: int = 120) -> int:
    """Stream a URL to ``dest``, resuming a ``.part`` file when one is present."""
    part = dest.with_suffix(dest.suffix + ".part")
    done = part.stat().st_size if part.exists() else 0
    headers = {"Referer": referer} if referer else {}
    if done:
        headers["Range"] = f"bytes={done}-"
    with session.get(url, stream=True, timeout=timeout, headers=headers) as response:
        if response.status_code not in (200, 206):
            raise GumroadError(f"HTTP {response.status_code} for {url[:80]}")
        if response.status_code == 200 and done:
            done = 0
        written = done
        with part.open("ab" if done else "wb") as handle:
            for chunk in response.iter_content(1 << 20):
                handle.write(chunk)
                written += len(chunk)
    os.replace(part, dest)
    return written


def download_hls(url: str, dest: Path, referer: str | None = None) -> None:
    """Copy an HLS rendition into an MP4 without re-encoding."""
    ffmpeg = require_binary("ffmpeg")
    require_binary("ffprobe")
    temporary = dest.with_suffix(dest.suffix + ".part.mp4")
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
    ]
    if referer:
        command += ["-headers", f"Referer: {referer}\r\n"]
    command += ["-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc",
                "-movflags", "+faststart", str(temporary)]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise GumroadError(f"ffmpeg failed: {completed.stderr[-300:]}")
    os.replace(temporary, dest)


def already_complete(item: Item, dest: Path) -> dict | None:
    """Return validation info when ``dest`` is already a valid copy of ``item``."""
    if not dest.exists() or dest.stat().st_size < (1 << 20):
        return None
    try:
        if item.is_video:
            return validate_video(dest, item.duration)
        if item.ext == "pdf":
            return validate_pdf(dest)
    except (GumroadError, ValueError, OSError):
        return None
    return None


def download_item(session: requests.Session, token: str, item: Item, outdir: Path,
                  logger: Callable[[str], None] = print) -> dict:
    """Download and validate a single item. Returns the result record."""
    dest = outdir / item.filename
    cached = already_complete(item, dest)
    if cached:
        logger(f"skip (already complete): {item.filename}")
        return {
            "index": item.index, "name": item.name, "ext": item.ext, "path": str(dest),
            "bytes": dest.stat().st_size, "expected": item.size, "sha256": sha256(dest),
            "status": "ok", "source": "cached", **cached,
        }
    started = time.time()
    logger(f"start: {item.filename}")
    info: dict = {}
    if item.is_video:
        variant, resolution, _bandwidth, title = video_variant(session, item, token)
        logger(f"  rendition {resolution} (playlist title {title!r})")
        download_hls(variant, dest, referer=f"{BASE}/d/{token}")
        info = validate_video(dest, item.duration)
        source = f"hls-{resolution}"
        logger(f"  video validated: {info.get('duration'):.0f}s, {info.get('codecs')}")
    else:
        signed = file_redirect(session, item, token)
        written = download_http(session, signed, dest, referer=f"{BASE}/d/{token}")
        if item.size and written < item.size * 0.5:
            raise GumroadError(f"{item.filename} is implausibly small ({written} bytes)")
        info = validate_pdf(dest) if item.ext == "pdf" else {}
        source = "direct"
        if info.get("pages"):
            logger(f"  pdf validated: {info['pages']} pages")
    return {
        "index": item.index, "name": item.name, "ext": item.ext, "path": str(dest),
        "bytes": dest.stat().st_size, "expected": item.size, "sha256": sha256(dest),
        "status": "ok", "source": source, "seconds": round(time.time() - started, 1), **info,
    }


def write_sha256sums(outdir: Path, records: Iterable[dict]) -> Path:
    target = outdir / "SHA256SUMS.txt"
    lines = [f"{record['sha256']}  {Path(record['path']).name}"
             for record in records if record.get("sha256")]
    target.write_text("\n".join(lines) + "\n")
    return target


def human_size(num_bytes: int | None) -> str:
    if not num_bytes:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"
