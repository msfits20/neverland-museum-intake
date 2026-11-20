"""Probe 2 (20 Nov 2026): Getty SPARQL + Library of Congress item detail."""
import requests, json, time
S = requests.Session(); S.headers["User-Agent"] = "NeverlandMuseumIntake/1.0 (open-access download; contact via GitHub msfits20)"
def show(r, n=400):
    print("  ", r.status_code, r.headers.get("Content-Type"), r.text[:n].replace("\n", " "))
print("== Getty SPARQL")
q = """PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>
PREFIX la: <https://linked.art/ns/terms/>
SELECT ?obj ?type WHERE { ?obj a crm:E22_Human-Made_Object ; crm:P2_has_type ?type . } LIMIT 3"""
try:
    r = S.get("https://data.getty.edu/museum/collection/sparql", params={"query": q}, headers={"Accept": "application/sparql-results+json"}, timeout=90); show(r)
except Exception as e: print("  ERR", e)
print("== Getty object sample via activity stream page 1")
try:
    r = S.get("https://data.getty.edu/museum/collection/activity-stream/page/1", timeout=60); j = r.json()
    its = j.get("orderedItems") or []
    print("  items", len(its), its[0]["object"]["id"] if its else None)
    for it in its[:30]:
        o = it.get("object", {})
        if o.get("type") == "HumanMadeObject":
            r2 = S.get(o["id"], timeout=60); jo = r2.json()
            print("  obj", jo.get("_label"), [c.get("_label") for c in jo.get("classified_as", [])][:4])
            print("   rep", json.dumps(jo.get("representation", [])[:1])[:300])
            print("   subj_to", json.dumps(jo.get("subject_of", [])[:2])[:300])
            break
except Exception as e: print("  ERR", e)
print("== Getty last page (newest) + totals")
try:
    r = S.get("https://data.getty.edu/museum/collection/activity-stream", timeout=60); j = r.json(); print("  last", j.get("last"))
except Exception as e: print("  ERR", e)
print("== LoC collection page + item")
for slug in ["works-progress-administration-posters", "japanese-fine-prints-pre-1915", "posters-world-war-i-posters"]:
    try:
        r = S.get("https://www.loc.gov/collections/%s/?fo=json&c=2" % slug, timeout=60)
        if r.status_code != 200: print("  ", slug, r.status_code); time.sleep(5); continue
        j = r.json(); print("  ", slug, "total", j.get("pagination", {}).get("of"))
        it = j["results"][0]; print("   ", it.get("id"), it.get("title")[:60], (it.get("image_url") or [])[-1:])
        time.sleep(4)
        r2 = S.get(it["id"] + "?fo=json", timeout=60); ji = r2.json().get("item", {})
        print("    rights:", str(ji.get("rights_advisory") or ji.get("rights"))[:150])
        res = r2.json().get("resources") or []
        if res:
            fl = res[0].get("files") or []
            big = [f for grp in fl for f in grp if f.get("mimetype") == "image/jpeg"]
            big.sort(key=lambda f: (f.get("width") or 0) * (f.get("height") or 0))
            print("    largest jpeg:", big[-1] if big else None)
            ti = [f for grp in fl for f in grp if "iiif" in str(f.get("url",""))][:1]
            print("    iiif:", ti)
        time.sleep(4)
    except Exception as e: print("  ERR", slug, e)
