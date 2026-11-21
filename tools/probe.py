"""Probe 10 (21 Nov 2026): Mia full-size image URL from search fields. Read-only."""
import requests

UA = {"User-Agent": "NeverlandMuseumIntake/1.0 (open-access download; contact via GitHub msfits20)"}
for cls in ("Paintings", "Drawings", "Prints"):
    r = requests.get('https://search.artsmia.org/rights_type:"Public Domain" AND classification:%s' % cls,
                     headers=UA, timeout=60, params={"size": 3, "from": 0}).json()
    print(cls, "total", r.get("hits", {}).get("total"))
    for h in r["hits"]["hits"][:3]:
        s = h["_source"]
        print("  ", s.get("id"), "|", s.get("title"), "|", s.get("artist"), "|", s.get("dated"), "|", s.get("image"),
              s.get("image_width"), s.get("image_height"), "|", s.get("Cache_Location"), s.get("Primary_RenditionNumber"),
              "|", s.get("Rights_Image_Display"), s.get("rights_type"), s.get("public_access"))
        if s.get("Cache_Location") and s.get("Primary_RenditionNumber"):
            u = "https://img.artsmia.org/web_objects_cache/%s/mia_%s_full.jpg" % (str(s["Cache_Location"]).strip("/"), s["Primary_RenditionNumber"])
            try:
                g = requests.get(u, headers=UA, timeout=120)
                print("     ", u, g.status_code, len(g.content), g.headers.get("Content-Type"))
            except Exception as e:
                print("     ", u, "ERR", type(e).__name__)
