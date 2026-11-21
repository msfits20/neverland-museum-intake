"""Probe 11 (21 Nov 2026): the new src_mia provider yields working full-size URLs. Read-only."""
import itertools, sys, requests
sys.path.insert(0, ".")
import intake
for it in itertools.islice(intake.src_mia(), 4):
    r = requests.get(it["url"], headers={"User-Agent": intake.UA}, timeout=180)
    print(it["id"], it["title"][:50], "|", it["artist"], "|", it["w"], it["h"], "|", r.status_code, len(r.content), r.headers.get("Content-Type"))
