import json, re, requests
UA = {"User-Agent": "NeverlandMuseumIntake/1.0 (open-access download; contact via GitHub msfits20)"}
def g(u, **kw): return requests.get(u, headers=UA, timeout=90, **kw)
r = g("https://search.artsmia.org/public_access:1%20image:valid%20classification:Paintings?size=1")
h = r.json()["hits"]["hits"][0]["_source"]; print("MIA keys", sorted(h.keys())[:60])
print("MIA sample", {k: h.get(k) for k in ["id", "title", "artist", "dated", "classification", "image", "image_width", "image_height", "rights_type", "public_access", "restricted"]})
for q in ["public_access:1%20image:valid%20classification:Paintings", "public_access:1%20image:valid%20(classification:Prints%20OR%20classification:Drawings)", "public_access:1%20image:valid"]:
    r = g("https://search.artsmia.org/" + q + "?size=0"); print("MIA total", q, r.json().get("hits", {}).get("total"))
for u in ["https://iiif.dx.artsmia.org/%s.jpg/info.json" % h["id"], "https://api.artsmia.org/images/%s/large.jpg" % h["id"], "https://api.artsmia.org/images/%s/full.jpg" % h["id"], "https://1.api.artsmia.org/full/%s.jpg" % h["id"]]:
    r = g(u); print("MIA img", u, r.status_code, r.headers.get("content-type"), len(r.content), r.text[:200] if "json" in (r.headers.get("content-type") or "") else "")
r = g("https://search.artsmia.org/public_access:1%20image:valid?size=2&from=20000"); print("MIA deep", r.status_code, r.text[:200])
a = g("https://data.getty.edu/museum/collection/activity-stream").json(); print("GETTY stream", json.dumps(a)[:600])
p = g(a["last"]["id"]).json() if "last" in a else {}; items = p.get("orderedItems", [])
objs = [it["object"]["id"] for it in items if it.get("object", {}).get("type") == "HumanMadeObject"][:2]
for o in objs:
    j = g(o).json(); print("GETTY obj", json.dumps({k: j.get(k) for k in ["_label", "classified_as", "representation", "subject_of", "referred_to_by"]})[:2500])
r = g("https://data.getty.edu/museum/collection/sparql", params={"query": "PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/> SELECT (COUNT(DISTINCT ?o) AS ?n) WHERE { ?o crm:P138i_has_representation ?r . }"}, headers={**UA, "Accept": "application/sparql-results+json"}); print("GETTY sparql", r.status_code, r.text[:300])
