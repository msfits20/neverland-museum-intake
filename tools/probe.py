import json, re, requests
UA = {"User-Agent": "NeverlandPrintsIntake/1.0 (hello@neverlandprints.com)"}
def g(u, **kw):
    r = requests.get(u, headers=UA, timeout=90, **kw); return r
# Finnish National Gallery: paging + categories
for q in ["limit=2&offset=50000", "limit=2&page=3", "limit=1&categoryId=artwork"]:
    r = g("https://www.kansallisgalleria.fi/api/v1/objects?" + q)
    try:
        j = r.json(); print("FNG", q, r.status_code, len(j), [x.get("objectId") for x in j][:3], r.headers.get("x-total-count"), dict(list(r.headers.items())[:0]))
    except Exception: print("FNG", q, r.status_code, r.text[:200])
r = g("https://www.kansallisgalleria.fi/api/v1/objects?limit=1"); print("FNG headers", {k: v for k, v in r.headers.items() if k.lower().startswith(("x-", "link", "content-range"))})
for u in ["https://www.kansallisgalleria.fi/api/v1/objects/count", "https://www.kansallisgalleria.fi/api/v1/", "https://www.kansallisgalleria.fi/api/docs", "https://www.kansallisgalleria.fi/media-assets/1/jpg/4000/276654.jpg"]:
    r = g(u); print("FNG", u, r.status_code, r.headers.get("content-type"), r.text[:300].replace("\n"," ") if "image" not in (r.headers.get("content-type") or "") else len(r.content))
# Europeana: image fields of a PD print
r = g("https://api.europeana.eu/record/v2/search.json", params={"wskey": "api2demo", "query": "what:painting", "qf": ["TYPE:IMAGE", "RIGHTS:*publicdomain*", "IMAGE_SIZE:extra_large"], "media": "true", "rows": 2, "profile": "rich"})
j = r.json(); print("EU total", j.get("totalResults"))
for it in j.get("items", []):
    print("EU item", {k: it.get(k) for k in ["id", "title", "dcCreator", "edmIsShownBy", "edmIsShownAt", "rights", "dataProvider", "year", "type", "edmPreview"]})
# YCBA: resource section + resumption token
r = g("https://harvester-bl.britishart.yale.edu/oaicatmuseum/OAIHandler?verb=ListRecords&metadataPrefix=lido&set=ycba:ps")
t = r.text
i = t.find("resourceWrap"); print("YCBA resource", re.sub(r"\s+", " ", t[i:i+2500]))
print("YCBA token", re.findall(r"<resumptionToken[^>]*>[^<]*</resumptionToken>", t)[:1], "records", t.count("<record>"))
r = g("https://harvester-bl.britishart.yale.edu/oaicatmuseum/OAIHandler?verb=ListSets"); print("YCBA sets", re.findall(r"<setSpec>([^<]+)</setSpec>\s*<setName>([^<]+)", r.text))
