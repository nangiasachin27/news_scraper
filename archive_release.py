"""Permanent daily news archive as GitHub Release assets.

  python archive_release.py pack    --date 2026-11-12 [--root .] [--extra wrsi/sentiment_gate/sentiment_2026-11-12.csv]
  python archive_release.py upload  --date 2026-11-12            # needs the gh CLI + GH_TOKEN (the workflow has both)
  python archive_release.py restore --start 2026-11-01 --end 2026-11-30 [--dest .]
  python archive_release.py list    [--start 2026-11-01] [--end 2026-11-30]

One zip per trading day, news_<date>.zip, holding (paths kept, so it unzips into place):
  data/news/<date>/*.json                 every article: title, summary, body, url, times, tags
  data/sentiment/<date>/*                 the gate's scores (the units the daily job used)
  data/sentiment_title/<date>/*           headline-only scores (the backtested setting)
  extras/...                              the gate CSV(s) and the run log, if passed with --extra
Zips go into one release per month, tag news-archive-YYYY-MM (created on first upload,
marked pre-release so it never shows as the repo's "latest" release). Release assets don't
count toward the repository's size, and each may be up to 2 GB.

`restore` needs no login for a public repo: it reads the release list from the GitHub API
(60 requests/hour unauthenticated - set GITHUB_TOKEN for more) and downloads the zips.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import date, timedelta
from pathlib import Path

REPO = os.environ.get("GITHUB_REPOSITORY", "nangiasachin27/news_scraper")
FOLDERS = ("data/news", "data/sentiment", "data/sentiment_title")


def tag_for(d: date) -> str:
    return f"news-archive-{d:%Y-%m}"


def asset_name(d: date) -> str:
    return f"news_{d.isoformat()}.zip"


# ---- pack ------------------------------------------------------------------------------------
def pack(d: date, root: Path, out_dir: Path, extras: list[Path] | None = None) -> tuple[Path, dict]:
    """Zip one day's folders. Returns (zip path, counts per folder)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / asset_name(d)
    tmp = target.with_suffix(".zip.tmp")
    counts: dict[str, int] = {}
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for folder in FOLDERS:
            src = root / folder / d.isoformat()
            files = sorted(p for p in src.glob("*") if p.is_file() and not p.name.endswith(".tmp")) if src.is_dir() else []
            for f in files:
                z.write(f, f"{folder}/{d.isoformat()}/{f.name}")
            counts[folder] = len(files)
        for e in extras or []:
            if e.is_file():
                z.write(e, f"extras/{e.name}")
                counts["extras"] = counts.get("extras", 0) + 1
        z.writestr("MANIFEST.json", json.dumps({"date": d.isoformat(), "files": counts}, indent=2))
    if not counts.get("data/news"):
        tmp.unlink()
        raise FileNotFoundError(f"no news files in {root / 'data/news' / d.isoformat()}")
    os.replace(tmp, target)
    return target, counts


# ---- upload (gh CLI) -----------------------------------------------------------------------------
def _gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    if not shutil.which("gh"):
        raise RuntimeError("the GitHub CLI (gh) is not installed")
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=check)


def upload(zip_path: Path, d: date, repo: str = REPO) -> str:
    tag = tag_for(d)
    if _gh("release", "view", tag, "-R", repo, check=False).returncode != 0:
        created = _gh("release", "create", tag, "-R", repo, "--prerelease", "--latest=false",
                      "--title", f"News archive {d:%Y-%m}",
                      "--notes", "Daily news + sentiment archive: one news_<date>.zip per trading day. "
                                 "Restore with `python archive_release.py restore --start ... --end ...`.",
                      check=False)
        # two runs racing to create the same month: the loser just uploads into it
        if created.returncode != 0 and _gh("release", "view", tag, "-R", repo, check=False).returncode != 0:
            raise RuntimeError(f"could not create release {tag}: {created.stderr.strip()}")
    res = _gh("release", "upload", tag, str(zip_path), "-R", repo, "--clobber", check=False)
    if res.returncode != 0:
        raise RuntimeError(f"upload to {tag} failed: {res.stderr.strip()}")
    return tag


# ---- restore / list (public GitHub API, no gh needed) ----------------------------------------------
def _api(url: str):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "news_scraper-archive"}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _months(start: date, end: date) -> list[date]:
    out, cur = [], start.replace(day=1)
    while cur <= end:
        out.append(cur)
        cur = (cur + timedelta(days=32)).replace(day=1)
    return out


