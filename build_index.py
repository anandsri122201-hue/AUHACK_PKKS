"""Build data/corpus.db from the index spreadsheet + any fetched full texts.

    python build_index.py

Everything here is deterministic (no LLM). Every extracted field keeps the
verbatim sentence it came from, so the UI can always show its evidence.
"""
import json
import re
import sqlite3
import time

import pandas as pd

from common import (DB_PATH, FULLTEXT_DIR, INDEX_XLSX, canonical_makes, clean_text,
                    split_sentences)

# ----------------------------------------------------------------------------- pages

PAGE_MARK = re.compile(r"\[\[Page (\d+)\]\]")


def page_range(pages_cell: str) -> tuple[int | None, int | None]:
    nums = [int(n) for n in re.findall(r"\d+", str(pages_cell or ""))]
    if not nums:
        return None, None
    return nums[0], nums[-1]


def fr_volume(citation: str) -> str | None:
    m = re.search(r"Volume (\d+)", str(citation or ""))
    return m.group(1) if m else None


# ----------------------------------------------------------------------------- sections

REG_START = re.compile(
    r"The Proposed Amendment|The Withdrawal|PART 39\s*[-—–]+\s*AIRWORTHINESS|§\s*39\.13|Sec\.\s*39\.13|39\.13\s*\[Amended\]",
    re.I)
LABEL = re.compile(r"(?:(?<=\n)|(?<=[.:]\s)|(?<=[.:]))\s*\(([a-z])\)\s+")
APPLIC_HINT = re.compile(
    r"applicab|This AD (?:applies to|affects)|What (?:airplanes|helicopters|engines|products|aircraft|propellers|appliances)[^?]{0,40}affected",
    re.I)


def find_sections(reg_text: str) -> list[dict]:
    """Split regulatory text into top-level lettered paragraphs (a), (b), ...

    Only accepts the *next expected* letter, which stops sub-items such as
    (i)/(ii) or inline cross-references from being mistaken for sections.
    """
    accepted = []
    expected = "a"
    for m in LABEL.finditer(reg_text):
        if m.group(1) == expected:
            accepted.append(m)
            expected = chr(ord(expected) + 1)
    sections = []
    for i, m in enumerate(accepted):
        end = accepted[i + 1].start() if i + 1 < len(accepted) else len(reg_text)
        body = reg_text[m.end():end].strip()
        before = reg_text[max(0, m.start() - 120):m.start()].rstrip().split("\n")
        prev_line = before[-1].strip() if before else ""
        first_line = body.split("\n", 1)[0].strip()
        if first_line and len(first_line) <= 60 and not first_line.endswith("."):
            title = first_line
            body = body[len(first_line):].strip()   # heading is metadata, not body text
        elif prev_line and len(prev_line) <= 60 and not prev_line.endswith((".", ":", ";")):
            title = prev_line
        else:
            title = ""   # no heading in the source; show just the paragraph letter
        sections.append({"label": m.group(1), "title": title, "start": m.start(), "end": end, "text": body})
    # a heading that sits *before* the next label belongs to the next section
    for a, b in zip(sections, sections[1:]):
        lines = a["text"].rstrip().split("\n")
        if len(lines) > 1 and lines[-1].strip() == b["title"]:
            a["text"] = "\n".join(lines[:-1]).rstrip()
    return sections


# ----------------------------------------------------------------------------- applicability

# "28-20000" style (Piper) is ONE serial; "5044-5063" is a range.
SN = r"(?:\d{1,2}-\d{5,8}|(?:[A-Z]{1,4}[- ]?)?\d{1,7}[A-Z]?)"
SERWORD = re.compile(
    r"(?:manufacturer(?:'s)?\s+)?serial\s+(?:numbers?|nos?\.?)(?:\s*\((?:S/N|MSN|S/Ns|MSNs)\))?|\bS/Ns?\b|\bMSNs?\b",
    re.I)
