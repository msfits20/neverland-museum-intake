"""Probe 5 (20 Nov 2026): dry listing from the new Google Art Project source (src_gap). Read-only."""
import itertools, os, sys
os.environ.setdefault("SOURCES", "gap")
sys.path.insert(0, os.getcwd())
import intake
n = 0
for it in itertools.islice(intake.src_gap(), 25):
    n += 1
    print("GAP", it["artist"], "|", it["title"], "|", it["w"], "x", it["h"], "|", it["date"], "|", it["url"][:90])
print("GAP listed", n)
