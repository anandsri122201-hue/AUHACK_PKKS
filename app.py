"""AeroTrace: traceable airworthiness-directive search.   streamlit run app.py"""
import html
import json
import os

import pandas as pd
import streamlit as st

from engine import (APPLIES, CONDITIONAL, NEED_INFO, NOT_APPLICABLE, NOT_EVALUATED, UNVERIFIED, WITHDRAWN, Engine)
import trap_tests

st.set_page_config(page_title="AeroTrace · AD search you can verify", page_icon="✈️", layout="wide")

VERDICT_STYLE = {
    APPLIES: ("#0f7b3f", "#e6f4ec", "✔"),
    CONDITIONAL: ("#8a5a00", "#fff4dc", "◐"),
    NEED_INFO: ("#8a5a00", "#fff4dc", "?"),
    UNVERIFIED: ("#5b5b66", "#eeeef2", "○"),
    NOT_EVALUATED: ("#3b4f8a", "#e9eefb", "•"),
    WITHDRAWN: ("#7a2e8a", "#f5e9f8", "⊘"),
    NOT_APPLICABLE: ("#a12b2b", "#fbeaea", "✖"),
}
STEP_ICON = {"pass": "✅", "fail": "❌", "unknown": "❓", "warn": "⚠️", "info": "ℹ️"}

st.markdown("""
<style>
.block-container {padding-top: 1.6rem; max-width: 1200px;}
.at-badge {display:inline-block; padding:2px 10px; border-radius:999px; font-size:0.78rem; font-weight:600;
           margin-right:6px; border:1px solid rgba(0,0,0,0.08);}
.at-card {border:1px solid rgba(0,0,0,0.10); border-radius:12px; padding:14px 18px; margin:10px 0 4px 0;}
.at-quote {border-left:3px solid #9aa4b2; padding:4px 12px; margin:6px 0; color:inherit; font-size:0.93rem;}
.at-cite {font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:0.78rem; opacity:0.75;}
.at-banner {border-radius:12px; padding:14px 18px; margin:8px 0 14px 0; font-size:1.02rem;}
</style>""", unsafe_allow_html=True)


@st.cache_resource
def get_engine(use_llm: bool):
    return Engine(use_llm=use_llm)


def set_example(text):
    st.session_state["q"] = text
    for k in ("f_make", "f_model", "f_serial"):
        st.session_state[k] = ""


def apply_followup(field):
    val = st.session_state.get(f"need_{field}", "").strip()
    if val:
        st.session_state[f"f_{field}"] = val


def badge(text, fg, bg):
    return f'<span class="at-badge" style="color:{fg};background:{bg}">{html.escape(text)}</span>'


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.markdown("### ✈️ AeroTrace")
    st.caption("Answers only from the provided FAA NPRM corpus. Every line is cited. "
               "When the documents don't settle it, it says so.")
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    use_llm = st.toggle("Use Claude for query understanding / evidence selection", value=has_key,
                        disabled=not has_key,
                        help="Claude never writes answer text. Its outputs are checked against the question "
                             "and the corpus. Without a key, the rule-based parser is used.")
    st.markdown("**Aircraft (optional, overrides the question)**")
    f_make = st.text_input("Manufacturer", key="f_make", placeholder="e.g. Gulfstream")
    f_model = st.text_input("Model", key="f_model", placeholder="e.g. GV-SP")
    f_serial = st.text_input("Serial number", key="f_serial", placeholder="e.g. 5050")
    st.markdown("**Try these**")
    examples = ["Which ADs apply to a Robinson R22?",
                "Robinson R22 S/N 2520",
                "Does the Gulfstream GV-SP AD apply to S/N 5050?",
                "What does NPRM-302 require?",
                "Boeing 737-800",
                "Boeing A330 fuel tank inspection",
                "What ADs apply to the Comac C919?"]
    for ex in examples:
        st.button(ex, use_container_width=True, on_click=set_example, args=(ex,))

engine = get_engine(use_llm)
cs = engine.corpus_summary()

st.markdown("## AeroTrace — airworthiness directive search you can verify")
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Documents", cs["records"])
c2.metric("Full text", cs["full_text"])
c3.metric("Summary only", cs["summary_only"])
c4.metric("FR pages (full text / all)", f"{cs['pages_fulltext']} / {cs['pages_total']}")
c5.metric("Indexed passages", cs["chunks"])

tab_search, tab_break, tab_about = st.tabs(["🔎 Search", "🧪 Try to break it", "📚 Corpus & method"])

