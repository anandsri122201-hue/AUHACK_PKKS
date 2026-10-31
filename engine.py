"""AeroTrace query engine.

Design rule: code decides, the LLM never writes facts.
  * Query understanding: regex first; Claude (optional) may fill gaps, but any
    value it returns must appear verbatim in the user's question.
  * Applicability: a deterministic rule engine over the parsed §(c) text.
  * Answers: verbatim sentences from the corpus, each with its citation.
    Claude (optional) may only *choose* among sentence IDs.
  * Refusals: explicit gates with a stated reason.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field

from common import DB_PATH, KNOWN_MANUFACTURERS, canonical_makes, split_sentences
from build_index import model_tokens, norm_model, sn_value

# verdicts
APPLIES = "APPLIES"
CONDITIONAL = "APPLIES IF CONDITION MET"
NEED_INFO = "CANNOT DETERMINE - NEED INFO"
NOT_APPLICABLE = "DOES NOT APPLY"
UNVERIFIED = "UNVERIFIED - SUMMARY ONLY"
WITHDRAWN = "WITHDRAWN PROPOSAL"
NOT_EVALUATED = "MATCHED BY TOPIC (applicability not evaluated)"

STOP = set("""a an the of for to in on at by with and or is are was were be been do does did what which who whom
whose when where why how any all my our your this that these those it its as from about into than then there
their them they we you i me can could should would will shall may might must have has had apply applies
applicable ad ads airworthiness directive directives directive's faa nprm proposed proposal rule rules
serial number numbers s/n sn msn model models aircraft airplane airplanes helicopter helicopters engine engines
please tell show give list find need know required require requirements latest current version""".split())

MIN_TOPIC_COVERAGE = 0.6      # share of topic terms a cited sentence must contain
MAX_CARDS = 12


@dataclass
class ParsedQuery:
    raw: str
    record_ids: list = field(default_factory=list)
    dockets: list = field(default_factory=list)
    ad_numbers: list = field(default_factory=list)
    makes: list = field(default_factory=list)
    out_of_corpus_makes: list = field(default_factory=list)
    models: list = field(default_factory=list)          # model tokens found in corpus vocabulary
    unknown_models: list = field(default_factory=list)  # model-like tokens not in corpus
    serial: str | None = None
    topic_terms: list = field(default_factory=list)
    parser: str = "rules"


class Engine:
    def __init__(self, db_path=DB_PATH, use_llm: bool | None = None):
        self.con = sqlite3.connect(db_path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.meta = {r["key"]: r["value"] for r in self.con.execute("SELECT * FROM meta")}
        self.vocab: dict[str, set[str]] = {}
        for r in self.con.execute("SELECT token, make FROM model_vocab"):
            self.vocab.setdefault(r["token"], set()).add(r["make"])
        self.corpus_makes = {m for ms in self.vocab.values() for m in ms} | {
            m for (j,) in self.con.execute("SELECT makes FROM records") for m in json.loads(j)}
        self.records = {r["record_id"]: dict(r) for r in self.con.execute("SELECT * FROM records")}
        self.llm = None
        if use_llm is None:
            use_llm = bool(os.environ.get("ANTHROPIC_API_KEY"))
        if use_llm:
            try:
                import anthropic
                self.llm = anthropic.Anthropic(timeout=2.5, max_retries=0)
                self.llm_model = os.environ.get("AEROTRACE_MODEL", "claude-haiku-5-5")
            except Exception:
                self.llm = None

    # ------------------------------------------------------------------ parsing
    def _vocab_match(self, tok: str) -> str | None:
        tok = norm_model(tok)
        if tok in self.vocab:
            return tok
        # "737" should match "737-800"; "A330-243" should match listed "A330-243" only
        if any(v.startswith(tok + "-") for v in self.vocab):
            return tok
        return None

    def parse(self, q: str, make: str = "", model: str = "", serial: str = "") -> ParsedQuery:
        p = ParsedQuery(raw=q)
        text = q
        p.record_ids = sorted({f"NPRM-{int(n):03d}" for n in re.findall(r"\bNPRM[- ]?(\d{1,3})\b", text, re.I)})
        p.dockets = sorted(set(re.findall(r"\bFAA-\d{4}-\d{3,5}\b", text, re.I)) |
                           set(re.findall(r"\b\d{4}-[A-Z]{2}-\d{2,3}-AD\b", text, re.I)))
        p.ad_numbers = sorted(set(re.findall(r"\bAD\s*((?:19|20)?\d{2}-\d{2}-\d{2}(?:\s*R\d)?)\b", text, re.I)))
        m = re.search(r"(?:\bS/N|\bserial(?:\s+(?:number|no\.?|#))?|\bMSN|\bSN)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-]*\d[A-Z0-9\-]*)",
                      text, re.I)
        if m:
            p.serial = m.group(1).upper()
        # strip identifiers before looking for model tokens
        scrub = text
        for pat in (r"\bNPRM[- ]?\d{1,3}\b", r"\bFAA-\d{4}-\d{3,5}\b", r"\b\d{4}-[A-Z]{2}-\d{2,3}-AD\b",
                    r"\bAD\s*(?:19|20)?\d{2}-\d{2}-\d{2}(?:\s*R\d)?\b"):
            scrub = re.sub(pat, " ", scrub, flags=re.I)
        if m:
            scrub = scrub.replace(m.group(0), " ")
        p.makes = canonical_makes(scrub)
        p.out_of_corpus_makes = [k for k, pat in KNOWN_MANUFACTURERS.items() if re.search(pat, scrub, re.I)]
        has_model_cue = bool(p.makes) or bool(re.search(r"\bmodels?\b", scrub, re.I))
        for tok in model_tokens(scrub.upper()):
            if tok.isdigit() and not has_model_cue:
                continue
            if re.fullmatch(r"\d{1,2}", tok):
                continue
            hit = self._vocab_match(tok)
            (p.models if hit else p.unknown_models).append(hit or tok)
        # structured overrides from the UI always win
        if make:
            p.makes = canonical_makes(make) or [make]
        if model:
            hit = self._vocab_match(model)
            p.models, p.unknown_models = ([hit], []) if hit else ([], [norm_model(model)])
        if serial:
            p.serial = serial.strip().upper()
        # topic terms
        words = re.findall(r"[a-zA-Z][a-zA-Z\-]{2,}", scrub.lower())
        make_words = set()
        for mk in p.makes:
            make_words |= set(re.findall(r"[a-z]+", mk.lower()))
        words = [w.strip("-") for w in words]
        model_blob = " ".join(p.models + p.unknown_models)
        p.topic_terms = [w for w in dict.fromkeys(words)
                         if len(w) > 2 and w not in STOP and w not in make_words
                         and norm_model(w) not in model_blob]
        if self.llm and not (p.makes or p.models or p.record_ids):
            self._llm_fill(p)
        return p

    def _llm_fill(self, p: ParsedQuery):
        """Ask Claude for make/model/serial. Keep only values present verbatim in the query."""
        try:
            msg = self.llm.messages.create(
                model=self.llm_model, max_tokens=200, temperature=0,
                system=("Extract fields from an aviation maintenance question. Return ONLY JSON: "
                        '{"make": str|null, "model": str|null, "serial": str|null}. '
                        "Copy text exactly as it appears in the question. Never infer or add values."),
                messages=[{"role": "user", "content": p.raw}])
            data = json.loads(re.search(r"\{.*\}", msg.content[0].text, re.S).group(0))
        except Exception:
            return
        low = p.raw.lower()
        if data.get("make") and data["make"].lower() in low:
            p.makes = canonical_makes(data["make"]) or p.makes
        if data.get("model") and data["model"].lower() in low:
            hit = self._vocab_match(data["model"])
            if hit:
                p.models = [hit]
        if data.get("serial") and data["serial"].lower() in low and not p.serial:
            p.serial = data["serial"].upper()
        p.parser = "rules+claude(verified)"

    # ------------------------------------------------------------------ helpers
    def cite(self, rec: dict, section: str = "", title: str = "", page=None) -> str:
        vol = rec.get("fr_volume")
        if page:
            pg = f"p. {page}"
        elif rec.get("page_start"):
            pg = f"pp. {rec['page_start']}-{rec['page_end']}"
        else:
            pg = "page n/a"
        fr = f"{vol} FR {pg}" if vol else pg
        sec = ""
        if section and section not in ("summary", "preamble", "reg"):
            sec = f" · §({section}) {title}".rstrip()
        elif section == "summary":
            sec = " · Summary (index sheet)"
        elif section == "preamble":
            sec = " · Preamble"
        return f"{rec['record_id']} · Docket {rec['docket']} · {fr}{sec}"

    def fts(self, terms: list[str], record_ids: list[str] | None, limit: int = 40):
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        sql = ("SELECT c.id, c.record_id, c.section, c.title, c.page, c.text, bm25(chunks_fts) AS score "
               "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid WHERE chunks_fts MATCH ?")
        args: list = [match]
        if record_ids is not None:
            if not record_ids:
                return []
            sql += f" AND c.record_id IN ({','.join('?' * len(record_ids))})"
            args += record_ids
        sql += " ORDER BY score LIMIT ?"
        args.append(limit)
        try:
            return [dict(r) for r in self.con.execute(sql, args)]
        except sqlite3.OperationalError:
            return []

    # ------------------------------------------------------------------ rule engine
    def evaluate_record(self, rec: dict, p: ParsedQuery, strict: bool = True) -> dict:
        steps = []
        app = json.loads(rec["applicability"]) if rec["applicability"] else None
        app_cite = self.cite(rec, rec["app_label"], rec["app_title"] or "Applicability", rec["app_page"]) if app else None
        rec_makes = json.loads(rec["makes"])
        tokens = set(json.loads(rec["model_tokens"]))
        fail = need = cond = False

        def step(check, result, detail, cite=None, evidence=None):
            steps.append({"check": check, "result": result, "detail": detail, "cite": cite, "evidence": evidence})

        # 1. status of the document itself
        if rec["withdrawn"]:
            step("Document status", "warn", "This proposal was WITHDRAWN.", self.cite(rec), rec["withdrawn_evidence"])
        if not rec["ad_number"]:
            step("Legal status", "info", "Proposed rule (NPRM) only; no final AD number in the index.", self.cite(rec))
        else:
            step("Legal status", "info", f"Final AD number in index: {rec['ad_number']}.", self.cite(rec))

        # 2. make
        if p.makes:
            if set(p.makes) & set(rec_makes):
                step("Manufacturer", "pass", f"{', '.join(set(p.makes) & set(rec_makes))} matches record make "
                     f"'{rec['make_raw']}'.", self.cite(rec))
            else:
                step("Manufacturer", "fail", f"Record is for '{rec['make_raw']}', not {', '.join(p.makes)}.", self.cite(rec))
                fail = True

        # 3. model
        if p.models:
            hits = [m for m in p.models if m in tokens or any(t.startswith(m + "-") or m.startswith(t + "-") for t in tokens)]
            src = "§(c) applicability" if app else "subject line / model column"
            if hits:
                ev = next((s for s in (app or {}).get("sentences", [])
                           if any(h.replace("-", "") in norm_model(s).replace("-", "") for h in hits)), None)
                step("Model", "pass", f"Model {', '.join(hits)} is listed in the {src}.", app_cite or self.cite(rec), ev or rec["subject"])
            else:
                step("Model", "fail", f"Model {', '.join(p.models)} is not listed in the {src}.", app_cite or self.cite(rec),
                     (rec["app_text"] or rec["subject"])[:400])
                fail = True
        elif app and strict:
            step("Model", "unknown", "No model given; the applicability section lists specific models.", app_cite,
                 rec["app_text"][:400])
            need = True
        elif app:
            step("Applicability", "info", "Applies to the products listed in the applicability section.", app_cite,
                 rec["app_text"][:400])

        # 4. text availability
        if rec["withdrawn"]:
            return self._verdict(rec, steps, fail, need=False, cond=False, unverified=False, p=p, withdrawn=True)
        if not app:
            reason = ("Full text not in corpus (summary only)." if rec["text_source"] == "summary_only"
                      else "Applicability section could not be located in the full text.")
            step("Applicability text", "unknown", reason + " Exact applicability cannot be verified.", self.cite(rec))
            return self._verdict(rec, steps, fail, need=True, cond=False, unverified=True, p=p)

        # 5. serial
        incl = [r for r in app["serial_ranges"] if not r.get("exclude")]
        excl = [r for r in app["serial_ranges"] if r.get("exclude")]
        if app.get("table"):
            step("Serial number", "unknown", "Applicability is given as a table of models/serials; check the table "
                 "manually.", app_cite, rec["app_text"][:600])
            need = True
        elif incl:
            desc = "; ".join(r["raw"] for r in incl[:6])
            if not p.serial and not strict:
                step("Serial number", "info", f"Applies only to certain serial numbers ({desc}).", app_cite,
                     incl[0]["evidence"])
            elif not p.serial:
                step("Serial number", "unknown", f"Applies only to certain serial numbers ({desc}). Provide the serial "
                     "number.", app_cite, incl[0]["evidence"])
                need = True
            else:
                qp, qv = sn_value(p.serial)

                def inside(r):
                    return (r["prefix"] or "") in ("", qp) and qv >= r["lo"] and (r["hi"] is None or qv <= r["hi"])

                inc_hit = next((r for r in incl if inside(r)), None)
                exc_hit = next((r for r in excl if inside(r)), None)
                if inc_hit and not exc_hit:
                    step("Serial number", "pass", f"S/N {p.serial} is within '{inc_hit['raw']}'.", app_cite, inc_hit["evidence"])
                elif exc_hit:
                    step("Serial number", "fail", f"S/N {p.serial} is explicitly excluded ('{exc_hit['raw']}').", app_cite,
                         exc_hit["evidence"])
                    fail = True
                else:
                    step("Serial number", "fail", f"S/N {p.serial} is outside the listed serials ({desc}).", app_cite,
                         incl[0]["evidence"])
                    fail = True
        elif app["all_serials"]:
            step("Serial number", "pass", "Applies to all serial numbers of the listed models.", app_cite,
                 next((s for s in app["sentences"] if re.search(r"all .{0,20}serial", s, re.I)), None))
        else:
            step("Serial number", "pass", "No serial-number restriction stated in the applicability section.", app_cite)

        # 6. conditions / referenced lists
        conds = [c for c in app["conditions"] if not re.search(r"serial", c, re.I) or re.search(
            r"except|excluding|having|installed|P/N|part number|modified|on which", c, re.I)]
        if app["sb_reference"]:
            step("Referenced list", "unknown", "Applicability depends on a list in a referenced service bulletin, "
                 "which is not in this corpus.", app_cite,
                 next((s for s in app["sentences"] if re.search(r"bulletin|service information", s, re.I)), None))
            need = True
        elif conds:
            step("Conditions", "warn", "Applicability has conditions that cannot be checked automatically.", app_cite,
                 " ".join(conds)[:600])
            cond = True
        return self._verdict(rec, steps, fail, need, cond, unverified=False, p=p)

    def _verdict(self, rec, steps, fail, need, cond, unverified, p, withdrawn=False):
        if withdrawn and not fail:
            v = WITHDRAWN
        elif fail:
            v = NOT_APPLICABLE
        elif unverified:
            v = UNVERIFIED
        elif need:
            v = NEED_INFO
        elif cond:
            v = CONDITIONAL
        else:
            v = APPLIES
        chain = json.loads(rec["chain"]) if rec["chain"] else None
        # a later record in the same docket that is a withdrawal kills this proposal
        if chain and chain["latest_record"] != rec["record_id"]:
            latest = self.records.get(chain["latest_record"])
            if latest and latest["withdrawn"]:
                would = {APPLIES: "would have applied", CONDITIONAL: "would have applied conditionally"}.get(v)
                steps.append({"check": "Revision chain", "result": "warn",
                              "detail": f"The latest record in docket {chain['docket']} ({latest['record_id']}, "
                                        f"{latest['publish_date']}) WITHDRAWS this proposal"
                                        + (f"; it {would} to this aircraft." if would else "."),
                              "cite": self.cite(latest), "evidence": latest["withdrawn_evidence"]})
                if v != NOT_APPLICABLE:
                    v = WITHDRAWN
        revision = None
        if chain:
            latest = chain["latest_record"] == rec["record_id"]
            revision = {"version": chain["version"], "total": chain["total"], "is_latest": latest,
                        "latest_record": chain["latest_record"], "latest_date": chain["latest_date"],
                        "members": chain["members"], "docket": chain["docket"]}
        return {
            "record_id": rec["record_id"], "docket": rec["docket"], "subject": rec["subject"],
            "make": rec["make_raw"], "publish_date": rec["publish_date"], "verdict": v,
            "withdrawn": bool(rec["withdrawn"]), "is_proposal": not rec["ad_number"], "ad_number": rec["ad_number"],
            "supersedes_ad": rec["supersedes_ad"], "revision": revision, "steps": steps,
            "text_source": rec["text_source"], "citation": self.cite(rec), "fr_link": rec["fr_link"],
            "applicability_text": rec["app_text"],
        }

    # ------------------------------------------------------------------ passages
    def passages(self, record_ids: list[str], terms: list[str], per_record: int = 3) -> tuple[list, float]:
        """Verbatim sentences that answer the topic, with citations. Returns (passages, best coverage)."""
        out, best = [], 0.0
        if terms:
            stems = [t[:6] for t in terms]
            for ch in self.fts(terms, record_ids, limit=60):
                rec = self.records[ch["record_id"]]
                for s in split_sentences(ch["text"]):
                    low = s.lower()
                    cov = sum(1 for st in stems if st in low) / len(stems)
                    if cov > 0:
                        out.append({"record_id": ch["record_id"], "text": s, "coverage": round(cov, 2),
                                    "cite": self.cite(rec, ch["section"], ch["title"], ch["page"]),
                                    "section": ch["section"]})
            out.sort(key=lambda x: (-x["coverage"], x["section"] == "summary"))
            best = out[0]["coverage"] if out else 0.0
        else:
            # no topic: show the unsafe condition / required action for each record
            for rid in record_ids:
                rec = self.records[rid]
                rows = self.con.execute(
                    "SELECT section, title, page, text FROM chunks WHERE record_id=? ORDER BY "
                    "CASE WHEN title LIKE '%Unsafe%' OR title LIKE '%Reason%' OR title LIKE '%problem%' THEN 0 "
                    "WHEN title LIKE '%Requir%' OR title LIKE '%Action%' OR title LIKE '%Inspect%' THEN 1 "
                    "WHEN section='summary' THEN 2 ELSE 3 END, id LIMIT 2", (rid,)).fetchall()
                for r in rows:
                    sents = split_sentences(r["text"])[:2]
                    for s in sents:
                        out.append({"record_id": rid, "text": s, "coverage": None,
                                    "cite": self.cite(rec, r["section"], r["title"], r["page"]), "section": r["section"]})
            best = 1.0
        # keep top N per record
        seen, kept = {}, []
        for x in out:
            if seen.get(x["record_id"], 0) < per_record and x["text"] not in {k["text"] for k in kept}:
                kept.append(x)
                seen[x["record_id"]] = seen.get(x["record_id"], 0) + 1
        return kept, best

    def _llm_select(self, question: str, passages: list) -> list | None:
        """Claude may only choose sentence IDs; it cannot write text."""
        if not self.llm or not passages:
            return None
        listing = "\n".join(f"[{i}] {p['text']}" for i, p in enumerate(passages[:40]))
        try:
            msg = self.llm.messages.create(
                model=self.llm_model, max_tokens=100, temperature=0,
                system=("You select evidence. Given a question and numbered sentences from FAA documents, return ONLY "
                        'JSON {"ids": [..]} with the sentences that directly answer the question. Return {"ids": []} '
                        "if none do. Never use outside knowledge."),
                messages=[{"role": "user", "content": f"Question: {question}\n\nSentences:\n{listing}"}])
            ids = json.loads(re.search(r"\{.*\}", msg.content[0].text, re.S).group(0))["ids"]
            return [passages[i] for i in ids if isinstance(i, int) and 0 <= i < len(passages)]
        except Exception:
            return None

    # ------------------------------------------------------------------ main entry
    def ask(self, question: str, make: str = "", model: str = "", serial: str = "") -> dict:
        t0 = time.perf_counter()
        p = self.parse(question, make, model, serial)
        res = {"query": asdict(p), "status": "answer", "message": "", "reasons": [], "cards": [],
               "passages": [], "needs": [], "corpus": self.corpus_summary()}

        def done(status, message, reasons=None):
            res.update(status=status, message=message, reasons=reasons or [])
            res["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return res

        if len(question.strip()) < 3 and not (make or model or serial):
            return done("refuse", "Please ask a question about an aircraft, engine, docket or record.")

        # --- gate: explicit identifiers
        ids = list(p.record_ids)
        for d in p.dockets:
            ids += [r for r, rec in self.records.items() if (rec["docket"] or "").upper() == d.upper()]
        for a in p.ad_numbers:
            a_norm = re.sub(r"\s+", " ", a.upper())
            hit = [r for r, rec in self.records.items()
                   if a_norm in (rec["ad_number"] or "").upper() or a_norm in (rec["supersedes_ad"] or "").upper()]
            ids += hit
            if not hit:
                res["reasons"].append(f"AD {a} does not appear in the corpus index.")
        ids = list(dict.fromkeys(i for i in ids if i in self.records))
        if (p.record_ids or p.dockets or p.ad_numbers) and not ids:
            return done("refuse", "None of the identifiers in your question are in the corpus.",
                        res["reasons"] + [f"Not found: {', '.join(p.record_ids + p.dockets + p.ad_numbers)}"])

        # --- gate: out-of-corpus manufacturer / model
        if p.out_of_corpus_makes and not p.makes and not ids:
            return done("refuse", f"Out of scope: the corpus contains no directives for "
                        f"{', '.join(p.out_of_corpus_makes)}.",
                        ["The corpus is limited to the 431 FAA NPRMs provided. I will not answer from general knowledge."])
        if p.unknown_models and not p.models and not ids:
            if p.makes:
                return done("refuse", f"Model {', '.join(p.unknown_models)} does not appear in any "
                            f"{', '.join(p.makes)} record in the corpus.",
                            ["Check the model designation, or the corpus may simply not cover it."])
            if not p.topic_terms:
                return done("refuse", f"Model {', '.join(p.unknown_models)} does not appear anywhere in the corpus.")

        # --- gate: contradiction (model belongs to a different manufacturer)
        if p.makes and p.models and not ids:
            for m in p.models:
                owners = {mk for tok, mks in self.vocab.items() if tok == m or tok.startswith(m + "-") for mk in mks}
                if owners and not owners & set(p.makes):
                    return done("refuse", f"Contradiction: model {m} appears only in records for "
                                f"{', '.join(sorted(owners))}, not {', '.join(p.makes)}.",
                                ["Please confirm the manufacturer and model."])

        # --- gate: ambiguous model (several manufacturers)
        if p.models and not p.makes and not ids:
            owners = {mk for m in p.models for tok, mks in self.vocab.items()
                      if tok == m or tok.startswith(m + "-") for mk in mks} - {"?"}
            if len(owners) > 1:
                return done("clarify", f"Ambiguous: model {', '.join(p.models)} appears in records for several "
                            f"manufacturers ({', '.join(sorted(owners))}). Which manufacturer?",
                            ["Re-ask with the manufacturer, or pick it in the sidebar."])

        # --- candidate records
        aircraft_given = bool(p.makes or p.models or p.serial)
        if ids:
            cands = ids
        elif p.makes or p.models:
            cands = []
            for rid, rec in self.records.items():
                rm = set(json.loads(rec["makes"]))
                if p.makes and not (rm & set(p.makes)):
                    continue
                if p.models:
                    toks = set(json.loads(rec["model_tokens"]))
                    if not any(m in toks or any(t.startswith(m + "-") or m.startswith(t + "-") for t in toks)
                               for m in p.models):
                        continue
                cands.append(rid)
            if p.topic_terms and cands:
                ranked = list(dict.fromkeys(c["record_id"] for c in self.fts(p.topic_terms, cands, 200)))
                cands = ranked + [c for c in cands if c not in ranked] if ranked else cands
        elif p.topic_terms:
            cands = list(dict.fromkeys(c["record_id"] for c in self.fts(p.topic_terms, None, 200)))
        else:
            return done("refuse", "I need an aircraft (manufacturer/model), a record/docket/AD number, or a topic.",
                        ["Example: 'Does the Gulfstream GV-SP AD apply to S/N 5064?'"])

        if not cands:
            return done("refuse", "No record in the corpus matches this question.",
                        ["The system only answers from the 431 FAA NPRMs provided."])

        # --- evaluate applicability
        cards = []
        for rid in cands:
            rec = self.records[rid]
            if aircraft_given or ids:
                cards.append(self.evaluate_record(rec, p, strict=aircraft_given))
            else:
                c = self._verdict(rec, [], False, False, False, False, p)
                c["verdict"] = NOT_EVALUATED
                cards.append(c)
        order = {APPLIES: 0, CONDITIONAL: 1, NEED_INFO: 2, UNVERIFIED: 3, NOT_EVALUATED: 4, WITHDRAWN: 5,
                 NOT_APPLICABLE: 6}
        if not ids:
            cards.sort(key=lambda c: (order.get(c["verdict"], 9), c["withdrawn"]))
        res["counts"] = {}
        for c in cards:
            res["counts"][c["verdict"]] = res["counts"].get(c["verdict"], 0) + 1
        res["total_matches"] = len(cards)
        res["cards"] = cards[:MAX_CARDS]

        # --- follow-up questions instead of flat refusals
        need_serial = [c["record_id"] for c in cards if any(s["check"] == "Serial number" and s["result"] == "unknown"
                                                             and "Provide" in s["detail"] for s in c["steps"])]
        need_model = [c["record_id"] for c in cards if any(s["check"] == "Model" and s["result"] == "unknown"
                                                            for s in c["steps"])]
        if need_serial:
            res["needs"].append({"field": "serial", "records": need_serial,
                                 "question": f"What is the serial number? It decides {len(need_serial)} record(s)."})
        if need_model:
            res["needs"].append({"field": "model", "records": need_model,
                                 "question": f"Which model? It decides {len(need_model)} record(s)."})

        # --- verbatim passages
        relevant = [c["record_id"] for c in res["cards"]
                    if c["verdict"] not in (NOT_APPLICABLE,) and (c["verdict"] != WITHDRAWN or ids)][:8]
        passages, best = self.passages(relevant, p.topic_terms)
        if self.llm and p.topic_terms and passages:
            chosen = self._llm_select(question, passages)
            if chosen is not None:
                passages = chosen
                res["query"]["parser"] += " · evidence selected by Claude (verbatim only)"
        res["passages"] = passages
        if p.topic_terms and (best < MIN_TOPIC_COVERAGE or not passages) and relevant:
            res["reasons"].append(
                f"No sentence in the matching records covers the topic terms ({', '.join(p.topic_terms)}). "
                "I will not fill the gap from general knowledge.")
            res["passages"] = []
            if not aircraft_given and not ids:
                return done("refuse", "The corpus does not contain a passage that answers this question.", res["reasons"])

        # --- headline
        n_app = sum(1 for c in cards if c["verdict"] in (APPLIES, CONDITIONAL))
        if ids:
            res["message"] = f"{len(cards)} record(s) found by identifier."
        elif aircraft_given:
            res["message"] = (f"{len(cards)} record(s) checked: {n_app} apply or apply conditionally, "
                              f"{res['counts'].get(WITHDRAWN, 0)} withdrawn, "
                              f"{res['counts'].get(NEED_INFO, 0)} need more information, "
                              f"{res['counts'].get(UNVERIFIED, 0)} cannot be verified (summary only), "
                              f"{res['counts'].get(NOT_APPLICABLE, 0)} do not apply.")
        else:
            res["message"] = (f"{len(cards)} record(s) match the topic. Applicability was not evaluated because "
                              "no aircraft was specified.")
        return done("answer", res["message"], res["reasons"])

    def corpus_summary(self) -> dict:
        m = self.meta
        return {"records": int(m["records"]), "full_text": int(m["fetched"]) + int(m["sheet_text"]),
                "summary_only": int(m["summary_only"]), "pages_total": int(m["pages_total"]),
                "pages_fulltext": int(m["pages_fulltext"]), "chunks": int(m["chunks"]),
                "est_pages_indexed": int(m["est_pages_indexed"]), "withdrawn": int(m["withdrawn"])}


if __name__ == "__main__":
    import sys
    e = Engine()
    r = e.ask(" ".join(sys.argv[1:]) or "Does the Gulfstream GV-SP AD apply to S/N 5064?")
    print(json.dumps({k: r[k] for k in ("status", "message", "reasons", "needs", "latency_ms")}, indent=2))
    for c in r["cards"][:5]:
        print(f"\n[{c['verdict']}] {c['record_id']} {c['subject']}")
        for s in c["steps"]:
            print(f"   {s['result']:7} {s['check']}: {s['detail']}")
    for x in r["passages"][:5]:
        print(f"\n  \"{x['text'][:200]}\"\n   -- {x['cite']}")
