"""Probe 3 (20 Nov 2026): Getty painting object JSON + counts by type."""
import requests, json
S = requests.Session(); S.headers["User-Agent"] = "NeverlandMuseumIntake/1.0 (open-access download; contact via GitHub msfits20)"
EP = "https://data.getty.edu/museum/collection/sparql"
def sparql(q):
    r = S.get(EP, params={"query": q}, headers={"Accept": "application/sparql-results+json"}, timeout=180)
    return r.json()["results"]["bindings"]
P = "PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/> PREFIX aat: <http://vocab.getty.edu/aat/> "
for name, t in [("painting", "300033618"), ("drawing", "300033973"), ("print", "300041273"), ("watercolor", "300078925")]:
    try:
        b = sparql(P + "SELECT (COUNT(DISTINCT ?o) AS ?n) WHERE { ?o a crm:E22_Human-Made_Object ; crm:P2_has_type aat:%s . }" % t)
        print(name, b[0]["n"]["value"])
    except Exception as e: print(name, "ERR", e)
b = sparql(P + "SELECT ?o WHERE { ?o a crm:E22_Human-Made_Object ; crm:P2_has_type aat:300033618 . } LIMIT 1")
u = b[0]["o"]["value"]; print("obj", u)
j = S.get(u, headers={"Accept": "application/ld+json"}, timeout=60).json()
def trim(x, d=0):
    if d > 4: return "…"
    if isinstance(x, dict): return {k: trim(v, d + 1) for k, v in x.items() if k in ("id","type","_label","content","classified_as","representation","digitally_shown_by","access_point","subject_to","conforms_to","format","produced_by","carried_out_by","timespan","identified_by","referred_to_by")}
    if isinstance(x, list): return [trim(v, d + 1) for v in x[:3]]
    return x
print(json.dumps(trim(j), indent=1)[:6000])