# ---------------------------------------------------------------- search tab
with tab_search:
    q = st.text_input("Ask about an aircraft, engine, docket, AD or record",
                      key="q", placeholder="Does the Gulfstream GV-SP AD apply to S/N 5064?",
                      on_change=lambda: [st.session_state.__setitem__(k, "") for k in ("f_make", "f_model", "f_serial")])
    if q or f_make or f_model or f_serial:
        res = engine.ask(q or "", make=f_make, model=f_model, serial=f_serial)
        pq = res["query"]

        # interpreted query, so the user can see exactly what was understood
        chips = []
        for label, val in (("make", ", ".join(pq["makes"])), ("model", ", ".join(pq["models"])),
                           ("serial", pq["serial"] or ""), ("records", ", ".join(pq["record_ids"] + pq["dockets"])),
                           ("AD", ", ".join(pq["ad_numbers"])), ("topic", ", ".join(pq["topic_terms"]))):
            if val:
                chips.append(badge(f"{label}: {val}", "#334", "#eef0f4"))
        st.markdown("Understood as: " + (" ".join(chips) or "<i>nothing specific</i>") +
                    f' <span class="at-cite">· parser: {html.escape(pq["parser"])} · {res["latency_ms"]} ms</span>',
                    unsafe_allow_html=True)

        if res["status"] == "refuse":
            st.markdown(f'<div class="at-banner" style="background:#fbeaea;color:#7a1f1f"><b>🛑 Cannot answer.</b> '
                        f'{html.escape(res["message"])}<br><small>{"<br>".join(map(html.escape, res["reasons"]))}'
                        f'</small></div>', unsafe_allow_html=True)
        elif res["status"] == "clarify":
            st.markdown(f'<div class="at-banner" style="background:#fff4dc;color:#6b4500"><b>🤔 Need clarification.</b> '
                        f'{html.escape(res["message"])}</div>', unsafe_allow_html=True)
        else:
            st.markdown(f'<div class="at-banner" style="background:#eef3fb"><b>{html.escape(res["message"])}</b>'
                        + (f'<br><small>{"<br>".join(map(html.escape, res["reasons"]))}</small>' if res["reasons"] else "")
                        + "</div>", unsafe_allow_html=True)

            # follow-up questions instead of a flat refusal
            for need in res["needs"]:
                with st.container(border=True):
                    st.markdown(f"**❓ {need['question']}**  ·  affects {', '.join(need['records'][:6])}")
                    col_a, col_b = st.columns([3, 1])
                    col_a.text_input(need["field"].title(), key=f"need_{need['field']}",
                                     label_visibility="collapsed", placeholder=f"Enter {need['field']}")
                    col_b.button("Re-check", key=f"btn_{need['field']}", on_click=apply_followup,
                                 args=(need["field"],))

            if res.get("total_matches", 0) > len(res["cards"]):
                st.caption(f"Showing {len(res['cards'])} of {res['total_matches']} matching records "
                           f"(sorted: applies → conditional → need info → unverified → withdrawn → does not apply).")

            for card in res["cards"]:
                fg, bg, icon = VERDICT_STYLE.get(card["verdict"], ("#333", "#eee", "•"))
                badges = [badge(f"{icon} {card['verdict']}", fg, bg)]
                badges.append(badge("PROPOSED RULE (NPRM)", "#6b4500", "#fff4dc") if card["is_proposal"]
                              else badge(f"FINAL AD {card['ad_number']}", "#0f7b3f", "#e6f4ec"))
                if card["withdrawn"]:
                    badges.append(badge("WITHDRAWN", "#7a2e8a", "#f5e9f8"))
                rv = card["revision"]
                if rv:
                    if rv["is_latest"]:
                        badges.append(badge(f"LATEST · version {rv['version']} of {rv['total']}", "#0f7b3f", "#e6f4ec"))
                    else:
                        badges.append(badge(f"OUTDATED · v{rv['version']} of {rv['total']} · latest is "
                                            f"{rv['latest_record']} ({rv['latest_date']})", "#a12b2b", "#fbeaea"))
                if card["supersedes_ad"]:
                    badges.append(badge(f"supersedes AD {card['supersedes_ad']}", "#334", "#eef0f4"))
                if card["text_source"] == "summary_only":
                    badges.append(badge("summary only", "#5b5b66", "#eeeef2"))
                st.markdown(
                    f'<div class="at-card" style="border-left:6px solid {fg}">'
                    f'<b>{card["record_id"]}</b> · {html.escape(card["subject"] or "")}<br>'
                    f'<span class="at-cite">{html.escape(card["make"][:90])} · published {card["publish_date"]} · '
                    f'{html.escape(card["citation"])}</span><br>{"".join(badges)}</div>', unsafe_allow_html=True)

                with st.expander("Show reasoning checklist and evidence", expanded=card["verdict"] in
                                 (APPLIES, CONDITIONAL, NEED_INFO, NOT_APPLICABLE) and len(res["cards"]) <= 4):
                    for s in card["steps"]:
                        st.markdown(f"{STEP_ICON.get(s['result'], '•')} **{s['check']}** — {html.escape(s['detail'])}"
                                    + (f'<br><span class="at-cite">↳ {html.escape(s["cite"])}</span>' if s["cite"] else ""),
                                    unsafe_allow_html=True)
                        if s.get("evidence"):
                            st.markdown(f'<div class="at-quote">“{html.escape(s["evidence"][:700])}”</div>',
                                        unsafe_allow_html=True)
                    if rv:
                        st.markdown("**Revision chain** (docket " + rv["docket"] + ")")
                        st.dataframe(pd.DataFrame(rv["members"]), hide_index=True, use_container_width=True)
                    if card["fr_link"]:
                        st.markdown(f"[Open in Federal Register ↗]({card['fr_link']})")

            if res["passages"]:
                st.markdown("### Evidence (verbatim from the corpus)")
                for p in res["passages"]:
                    st.markdown(f'<div class="at-quote">“{html.escape(p["text"])}”<br>'
                                f'<span class="at-cite">— {html.escape(p["cite"])}</span></div>', unsafe_allow_html=True)

        with st.expander("Structured output (JSON)"):
            st.download_button("Download JSON", json.dumps(res, indent=2, default=str), "aerotrace_result.json",
                               "application/json")
            st.json({k: v for k, v in res.items() if k != "corpus"}, expanded=False)

