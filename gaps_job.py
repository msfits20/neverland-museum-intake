"""
gaps_job.py - find the famous artworks Neverland doesn't sell yet (Tobi, 19 Nov 2026)
=====================================================================================
"Keep sourcing for famous artworks we don't have. Have something set up to identify our lapses,
top artists, etc. and find the best. Might already be in the intake folder."

Once a week:
  1. FAMOUS: every painting, print, drawing and fresco Wikidata knows with >= MIN_LINKS Wikipedia/Wikimedia
     pages ("sitelinks": the Mona Lisa has ~150), by an artist who died by PD_DEATH_YEAR, made before
     PD_MADE_BEFORE. Statues, buildings and books are not wall art and are never asked for.
  2. TOP ARTISTS: the catalog's best-known artists (artist_signals), and their works with >= ARTIST_MIN_LINKS
     sitelinks, so big names are covered beyond their one or two icons.
  3. Each candidate is checked against the catalog (same title keys + artist surname as the fame check, plus
     "the catalog title contains every word of the famous title", so 'Night Watch Militia Company...' counts
     as The Night Watch). Anything we already sell is skipped.
  4. Each gap is checked against the intake folder (files downloaded but not listed yet): same artist, the
     title, the year within 3, never an engraving/copy/study after it. A match is tagged so the brain lists it
     first - no download needed.
  5. Everything else goes on the wanted list (intake_wanted), most famous first. The re-sourcing job then
     searches every source for them, MODE=wanted, a batch every 6 hours.

Read-only towards Wikidata (its public query service). Writes only intake_wanted and the wanted tag on
intake_queue rows. Run: python gaps_job.py   (DRY_RUN=1 reports without writing)
"""
import json
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict

import requests

import intake
import catalog as catalog_mod
import resource_job as R

CFG = json.loads(os.environ["NEVERLAND_READ"])
DRY = os.environ.get("DRY_RUN") == "1"
MIN_LINKS = int(os.environ.get("MIN_LINKS", "15"))
ARTIST_MIN_LINKS = int(os.environ.get("ARTIST_MIN_LINKS", "6"))
TOP_ARTISTS = int(os.environ.get("TOP_ARTISTS", "150"))
PD_DEATH_YEAR = int(os.environ.get("PD_DEATH_YEAR", str(time.gmtime().tm_year - 71)))   # 70 years after death
PD_MADE_BEFORE = int(os.environ.get("PD_MADE_BEFORE", "1930"))                          # US: before 1930
MAX_NEW = int(os.environ.get("MAX_NEW", "400"))
SPARQL = "https://query.wikidata.org/sparql"
UA = "NeverlandPrintsBot/1.0 (https://neverlandprints.com; hello@neverlandprints.com)"
TYPES = {"painting": "Q3305213", "print": "Q11060274", "drawing": "Q93184", "fresco": "Q134194"}
OTHER_LANGS = ["fr", "es", "it", "de", "nl", "pt"]
NOT_WALL_ART = re.compile(r"\b(series|cycle|black paintings|ceiling|chapel|room|altarpieces|frescoes|murals|"
                          r"tapestr\w*|sculpture|statue|monument|cathedral series)\b", re.I)
log = R.log


