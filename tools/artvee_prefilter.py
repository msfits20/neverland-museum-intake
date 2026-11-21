"""Artvee prefilter (21 Nov 2026, Tobi: "milk TF outta artvee"; paintings first).

The browser signer signs ~390 Artvee links an hour, and about 3 in 4 turned out to be works the catalog
already has (skipped after downloading). This marks those queue rows 'known' BEFORE they are signed, and
gives the rest a priority so the signer takes the most sellable first (artvee_todo / artvee_todo_b order by
prio desc):
  3  Artvee works we know we don't have (artvee_new_titles / missing_artvee_titles lists)
  2  paintings and other sellable subjects (title words)
  1  everything else
  0  numbered scientific plates ("1. Chaetodon …", "Pl. 12", "Fig. 3")
Never marks a work 'known' on a weak match: needs artist+title keys, or a title of 5+ words found in the
catalog (5+ words, single version). Run: Actions -> "Artvee prefilter" (also daily). Safe to re-run.
"""
import gzip, hashlib, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import intake
import catalog as catalog_mod

CFG = json.loads(os.environ["NEVERLAND_READ"])
DRY = os.environ.get("DRY_RUN") == "1"
SELLABLE = re.compile(r"\b(portrait|landscape|still life|seascape|view of|madonna|virgin|venus|nymph|flowers|bouquet|"
                      r"garden|river|harbou?r|sunset|moonlight|winter|snow|forest|woman|women|girl|lady|bather|nude|"
                      r"village|street|canal|venice|paris|lake|mountains?|sea|ships?|boats?|cottage|evening|morning|"
                      r"interior|cafe|dance|dancer|horse|cat|dog|bird|birds|tiger|lion|poster|vase|fruit)\b")
PLATE = re.compile(r"^(\d+\s|\d+[a-z]?\s|pl\s?\d|plate\s?\d|fig\s?\d|tab\s?\d|no\s?\d)|\b(pl|plate|fig|tab)\s?\d+\b")


def title_of(row):
    t = row.get("title") or re.sub(r"-\d+$", "", row["slug"]).replace("-", " ")
    return re.sub(r"\s*\(\d{3,4}(?:[-–]\d{2,4})?\)\s*$", "", t)


def th(t):
    return hashlib.sha1(t.encode()).hexdigest()[:12]


def main():
    def load(name):
        with gzip.open(name, "rt") as fh:
            return set(fh.read().split())
    known_keys = load("known_keys.txt.gz")
    not_owned = load("artvee_new_titles.txt.gz") | load("missing_artvee_titles.txt.gz")
    rows = catalog_mod.index_rows(CFG)
    cat_keys, cat_titles = set(), set()
    for r in rows:
        artist = (r.get("path") or "").split("/")[0]
        name = r.get("name") or ""
        cat_keys.update(intake.keys(artist, name))
        t = intake.norm(name)
        if len(t.split()) >= 5:
            cat_titles.add(t)
    print("catalog rows %d, keys %d, long titles %d, known_keys %d, not-owned list %d"
          % (len(rows), len(cat_keys), len(cat_titles), len(known_keys), len(not_owned)))

    after, stats, batch = "", {"seen": 0, "known": 0, "p3": 0, "p2": 0, "p1": 0, "p0": 0}, []

    def flush():
        if batch and not DRY:
            catalog_mod._rpc(CFG, "artvee_mark", p_rows=batch)
        batch.clear()

    while True:
        page = catalog_mod._rpc(CFG, "artvee_new_list", p_after=after, p_limit=5000) or []
        if not page:
            break
        after = page[-1]["slug"]
        for row in page:
            stats["seen"] += 1
            title = title_of(row)
            t = intake.norm(title)
            artist = row.get("artist") or ""
            if th(t) in not_owned:
                prio, known = 3, False
            else:
                known = bool(artist) and any(k in known_keys or k in cat_keys for k in intake.keys(artist, title))
                # title alone only for 5+ word titles Artvee has one version of (no "-8" slug suffix):
                # "The Resurrection of Christ" exists by dozens of artists
                known = known or (not re.search(r"-\d+$", row["slug"]) and len(t.split()) >= 5 and t in cat_titles)
                prio = 0 if PLATE.search(t) else 2 if SELLABLE.search(t) else 1
            if known:
                stats["known"] += 1
            else:
                stats["p%d" % prio] += 1
            batch.append({"slug": row["slug"], "known": known, "prio": prio})
            if len(batch) >= 1000:
                flush()
        if stats["seen"] % 20000 < 5000:
            print(json.dumps(stats))
    flush()
    print("DONE" + (" (dry run)" if DRY else ""), json.dumps(stats))


if __name__ == "__main__":
    main()
