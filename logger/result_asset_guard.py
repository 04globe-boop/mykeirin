"""Verify the monthly result asset before any replacement."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

REPO = "04globe-boop/mykeirin2"
TAG = "extra"
RECEIPT = "restore.json"
ASSET_NAME = re.compile(r"extra_\d{4}(?:0[1-9]|1[0-2])\.jsonl\.gz")


def gh(*args):
    call = subprocess.run(["gh", *args], capture_output=True, timeout=120)
    if call.returncode != 0:
        raise RuntimeError("GitHub asset operation did not complete")
    return call.stdout


def validate_name(name):
    if ASSET_NAME.fullmatch(name) is None:
        raise RuntimeError("Unexpected asset name")


def lookup(name):
    validate_name(name)
    repository = json.loads(gh("api", f"repos/{REPO}"))
    if repository.get("full_name") != REPO or repository.get("private") is not True:
        raise RuntimeError("Private repository identity did not match")
    release = json.loads(gh("api", f"repos/{REPO}/releases/tags/{TAG}"))
    release_id = release.get("id")
    if release.get("tag_name") != TAG or type(release_id) is not int:
        raise RuntimeError("Unexpected release response")
    pages = json.loads(gh("api", "--paginate", "--slurp",
                          f"repos/{REPO}/releases/{release_id}/assets?per_page=100"))
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise RuntimeError("Invalid asset inventory")
    matches = [asset for page in pages for asset in page if asset["name"] == name]
    if not matches:
        return release_id, None
    if len(matches) != 1:
        raise RuntimeError("Ambiguous asset identity")
    asset = {key: matches[0][key] for key in ("id", "size", "digest")}
    if (type(asset["id"]) is not int or type(asset["size"]) is not int or asset["size"] <= 0
            or not isinstance(asset["digest"], str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", asset["digest"]) is None):
        raise RuntimeError("Asset has no verifiable digest and size")
    return release_id, asset


def validate_rows(path):
    count = 0
    with gzip.open(path, "rt", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            if not isinstance(json.loads(line), dict):
                raise RuntimeError("Invalid result record")
            count += 1
    if not count:
        raise RuntimeError("Result file has no records")
    return count


def restore(name, directory):
    validate_name(name)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    receipt_path = directory / RECEIPT
    if path.exists() or receipt_path.exists():
        raise RuntimeError("Restore requires a fresh directory")
    release_id, asset = lookup(name)
    count = 0
    if asset is not None:
        raw = gh("api", "-H", "Accept: application/octet-stream",
                 f"repos/{REPO}/releases/assets/{asset['id']}")
        if (len(raw) != asset["size"]
                or "sha256:" + hashlib.sha256(raw).hexdigest() != asset["digest"]):
            raise RuntimeError("Downloaded original differs from published metadata")
        path.write_bytes(raw)
        count = validate_rows(path)
    receipt = dict(name=name, release_id=release_id, original=asset, rows=count)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    return receipt


def verify(name, directory):
    validate_name(name)
    receipt = json.loads((directory / RECEIPT).read_text(encoding="utf-8"))
    release_id, asset = lookup(name)
    if (receipt["name"], receipt["release_id"], receipt["original"]) != (name, release_id, asset):
        raise RuntimeError("Published asset changed since restore")
    path = directory / name
    if not path.is_file():
        if asset is None:
            return "unchanged"
        raise RuntimeError("Verified original missing from collection directory")
    count = validate_rows(path)
    if asset is None:
        return "create"
    size = path.stat().st_size
    with path.open("rb") as file:
        original = file.read(asset["size"])
    if len(original) != asset["size"] or "sha256:" + hashlib.sha256(original).hexdigest() != asset["digest"]:
        raise RuntimeError("Collected file did not preserve the original bytes")
    if size == asset["size"]:
        return "unchanged"
    if count <= receipt["rows"]:
        raise RuntimeError("Collected file did not add valid records")
    return "replace"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("restore", "verify"))
    parser.add_argument("name")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        result = restore(args.name, args.directory) if args.mode == "restore" else verify(args.name, args.directory)
        print(json.dumps(result) if isinstance(result, dict) else result)
    except (RuntimeError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        print("Result asset guard stopped the operation; verify the Release and its original before retrying.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
