#!/usr/bin/env python3
"""Build data/publications.json from the team's DBLP profiles.

Run by .github/workflows/update-publications.yml every week, or by hand:

    python3 scripts/update_publications.py

Uses only the Python standard library. Settings live in
data/publications-config.json (members, how many years, hidden records).

What it does:
  * queries DBLP's SPARQL endpoint for the members' records (with a mirror as
    fallback; the dblp.org pages themselves block cloud servers)
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
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "data" / "publications-config.json"
OUTPUT = ROOT / "data" / "publications.json"
USER_AGENT = "csiro-funml-website/1.0 (+https://csiro-funml.github.io; publication list updater)"

# DBLP's SPARQL endpoints, meant for programmatic access (the main dblp.org
# pages sit behind a bot check that blocks cloud servers such as GitHub's).
ENDPOINTS = [
    "https://sparql.dblp.org/sparql",
    "https://qlever.cs.uni-freiburg.de/api/dblp",  # mirror run by the QLever team
]

PREFIXES = """PREFIX dblp: <https://dblp.org/rdf/schema#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""

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


def sparql(query, tries=3):
    """Run a SPARQL query, trying each endpoint in turn. Returns result rows."""
    errors = []
    for endpoint in ENDPOINTS:
        for attempt in range(tries):
            try:
                url = endpoint + "?" + urllib.parse.urlencode({"query": PREFIXES + query})
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"})
                with urllib.request.urlopen(req, timeout=120) as res:
                    return json.loads(res.read())["results"]["bindings"]
            except Exception as err:  # noqa: BLE001
                errors.append(f"{endpoint}: {err}")
                if attempt < tries - 1:
                    time.sleep(5 * (attempt + 1))
        print(f"  {endpoint} failed, trying the next endpoint", file=sys.stderr)
    raise RuntimeError("all DBLP SPARQL endpoints failed:\n  " + "\n  ".join(errors))


def selector(member_pids=None, keys=None):
    if member_pids:
        vals = " ".join(f"<https://dblp.org/pid/{p}>" for p in member_pids)
        return f"VALUES ?m {{ {vals} }}\n  ?p dblp:authoredBy ?m ."
    vals = " ".join(f"<https://dblp.org/rec/{k}>" for k in keys)
    return f"VALUES ?p {{ {vals} }}"


def fetch_rows(sel, since):
    year = f'?p dblp:yearOfPublication ?year . FILTER(?year >= "{since}"^^xsd:gYear)' if since else "?p dblp:yearOfPublication ?year ."
    recs = sparql(f"""SELECT DISTINCT ?p ?type ?title ?year ?venue ?vol ?page WHERE {{
  {sel}
  {year}
  ?p dblp:title ?title ; a ?type .
  FILTER(?type != dblp:Publication)
  OPTIONAL {{ ?p dblp:publishedIn ?venue }}
  OPTIONAL {{ ?p dblp:publishedInJournalVolume ?vol }}
  OPTIONAL {{ ?p dblp:primaryDocumentPage ?page }}
}}""")
    time.sleep(1)
    auth = sparql(f"""SELECT DISTINCT ?p ?ord ?name ?creator WHERE {{
  {sel}
  {year}
  ?p dblp:hasSignature ?s .
  ?s dblp:signatureOrdinal ?ord ; dblp:signatureDblpName ?name .
  OPTIONAL {{ ?s dblp:signatureCreator ?creator }}
}}""")
    return recs, auth


def v(row, name):
    return row[name]["value"] if name in row else ""


def clean_name(name):
    return re.sub(r"\s+\d{4}$", "", name)  # DBLP disambiguation suffix, e.g. "He Zhao 0001"


def norm_title(title):
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def build_records(rec_rows, auth_rows, team_pids):
    team_uris = {f"https://dblp.org/pid/{p}" for p in team_pids}
    authors = {}
    for r in auth_rows:
        authors.setdefault(v(r, "p"), {})[int(v(r, "ord"))] = {
            "name": clean_name(v(r, "name")), "team": v(r, "creator") in team_uris}
    out = {}
    for r in rec_rows:
        uri, rtype = v(r, "p"), v(r, "type").rsplit("#", 1)[-1]
        key = uri.replace("https://dblp.org/rec/", "")
        if key in out:  # extra rows for a second document page etc.
            continue
        venue_full = v(r, "venue")
        is_corr = venue_full == "CoRR"
        if rtype == "Informal" and not is_corr:
            continue  # informal non-arXiv records (tech notes, withdrawn papers)
        if rtype not in ("Article", "Inproceedings", "Incollection", "Book", "Informal"):
            continue  # edited proceedings, theses, data and software entries
        arxiv = None
        m = re.search(r"abs/(\S+)", v(r, "vol"))
        if is_corr and m:
            arxiv = f"https://arxiv.org/abs/{m.group(1)}"
        out[key] = {
            "key": key,
            "title": re.sub(r"\s+", " ", v(r, "title")).strip().rstrip("."),
            "year": int(v(r, "year")[:4]),
            "venue": "arXiv" if is_corr else VENUE_SHORT.get(venue_full, re.sub(r"\s*\(\d+\)$", "", venue_full)),
            "venueFull": venue_full,
            "type": "preprint" if is_corr else ("journal" if rtype == "Article" else "conference"),
            "authors": [a for _, a in sorted(authors.get(uri, {}).items())],
            "url": arxiv or v(r, "page") or uri,
            "arxiv": arxiv,
        }
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
    ap.add_argument("--from-dir", help="read records.json and authors.json (saved SPARQL results) from this folder instead of querying DBLP (for testing)")
    ap.add_argument("--year", type=int, default=dt.date.today().year, help="treat this as the current year")
    ap.add_argument("--output", default=str(OUTPUT))
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text())
    since = args.year - int(cfg.get("years", 5))
    team_pids = {m["pid"] for m in cfg["members"]}
    hide = set(cfg.get("hide", []))

    pids = [m["pid"] for m in cfg["members"]]
    if args.from_dir:  # testing: saved SPARQL JSON responses
        d = Path(args.from_dir)
        rec_rows = json.loads((d / "records.json").read_text())["results"]["bindings"]
        auth_rows = json.loads((d / "authors.json").read_text())["results"]["bindings"]
    else:
        print(f"Querying DBLP for {', '.join(m['name'] for m in cfg['members'])} (since {since})")
        rec_rows, auth_rows = fetch_rows(selector(member_pids=pids), since)
        if cfg.get("extra"):
            print(f"Querying {len(cfg['extra'])} extra record(s)")
            r2, a2 = fetch_rows(selector(keys=cfg["extra"]), None)
            rec_rows, auth_rows = rec_rows + r2, auth_rows + a2
    records = build_records(rec_rows, auth_rows, team_pids)
    if not records:
        sys.exit("DBLP returned no publications; leaving the existing list unchanged.")

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