# ---------------------------------------------------------------- break-it tab
with tab_break:
    st.markdown("Trap questions the system must handle without guessing. Run the suite, then try your own above.")
    if st.button("▶ Run trap-question suite", type="primary"):
        rows = trap_tests.run(engine)
        df = pd.DataFrame(rows)
        n = int(df["pass"].sum())
        a, b, c = st.columns(3)
        a.metric("Passed", f"{n}/{len(df)}")
        b.metric("Max latency", f"{df['latency_ms'].max():.0f} ms")
        c.metric("Target", "< 3000 ms")
        df["pass"] = df["pass"].map({True: "✅", False: "❌"})
        st.dataframe(df, hide_index=True, use_container_width=True)

# ---------------------------------------------------------------- about tab
with tab_about:
    st.markdown(f"""
**Corpus.** {cs['records']} FAA proposed airworthiness directives (NPRMs). {cs['full_text']} have full text indexed,
{cs['summary_only']} are summary only (run `fetch_fulltext.py` to add the rest from the Federal Register).
They cover {cs['pages_total']} Federal Register pages in total; {cs['pages_fulltext']} of those are full text.
{cs['chunks']} passages are indexed (about {cs['est_pages_indexed']} printed pages of text). {cs['withdrawn']} records are withdrawals.

**How an answer is produced**
1. **Understand the question.** Rules extract manufacturer, model, serial, record/docket/AD numbers. If Claude is
   enabled it may fill gaps, but any value it returns must appear word for word in the question.
2. **Refusal gates.** Unknown identifiers, manufacturers or models not in the corpus, contradictions (a model that
   belongs to another manufacturer) and ambiguous models stop the pipeline with a stated reason.
3. **Rule engine.** Each record's §(c) Applicability text is parsed into models, serial ranges, exclusions,
   conditions and referenced service-bulletin lists. The verdict is computed in code, with the exact sentence shown
   for every step.
4. **Evidence.** Only verbatim sentences from the corpus are shown, each with record ID, docket, Federal Register
   volume and page, and section. Claude (optional) can only choose sentence IDs. It never writes answer text.
5. **Revision logic.** Dockets with several records are shown as a chain; earlier versions are flagged OUTDATED with
   the latest record named. Withdrawn proposals and proposed-vs-final status are labelled on every card.

**What the verdicts mean**
- **APPLIES**: every check passed against the applicability text.
- **APPLIES IF CONDITION MET**: model/serial match, but the text adds a condition (e.g. "excluding airplanes on which SB X has been done").
- **CANNOT DETERMINE - NEED INFO**: a fact is missing (serial, model) or the list sits in a referenced document.
- **UNVERIFIED - SUMMARY ONLY**: the full text is not in the corpus, so exact applicability is not claimed.
- **DOES NOT APPLY**: a check failed; the failing sentence is shown.
""")