RANGE = re.compile(rf"(?<![\w-])({SN})\s*(?:through|thru|to|-|–|—)\s*({SN})(?![\w])(\s*,?\s*inclusive)?")
EXCEPT_BEFORE = re.compile(r"(?:except|excluding|other than|but not)\s*(?:for\s*)?(?:airplanes|helicopters|aircraft|engines)?\s*(?:with\s*)?$", re.I)
OPEN_HI = re.compile(rf"({SN})\s*(?:and|&)\s*(?:subsequent|on|higher|up|later|after)\b", re.I)
PRIOR = re.compile(rf"(?:prior to|before|below|lower than)\s*(?:S/N\s*|MSN\s*)?({SN})", re.I)
UPTO = re.compile(rf"(?:up to and including|up to|through)\s*(?:S/N\s*|MSN\s*)?({SN})", re.I)
AND_PRIOR = re.compile(rf"({SN})\s*(?:and|&)\s*(?:prior|lower|earlier|below)\b", re.I)
ALL_SERIALS = re.compile(r"\ball\s+(?:manufacturer(?:'s)?\s+)?serial\s+numbers\b|\ball\s+(?:S/Ns|MSNs)\b", re.I)
CONDITION = re.compile(
    r"\b(?:except|excluding|other than|having|equipped with|part numbers?|P/Ns?|installed|identified in|"
    r"listed in|specified in|as identified|modified|on which|that have|that has|that do not|which have|if)\b",
    re.I)
SB_REF = re.compile(r"(?:identified|listed) in .{0,80}?(?:bulletin|service information)|"
                    r"(?:service bulletin|customer bulletin|service information).{0,120}?(?:identified|listed)", re.I)
BOILERPLATE = re.compile(r"having serial numbers?|,?\s*certificated in any category,?|,?\s*all\s+(?:manufacturer(?:'s)?\s+)?serial\s+numbers,?", re.I)

MODEL_TOK = re.compile(r"\b(?:[A-Z]{1,6}[- ]?\d{1,5}[A-Z0-9]*(?:-[A-Z0-9]+)*|\d{3,4}[A-Z]{0,3}(?:-[A-Z0-9]+)*|[A-Z]{1,3}-[A-Z]{1,3})\b")
DASH_LIST = re.compile(r"\b([A-Z]*\d{2,5}[A-Z]*)-([A-Z0-9]+)((?:\s*,\s*(?:and\s+|or\s+)?-[A-Z0-9]+|\s+(?:and|or)\s+-[A-Z0-9]+)+)")


def norm_model(tok: str) -> str:
    """'R-44' == 'R44', 'EMB-120' == 'EMB120', 'PA-28-140' -> 'PA28-140'."""
    t = re.sub(r"\s+", "", tok.upper())
    return re.sub(r"^([A-Z]{1,4})-(?=\d)", r"\1", t)


_GLUE_BLOCK = set("""MODEL MODELS AND OR SERIES PART PARTS SECTION SEC NO NOS AD ADS FR CFR THE OF TO ON IN
FOR WITH ALL AIRPLANES HELICOPTERS ENGINES TABLE PARAGRAPH CODE ATA BOEING AIRBUS EMBRAER CESSNA PIPER BELL
GULFSTREAM DASSAULT BOMBARDIER SIKORSKY ROBINSON HONEYWELL LEONARDO AGUSTA FOKKER LOCKHEED MITSUBISHI ROLLS ROYCE
PRATT WHITNEY GENERAL ELECTRIC DOUGLAS MCDONNELL TEXTRON COMPANY INC LTD CORPORATION SAS DOCKET AMENDMENT
DIRECTORATE IDENTIFIER VOLUME NUMBER PAGES PAGE WITHIN EACH HOURS DAYS CYCLES MONTHS YEARS""".split())


def model_tokens(text: str) -> set[str]:
    text = text.upper()
    toks = set()
    for t in MODEL_TOK.findall(text):
        if " " in t:
            word, rest = t.split(" ", 1)
            toks.add(norm_model(rest))
            if word not in _GLUE_BLOCK:
                toks.add(norm_model(t))   # "FALCON 2000" -> FALCON2000 (and 2000)
        else:
            toks.add(norm_model(t))
    for m in DASH_LIST.finditer(text):
        base = m.group(1)
        toks.add(norm_model(f"{base}-{m.group(2)}"))
        for suf in re.findall(r"-([A-Z0-9]+)", m.group(3)):
            toks.add(norm_model(f"{base}-{suf}"))
    # drop obvious non-models: years, FR pages, docket fragments, CFR parts
    return {t for t in toks if not re.fullmatch(r"(19|20)\d\d|39|\d{5,}|CFR\d*", t)}


def sn_value(raw: str) -> tuple[str, int]:
    """Split a serial into (prefix, numeric value). '28-20000' -> ('28-', 20000)."""
    raw = raw.strip().upper()
    m = re.fullmatch(r"(\d{1,2}-)(\d{5,8})", raw)
    if m:
        return m.group(1), int(m.group(2))
    m = re.match(r"([A-Z]*)[- ]?(\d+)", raw)
    return (m.group(1), int(m.group(2))) if m else ("", -1)


