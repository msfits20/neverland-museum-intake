"""Probe 9 (21 Nov 2026): AIC IIIF size limits; Mia image download URL. Read-only."""
import requests

UA = {"User-Agent": "NeverlandMuseumIntake/1.0 (open-access download; contact via GitHub msfits20)",
      "AIC-User-Agent": "NeverlandMuseumIntake/1.0 (contact via GitHub msfits20)"}


def get(url, **kw):
    try:
        r = requests.get(url, headers=UA, timeout=90, **kw)
        return r.status_code, r
    except Exception as e:
        return "ERR " + type(e).__name__, None


r = requests.get("https://api.artic.edu/api/v1/artworks/search", headers=UA, timeout=60, params={
    "query[bool][must][0][term][is_public_domain]": "true",
    "query[bool][must][1][term][artwork_type_title.keyword]": "Painting",
    "fields": "id,title,image_id", "limit": 1}).json()
iid = r["data"][0]["image_id"]
base = "https://www.artic.edu/iiif/2/" + iid
c, inf = get(base + "/info.json")
j = inf.json()
print("aic info", j.get("width"), j.get("height"), "maxWidth", j.get("maxWidth"), "maxArea", j.get("maxArea"),
      "sizes", j.get("sizes", [])[-3:], "tiles", j.get("tiles"), "profile", str(j.get("profile"))[:300])
for path in ("/full/1686,/0/default.jpg", "/full/2000,/0/default.jpg", "/full/3000,/0/default.jpg",
             "/0,0,1024,1024/1024,/0/default.jpg", "/0,0,2048,2048/1024,/0/default.jpg",
             "/0,0,1686,1686/1686,/0/default.jpg", "/0,0,2000,2000/2000,/0/default.jpg"):
    c2, im = get(base + path)
    print("  aic", path, c2, len(im.content) if c2 == 200 else (im.text[:120] if im is not None else ""))

r = requests.get("https://search.artsmia.org/rights_type:%22Public%20Domain%22", headers=UA, timeout=60, params={"size": 1}).json()
print("mia PD all total", r.get("hits", {}).get("total"))
s = r["hits"]["hits"][0]["_source"]
print("mia keys", sorted(s.keys())[:80])
oid = s["id"]
for u in ("https://api.artsmia.org/images/%s/large.jpg", "https://api.artsmia.org/images/%s/full.jpg",
          "https://images.artsmia.org/%s.jpg", "https://1.api.artsmia.org/%s.jpg", "https://iiif.dx.artsmia.org/%s.jpg/info.json",
          "https://collections.artsmia.org/art/%s", "https://api.artsmia.org/objects/%s/full/json"):
    c3, im = get(u % oid)
    print("  mia", u % oid, c3, (len(im.content), im.headers.get("Content-Type")) if c3 == 200 else "")
