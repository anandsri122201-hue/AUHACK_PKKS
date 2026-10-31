"""Shared helpers: paths, make normalisation, text cleanup."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
FULLTEXT_DIR = DATA / "fulltext"
DB_PATH = DATA / "corpus.db"
INDEX_XLSX = DATA / "challenge4_ad_index.xlsx"

# Canonical manufacturer -> regex of aliases (matched case-insensitively against
# the sheet's make column, the subject line and user queries).
MAKE_ALIASES = {
    "Airbus Helicopters": r"airbus helicopters|eurocopter|\bahd\b",
    "Airbus Canada": r"airbus canada|\ba220\b",
    "Airbus": r"\bairbus\b",
    "Boeing": r"\bboeing\b",
    "McDonnell Douglas": r"mcdonnell douglas|\bmd-\d",
    "Embraer": r"embraer|empresa brasileira",
    "Bombardier": r"bombardier|canadair",
    "De Havilland": r"de havilland",
    "Cessna / Textron Aviation": r"\bcessna\b|textron aviation",
    "Bell": r"\bbell\b",
    "Gulfstream": r"gulfstream",
    "Dassault": r"dassault|falcon",
    "Sikorsky": r"sikorsky",
    "Leonardo / Agusta": r"leonardo|agusta",
    "Robinson": r"robinson",
    "MD Helicopters": r"md helicopters",
    "Piper": r"\bpiper\b",
    "Rolls-Royce": r"rolls[- ]royce",
    "Pratt & Whitney": r"pratt|\bp&w\b",
    "General Electric": r"general electric|\bge aerospace\b|\bge\b",
    "CFM International": r"\bcfm\b",
    "Honeywell": r"honeywell",
    "Safran": r"safran|turbomeca",
    "BAE Systems": r"\bbae\b|british aerospace",
    "Fokker": r"fokker",
    "Mitsubishi": r"mitsubishi",
    "Lockheed": r"lockheed",
    "Grob": r"\bgrob\b",
    "ATR": r"\batr\b|avions de transport",
    "Saab": r"\bsaab\b",
    "Pilatus": r"pilatus",
    "Hawker / Beechcraft": r"hawker|beech|raytheon",
}

# Manufacturers a user might plausibly name. If a query names one that is not
# in the corpus we refuse explicitly as out of scope.
KNOWN_MANUFACTURERS = {
    "Comac": r"\bcomac\b|\bc919\b|\barj21\b",
    "Sukhoi": r"sukhoi|superjet",
    "Tupolev": r"tupolev",
    "Ilyushin": r"ilyushin",
    "Antonov": r"antonov",
    "Cirrus": r"\bcirrus\b",
    "Diamond": r"diamond aircraft|\bda42\b|\bda40\b",
    "Tecnam": r"tecnam",
    "Daher / Socata": r"daher|socata|tbm",
    "Viking": r"viking air|twin otter",
    "HondaJet": r"honda",
    "Mooney": r"mooney",
}


def canonical_makes(text: str) -> list[str]:
    """Return canonical makes mentioned in text (ordered, unique)."""
    if not text:
        return []
    t = text.lower()
    found = []
    for canon, pat in MAKE_ALIASES.items():
        if re.search(pat, t):
            # "Airbus Helicopters"/"Airbus Canada" should not also count as plain Airbus
            if canon == "Airbus" and any(c in found for c in ("Airbus Helicopters", "Airbus Canada")) \
                    and not re.search(r"\bairbus (sas|s\.a\.s|model a3|a3\d\d)", t):
                continue
            found.append(canon)
    return found


def clean_text(s: str) -> str:
    """Undo escaped newlines/tabs that some sheet cells contain, normalise spaces."""
    if not isinstance(s, str):
        return ""
    s = s.replace("\\n", "\n").replace("\\t", "\t").replace("\r", "")
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n[ ]+", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


_SENT_SPLIT = re.compile(r"(?<=[.;:])\s+(?=(?:\(\w{1,4}\)\s*)?[A-Z(])")
_ABBREV = re.compile(r"\b(?:No|Nos|Sec|S/N|P/N|Inc|Ltd|Co|U\.S|e\.g|i\.e|approx|Fig|para)\.$")


def split_sentences(text: str) -> list[str]:
    """Conservative sentence splitter that keeps abbreviations intact."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    parts = _SENT_SPLIT.split(text)
    out = []
    for p in parts:
        if out and _ABBREV.search(out[-1]):
            out[-1] += " " + p
        else:
            out.append(p)
    return [p.strip() for p in out if p.strip()]
