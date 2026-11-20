"""Probe candidate open-access sources from GitHub's network (read-only, 20 Nov 2026)."""
import requests, json, time
S = requests.Session(); S.headers["User-Agent"] = "NeverlandMuseumIntake/1.0 (open-access download; contact via GitHub msfits20)"
def get(u, **kw):
    try:
        r = S.get(u, timeout=60, **kw); return r
    except Exception as e:
        print("  ERR", u[:120], e); return None
def head_img(u):
    r = get(u, stream=True)
    if r is None: return "ERR"
    n = 0
    for c in r.iter_content(1 << 16):
        n += len(c)
        if n > 3 << 20: break
    return "%s %s %s+ bytes" % (r.status_code, r.headers.get("Content-Type"), n)

print("== Mia (Minneapolis)")
r = get("https://search.artsmia.org/public_access:1%20AND%20classification:Paintings%20AND%20image:valid?size=3")
if r is not None:
    print(" ", r.status_code, r.text[:300].replace("\n", " "))
    try:
        j = r.json(); print("  total", j.get("hits", {}).get("total"))
        h = j["hits"]["hits"][0]["_source"]; print("  sample", h.get("id"), h.get("title"), h.get("artist"), h.get("image_width"), h.get("image_height"), h.get("rights_type"), h.get("restricted"))
        for u in ("https://api.artsmia.org/images/%s/full/full/0/default.jpg" % h["id"], "https://iiif.dx.artsmia.org/%s.jpg/info.json" % h["id"]):
            print("  img", u, head_img(u))
    except Exception as e: print("  parse", e)

print("== Library of Congress (posters)")
r = get("https://www.loc.gov/pictures/search/?q=poster&fo=json&c=3")
print(" ", r.status_code if r is not None else None, (r.text[:200] if r is not None else "").replace("\n"," "))
r = get("https://www.loc.gov/collections/works-progress-administration-posters/?fo=json&c=3")
if r is not None:
    print(" ", r.status_code)
    try:
        j = r.json(); print("  total", j.get("pagination", {}).get("of"))
        it = j["results"][0]; print("  sample", it.get("title"), it.get("rights", "")[:80] if isinstance(it.get("rights"), str) else it.get("rights"), (it.get("image_url") or [])[-1:] )
    except Exception as e: print("  parse", e)

print("== Europeana (api2demo)")
r = get("https://api.europeana.eu/record/v2/search.json", params={"wskey": "api2demo", "query": "what:painting", "qf": ["TYPE:IMAGE", "IMAGE_SIZE:extra_large", "RIGHTS:*publicdomain*"], "rows": 3, "profile": "rich", "cursor": "*"})
if r is not None:
    print(" ", r.status_code, r.text[:200].replace("\n"," "))
    try:
        j = r.json(); print("  total", j.get("totalResults"))
        it = j["items"][0]; print("  sample", it.get("title"), it.get("dcCreator"), it.get("dataProvider"), it.get("rights"), it.get("edmIsShownBy"))
        if it.get("edmIsShownBy"): print("  img", head_img(it["edmIsShownBy"][0]))
    except Exception as e: print("  parse", e)

print("== Getty")
r = get("https://data.getty.edu/museum/collection/activity-stream")
print(" ", r.status_code if r is not None else None, (r.text[:300] if r is not None else "").replace("\n"," "))

print("== Nationalmuseum Sweden")
r = get("https://api.nationalmuseum.se/api/objects?limit=2")
print(" ", r.status_code if r is not None else None, (r.text[:200] if r is not None else "").replace("\n"," "))

print("== Paris Musees (no token)")
r = get("https://apicollections.parismusees.paris.fr/graphql")
print(" ", r.status_code if r is not None else None)

print("== Harvard (no key)")
r = get("https://api.harvardartmuseums.org/object?size=1")
print(" ", r.status_code if r is not None else None, (r.text[:120] if r is not None else ""))
