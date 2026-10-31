"""Trap-question test suite. Used by the CLI and the "Try to break it" tab.

    python trap_tests.py
"""
import time

from engine import (APPLIES, CONDITIONAL, NEED_INFO, NOT_APPLICABLE, UNVERIFIED, WITHDRAWN, Engine)

# (name, question, expectation)
#   expectation keys: status, record, verdict, needs, latest_is, reason_contains, no_passages
CASES = [
    ("Serial inside range", "Bombardier DHC-8-400 serial 4050",
     {"status": "answer", "record": "NPRM-332", "verdict": APPLIES}),
    ("Serial on inclusive boundary", "Bombardier DHC-8-400 S/N 4081",
     {"status": "answer", "record": "NPRM-332", "verdict": APPLIES}),
    ("Serial just outside range", "Bombardier DHC-8-400 S/N 4082",
     {"status": "answer", "record": "NPRM-332", "verdict": NOT_APPLICABLE}),
    ("Serial missing -> ask for it", "Which ADs apply to a Bombardier DHC-8-400?",
     {"status": "answer", "record": "NPRM-332", "verdict": NEED_INFO, "needs": "serial"}),
    ("In range, but later withdrawn in same docket", "Does the Gulfstream GV-SP AD apply to S/N 5050?",
     {"status": "answer", "record": "NPRM-328", "verdict": WITHDRAWN, "step_contains": "would have applied"}),
    ("Outside range (withdrawn docket)", "Does the Gulfstream GV-SP AD apply to S/N 5064?",
     {"status": "answer", "record": "NPRM-328", "verdict": NOT_APPLICABLE}),
    ("Serial explicitly excluded", "Dassault Falcon 2000 S/N 130",
     {"status": "answer", "record": "NPRM-412", "verdict": NOT_APPLICABLE}),
    ("In range + SB condition, later withdrawn", "Dassault Falcon 2000 S/N 100",
     {"status": "answer", "record": "NPRM-412", "verdict": WITHDRAWN, "step_contains": "conditionally"}),
    ("Earlier version, latest is a withdrawal", "Robinson R22 S/N 0100",
     {"status": "answer", "record": "NPRM-336", "verdict": WITHDRAWN, "step_contains": "would have applied"}),
    ("Model match with extra condition", "Boeing 737-800",
     {"status": "answer", "record": "NPRM-354", "verdict": CONDITIONAL}),
    ("All serial numbers", "Cessna 402 wing spar",
     {"status": "answer", "record": "NPRM-402", "verdict": APPLIES}),
    ("Serial table -> manual check (docket later withdrawn)", "Piper PA-28-140 serial 28-21000",
     {"status": "answer", "record": "NPRM-241", "verdict": WITHDRAWN, "step_contains": "table"}),
    ("Earlier version -> latest flagged", "What does NPRM-302 require?",
     {"status": "answer", "record": "NPRM-302", "latest_is": "NPRM-270"}),
    ("Withdrawn proposal", "Tell me about NPRM-320",
     {"status": "answer", "record": "NPRM-320", "verdict": WITHDRAWN}),
    ("Summary-only record is not over-claimed", "Dassault Falcon 7X",
     {"status": "answer", "record": "NPRM-043", "verdict": UNVERIFIED}),
    ("Manufacturer not in corpus", "What ADs apply to the Comac C919?",
     {"status": "refuse"}),
    ("Contradiction: wrong make for model", "Boeing A330 fuel tank inspection",
     {"status": "refuse", "reason_contains": "Contradiction"}),
    ("Ambiguous model (airframe vs engine records)", "Does it apply to the A330?",
     {"status": "clarify"}),
    ("Unknown record id", "What does NPRM-999 say?",
     {"status": "refuse"}),
    ("Unknown AD number", "Is AD 1999-99-99 still in force?",
     {"status": "refuse"}),
    ("Off-topic question", "What's the weather in Singapore today?",
     {"status": "refuse"}),
    ("Fact not in corpus -> no guessing", "Gulfstream GV-SP brake torque value in inch-pounds",
     {"no_passages_with": "inch-pound"}),
]


def check(res: dict, exp: dict) -> tuple[bool, str]:
    if "status" in exp and res["status"] != exp["status"]:
        return False, f"status {res['status']} != {exp['status']} ({res['message']})"
    card = None
    if "record" in exp:
        card = next((c for c in res["cards"] if c["record_id"] == exp["record"]), None)
        if not card:
            return False, f"{exp['record']} not in results"
    if "verdict" in exp and card["verdict"] != exp["verdict"]:
        return False, f"{exp['record']} verdict {card['verdict']} != {exp['verdict']}"
    if "step_contains" in exp and not any(exp["step_contains"] in st["detail"] for st in card["steps"]):
        return False, f"no step mentions '{exp['step_contains']}'"
    if "needs" in exp and not any(n["field"] == exp["needs"] for n in res["needs"]):
        return False, f"no follow-up question for {exp['needs']}"
    if "latest_is" in exp:
        rv = card.get("revision")
        if not rv or rv["latest_record"] != exp["latest_is"] or rv["is_latest"]:
            return False, f"revision info wrong: {rv}"
    if "reason_contains" in exp and exp["reason_contains"] not in res["message"] + " ".join(res["reasons"]):
        return False, "expected reason missing"
    if "no_passages_with" in exp and any(exp["no_passages_with"] in p["text"].lower() for p in res["passages"]):
        return False, "returned a passage that does not exist in corpus"
    if card:
        key = next((st for st in card["steps"] if st["result"] in ("fail", "unknown", "warn")), None) \
            or next((st for st in reversed(card["steps"]) if st["result"] == "pass"), None)
        return True, f"{card['record_id']}: {card['verdict'].lower()}. {key['detail'] if key else ''}"
    return True, res["message"]


def run(engine: Engine | None = None) -> list[dict]:
    e = engine or Engine(use_llm=False)
    out = []
    for name, q, exp in CASES:
        t = time.perf_counter()
        res = e.ask(q)
        ms = (time.perf_counter() - t) * 1000
        ok, detail = check(res, exp)
        out.append({"test": name, "question": q, "pass": ok, "latency_ms": round(ms, 1), "detail": detail})
    return out


if __name__ == "__main__":
    rows = run()
    for r in rows:
        print(f"{'PASS' if r['pass'] else 'FAIL'}  {r['latency_ms']:7.1f} ms  {r['test']:45}  {r['detail']}")
    n = sum(r["pass"] for r in rows)
    lat = sorted(r["latency_ms"] for r in rows)
    print(f"\n{n}/{len(rows)} passed · max latency {lat[-1]:.1f} ms · median {lat[len(lat)//2]:.1f} ms")
