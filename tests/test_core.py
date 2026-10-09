"""Offline tests for gumroad_dl.core (no network, no ffmpeg required)."""

from __future__ import annotations

import json

import pytest

from gumroad_dl import core

PDF_HEAD = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R>>endobj

"""
PDF_TAIL = b"trailer<</Root 1 0 R>>\n%%EOF\n"


def make_pdf(padding: bytes = b"") -> bytes:
    """A minimal one page PDF, optionally padded the way stamped downloads are."""
    return PDF_HEAD + padding + PDF_TAIL


class FakeResponse:
    def __init__(self, text="", status_code=200, headers=None, chunks=()):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)

    def iter_content(self, _size=0):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeSession:
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append(("GET", url, kwargs))
        return self.routes[url]

    def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        return self.routes[url]


def app_page(props: dict) -> str:
    import html as html_module

    escaped = html_module.escape(json.dumps(props), quote=True)
    return f'<html><body><div id="app" data-page="{escaped}"></div></body></html>'


# --------------------------------------------------------------------------- tokens

# Synthetic fixture values. They keep the shape of the real ones (32 hex characters, a
# base64 file id) but stay far below the entropy an entropy based scanner needs to call
# something a secret (about 2.5 bits per character here, the usual gate is 3.5), so a
# placeholder can never be a live Gumroad purchase id. Never paste a real token here.
TOKEN = "abcdefababcdefababcdefababcdefab"
FILE_ID = "AAAAAAAAAAAAAAAAAAAAAA=="


def test_extract_token_from_download_link():
    link = f"https://gumroad.com/d/{TOKEN.upper()}"
    assert core.extract_token(link) == TOKEN


def test_extract_token_from_stream_link():
    link = f"https://gumroad.com/s/{TOKEN}/{FILE_ID}"
    assert core.extract_token(link) == TOKEN


def test_extract_token_rejects_junk():
    with pytest.raises(core.GumroadError):
        core.extract_token("https://gumroad.com/d/not-a-token")


# -------------------------------------------------------------------------- naming


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  01 chin  & nose   ", "01 chin & nose"),
        ("06 crotch", "06 crotch"),
        ("weird/name:with*illegal", "weird_name_with_illegal"),
        ("", "unnamed"),
        ("...", "unnamed"),
    ],
)
def test_sanitize(raw, expected):
    assert core.sanitize(raw) == expected


# ------------------------------------------------------------------------ manifest


def test_items_from_content_builds_items_and_flags_videos():
    props = {
        "content": {
            "content_items": [
                {"file_name": "Pattern", "extension": "PDF", "file_size": 100, "id": "pdf1",
                 "download_url": "/r/tok/product_files?product_file_ids%5B%5D=pdf1",
                 "stream_url": None, "duration": None},
                {"file_name": "01 chin", "extension": "MP4", "file_size": 376897239, "id": "vid1",
                 "download_url": None, "stream_url": "/s/tok/vid1", "duration": 230},
            ]
        }
    }
    items = core.items_from_content(props)
    assert [item.filename for item in items] == ["Pattern.pdf", "01 chin.mp4"]
    assert items[0].is_video is False
    assert items[1].is_video is True
    assert items[1].duration == 230


def test_items_from_content_rejects_empty():
    with pytest.raises(core.GumroadError):
        core.items_from_content({"content": {"content_items": []}})


def test_parse_app_div_roundtrips_props():
    props = {"props": {"title": "x", "content": {"content_items": []}}}
    assert core.parse_app_div(app_page(props)) == props


def test_parse_app_div_without_app_div():
    with pytest.raises(core.GumroadError):
        core.parse_app_div("<html></html>")


# ------------------------------------------------------------------------- playlist


PLAYLIST = [
    {"external_id": "a", "sources": ["/r/tok/a/index.m3u8", "https://files/a.mp4"]},
    {"external_id": "b", "sources": ["/r/tok/b/index.m3u8"]},
    {"external_id": "c", "sources": ["/r/tok/c/index.m3u8"]},
]


def test_choose_playlist_entry_matches_external_id():
    assert core.choose_playlist_entry(PLAYLIST, "b")["external_id"] == "b"


def test_choose_playlist_entry_never_returns_the_first_entry_by_accident():
    assert core.choose_playlist_entry(PLAYLIST, "c")["external_id"] == "c"


def test_choose_playlist_entry_single_entry_fallback():
    assert core.choose_playlist_entry([PLAYLIST[0]], "unknown")["external_id"] == "a"


def test_choose_playlist_entry_raises_for_unknown_id_in_multi_entry_playlist():
    with pytest.raises(core.GumroadError):
        core.choose_playlist_entry(PLAYLIST, "zzz")


def test_hls_source_prefers_playlist_path():
    assert core.hls_source(PLAYLIST[1]) == core.BASE + "/r/tok/b/index.m3u8"


MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=910800,RESOLUTION=854x480,CODECS="avc1.4d401f,mp4a.40.2"
https://cdn.example/index_480p.m3u8?Signature=aaa
#EXT-X-STREAM-INF:BANDWIDTH=4540800,RESOLUTION=1920x1080,CODECS="avc1.4d4028,mp4a.40.2"
https://cdn.example/index_1080p.m3u8?Signature=bbb
"""


