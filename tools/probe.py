"""Probe 6 (20 Nov 2026): can GitHub runners fetch an Artvee signed Max-size link? Read-only."""
import json, os, requests
cfg = json.loads(os.environ["NEVERLAND_READ"])
r = requests.post(cfg["db_url"] + "/rest/v1/rpc/artvee_peek", json={"p_token": cfg["db_token"]},
                  headers={"apikey": cfg["db_key"], "Content-Type": "application/json"}, timeout=60)
rows = r.json() if r.status_code == 200 else []
print("PEEK", r.status_code, len(rows))
for x in rows[:3]:
    g = requests.get(x["signed_url"], timeout=120, headers={"User-Agent": "Mozilla/5.0"})
    print("ARTVEE", x["slug"][:60], g.status_code, g.headers.get("content-type"), len(g.content))