def assets_between(start: date, end: date, repo: str = REPO) -> list[tuple[date, str, int]]:
    """(date, download url, size) for every archived day in [start, end]."""
    found = []
    for m in _months(start, end):
        try:
            rel = _api(f"https://api.github.com/repos/{repo}/releases/tags/{tag_for(m)}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue  # no archive for that month
            raise
        for a in rel.get("assets", []):
            name = a.get("name", "")
            if name.startswith("news_") and name.endswith(".zip"):
                try:
                    d = date.fromisoformat(name[5:-4])
                except ValueError:
                    continue
                if start <= d <= end:
                    found.append((d, a["browser_download_url"], int(a.get("size") or 0)))
    return sorted(found)


def restore(start: date, end: date, dest: Path, repo: str = REPO, overwrite: bool = False) -> list[date]:
    done = []
    for d, url, _ in assets_between(start, end, repo):
        news_dir = dest / "data/news" / d.isoformat()
        if news_dir.is_dir() and any(news_dir.glob("*.json")) and not overwrite:
            print(f"  {d}  already present, skipped (use --overwrite)")
            continue
        req = urllib.request.Request(url, headers={"User-Agent": "news_scraper-archive"})
        tmp = dest / f".{asset_name(d)}.part"
        dest.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as fh:
            shutil.copyfileobj(r, fh)
        with zipfile.ZipFile(tmp) as z:
            for member in z.namelist():  # never write outside dest
                if member.startswith(("/", "\\")) or ".." in Path(member).parts:
                    raise ValueError(f"unsafe path in {url}: {member}")
            z.extractall(dest)
        tmp.unlink()
        done.append(d)
        print(f"  {d}  restored", flush=True)
    return done


# ---- CLI ---------------------------------------------------------------------------------------
def _d(v: str) -> date:
    try:
        return date.fromisoformat(v)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {v!r}") from exc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="archive_release", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    pk = sub.add_parser("pack", help="zip one day's news + sentiment")
    pk.add_argument("--date", type=_d, required=True)
    pk.add_argument("--root", type=Path, default=Path("."), help="folder that contains data/ (default .)")
    pk.add_argument("--out-dir", type=Path, default=Path("archive"))
    pk.add_argument("--extra", type=Path, action="append", default=[], help="extra file(s) to include, e.g. the gate CSV")
    pk.add_argument("--upload", action="store_true", help="also upload it (same as running upload afterwards)")
    up = sub.add_parser("upload", help="upload archive/news_<date>.zip to its monthly release")
    up.add_argument("--date", type=_d, required=True)
    up.add_argument("--out-dir", type=Path, default=Path("archive"))
    for name in ("restore", "list"):
        r = sub.add_parser(name, help="download + unzip archived days" if name == "restore" else "show archived days")
        r.add_argument("--start", type=_d, required=name == "restore")
        r.add_argument("--end", type=_d)
        if name == "restore":
            r.add_argument("--dest", type=Path, default=Path("."), help="unzip under this folder (default .)")
            r.add_argument("--overwrite", action="store_true", help="replace days already on disk")
    for sp in sub.choices.values():
        sp.add_argument("--repo", default=REPO, help=f"owner/repo (default {REPO})")
    a = p.parse_args(argv)

    try:
        if a.cmd == "pack":
            z, counts = pack(a.date, a.root, a.out_dir, a.extra)
            print(f"{z}  ({z.stat().st_size / 1e6:.1f} MB): " + ", ".join(f"{k} {v}" for k, v in counts.items()))
            if a.upload:
                print(f"uploaded to release {upload(z, a.date, a.repo)}")
        elif a.cmd == "upload":
            z = a.out_dir / asset_name(a.date)
            if not z.exists():
                print(f"error: {z} not found (run pack first)", file=sys.stderr)
                return 2
            print(f"uploaded to release {upload(z, a.date, a.repo)}")
        elif a.cmd == "list":
            start = a.start or date(2026, 1, 1)
            end = a.end or date.today()
            rows = assets_between(start, end, a.repo)
            for d, _, size in rows:
                print(f"  {d}  {size / 1e6:6.1f} MB")
            print(f"{len(rows)} archived day(s) between {start} and {end}, {sum(s for *_, s in rows) / 1e9:.2f} GB")
        else:
            end = a.end or a.start
            got = restore(a.start, end, a.dest, a.repo, a.overwrite)
            print(f"{len(got)} day(s) restored into {a.dest / 'data'}")
    except (FileNotFoundError, RuntimeError, ValueError, urllib.error.URLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
