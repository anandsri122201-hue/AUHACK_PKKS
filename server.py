"""AeroTrace web server: JSON API + custom frontend.

    python server.py            # http://localhost:8000
"""
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import trap_tests
from engine import Engine

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="AeroTrace")
engine = Engine()  # uses Claude automatically if ANTHROPIC_API_KEY is set


class Ask(BaseModel):
    q: str = ""
    make: str = ""
    model: str = ""
    serial: str = ""


app.mount("/fonts", StaticFiles(directory=ROOT / "web" / "fonts"), name="fonts")


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.post("/api/ask")
def ask(body: Ask):
    return engine.ask(body.q, make=body.make, model=body.model, serial=body.serial)


@app.get("/api/corpus")
def corpus():
    return {**engine.corpus_summary(), "llm": bool(engine.llm)}


@app.get("/api/source/{record_id}")
def source(record_id: str):
    rec = engine.records.get(record_id)
    if not rec:
        raise HTTPException(404, f"{record_id} is not in the corpus")
    rows = engine.con.execute(
        "SELECT id, section, title, page, text FROM chunks WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
    sections = []
    for r in rows:
        key = (r["section"], r["title"])
        if sections and sections[-1]["key"] == key:
            sections[-1]["paras"].append({"text": r["text"], "page": r["page"]})
        else:
            sections.append({"key": key, "section": r["section"], "title": r["title"],
                             "cite": engine.cite(rec, r["section"], r["title"], r["page"]),
                             "paras": [{"text": r["text"], "page": r["page"]}]})
    for s in sections:
        s.pop("key")
    return {"record_id": record_id, "docket": rec["docket"], "subject": rec["subject"], "make": rec["make_raw"],
            "publish_date": rec["publish_date"], "citation": engine.cite(rec), "fr_link": rec["fr_link"],
            "text_source": rec["text_source"], "sections": sections}


@app.get("/api/tests")
def tests():
    return trap_tests.run(engine)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
