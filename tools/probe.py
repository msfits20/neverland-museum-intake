"""Probe 4 (20 Nov 2026): rarer, famous sources. Read-only.
- Wikimedia Commons "Google Art Project" categories (gigapixel copies of paintings from museums that
  aren't open access: Uffizi, Hermitage, Prado-era loans, Tate, Van Gogh Museum...)
- Nationalmuseum Sweden, Princeton, Yale University Art Gallery open APIs."""
import json
import requests

S = requests.Session()
S.headers["User-Agent"] = "NeverlandMuseumIntake/1.0 (open-access download; contact via GitHub msfits20)"
API = "https://commons.wikimedia.org/w/api.php"


def cat_info(name):
    j = S.get(API, params={"action": "query", "format": "json", "prop": "categoryinfo", "titles": "Category:" + name}, timeout=60).json()
    for p in j["query"]["pages"].values():
        return p.get("categoryinfo")


for c in ["Google Art Project", "Google Art Project paintings", "Google Art Project works by collection",
          "Paintings in the Google Art Project", "Google Art Project works", "Featured pictures of paintings",
          "Quality images of paintings", "Gigapixel images"]:
    try:
        print("CAT", c, cat_info(c))
    except Exception as e:
        print("CAT", c, "ERR", e)

# a few sample files from the biggest one, with size and licence
j = S.get(API, params={"action": "query", "format": "json", "generator": "categorymembers",
                       "gcmtitle": "Category:Google Art Project works by collection", "gcmlimit": 20, "gcmtype": "subcat"}, timeout=60).json()
subs = [p["title"] for p in (j.get("query") or {}).get("pages", {}).values()]
print("SUBCATS sample", len(subs), subs[:20])
if subs:
    j = S.get(API, params={"action": "query", "format": "json", "generator": "categorymembers", "gcmtitle": subs[0],
                           "gcmlimit": 5, "gcmtype": "file", "prop": "imageinfo", "iiprop": "size|mime|extmetadata"}, timeout=60).json()
    for p in (j.get("query") or {}).get("pages", {}).values():
        ii = p["imageinfo"][0]
        md = ii.get("extmetadata", {})
        print(" ", p["title"][:80], ii["width"], ii["height"], ii["mime"], (md.get("LicenseShortName") or {}).get("value"),
              ((md.get("Artist") or {}).get("value") or "")[:60])

for name, url, params in [
    ("nationalmuseum", "https://api.nationalmuseum.se/api/objects", {"limit": 1}),
    ("princeton", "https://data.artmuseum.princeton.edu/search/objects", {"q": "painting", "size": 1}),
    ("yuag-lux", "https://lux.collections.yale.edu/api/search/item", {"q": json.dumps({"text": "painting"}), "page": 1, "pageLength": 1}),
]:
    try:
        r = S.get(url, params=params, timeout=60)
        print(name, r.status_code, r.headers.get("content-type"), r.text[:400].replace("\n", " "))
    except Exception as e:
        print(name, "ERR", e)
