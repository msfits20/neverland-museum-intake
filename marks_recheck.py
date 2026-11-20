#!/usr/bin/env python3
"""
Watermark / colour-chart recheck (20 Nov 2026).

The Met and NGA downloads were approved before review.py's watermark check existed, so they
never got a marks score. This scores them now with the same model and threshold (FrameModel,
MARKS_T 0.6) from Drive's own 448 px thumbnail, records every score in `marks_recheck`, and
holds rows still waiting (status new) exactly like the review would. Rows already launched are
only recorded: what happens to those products is decided after looking at a sample
(contact sheet on branch review-samples). Read-only on Drive.
"""
import io, json, os, threading, time, hashlib
from concurrent.futures import ThreadPoolExecutor
import requests
from PIL import Image, ImageDraw
import intake, catalog as catalog_mod
import review

SOURCES = os.environ.get("SOURCES", "met,nga").split(",")
SHARD, SHARDS = int(os.environ.get("SHARD", "0")), int(os.environ.get("SHARDS", "1"))
WORKERS = int(os.environ.get("WORKERS", "6"))
RUN_SECONDS = int(os.environ.get("RUN_MINUTES", "320")) * 60
CFG = json.loads(os.environ["NEVERLAND_READ"])
T0 = time.time()
LOCK = threading.Lock()
log = intake.log

def main():
    d = intake.Drive()
    todo = []
    for src in SOURCES:   # one source at a time: the list is paged by file id within a source
        after = ""
        while True:
            rows = catalog_mod._rpc(CFG, "intake_marks_todo", p_sources=[src], p_after=after, p_limit=5000)
            if not rows:
                break
            todo += rows
            after = rows[-1]["drive_file_id"]
    mine = [r for r in todo if int(hashlib.md5(r["drive_file_id"].encode()).hexdigest(), 16) % SHARDS == SHARD]
    log("to score: %d (shard %d/%d of %d)" % (len(mine), SHARD, SHARDS, len(todo)))
    frames = review.FrameModel()
    pending, top, n = [], [], [0]

    def flush(force=False):
        with LOCK:
            if not pending or (len(pending) < 200 and not force):
                return
            batch = pending[:]; pending.clear()
        catalog_mod._rpc(CFG, "intake_marks_submit", p_rows=batch, p_threshold=review.MARKS_T)

    def work(r):
        if time.time() - T0 > RUN_SECONDS:
            return
        fid = r["drive_file_id"]
        try:
            m = d.req("GET", d.API + "/files/" + fid, params={"fields": "thumbnailLink"}).json()
            link = (m.get("thumbnailLink") or "").rsplit("=s", 1)[0] + "=s448"
            resp = d.req("GET", link) if m.get("thumbnailLink") else d.req("GET", d.API + "/files/" + fid, params={"alt": "media"})
            im = Image.open(io.BytesIO(resp.content)).convert("RGB")
            im.thumbnail((448, 448))
            fs = frames.score(im)
            ms = frames.local.marks
        except Exception as e:
            log("  could not score", fid, str(e)[:80]); return
        with LOCK:
            pending.append({"drive_file_id": fid, "marks": ms, "frame": fs})
            if ms >= review.MARKS_T or len(top) < 0:
                top.append((ms, r.get("status"), im.copy()))
            n[0] += 1
            if n[0] % 1000 == 0:
                log("scored", n[0], "of", len(mine), "| flagged", len(top))
        flush()

    with ThreadPoolExecutor(WORKERS) as ex:
        list(ex.map(work, mine))
    flush(force=True)
    log("DONE: scored %d, flagged %d (marks >= %.2f)" % (n[0], len(top), review.MARKS_T))
    # contact sheet: the flagged ones, highest score first, and the ones just over the line
    os.makedirs("calibration", exist_ok=True)
    top.sort(key=lambda x: -x[0])
    for name, items in (("top", top[:48]), ("edge", [x for x in top if x[0] < 0.75][:48])):
        if items:
            review.sheet([(im, "%.2f %s" % (s, st)) for s, st, im in items],
                         "Watermark/colour chart recheck %s: %s (%d flagged)" % (name, ",".join(SOURCES), len(top)),
                         "calibration/marks_s%d_%s.jpg" % (SHARD, name))

if __name__ == "__main__":
    main()
