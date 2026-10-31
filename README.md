# AeroTrace: airworthiness directive search you can verify

Acacia Hackathon 2026 · Challenge #4 · AI & Aerospace track

AeroTrace answers questions about the provided FAA airworthiness directive proposals (NPRMs). It **only** answers from
those documents, **cites every line**, and **refuses or asks a follow-up question** when the documents don't settle it.

The core idea: **code decides, the LLM never writes facts.**

| Requirement | How AeroTrace meets it |
|---|---|
| Specific references | Every checklist step and evidence sentence carries `record_id · docket · FR volume + page · §section` |
| Zero hallucination | Answers are verbatim corpus sentences. Claude (optional) can only *choose* sentence IDs; its query parsing is accepted only if the value appears word for word in the question |
| Configuration filtering | Rule engine parses §(c) Applicability: models, serial ranges (incl. boundaries, open ranges, `28-20000`-style serials), exclusions ("except S/N 123"), conditions, referenced SB lists, tables |
| Revision logic | `revision_chains` → every card shows *LATEST* or *OUTDATED · latest is NPRM-xxx*. If a later record in the docket is a withdrawal, the earlier proposal is marked WITHDRAWN ("would have applied") |
| Clear uncertainty | Refusal gates: unknown IDs, manufacturer/model not in corpus, contradictions (Boeing + A330), ambiguous models, topic not covered. Missing facts produce a **follow-up question** (e.g. "What is the serial number?") |
| < 3 s retrieval | SQLite FTS5 + in-memory rule engine: ~1-10 ms per query on the current corpus |
| Structured output | Verdict cards + checklist in the UI; full JSON download for every answer |
| Proposed vs final | Every card says PROPOSED RULE (NPRM) unless the index has a final AD number |

## Run it

```bash
pip install -r requirements.txt
# 1. (laptop with internet) fetch full text for the 235 records that only have a URL
python fetch_fulltext.py          # try --limit 5 first; safe to re-run
# 2. build the index (deterministic, ~3 s)
python build_index.py
# 3. check it
python trap_tests.py              # 22 trap questions, should be 22/22
# 4. demo (custom web UI)
python server.py                  # open http://localhost:8000
# backup UI if anything goes wrong: streamlit run app.py
```

Optional: `export ANTHROPIC_API_KEY=...` (hackathon credits) to turn on Claude for query understanding and evidence
selection. Default model `claude-haiku-5-5` (override with `AEROTRACE_MODEL`). Without a key everything still works on
the rule-based parser.

## Corpus

- 431 NPRMs from `challenge4_ad_index.xlsx` (put it in `data/`).
- 169 already include full text in the sheet's link column (incl. withdrawal notices); the rest have a Federal
  Register URL. `fetch_fulltext.py` pulls those through the Federal Register API (raw text keeps `[[Page N]]` markers,
  which give page-level citations).
- Page ranges in the index add up to **1,341 Federal Register pages**. The UI header shows how many are full text vs
  summary only. Records still summary-only are labelled **UNVERIFIED** and never get an "applies" verdict.

## Files

| File | What it does |
|---|---|
| `fetch_fulltext.py` | Fetch Federal Register full text → `data/fulltext/` |
| `build_index.py` | Split documents into lettered sections, parse applicability, build revision chains, FTS5 index → `data/corpus.db` |
| `engine.py` | Query parsing, refusal gates, rule engine, verbatim evidence, optional Claude |
| `server.py` + `web/index.html` | Main UI: FastAPI JSON API + custom frontend (maintenance-tag verdicts, source viewer with highlighted sentence, revision timeline). Fonts are bundled, so it works offline |
| `app.py` | Backup Streamlit UI |
| `trap_tests.py` | Trap-question suite (also runs from the UI) |

## Verdicts

- **APPLIES**: every check passed against the applicability text
- **APPLIES IF CONDITION MET**: matches, but the text adds a condition that needs a human check
- **CANNOT DETERMINE - NEED INFO**: serial/model missing, list sits in a referenced service bulletin, or a table
- **UNVERIFIED - SUMMARY ONLY**: no full text, so applicability is not claimed
- **WITHDRAWN PROPOSAL**: the record, or a later record in its docket, withdraws it
- **DOES NOT APPLY**: a check failed; the failing sentence is shown

## Demo script (3 min)

1. *"Which ADs apply to a Bombardier DHC-8-400?"* → yellow **Hold** tag: the AD only covers certain serials, so it
   asks. Type `4082` → green **Not affected** (range is 4003-4081), sentence highlighted in the source panel. Try
   `4081` → red **Action required** (inclusive boundary).
2. *"Robinson R22 S/N 0100"* → the serial is in range, **but** the docket's latest record withdraws the proposal → grey
   **Withdrawn**, "would have applied". 125 of the 431 records are withdrawals; a chatbot would present them as
   requirements.
3. *"What does NPRM-302 require?"* → quoted answer with citations + **Outdated: version 1 of 4, latest is NPRM-270**,
   with the revision timeline.
4. *"Boeing A330 fuel tank inspection"* → **Can't answer: contradiction**. *"Comac C919"* → **out of scope**.
5. **Try to break it** tab → run 22 trap questions live → 22 of 22, slowest a few ms. Hand judges the keyboard.

Pitch line: *"A chatbot guesses. AeroTrace proves it, or tells you exactly what it needs to know."*

## Known limits (say these out loud, judges respect it)

- Applicability parsing is regex-based; anything it can't parse confidently becomes NEED INFO or CONDITIONAL, never
  APPLIES.
- Tables of model/serial pairs are flagged for manual review rather than machine-checked.
- Component serial numbers (e.g. a part S/N in §(f)) are not evaluated; only §(c) applicability is.