def parse_serials(sentence: str) -> list[dict]:
    """Return serial constraints found after a serial keyword in one sentence."""
    out = []
    for kw in SERWORD.finditer(sentence):
        exclude = bool(EXCEPT_BEFORE.search(sentence[max(0, kw.start() - 40):kw.start()]))
        n_before = len(out)
        tail = sentence[kw.end():kw.end() + 220]
        tail = re.split(r"\bModels?\b|;|\.\s", tail)[0]
        used = []
        for m in RANGE.finditer(tail):
            (p1, lo), (p2, hi) = sn_value(m.group(1)), sn_value(m.group(2))
            if lo <= hi:
                out.append({"lo": lo, "hi": hi, "prefix": p1 or p2, "raw": m.group(0).strip()})
                used.append(m.span())
        for pat, kind in ((OPEN_HI, "open_hi"), (AND_PRIOR, "and_prior")):
            for m in pat.finditer(tail):
                p, v = sn_value(m.group(1))
                out.append({"lo": v if kind == "open_hi" else 0, "hi": None if kind == "open_hi" else v,
                            "prefix": p, "raw": m.group(0).strip()})
                used.append(m.span())
        for m in PRIOR.finditer(sentence[max(0, kw.start() - 40):kw.end() + 120]):
            p, v = sn_value(m.group(1))
            out.append({"lo": 0, "hi": v - 1, "prefix": p, "raw": m.group(0).strip()})
        # remaining single serials in a list ("S/N 1012, 1015, and 1020")
        if not used:
            singles = re.findall(rf"(?<![\w-])({SN})(?![\w-])", tail)
            if singles and re.match(r"\s*(?:\(\w+\)\s*)?[A-Z]{0,4}[- ]?\d", tail):
                for s in singles[:200]:
                    p, v = sn_value(s)
                    out.append({"lo": v, "hi": v, "prefix": p, "raw": s})
        for r in out[n_before:]:
            r["exclude"] = exclude
    # de-duplicate
    uniq, seen = [], set()
    for r in out:
        k = (r["lo"], r["hi"], r["prefix"], r.get("exclude"))
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq


def parse_applicability(text: str) -> dict:
    sents = split_sentences(text)
    ranges, conditions = [], []
    for s in sents:
        for r in parse_serials(s):
            r["evidence"] = s
            ranges.append(r)
        stripped = BOILERPLATE.sub(" ", s)
        if CONDITION.search(stripped):
            conditions.append(s)
    return {
        "all_serials": bool(ALL_SERIALS.search(text)),
        "serial_ranges": ranges,
        "conditions": conditions,
        "sb_reference": any(SB_REF.search(x) for x in sents),
        # tables of model/serial pairs are not machine-checked: flagged for manual review
        "table": bool(re.search(r"\btable\b", text, re.I)),
        "sentences": sents,
    }


# ----------------------------------------------------------------------------- chunking

def paragraphs(text: str, target: int = 900) -> list[tuple[int, str]]:
    """Paragraph chunks with their start offsets, merged up to ~target chars."""
    out, buf, start, pos = [], "", None, 0
    for para in re.split(r"(\n\s*\n)", text):
        if not para.strip() or re.fullmatch(r"\n\s*\n", para):
            pos += len(para)
            continue
        if start is None:
            start = pos
        buf = (buf + "\n\n" + para.strip()).strip()
        pos += len(para)
        if len(buf) >= target:
            out.append((start, buf))
            buf, start = "", None
    if buf:
        out.append((start or 0, buf))
    return out


# ----------------------------------------------------------------------------- main build

def load_records() -> pd.DataFrame:
    df = pd.read_excel(INDEX_XLSX, sheet_name="nprm_index", header=2)
    df = df[df["record_id"].notna()].copy()
    for c in ("issue_date", "publish_date"):
        df[c] = pd.to_datetime(df[c], errors="coerce").dt.strftime("%Y-%m-%d")
    return df