# ---------------------------------------------------------- same keys as the brain's fame check (pop-fame.js) --
def pnorm(t):
    s = unicodedata.normalize("NFD", str(t or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\b(c|ca|circa)\b\.?", " ", s)
    s = re.sub(r"[0-9]+s?", " ", s)
    s = re.sub(r"[^a-z ]+", " ", s)
    s = re.sub(r"\b(the|a|an|le|la|les|de|il|lo|el)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def surname_of(creator):
    words = re.sub(r"\b(elder|younger|workshop|school|circle|follower|after)\b", " ", pnorm(creator)).split()
    w = words[-1] if words else ""
    return w if len(w) >= 3 else ""


# ---------------------------------------------------------------------------------------------- Wikidata --
def sparql(q):
    for attempt in range(1, 4):
        try:
            r = requests.post(SPARQL, data={"query": q}, timeout=300,
                              headers={"Accept": "application/sparql-results+json", "User-Agent": UA})
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(20 * attempt)
                continue
            r.raise_for_status()
            return r.json()["results"]["bindings"]
        except requests.RequestException as ex:
            log("  Wikidata: %s (try %d)" % (str(ex)[:120], attempt))
            time.sleep(15 * attempt)
    return []


def qid(uri):
    return uri.rsplit("/", 1)[-1]


def year(v):
    m = re.match(r"^[+-]?(\d{1,4})", v or "")
    return int(m.group(1)) * (-1 if (v or "").startswith("-") else 1) if m else None


def add_rows(works, rows, reason):
    n = 0
    for r in rows:
        q, label, creator = qid(r["item"]["value"]), r.get("itemLabel", {}).get("value", ""), r.get("creatorLabel", {}).get("value", "")
        if q in works or re.match(r"^Q\d+$", label) or re.match(r"^Q\d+$", creator) or "genid" in creator or not creator:
            continue
        made, died = year(r.get("inception", {}).get("value")), year(r.get("death", {}).get("value"))
        if died is None or died > PD_DEATH_YEAR:
            continue
        if made is not None and made >= PD_MADE_BEFORE:
            continue
        if made is None and died >= PD_MADE_BEFORE:
            continue
        if len(pnorm(label)) < 3 or NOT_WALL_ART.search(label):
            continue
        works[q] = {"qid": q, "label": label, "creator": creator, "surname": surname_of(creator), "year": made,
                    "links": int(r["links"]["value"]), "reason": reason, "keys": {pnorm(label)}, "names": {label}}
        n += 1
    return n


def famous_works():
    works = {}
    for name, t in TYPES.items():
        rows = sparql("""SELECT ?item ?itemLabel ?creatorLabel ?inception ?death ?links WHERE {
          ?item wdt:P31/wdt:P279? wd:%s; wikibase:sitelinks ?links; wdt:P170 ?creator.
          FILTER(?links >= %d) ?creator wdt:P570 ?death.
          OPTIONAL { ?item wdt:P571 ?inception. }
          SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } }""" % (t, MIN_LINKS))
        log("  famous %ss: %d new" % (name, add_rows(works, rows, "famous gap")))
        time.sleep(3)
    return works


def top_artist_works(works, artists):
    names = [a for a in artists if a and not re.search(r"[\"\\]", a)]
    for i in range(0, len(names), 25):
        values = " ".join('"%s"@en' % n for n in names[i:i + 25])
        rows = sparql("""SELECT ?item ?itemLabel ?creatorLabel ?inception ?death ?links WHERE {
          VALUES ?name { %s } ?creator rdfs:label ?name; wdt:P570 ?death.
          ?item wdt:P170 ?creator; wdt:P31/wdt:P279? ?type; wikibase:sitelinks ?links.
          VALUES ?type { wd:Q3305213 wd:Q11060274 wd:Q93184 wd:Q134194 }
          FILTER(?links >= %d)
          OPTIONAL { ?item wdt:P571 ?inception. }
          SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } }""" % (values, ARTIST_MIN_LINKS))
        add_rows(works, rows, "top artist gap")
        time.sleep(2)
    log("  with top artists' works: %d candidates" % len(works))


def other_names(works):
    ids = list(works)
    for i in range(0, len(ids), 250):
        values = " ".join("wd:" + q for q in ids[i:i + 250])
        rows = sparql("""SELECT ?item ?name WHERE { VALUES ?item { %s }
          { ?item skos:altLabel ?name FILTER(lang(?name) = "en") } UNION
          { ?item rdfs:label ?name FILTER(lang(?name) IN (%s)) } }""" % (values, ",".join('"%s"' % l for l in OTHER_LANGS)))
        for r in rows:
            w = works.get(qid(r["item"]["value"]))
            k = pnorm(r["name"]["value"])
            if w is not None and len(k) >= 3:
                w["keys"].add(k)
                w["names"].add(r["name"]["value"])
        time.sleep(1.5)


# ------------------------------------------------------------------------------------- what we already have --
def pages(fn, key):
    after, out = None, []
    while True:
        rows = catalog_mod._rpc(CFG, fn, p_after=after, p_limit=1000) or []   # the API caps a reply at 1,000 rows
        out += rows
        if len(rows) < 1000:
            return out
        after = rows[-1][key]


def have_index():
    """surname token -> list of catalog title-key word sets"""
    idx = defaultdict(list)
    for r in pages("intake_catalog_keys_page", "asset_id"):
        an = (r.get("an") or "").split()
        keys = [k for k in (r.get("k1"), r.get("k2")) if k]
        for tok in set(an):
            if len(tok) >= 3:
                for k in keys:
                    idx[tok].append((k, frozenset(k.split())))
    return idx


def already_have(w, idx):
    cands = idx.get(w["surname"]) or []
    for key in w["keys"]:
        kw = frozenset(key.split())
        for k, kws in cands:
            if key == k or (len(kw) >= 2 and kw <= kws) or (len(kw) == 1 and key == k):
                return True
    return False


def intake_index():
    idx = defaultdict(list)
    for r in pages("intake_queue_new_page", "drive_file_id"):
        for tok in set(R.name_tokens(r.get("artist") or "")):
            if len(tok) >= 3:
                idx[tok].append(r)
    return idx


def in_intake(w, idx):
    best = None
    for r in idx.get(w["surname"]) or []:
        text = " ".join([r.get("title") or "", r.get("artist") or ""])
        if R.NOT_ORIGINAL.search(text):
            continue
        ts = max(R.title_score(n, r.get("title") or "") for n in w["names"])
        if ts < R.STRICT_TITLE_MIN:
            continue
        ry = R.year_of(r.get("title"))
        if w["year"] is not None and ry:
            if abs(int(ry) - w["year"]) > R.WANTED_YEAR_TOL:
                continue
        elif ts < 1.0:
            continue
        size = max(r.get("width_px") or 0, r.get("height_px") or 0)
        if best is None or size > best[0]:
            best = (size, r)
    return best[1] if best else None


def artist_namer():
    """Wikidata's name -> the catalog's own name for that artist ('Rembrandt' -> 'Rembrandt van Rijn'),
    so a new piece joins the artist's existing page. Unknown artists keep Wikidata's name."""
    by_tok = defaultdict(list)
    for r in catalog_mod.rpc_offset_all(CFG, "intake_catalog_artists"):
        toks = R.name_tokens(r["artist"])
        if toks:
            by_tok[toks[-1]].append((int(r["n"]), r["artist"]))
            by_tok[toks[0]].append((int(r["n"]), r["artist"]))

    def name(creator):
        toks = R.name_tokens(creator)
        if not toks:
            return creator
        hits = {a: n for t in set(toks) for n, a in by_tok.get(t, []) if R.artist_ok(a, creator, [])}
        hits.update({a: n for t in set(toks) for n, a in by_tok.get(t, []) if R.artist_ok(creator, a, [])})
        return max(hits, key=hits.get) if hits else creator
    return name


# ------------------------------------------------------------------------------------------------------ run --
def main():
    if os.environ.get("PAUSED") == "1":
        log("Paused (repository variable PAUSED=1).")
        return
    log("Gap finder: famous works >= %d sitelinks + top %d artists' works >= %d; artist died by %d, made before %d"
        % (MIN_LINKS, TOP_ARTISTS, ARTIST_MIN_LINKS, PD_DEATH_YEAR, PD_MADE_BEFORE))
    works = famous_works()
    artists = [a["artist"] for a in (catalog_mod._rpc(CFG, "intake_top_artists", p_limit=TOP_ARTISTS) or [])]
    top_artist_works(works, artists)
    if len(works) < 300:
        raise SystemExit("only %d candidates came back from Wikidata - not trusting this run" % len(works))
    other_names(works)

    log("loading catalog keys ...")
    have = have_index()
    log("loading intake folder ...")
    queue = intake_index()
    namer = artist_namer()

    gaps = [w for w in works.values() if w["surname"] and not already_have(w, have)]
    gaps.sort(key=lambda w: -(w["links"] + (5 if w["reason"] == "top artist gap" else 0)))
    log("candidates %d | already sold %d | gaps %d" % (len(works), len(works) - len(gaps), len(gaps)))

    rows, staged = [], {}
    for w in gaps[:MAX_NEW]:
        hit = in_intake(w, queue)
        rows.append({"title": w["label"], "artist": namer(w["creator"]), "alt_artist": w["creator"], "qid": w["qid"], "links": w["links"],
                     "year": str(w["year"]) if w["year"] is not None else None, "reason": w["reason"],
                     "priority": w["links"] + (5 if w["reason"] == "top artist gap" else 0),
                     "notes": "Wikidata %s, %d sitelinks" % (w["qid"], w["links"])})
        if hit:
            staged[w["qid"]] = hit
    summary = ["## Gap finder", "", "Candidates: %d, already sold: %d, gaps: %d (added up to %d this run), "
               "already in the intake folder: %d" % (len(works), len(works) - len(gaps), len(gaps), MAX_NEW, len(staged)), ""]
    summary += ["- %s - %s (%s sitelinks)%s" % (r["artist"], r["title"], r["links"],
                                                " - in the intake folder" if r["qid"] in staged else "") for r in rows[:60]]
    if DRY:
        log("\n".join(summary))
        return
    added = []
    for i in range(0, len(rows), 100):
        added += catalog_mod._rpc(CFG, "intake_wanted_insert", p_rows=rows[i:i + 100]) or []
    linked = 0
    for a in added:
        hit = staged.get(a["qid"])
        if hit and catalog_mod._rpc(CFG, "intake_wanted_link", p_id=a["id"], p_drive_file_id=hit["drive_file_id"]):
            linked += 1
    msg = "GAPS DONE: %d new on the wanted list, %d of them already in the intake folder (listed next hour)" % (len(added), linked)
    log(msg)
    summary.insert(1, msg)
    with open(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a") as fh:
        fh.write("\n".join(summary) + "\n")


if __name__ == "__main__":
    main()
