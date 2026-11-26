#!/usr/bin/env python3
"""Delete forecast files from this repository once every challenge has handled them.

Each challenge reads a new file in forecasts/ once and publishes what it did with every file,
for each model, at <bucket>/submission_results/<model_id>.csv: the file's path, its git blob
SHA (the fingerprint git keeps of the file's contents) and a status. One file can go to
several challenges (every challenge block in challenge-model.yaml), so a forecast is deleted
here only when *each* of those challenges says it has handled *this exact file*: the same
path with the same SHA and the status "processed" (stored) or "skipped" (nothing in it for
that challenge, or already stored). A file a challenge has not handled yet, one it rejected,
and one that was changed after it was handled (the challenge ignores such edits) are all
kept, and the reason is printed. If a list cannot be read, nothing is deleted.

Run from the repository root:
  python cleanup_forecasts.py [--dry-run] [--keep N] [--keep-days D] [--challenge NAME=URL ...]
Needs the packages in requirements.txt (pip install -r requirements.txt).

  --dry-run      only say what would be deleted
  --keep N       keep the newest N forecasts every challenge has (default 0)
  --keep-days D  keep every forecast from the last D days, whether or not the challenges
                 have it (default 0: nothing is kept for its age). A forecast's age is
                 today minus its reference date, so --keep-days 7 keeps the files whose
                 reference date is 7 days ago or later.
  --challenge NAME=URL  the bucket of a challenge listed in challenge-model.yaml (repeat for
                 each). The default challenge below needs no flag.
  --today DATE   treat DATE (YYYY-MM-DD) as today, for testing (default: today, UTC)

The two protections are independent: a forecast is kept if it is among the newest N the
challenges have or is from the last D days.

Environment variables (all optional)
  S3_BASE_URL  the default challenge's bucket (default: the template challenge's)
  CHALLENGE    the default challenge's project_id
  S3_KEY, S3_SECRET  a key that can read the challenges' buckets. Set both when a bucket is
               private (ask the challenge for a read-only key).
"""
import argparse
import datetime
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd
import yaml

from challenge_s3 import read_csv

S3_BASE_URL = os.environ.get("S3_BASE_URL") or "https://s3.us-east-005.backblazeb2.com/template-challenge"
CHALLENGE = os.environ.get("CHALLENGE") or "templatecast"
DONE = ("processed", "skipped")


def git_blob_sha(path):
    """The SHA git gives the file as committed (what `git hash-object` prints).

    Asking git, not hashing the bytes, matters for a file stored with Git LFS (Hugging Face
    does this for every .gz file): what git holds, and the challenge records, is the small LFS
    pointer, whose SHA differs from the file's.
    """
    done = subprocess.run(["git", "hash-object", "--", str(path)], capture_output=True, text=True)
    if done.returncode == 0 and done.stdout.strip():
        return done.stdout.strip()
    data = Path(path).read_bytes()                  # not in a git repository: hash the bytes
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def challenges_in(path="challenge-model.yaml"):
    """{challenge: [(model_id, folder), ...]} for every challenge block in challenge-model.yaml."""
    parsed = yaml.safe_load(Path(path).read_text())
    blocks = [parsed] if isinstance(parsed, dict) else parsed
    found = {}
    for block in blocks:
        if isinstance(block, dict) and block.get("challenge"):
            for model in block.get("models") or []:
                found.setdefault(block["challenge"], []).append(
                    (model["model_id"], str(model.get("path") or "forecasts/")))
    if not found:
        sys.exit(f"No challenges in {path}.")
    return found


def forecast_files(folder):
    """The .csv and .csv.gz files in `folder`. Their names are free."""
    root = Path(folder)
    return sorted(p for p in root.glob("*") if p.is_file() and re.search(r"\.csv(\.gz)?$", p.name))


def reference_date(path):
    """A forecast's reference date: the first reference_datetime in it."""
    column = pd.read_csv(path, usecols=["reference_datetime"])["reference_datetime"]
    return pd.to_datetime(column).dt.date.min()


