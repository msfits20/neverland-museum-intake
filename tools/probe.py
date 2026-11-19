import json, os, re, requests
UA = {"User-Agent": "NeverlandMuseumIntake/1.0 (open-access download; contact via GitHub msfits20)"}
def g(u, **kw):
    try: return requests.get(u, headers={**UA, **kw.pop("headers", {})}, timeout=90, **kw)
    except Exception as e: print("   ERR", u[:120], str(e)[:120]); return None
# key from the database (same token the intake uses)
key = None
try:
    c = json.loads(os.environ["NEVERLAND_READ"])
    r = requests.post(c["db_url"] + "/rest/v1/rpc/intake_get_config", json={"p_token": c["db_token"], "p_key": "si_api_key"}, headers={"apikey": c["db_key"], "Content-Type": "application/json"}, timeout=60)
    key = r.json() if r.status_code == 200 else None; print("SI key from db:", bool(key), r.status_code)
except Exception as e: print("SI key ERR", e)
# Mia image URLs
for u in ["https://api.artsmia.org/images/529/large.jpg", "https://api.artsmia.org/images/529/full.jpg", "https://1.api.artsmia.org/800/529.jpg", "https://iiif.artsmia.org/529.jpg/info.json", "https://iiif.dx.artsmia.org/529/info.json", "https://collections.artsmia.org/art/529"]:
    r = g(u)
    if r is not None: print("MIA img", u, r.status_code, r.headers.get("content-type"), len(r.content), re.findall(r"https?://[^\"' ]*(?:iiif|images|api\.artsmia)[^\"' ]*", r.text)[:6] if "html" in (r.headers.get("content-type") or "") else "")
r = g("https://search.artsmia.org/public_access:1%20image:valid?size=2&from=20000"); print("MIA deep", r.status_code if r is not None else None, r.text[:200] if r is not None else "")
# Smithsonian
if key:
    for q in ['online_media_type:"Images" AND unit_code:(SAAM OR NPG OR FSG OR CHNDM OR HMSG) AND media_usage:CC0', 'unit_code:SAAM AND online_media_type:"Images"']:
        r = g("https://api.si.edu/openaccess/api/v1.0/search", params={"q": q, "rows": 1, "api_key": key})
        if r is not None:
            j = r.json(); print("SI", q, r.status_code, (j.get("response") or {}).get("rowCount"))
            rows = (j.get("response") or {}).get("rows") or []
            if rows: print("SI row", json.dumps(rows[0])[:2500])
# Getty
a = g("https://data.getty.edu/museum/collection/activity-stream")
if a is not None:
    a = a.json(); print("GETTY stream keys", list(a.keys()), json.dumps(a.get("first") or a.get("last"))[:200], a.get("totalItems"))
    p = g((a.get("last") or {}).get("id", ""))
    if p is not None:
        items = p.json().get("orderedItems", [])
        objs = [it["object"]["id"] for it in items if it.get("object", {}).get("type") == "HumanMadeObject"][:1]
        for o in objs:
            j = g(o).json(); print("GETTY obj", json.dumps({k: j.get(k) for k in ["_label", "classified_as", "representation", "subject_to", "referred_to_by"]})[:3000])
