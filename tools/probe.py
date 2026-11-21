"""Probe 8 (21 Nov 2026): can GitHub reach the next sources, and how big are their images?
Read-only. Prints one line per check."""
import json, requests

UA = {"User-Agent": "NeverlandMuseumIntake/1.0 (open-access download; contact via GitHub msfits20)",
      "AIC-User-Agent": "NeverlandMuseumIntake/1.0 (contact via GitHub msfits20)"}


def get(url, **kw):
    try:
        r = requests.get(url, headers=UA, timeout=60, **kw)
        return r.status_code, r
    except Exception as e:
        return "ERR " + type(e).__name__, None


def show(name, url, **kw):
    code, r = get(url, **kw)
    body = (r.text[:300].replace("\n", " ") if r is not None else "")
    print("%-28s %s %s" % (name, code, body if code != 200 else ""))
    return r if code == 200 else None


# Art Institute of Chicago (dropped 18 Nov: blocked GitHub)
r = show("aic search", "https://api.artic.edu/api/v1/artworks/search", params={
    "query[bool][must][0][term][is_public_domain]": "true",
    "query[bool][must][1][term][artwork_type_title.keyword]": "Painting",
    "fields": "id,title,image_id,thumbnail", "limit": 3})
if r:
    j = r.json()
    print("aic PD paintings total", j.get("pagination", {}).get("total"))
    for it in j.get("data", []):
        if it.get("image_id"):
            base = "https://www.artic.edu/iiif/2/" + it["image_id"]
            c, inf = get(base + "/info.json")
            print("  aic info.json", c, (inf.json().get("width"), inf.json().get("height")) if c == 200 else "")
            for path in ("/full/843,/0/default.jpg", "/full/pct:100/0/default.jpg", "/0,0,3000,3000/3000,/0/default.jpg"):
                c2, im = get(base + path)
                print("  aic", path, c2, len(im.content) if c2 == 200 else "")
            break

# Minneapolis Institute of Art
r = show("mia search", "https://search.artsmia.org/rights_type:%22Public%20Domain%22%20classification:Paintings", params={"size": 3})
if r:
    try:
        j = r.json(); hits = j.get("hits", {})
        print("mia PD paintings total", hits.get("total"))
        for h in hits.get("hits", [])[:2]:
            s = h.get("_source", {})
            print("  mia", s.get("id"), s.get("title"), s.get("image"), s.get("image_width"), s.get("image_height"), s.get("rights_type"))
    except ValueError:
        print("mia not json", r.text[:200])
show("mia iiif sample", "https://iiif.dx.artsmia.org/1218.jpg/info.json")

# Getty (Linked Art SPARQL)
show("getty sparql", "https://data.getty.edu/museum/collection/sparql", params={"query": "SELECT (COUNT(*) AS ?n) WHERE { ?s a <http://www.cidoc-crm.org/cidoc-crm/E22_Human-Made_Object> } LIMIT 1"}, )

# Library of Congress
r = show("loc wpa posters", "https://www.loc.gov/collections/works-progress-administration-posters/", params={"fo": "json", "c": 2})
if r:
    j = r.json()
    print("loc wpa total", j.get("pagination", {}).get("of"))
    for it in j.get("results", [])[:1]:
        print("  loc", it.get("title"), (it.get("image_url") or [])[-1:])

# Slovak National Gallery (Web umenia)
show("webumenia api", "https://www.webumenia.sk/api/v1/items", params={"size": 2, "filter[is_free]": 1})
show("webumenia site", "https://www.webumenia.sk/en/katalog?is_free=1")
