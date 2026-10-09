"""CLI level tests, driven with a stub session so nothing hits the network."""

from __future__ import annotations

import json

import pytest

from gumroad_dl import cli, core

TOKEN = "abcdefababcdefababcdefababcdefab"


def manifest_items():
    return [
        core.Item(index=0, name="Pattern", ext="pdf", file_id="p", size=61_800_000,
                  download_url="/r/tok/product_files?x=1"),
        core.Item(index=1, name="01 chin", ext="mp4", file_id="v1", size=376_897_239,
                  stream_url="/s/tok/v1", duration=230),
    ]


@pytest.fixture
def stub(monkeypatch):
    """Replace the network entry points the CLI uses."""

    class SessionStub:
        pass

    monkeypatch.setattr(cli, "new_http_session", lambda: SessionStub())
    monkeypatch.setattr(cli, "authorize", lambda session, token, email: None)
    monkeypatch.setattr(cli, "fetch_items", lambda session, token: manifest_items())
    return SessionStub


def test_list_mode_prints_the_manifest_and_exits_zero(stub, capsys):
    code = cli.main([f"https://gumroad.com/d/{TOKEN}", "--email", "buyer@example.com", "--list"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Pattern.pdf" in out
    assert "01 chin.mp4" in out
    assert "2 file(s)" in out


def test_bad_token_exits_two(stub, capsys):
    code = cli.main(["https://gumroad.com/d/oops", "--email", "buyer@example.com", "--list"])
    assert code == 2
    assert "no 32 hex character purchase token" in capsys.readouterr().err


def test_only_filter_selects_one_index(stub, capsys):
    code = cli.main([TOKEN, "--email", "buyer@example.com", "--list", "--only", "1"])
    out = capsys.readouterr().out
    assert code == 0
    assert "01 chin.mp4" in out
    assert "Pattern.pdf" not in out


def test_only_filter_with_no_match_exits_two(stub, capsys):
    code = cli.main([TOKEN, "--email", "buyer@example.com", "--list", "--only", "99"])
    assert code == 2
    assert "no content item matches" in capsys.readouterr().err


def test_dry_run_reports_without_downloading(stub, tmp_path, capsys, monkeypatch):
    def explode(*_args, **_kwargs):  # pragma: no cover - guard
        raise AssertionError("dry run must not download")

    monkeypatch.setattr(cli, "download_item", explode)
    code = cli.main([TOKEN, "--email", "buyer@example.com", "--out", str(tmp_path), "--dry-run"])
    assert code == 0
    assert "would download [0] Pattern.pdf" in capsys.readouterr().out
    assert not (tmp_path / "results.jsonl").exists()


def test_missing_content_items_is_reported(stub, capsys, monkeypatch):
    def boom(_session, _token):
        raise core.GumroadError("Gumroad did not accept that email for this purchase")

    monkeypatch.setattr(cli, "fetch_items", boom)
    code = cli.main([TOKEN, "--email", "wrong@example.com", "--list"])
    assert code == 1
    assert "did not accept that email" in capsys.readouterr().err


def test_successful_run_writes_results_and_checksums(stub, tmp_path, capsys, monkeypatch):
    def fake_download(_session, _token, item, outdir):
        path = outdir / item.filename
        path.write_bytes(b"data")
        return {"index": item.index, "name": item.name, "ext": item.ext, "path": str(path),
                "bytes": 4, "status": "ok", "sha256": core.sha256(path)}

    monkeypatch.setattr(cli, "download_item", fake_download)
    code = cli.main([TOKEN, "--email", "buyer@example.com", "--out", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert code == 0
    assert (tmp_path / "results.jsonl").exists()
    assert (tmp_path / "SHA256SUMS.txt").exists()
    assert len((tmp_path / "SHA256SUMS.txt").read_text().splitlines()) == 2
    json_lines = [line for line in out.splitlines() if line.startswith("{")]
    assert json.loads(json_lines[-1])["status"] == "ok"


def test_failure_stops_the_run_unless_keep_going(stub, tmp_path, monkeypatch, capsys):
    attempts = []

    def flaky(_session, _token, item, outdir):
        attempts.append(item.index)
        raise core.GumroadError("ffmpeg failed")

    monkeypatch.setattr(cli, "download_item", flaky)
    code = cli.main([TOKEN, "--email", "buyer@example.com", "--out", str(tmp_path)])
    assert code == 1
    assert attempts == [0], "without --keep-going the run stops at the first failure"
    records = (tmp_path / "results.jsonl").read_text().splitlines()
    assert json.loads(records[0])["status"] == "failed"

    attempts.clear()
    code = cli.main([TOKEN, "--email", "buyer@example.com", "--out", str(tmp_path), "--keep-going"])
    assert code == 1
    assert attempts == [0, 1]


def test_parser_requires_email():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["https://gumroad.com/d/" + TOKEN])
