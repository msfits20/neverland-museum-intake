"""Probe 7 (20 Nov 2026): load the ~302k Artvee works Tobi already has (from his old lists)
into artvee_known, so the live crawl doesn't queue works we already own."""
import gzip, json, os, requests
cfg = json.loads(os.environ["NEVERLAND_READ"])
slugs = [s for s in gzip.open("data/artvee_known_slugs.txt.gz", "rt").read().split("\n") if s]
tot = 0
for i in range(0, len(slugs), 10000):
    r = requests.post(cfg["db_url"] + "/rest/v1/rpc/artvee_known_add",
                      json={"p_token": cfg["db_token"], "p_slugs": slugs[i:i + 10000]},
                      headers={"apikey": cfg["db_key"], "Content-Type": "application/json"}, timeout=180)
    print("chunk", i, r.status_code, r.text[:80])
    if r.status_code == 200:
        tot += r.json()
print("KNOWN slugs", len(slugs), "inserted", tot)