def test_best_variant_picks_highest_bandwidth():
    bandwidth, resolution, url = core.best_variant(MASTER)
    assert (bandwidth, resolution) == (4540800, "1920x1080")
    assert url.endswith("index_1080p.m3u8?Signature=bbb")


def test_best_variant_rejects_non_playlist():
    with pytest.raises(core.GumroadError):
        core.best_variant("<!DOCTYPE html><html>confirm email</html>")


def test_best_variant_rejects_playlist_without_variants():
    with pytest.raises(core.GumroadError):
        core.best_variant("#EXTM3U\n#EXT-X-VERSION:3\n")


# ------------------------------------------------------------------------ durations


@pytest.mark.parametrize(
    "actual,declared,expected",
    [(230.0, 230, True), (231.0, 230, True), (312.0, 126, False), (10.0, None, True),
     (0.0, None, False)],
)
def test_duration_ok(actual, declared, expected):
    assert core.duration_ok(actual, declared) is expected


# ---------------------------------------------------------------------------- pdfs


def test_validate_pdf_accepts_structurally_sound_file(tmp_path):
    path = tmp_path / "pattern.pdf"
    path.write_bytes(make_pdf(b"0" * 4096))
    info = core.validate_pdf(path)
    assert info["pages"] >= 1


def test_validate_pdf_rejects_non_pdf(tmp_path):
    path = tmp_path / "not.pdf"
    path.write_bytes(b"<html>confirm email</html>")
    with pytest.raises(core.GumroadError):
        core.validate_pdf(path)


def test_validate_pdf_rejects_truncated_file(tmp_path):
    path = tmp_path / "cut.pdf"
    path.write_bytes(b"%PDF-1.4\n1 0 obj<</Type/Page>>endobj\n")
    with pytest.raises(core.GumroadError):
        core.validate_pdf(path)


def test_already_complete_ignores_absent_and_tiny_files(tmp_path):
    item = core.Item(index=0, name="Pattern", ext="pdf", file_id="x", size=1000)
    assert core.already_complete(item, tmp_path / "missing.pdf") is None
    small = tmp_path / "small.pdf"
    small.write_bytes(b"%PDF-1.4\n%%EOF\n")
    assert core.already_complete(item, small) is None


def test_already_complete_accepts_stamped_pdf_that_differs_in_size(tmp_path):
    # Gumroad stamps PDFs, so a valid download never matches the declared byte count.
    item = core.Item(index=0, name="Pattern", ext="pdf", file_id="x", size=64_837_251)
    path = tmp_path / "Pattern.pdf"
    path.write_bytes(make_pdf(b"0" * (2 << 20)))
    assert path.stat().st_size != item.size
    assert core.already_complete(item, path)


# ------------------------------------------------------------------------ transport


def test_download_http_resumes_from_part_file(tmp_path):
    dest = tmp_path / "video.mp4"
    part = tmp_path / "video.mp4.part"
    part.write_bytes(b"0123456789")
    session = FakeSession()
    session.routes["https://files.example/original"] = FakeResponse(
        status_code=206, chunks=[b"abcdefghij"]
    )
    written = core.download_http(session, "https://files.example/original", dest)
    assert written == 20
    assert dest.read_bytes() == b"0123456789abcdefghij"
    assert not part.exists()
    assert session.requests[0][2]["headers"]["Range"] == "bytes=10-"


def test_download_http_rejects_error_status(tmp_path):
    session = FakeSession()
    session.routes["https://files.example/x"] = FakeResponse(status_code=403, chunks=[b"nope"])
    with pytest.raises(core.GumroadError):
        core.download_http(session, "https://files.example/x", tmp_path / "x.mp4")


# ----------------------------------------------------------------------- authorising


