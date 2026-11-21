"""Probe 12 (21 Nov 2026): data formats of the standby sources (LoC, Getty, Web umenia, Walters). Read-only."""
import json, requests

UA = {"User-Agent": "NeverlandMuseumIntake/1.0 (open-access download; contact via GitHub msfits20)"}


def get(url, **kw):
    try:
        h = kw.pop("headers", UA)
        r = requests.get(url, headers=h, timeout=90, **kw)
        return r.status_code, r
    except Exception as e:
        return "ERR " + type(e).__name__, None


print("=== LoC")
for coll in ("works-progress-administration-posters", "fine-prints-japanese-pre-1915", "artist-posters",
             "cabinet-of-american-illustration", "popular-graphic-arts", "panoramic-maps", "free-to-use"):
    c, r = get("https://www.loc.gov/collections/%s/" % coll, params={"fo": "json", "c": 1, "at": "pagination,results"})
    if c != 200:
        print(coll, c); continue
    j = r.json()
    res = (j.get("results") or [{}])[0]
    print(coll, "total", j.get("pagination", {}).get("of"), "| keys", sorted(res.keys())[:40])
    print("   sample", res.get("id"), res.get("title"), res.get("date"), res.get("image_url"))
c, r = get("https://www.loc.gov/collections/works-progress-administration-posters/", params={"fo": "json", "c": 1})
iid = r.json()["results"][0]["id"]
c, r = get(iid, params={"fo": "json", "at": "item,resources"})
if c == 200:
    j = r.json()
    it = j.get("item", {})
    print("item keys", sorted(it.keys())[:60])
    print("rights", it.get("rights_advisory"), "| contributor", it.get("contributor_names") or it.get("creator"), "| date", it.get("date"))
    res = (j.get("resources") or [{}])[0]
    print("resource keys", sorted(res.keys()))
    files = res.get("files") or []
    for f in (files[0] if files else [])[:12]:
        print("   file", f.get("mimetype"), f.get("width"), f.get("height"), f.get("url"))

print("=== Getty")
q = """PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>
SELECT ?o WHERE { ?o a crm:E22_Human-Made_Object ; crm:P2_has_type <http://vocab.getty.edu/aat/300033618> . } LIMIT 3"""
c, r = get("https://data.getty.edu/museum/collection/sparql", params={"query": q, "format": "json"},
           headers={**UA, "Accept": "application/sparql-results+json"})
print("sparql paintings", c, r.text[:600] if r is not None else "")

print("=== Web umenia")
for u in ("https://www.webumenia.sk/api/items?size=1", "https://www.webumenia.sk/api/v2/items?size=1",
          "https://www.webumenia.sk/api/v1/items?size=1&filter[is_free]=true", "https://www.webumenia.sk/oai-pmh?verb=Identify"):
    c, r = get(u, headers={**UA, "Accept": "application/json"})
    print(u, c, (r.text[:250].replace("\n", " ") if r is not None else ""))

print("=== Walters")
c, r = get("https://api.github.com/repos/WaltersArtMuseum/api-thewalters-org/contents")
print("walters repo", c, (r.text[:800].replace("\n", " ") if r is not None else ""))

print("=== src_loc sample (standby provider)")
import itertools, sys
sys.path.insert(0, ".")
import intake
for it in itertools.islice(intake.src_loc(), 3):
    c, r = get(it["url"])
    print(it["id"], it["title"][:60], "|", it["artist"], "|", c, len(r.content) if c == 200 else "")
