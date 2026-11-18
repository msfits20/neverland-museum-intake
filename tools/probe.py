import json, re, requests
UA = {"User-Agent": "NeverlandPrintsIntake/1.0 (hello@neverlandprints.com)"}
def g(u, **kw): return requests.get(u, headers=UA, timeout=120, **kw)
for host in ["https://www.kansallisgalleria.fi", "https://kansallisgalleria.fi", "https://media.kansallisgalleria.fi", "https://www.kansallisgalleria.fi/api", "https://ka-media.kansallisgalleria.fi", "https://d3uvo7vkyyb63c.cloudfront.net"]:
    for p in ["/media-assets/1/jpg/4000/276654.jpg", "/media-assets/1/jpg/1000/276654.jpg"]:
        try:
            r = g(host + p); print("FNGM", host + p, r.status_code, r.headers.get("content-type"), len(r.content))
        except Exception as e: print("FNGM", host + p, "ERR", str(e)[:80])
r = g("https://www.kansallisgalleria.fi/fi/object/484636"); print("FNGPAGE", r.status_code, sorted(set(re.findall(r"https?://[^\"' ]*media[^\"' ]*", r.text)))[:8])
r = g("https://www.kansallisgalleria.fi/api/v1/objects"); j = r.json()
cats = {}
for o in j:
    c = (o.get("category") or {}).get("en"); cats[c] = cats.get(c, 0) + 1
print("FNGCATS", sorted(cats.items(), key=lambda x: -x[1])[:30])
cc0 = [o for o in j if any((m.get("license") == "CC0") for m in (o.get("multimedia") or []))]
print("FNGCC0", len(cc0))
big = [o for o in cc0 if any(max(m.get("width") or 0, m.get("height") or 0) >= 2400 for m in o.get("multimedia") or [])]
print("FNGBIG", len(big)); print("FNGSAMPLE", json.dumps({k: big[5].get(k) for k in ["objectId", "title", "category", "yearFrom", "yearTo", "people", "classifications"]}, ensure_ascii=False)[:1200] if len(big) > 5 else "")
m = g("https://manifests.collections.yale.edu/v2/ycba/obj/7").json()
print("YCBAMAN", json.dumps({k: m.get(k) for k in ["label", "license", "attribution", "metadata"]})[:1500])
print("YCBASVC", json.dumps(m["sequences"][0]["canvases"][0]["images"][0]["resource"])[:600])
r = g("https://iiif.wellcomecollection.org/image/V0032941/info.json"); print("WELL", r.status_code, r.text[:400])