def results_for(base_url, model_id, cache):
    """The challenge's published results for a model, read once."""
    url = f"{base_url.rstrip('/')}/submission_results/{model_id}.csv"
    if url not in cache:
        try:
            cache[url] = read_csv(url)
        except Exception as error:                  # a missing list is not "nothing processed"
            sys.exit(f"Could not read {url}: {error}\nNothing was deleted.")
    return cache[url]


def handled_by(results, name, sha):
    """(done, reason) for one file in one challenge's results for the folder's models."""
    mine = results[results["file"] == name]
    if ((mine["sha"] == sha) & mine["status"].isin(DONE)).any():
        return True, ""
    if mine["status"].isin(DONE).any():
        return False, "changed since the challenge handled it"
    if len(mine):
        return False, "the challenge's status: " + ", ".join(sorted(set(mine["status"])))
    return False, "not handled by the challenge yet"


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="only say what would be deleted")
    parser.add_argument("--keep", type=int, default=0,
                        help="keep the newest N forecasts every challenge has")
    parser.add_argument("--keep-days", type=int, default=0,
                        help="keep every forecast from the last D days, handled or not")
    parser.add_argument("--challenge", action="append", default=[], metavar="NAME=URL",
                        help="the bucket of a challenge in challenge-model.yaml (repeatable)")
    parser.add_argument("--today", help="treat this date (YYYY-MM-DD) as today, for testing")
    args = parser.parse_args()
    today = (datetime.date.fromisoformat(args.today) if args.today
             else datetime.datetime.now(datetime.timezone.utc).date())
    oldest_kept = today - datetime.timedelta(days=args.keep_days)

    urls = {CHALLENGE: S3_BASE_URL}
    for item in args.challenge:
        name, _, url = item.partition("=")
        if not url:
            sys.exit(f"--challenge needs NAME=URL, got '{item}'")
        urls[name] = url
    wanted = challenges_in()
    unknown = [name for name in wanted if name not in urls]
    if unknown:
        sys.exit(f"challenge-model.yaml lists {unknown} but no bucket is known for "
                 + ("it" if len(unknown) == 1 else "them")
                 + "; pass --challenge NAME=<bucket URL> for each. Nothing was deleted.")

    folders = sorted({folder for models in wanted.values() for _, folder in models})
    cache, deletable, kept = {}, [], []
    for folder in folders:
        candidates = []
        for path in forecast_files(folder):
            name, sha = path.as_posix(), git_blob_sha(path)
            reasons = []
            for challenge, models in wanted.items():
                results = pd.concat([results_for(urls[challenge], model_id, cache)
                                     for model_id, model_folder in models if model_folder == folder],
                                    ignore_index=True)
                done, reason = handled_by(results, name, sha)
                if not done:
                    reasons.append(f"{challenge}: {reason}")
            if reasons:
                kept.append((name, "; ".join(reasons)))
            else:
                candidates.append(path)
        candidates.sort(key=lambda p: (reference_date(p), p.name))      # oldest first
        newest = candidates[len(candidates) - args.keep:] if args.keep > 0 else []
        for path in candidates:
            if path in newest:
                kept.append((path.as_posix(), f"kept as one of the newest {args.keep}"))
            elif args.keep_days > 0 and reference_date(path) >= oldest_kept:
                kept.append((path.as_posix(),
                             f"from the last {args.keep_days} days, kept whether or not it is on S3"))
            else:
                deletable.append(path)

    for path in deletable:
        print(f"{'would delete' if args.dry_run else 'deleting'}: {path.as_posix()}")
        if not args.dry_run:
            path.unlink()
    for name, reason in kept:
        print(f"keeping: {name} ({reason})")
    print(f"{len(deletable)} file(s) {'to delete' if args.dry_run else 'deleted'}, {len(kept)} kept.")


if __name__ == "__main__":
    main()
