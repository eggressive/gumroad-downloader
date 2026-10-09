"""Console entry point for gumroad-dl."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .core import (
    GumroadError,
    authorize,
    download_item,
    extract_token,
    fetch_items,
    human_size,
    new_http_session,
    write_sha256sums,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gumroad-dl",
        description=(
            "Download every file of a Gumroad purchase from its /d/<token> link. "
            "Videos come from Gumroad's signed HLS renditions because the original "
            "MP4 objects are not retrievable."
        ),
    )
    parser.add_argument(
        "link", help="gumroad.com/d/<token> link, or the bare 32 character token"
    )
    parser.add_argument(
        "-e", "--email", required=True, help="email address used to buy the product"
    )
    parser.add_argument("-o", "--out", default=".", help="output directory (default: current one)")
    parser.add_argument("-l", "--list", action="store_true",
                        help="list the purchase contents and exit")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="resolve URLs and report what would be downloaded")
    parser.add_argument("--only", type=int, action="append", default=None,
                        help="download only this content index (repeatable)")
    parser.add_argument("--json", action="store_true",
                        help="print the result records as JSON lines")
    parser.add_argument("--keep-going", action="store_true",
                        help="continue after a per-file failure instead of stopping")
    parser.add_argument("--version", action="version", version=f"gumroad-dl {__version__}")
    return parser


def main(argv: list = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        token = extract_token(args.link)
    except GumroadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    session = new_http_session()
    try:
        authorize(session, token, args.email)
        items = fetch_items(session, token)
    except (GumroadError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.only:
        wanted = set(args.only)
        items = [item for item in items if item.index in wanted]
        if not items:
            print(f"error: no content item matches --only {sorted(wanted)}", file=sys.stderr)
            return 2

    total = sum(item.size or 0 for item in items)
    print(f"{len(items)} file(s), {human_size(total)} declared on the product page")
    for item in items:
        kind = "video" if item.is_video else (item.ext or "file")
        print(f"  [{item.index:2d}] {kind:5s} {human_size(item.size):>9s}  {item.filename}")

    if args.list:
        return 0

    outdir = Path(args.out).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)

    records = []
    failures = 0
    for item in items:
        if args.dry_run:
            print(f"would download [{item.index}] {item.filename}")
            continue
        try:
            record = download_item(session, token, item, outdir)
        except (GumroadError, OSError) as exc:
            failures += 1
            print(f"  failed: {exc}", file=sys.stderr)
            records.append({"index": item.index, "name": item.name, "status": "failed",
                            "error": str(exc)[:400]})
            if not args.keep_going:
                break
            continue
        records.append(record)
        if args.json:
            print(json.dumps(record))

    if records:
        (outdir / "results.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n"
        )
        completed = [record for record in records if record.get("sha256")]
        if completed:
            write_sha256sums(outdir, completed)
            print(f"wrote {outdir / 'SHA256SUMS.txt'} ({len(completed)} entries)")
        print(f"wrote {outdir / 'results.jsonl'}")

    if failures:
        print(f"{failures} file(s) failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