def test_authorize_posts_the_email_and_checks_the_result():
    token = TOKEN
    confirm_url = f"{core.BASE}/confirm?destination=download_page&id={token}"
    session = FakeSession()
    session.routes[confirm_url] = FakeResponse(
        text=app_page({"props": {"authenticity_token": "TOKEN123"}})
    )
    session.routes[f"{core.BASE}/confirm-redirect"] = FakeResponse(
        text=app_page({"props": {"content": {"content_items": [{"id": "1"}]}}})
    )
    core.authorize(session, token, "buyer@example.com")
    method, url, kwargs = session.requests[-1]
    assert (method, url) == ("POST", f"{core.BASE}/confirm-redirect")
    assert kwargs["data"]["email"] == "buyer@example.com"
    assert kwargs["data"]["authenticity_token"] == "TOKEN123"


def test_authorize_raises_when_gumroad_reprompts():
    token = TOKEN
    confirm_url = f"{core.BASE}/confirm?destination=download_page&id={token}"
    session = FakeSession()
    session.routes[confirm_url] = FakeResponse(
        text=app_page({"props": {"authenticity_token": "TOKEN123"}})
    )
    session.routes[f"{core.BASE}/confirm-redirect"] = FakeResponse(
        text=app_page(
            {"props": {"content_unavailability_reason_code": "email_confirmation_required"}}
        )
    )
    with pytest.raises(core.GumroadError):
        core.authorize(session, token, "wrong@example.com")


def test_fetch_items_reads_inertia_json_directly():
    token = TOKEN
    payload = {"props": {"content": {"content_items": [
        {"file_name": "Pattern", "extension": "PDF", "file_size": 10, "id": "p",
         "download_url": "/r/tok/product_files?x=1", "stream_url": None, "duration": None}]}}}
    session = FakeSession()
    session.routes[f"{core.BASE}/d/{token}"] = FakeResponse(text=json.dumps(payload))
    items = core.fetch_items(session, token)
    assert items[0].filename == "Pattern.pdf"
    assert session.requests[0][2]["headers"]["X-Inertia"] == "true"


def test_fetch_items_surfaces_unavailability_reason():
    token = TOKEN
    payload = {"props": {"content_unavailability_reason_code": "email_confirmation_required"}}
    session = FakeSession()
    session.routes[f"{core.BASE}/d/{token}"] = FakeResponse(text=json.dumps(payload))
    with pytest.raises(core.GumroadError, match="email_confirmation_required"):
        core.fetch_items(session, token)


def test_file_redirect_returns_signed_location():
    item = core.Item(index=0, name="Pattern", ext="pdf", file_id="p",
                     download_url="/r/tok/product_files?x=1")
    session = FakeSession()
    session.routes[core.BASE + item.download_url] = FakeResponse(
        status_code=302, headers={"location": "https://files.gumroad.com/original/p.pdf?verify=1-abc"}
    )
    assert core.file_redirect(session, item, "tok").startswith("https://files.gumroad.com/")


def test_video_variant_uses_the_matching_playlist_entry():
    token = TOKEN
    item = core.Item(index=1, name="02 neck", ext="mp4", file_id="b",
                     stream_url="/s/tok/b", duration=185)
    session = FakeSession()
    session.routes[core.BASE + "/s/tok/b"] = FakeResponse(
        text=app_page({"props": {"playlist": [
            {"external_id": "a", "sources": ["/r/tok/a/index.m3u8"], "title": "01 chin"},
            {"external_id": "b", "sources": ["/r/tok/b/index.m3u8"], "title": "02 neck"},
        ]}})
    )
    session.routes[core.BASE + "/r/tok/b/index.m3u8"] = FakeResponse(text=MASTER)
    url, resolution, bandwidth, title = core.video_variant(session, item, token)
    assert title == "02 neck"
    assert resolution == "1920x1080"
    assert url.endswith("index_1080p.m3u8?Signature=bbb")


# ------------------------------------------------------------------------ reporting


def test_write_sha256sums_lists_only_hashed_records(tmp_path):
    first = tmp_path / "a.mp4"
    first.write_bytes(b"a")
    records = [
        {"path": str(first), "sha256": core.sha256(first)},
        {"path": str(tmp_path / "missing.mp4"), "status": "failed"},
    ]
    target = core.write_sha256sums(tmp_path, records)
    lines = target.read_text().splitlines()
    assert len(lines) == 1
    assert lines[0].endswith("  a.mp4")


def test_human_size_formats():
    assert core.human_size(None) == "-"
    assert core.human_size(0) == "-"
    assert core.human_size(1024) == "1.0 KB"
    assert core.human_size(1536) == "1.5 KB"


def test_module_exposes_version():
    from gumroad_dl import __version__

    assert __version__.count(".") == 2
