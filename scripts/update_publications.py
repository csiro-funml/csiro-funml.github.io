#!/usr/bin/env python3
"""Build data/publications.json from the team's DBLP profiles.

Run by .github/workflows/update-publications.yml every week, or by hand:

    python3 scripts/update_publications.py

Uses only the Python standard library. Settings live in
data/publications-config.json (members, how many years, hidden records).

What it does:
  * downloads each member's DBLP record list (https://dblp.org/pid/<pid>.xml)
  * keeps papers from the last N years
  * merges an arXiv preprint with its published version (same title), keeping
    the venue version and attaching the arXiv link
  * labels papers that only exist on arXiv as "Preprint"
If any download fails, the existing publications.json is left untouched.
"""
import argparse
import datetime as dt
import difflib
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "data" / "publications-config.json"
OUTPUT = ROOT / "data" / "publications.json"
USER_AGENT = "csiro-funml-website/1.0 (+https://csiro-funml.github.io; publication list updater)"

# Short names for common venues; anything else is shown as DBLP writes it.
VENUE_SHORT = {
    "Trans. Mach. Learn. Res.": "TMLR",
    "J. Mach. Learn. Res.": "JMLR",
    "IEEE Trans. Pattern Anal. Mach. Intell.": "TPAMI",
    "IEEE Trans. Neural Networks Learn. Syst.": "IEEE TNNLS",
    "IEEE Trans. Autom. Control.": "IEEE TAC",
    "IEEE Trans. Circuits Syst. Video Technol.": "IEEE TCSVT",
    "IEEE Trans. Knowl. Data Eng.": "IEEE TKDE",
    "Int. J. Comput. Vis.": "IJCV",
    "Pattern Recognit.": "Pattern Recognition",
    "Trans. Assoc. Comput. Linguistics": "TACL",
    "Mach. Learn.": "Machine Learning",
    "Nat. Mach. Intell.": "Nature Machine Intelligence",
}


def fetch(url, tries=4):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml"})
            with urllib.request.urlopen(req, timeout=60) as res:
                body = res.read()
            if b"<dblpperson" not in body[:2000]:
                raise RuntimeError("response is not a DBLP person record (possibly a bot check page)")
            return body
        except Exception as err:  # noqa: BLE001
            if attempt == tries - 1:
                raise
            wait = 5 * (attempt + 1)
            print(f"  retrying {url} in {wait}s ({err})", file=sys.stderr)
            time.sleep(wait)


def text(el):
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def clean_name(name):
    return re.sub(r"\s+\d{4}$", "", name)  # DBLP disambiguation suffix, e.g. "He Zhao 0001"


def norm_title(title):
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def parse_records(xml_bytes, team_pids):
    root = ET.fromstring(xml_bytes)
    out = []
    for r in root.findall("r"):
        e = r[0]
        if e.tag not in ("article", "inproceedings", "incollection", "book"):
            continue  # skip edited proceedings, theses, data entries
        key = e.get("key")
        journal, booktitle = text(e.find("journal")), text(e.find("booktitle"))
        is_corr = journal == "CoRR"
        if e.get("publtype") == "informal" and not is_corr:
            continue  # informal non-arXiv records (e.g. withdrawn or tech notes)
        title = text(e.find("title")).rstrip(".")
        year = int(text(e.find("year")) or 0)
        venue_full = booktitle or journal
        venue = "arXiv" if is_corr else VENUE_SHORT.get(venue_full, re.sub(r"\s*\(\d+\)$", "", venue_full))
        ees = [text(x) for x in e.findall("ee")]
        arxiv = None
        if is_corr:
            m = re.search(r"abs/(\S+)", text(e.find("volume")))
            if m:
                arxiv = f"https://arxiv.org/abs/{m.group(1)}"
        authors = [{"name": clean_name(text(a)), "team": a.get("pid") in team_pids} for a in e.findall("author")]
        out.append({
            "key": key, "title": title, "year": year, "venue": venue, "venueFull": venue_full,
            "type": "preprint" if is_corr else ("journal" if e.tag == "article" else "conference"),
            "authors": authors, "url": arxiv or (ees[0] if ees else f"https://dblp.org/rec/{key}"),
            "arxiv": arxiv,
        })
    return out


def same_paper(a, b):
    if norm_title(a["title"]) == norm_title(b["title"]):
        return True
    ratio = difflib.SequenceMatcher(None, norm_title(a["title"]), norm_title(b["title"])).ratio()
    shared = {x["name"] for x in a["authors"] if x["team"]} & {x["name"] for x in b["authors"] if x["team"]}
    return ratio >= 0.9 and bool(shared)


def merge(records):
    groups = []
    for rec in sorted(records, key=lambda r: (r["type"] == "preprint", -r["year"])):
        for g in groups:
            if any(same_paper(rec, other) for other in g):
                g.append(rec)
                break
        else:
            groups.append([rec])
    merged = []
    for g in groups:
        published = [r for r in g if r["type"] != "preprint"]
        main = dict(max(published, key=lambda r: r["year"]) if published else max(g, key=lambda r: r["year"]))
        arxiv = next((r["arxiv"] for r in g if r["arxiv"]), None)
        main["arxiv"] = arxiv
        main["bib"] = f"https://dblp.org/rec/{main['key']}.bib"
        merged.append(main)
    order = {"conference": 0, "journal": 0, "preprint": 1}
    merged.sort(key=lambda r: (-r["year"], order[r["type"]], r["venue"], r["title"]))
    return merged


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-dir", help="read <pid with / replaced by _>.xml files from this folder instead of downloading (for testing)")
    ap.add_argument("--year", type=int, default=dt.date.today().year, help="treat this as the current year")
    ap.add_argument("--output", default=str(OUTPUT))
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text())
    since = args.year - int(cfg.get("years", 5))
    team_pids = {m["pid"] for m in cfg["members"]}
    hide, records = set(cfg.get("hide", [])), {}

    for i, m in enumerate(cfg["members"]):
        print(f"Fetching {m['name']} ({m['pid']})")
        if args.from_dir:
            data = (Path(args.from_dir) / (m["pid"].replace("/", "_") + ".xml")).read_bytes()
        else:
            if i:
                time.sleep(3)  # be polite to DBLP
            data = fetch(f"https://dblp.org/pid/{m['pid']}.xml")
        for rec in parse_records(data, team_pids):
            records.setdefault(rec["key"], rec)

    for key in cfg.get("extra", []):
        if key not in records and not args.from_dir:
            print(f"Fetching extra record {key}")
            time.sleep(2)
            req = urllib.request.Request(f"https://dblp.org/rec/{key}.xml", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as res:
                rec_xml = res.read().replace(b"<dblp>", b"<dblpperson><r>").replace(b"</dblp>", b"</r></dblpperson>")
            for rec in parse_records(rec_xml, team_pids):
                records[rec["key"]] = rec

    kept = [r for k, r in records.items() if r["year"] >= since and k not in hide]
    pubs = merge(kept)
    result = {
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
        "since": since,
        "source": "DBLP (https://dblp.org), CC0",
        "members": cfg["members"],
        "publications": pubs,
    }
    out = Path(args.output)
    old = json.loads(out.read_text()) if out.exists() else None
    if old and old.get("publications") == pubs:
        print(f"No changes ({len(pubs)} publications).")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    n_pre = sum(p["type"] == "preprint" for p in pubs)
    print(f"Wrote {len(pubs)} publications since {since} ({n_pre} preprints) to {out}")


if __name__ == "__main__":
    main()