def load_chains() -> dict:
    rc = pd.read_excel(INDEX_XLSX, sheet_name="revision_chains", header=2)
    rc = rc[rc["record_id"].notna()].copy()
    rc["publish_date"] = pd.to_datetime(rc["publish_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    chains = {}
    for docket, g in rc.groupby("docket_number"):
        g = g.sort_values("version")
        latest = g[g["status"] == "latest"].iloc[-1] if (g["status"] == "latest").any() else g.iloc[-1]
        members = [{"record_id": r.record_id, "version": int(r.version), "publish_date": r.publish_date,
                    "status": r.status} for r in g.itertuples()]
        for r in g.itertuples():
            chains[r.record_id] = {"docket": docket, "version": int(r.version), "total": int(r.versions_total),
                                   "latest_record": latest.record_id, "latest_date": latest.publish_date,
                                   "members": members}
    return chains


def build():
    t0 = time.time()
    df = load_records()
    chains = load_chains()
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
    CREATE TABLE records (
        record_id TEXT PRIMARY KEY, docket TEXT, subject TEXT, make_raw TEXT, makes TEXT, model_col TEXT,
        product_type TEXT, product_subtype TEXT, issue_date TEXT, publish_date TEXT, ad_number TEXT,
        supersedes_ad TEXT, mentions_serial TEXT, citation TEXT, fr_volume TEXT, page_start INT, page_end INT,
        fr_link TEXT, summary TEXT, text_source TEXT, withdrawn INT, withdrawn_evidence TEXT,
        app_label TEXT, app_title TEXT, app_text TEXT, app_page INT, applicability TEXT, model_tokens TEXT,
        chain TEXT);
    CREATE TABLE chunks (id INTEGER PRIMARY KEY, record_id TEXT, section TEXT, title TEXT, page INT, text TEXT);
    CREATE VIRTUAL TABLE chunks_fts USING fts5(text, subject, content='', tokenize='porter unicode61');
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE model_vocab (token TEXT, make TEXT, record_id TEXT);
    """)

    stats = {"records": 0, "fetched": 0, "sheet_text": 0, "summary_only": 0, "withdrawn": 0,
             "with_applicability": 0, "pages_fulltext": 0, "pages_total": 0, "chunks": 0, "chars": 0}

    for r in df.itertuples():
        rid = r.record_id
        link = r.federal_register_link if isinstance(r.federal_register_link, str) else ""
        stub_withdrawal = ""
        if link and not link.startswith("http") and len(link) < 300 and "http" in link:
            # cells like 'Withdrawal: https://www.federalregister.gov/d/2021-01771'
            stub_withdrawal = "Index sheet marks this record as a withdrawal notice: " + link.strip(' "')
            link = re.search(r"https?://\S+", link).group(0).strip('"')
        p0, p1 = page_range(r.pages)
        npages = (p1 - p0 + 1) if p0 else 0
        stats["pages_total"] += npages

        fetched = FULLTEXT_DIR / f"{rid}.txt"
        if fetched.exists() and fetched.stat().st_size > 500:
            full, source = fetched.read_text(encoding="utf-8", errors="ignore"), "fetched"
        elif link and not link.startswith("http"):
            full, source = link, "sheet"
        else:
            full, source = "", "summary_only"
        full = clean_text(full)
        summary = clean_text(r.summary if isinstance(r.summary, str) else "")
        stats[{"fetched": "fetched", "sheet": "sheet_text", "summary_only": "summary_only"}[source]] += 1
        if source != "summary_only":
            stats["pages_fulltext"] += npages

        # page lookup from [[Page N]] markers, if present
        marks = [(m.start(), int(m.group(1))) for m in PAGE_MARK.finditer(full)]

        def page_at(pos: int) -> int | None:
            pg = p0
            for mpos, n in marks:
                if mpos <= pos:
                    pg = n
                else:
                    break
            return pg

        withdrawn_ev = stub_withdrawal
        m = re.search(r"[^\n]*(?:is withdrawn|we withdraw the (?:NPRM|notice|proposed))[^\n]*", full, re.I)
        if m:
            withdrawn_ev = re.sub(r"\s+", " ", m.group(0)).strip()[:400]
        if not withdrawn_ev:
            # summary-only withdrawals: subject says "Withdrawal" or the summary says the FAA withdraws the NPRM
            ms = re.search(r"[^.]*\b(?:is withdrawing|withdraws|are withdrawing|is withdrawn)\b[^.]*\.", summary[:600], re.I)
            if ms:
                withdrawn_ev = ms.group(0).strip()
            elif re.search(r"\bwithdrawal\b", r.subject or "", re.I):
                withdrawn_ev = f"Subject line: {r.subject}"

        # regulatory text and sections
        rs = REG_START.search(full)
        reg_off = rs.start() if rs else 0
        preamble, reg = full[:reg_off], full[reg_off:]
        sections = find_sections(reg) if reg else []
        app = next((s for s in sections if re.search(r"applicab", s["title"], re.I)), None)
        if app is None:
            app = next((s for s in sections if APPLIC_HINT.search(s["title"] + " " + s["text"][:200])), None)
        if app is None:  # older format: "Applicability: ..." without a letter
            m = re.search(r"Applicability\s*[:.]?\s*\n?\s*(.+?)(?=\n\s*\n[A-Z][^\n]{0,40}\n|Compliance[:.]|\n\s*\(?[b-e]\)\s)", reg or full, re.S)
            if m:
                app = {"label": "", "title": "Applicability", "start": m.start(), "text": m.group(1).strip()}

        app_text = re.sub(r"\s+", " ", PAGE_MARK.sub(" ", app["text"])).strip() if app else ""
        # standard "Note 1: This AD applies to each airplane ... regardless of whether it has been modified"
        # boilerplate is not an applicability condition
        app_text = re.split(r"\bNote \d+:", app_text)[0].strip()
        applic = parse_applicability(app_text) if app_text else None
        if applic:
            stats["with_applicability"] += 1
        tokens = model_tokens(app_text) if app_text else set()
        # the sheet's model column and subject line are also verbatim sources
        for extra in (r.model if isinstance(r.model, str) else "", r.subject or ""):
            tokens |= model_tokens(extra.replace("|", ","))
        makes = canonical_makes(f"{r.make or ''} | {r.subject or ''}")

        con.execute("INSERT INTO records VALUES (" + ",".join("?" * 29) + ")", (
            rid, r.docket_number, r.subject, r.make if isinstance(r.make, str) else "", json.dumps(makes),
            r.model if isinstance(r.model, str) else "", r.product_type if isinstance(r.product_type, str) else "",
            r.product_subtype if isinstance(r.product_subtype, str) else "", r.issue_date, r.publish_date,
            r.ad_number if isinstance(r.ad_number, str) else "",
            r.supersedes_ad if isinstance(r.supersedes_ad, str) else "", str(r.mentions_serial_numbers),
            r.citation, fr_volume(r.citation), p0, p1, link if link.startswith("http") else "", summary, source,
            int(bool(withdrawn_ev)), withdrawn_ev,
            app["label"] if app else "", app["title"] if app else "", app_text,
            page_at(reg_off + app["start"]) if app else None,
            json.dumps(applic) if applic else None, json.dumps(sorted(tokens)),
            json.dumps(chains.get(rid)) if rid in chains else None))
        stats["withdrawn"] += int(bool(withdrawn_ev))
        for t in tokens:
            for mk in makes or ["?"]:
                con.execute("INSERT INTO model_vocab VALUES (?,?,?)", (t, mk, rid))

        # chunks: summary, preamble paragraphs, then each lettered section
        chunk_rows = []
        if summary:
            chunk_rows.append(("summary", "Summary (index sheet)", p0, summary))
        for off, txt in paragraphs(preamble):
            chunk_rows.append(("preamble", "Preamble / discussion", page_at(off), txt))
        if sections:
            for s in sections:
                for off, txt in paragraphs(s["text"]):
                    chunk_rows.append((s["label"], s["title"], page_at(reg_off + s["start"] + off), txt))
        elif reg.strip():
            for off, txt in paragraphs(reg):
                chunk_rows.append(("reg", "Regulatory text", page_at(reg_off + off), txt))
        for sec, title, pg, txt in chunk_rows:
            txt = re.sub(r"[ \t]+", " ", PAGE_MARK.sub(" ", txt)).strip()
            if len(txt) < 25:
                continue
            cur = con.execute("INSERT INTO chunks (record_id, section, title, page, text) VALUES (?,?,?,?,?)",
                              (rid, sec, title, pg, txt))
            con.execute("INSERT INTO chunks_fts (rowid, text, subject) VALUES (?,?,?)",
                        (cur.lastrowid, txt, r.subject or ""))
            stats["chunks"] += 1
            stats["chars"] += len(txt)
        stats["records"] += 1

    stats["est_pages_indexed"] = round(stats["chars"] / 3000)  # ~3,000 chars per printed FR page
    stats["build_seconds"] = round(time.time() - t0, 1)
    for k, v in stats.items():
        con.execute("INSERT INTO meta VALUES (?,?)", (k, str(v)))
    con.execute("CREATE INDEX idx_chunks_rec ON chunks(record_id)")
    con.execute("CREATE INDEX idx_vocab_tok ON model_vocab(token)")
    con.commit()
    con.close()
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    build()
