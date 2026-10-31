"""Fetch full Federal Register text for every NPRM that has a URL.

Run this ON YOUR LAPTOP (needs internet). It is safe to re-run: records that
are already saved are skipped.

    python fetch_fulltext.py            # fetch everything missing
    python fetch_fulltext.py --limit 5  # quick smoke test

Output: data/fulltext/<record_id>.txt  (+ a .json with fetch metadata)

Rows whose "federal_register_link" cell already contains the document text
(171 of them) need no fetching; build_index.py reads those from the sheet.
"""
import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape

import pandas as pd
import requests

from common import FULLTEXT_DIR, INDEX_XLSX

API = "https://www.federalregister.gov/api/v1/documents/{doc}.json"
FIELDS = ["raw_text_url", "body_html_url", "start_page", "end_page", "citation", "html_url"]
HEADERS = {"User-Agent": "AeroTrace-hackathon/1.0 (NUS Acacia Hackathon; research use)"}


def doc_number_from_url(url: str) -> str | None:
    m = re.search(r"/documents/\d{4}/\d{2}/\d{2}/([^/]+)/", url)
    if m:
        return m.group(1)
    m = re.search(r"/d/([^/?#]+)", url)
    return m.group(1) if m else None


def html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>|</li>|</tr>", "\n", html)
    html = re.sub(r"(?s)<[^>]+>", "", html)
    return unescape(html)


def get(url: str, session: requests.Session, tries: int = 4) -> requests.Response:
    for i in range(tries):
        r = session.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** i)
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r


def fetch_one(record_id: str, url: str, session: requests.Session) -> tuple[str, str]:
    out_txt = FULLTEXT_DIR / f"{record_id}.txt"
    if out_txt.exists() and out_txt.stat().st_size > 500:
        return record_id, "skipped (exists)"
    doc = doc_number_from_url(url)
    meta = {"record_id": record_id, "source_url": url, "document_number": doc}
    text = ""
    if doc:
        info = get(API.format(doc=doc) + "?" + "&".join(f"fields[]={f}" for f in FIELDS), session).json()
        meta.update({k: info.get(k) for k in FIELDS})
        # raw text keeps the [[Page NNNNN]] markers we use for page-level citations
        if info.get("raw_text_url"):
            text = html_to_text(get(info["raw_text_url"], session).text)
        elif info.get("body_html_url"):
            text = html_to_text(get(info["body_html_url"], session).text)
    if not text:  # fall back to the human page
        text = html_to_text(get(url, session).text)
        meta["fallback"] = "html_page"
    meta["has_page_markers"] = bool(re.search(r"\[\[Page \d+\]\]", text))
    out_txt.write_text(text, encoding="utf-8")
    (FULLTEXT_DIR / f"{record_id}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return record_id, f"ok ({len(text):,} chars, page markers={meta['has_page_markers']})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    FULLTEXT_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.read_excel(INDEX_XLSX, sheet_name="nprm_index", header=2)
    link = df["federal_register_link"].fillna("")
    # real URLs, plus short cells like 'Withdrawal: https://...'
    df["url"] = link.where(link.str.startswith("http"),
                           link.str.extract(r"(https?://[^\s\"]+)")[0].where(link.str.len() < 300))
    rows = df[df["url"].notna()]
    if args.limit:
        rows = rows.head(args.limit)
    print(f"{len(rows)} records with URLs to fetch")

    ok = fail = 0
    session = requests.Session()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_one, r.record_id, r.url, session): r.record_id
                for r in rows.itertuples()}
        for f in as_completed(futs):
            rid = futs[f]
            try:
                _, msg = f.result()
                ok += 1
                print(f"  {rid}: {msg}")
            except Exception as e:  # keep going; failures just stay summary-only
                fail += 1
                print(f"  {rid}: FAILED {e}", file=sys.stderr)
    print(f"done: {ok} ok, {fail} failed. Now run: python build_index.py")


if __name__ == "__main__":
    main()
