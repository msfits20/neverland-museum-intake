#!/usr/bin/env python3
"""
Neverland intake review (read-only on Drive).

Checks every image the museum intake saved in "Neverland Museum Intake"
before anything joins the catalog, and records one row per file in the
store database (intake_queue):

  status "new"  -> fine, waiting for the batch merge
  status "held" -> kept out, with the reason in hold_reason:
     "broken"              file doesn't open
     "too soft"            prints sharp at under 8 inches
     "frame in picture"    the museum photo shows the frame / wall
     "already in catalog"  look-alike of an image we already sell (duplicate_of)
     "repeat of download"  look-alike of another intake download (duplicate_of)

Also noted in the row's quality data:
  upgrade_for          catalog file this one should replace (ours is weak)
  fills_missing_artvee title matches an Artvee artwork the old scraper
                       claimed but never saved

It never moves, renames or deletes anything. Files already reviewed are
skipped (set REDO=1 to review everything again).
CALIBRATE=N: review N random files without writing to the database and
make contact sheets (calibration/) so the thresholds can be checked by eye.
"""
import gzip
import hashlib
import io
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image, ImageDraw
import imagehash

import intake
import catalog as catalog_mod

Image.MAX_IMAGE_PIXELS = None
DPI = 150
SHARD = int(os.environ.get("SHARD", "0"))
SHARDS = int(os.environ.get("SHARDS", "1"))
WORKERS = int(os.environ.get("WORKERS", "4"))
RUN_SECONDS = int(os.environ.get("RUN_MINUTES", "320")) * 60
MARKS_T = float(os.environ.get("MARKS_T", "0.6"))
FRAME_T = float(os.environ.get("FRAME_T", "0.6"))
CALIBRATE = int(os.environ.get("CALIBRATE", "0"))
REDO = os.environ.get("REDO") == "1"
T0 = time.time()
LOCK = threading.Lock()
log = intake.log
CFG = json.loads(os.environ["NEVERLAND_READ"])


# ------------------------------------------------------------ inventory --
def list_all(d, q, fields):
    out, page = [], None
    while True:
        p = {"q": q, "fields": "nextPageToken,files(%s)" % fields, "pageSize": 1000}
        if page:
            p["pageToken"] = page
        j = d.req("GET", d.API + "/files", params=p).json()
        out += j.get("files", [])
        page = j.get("nextPageToken")
        if not page:
            return out


def inventory(d):
    """All intake images with their Artist/Tier/name path, plus log entries."""
    folders = list_all(d, "mimeType = 'application/vnd.google-apps.folder' and trashed = false",
                       "id,name,parents")
    fname = {f["id"]: f["name"] for f in folders}
    fpar = {f["id"]: (f.get("parents") or [None])[0] for f in folders}

    def path_of(pid):
        parts = []
        while pid and pid != d.root and pid in fname:
            parts.append(fname[pid])
            pid = fpar.get(pid)
        return "/".join(reversed(parts)) if pid == d.root else None

    imgs = []
    for f in list_all(d, "mimeType contains 'image/' and trashed = false", "id,name,parents,size"):
        p = path_of((f.get("parents") or [None])[0])
        if p and not p.startswith("_"):
            f["path"] = p + "/" + f["name"]
            imgs.append(f)
    logs = list_all(d, "'%s' in parents and name contains 'log-' and trashed = false" % d.state_dir, "id,name")
    entries = {}

    def read(f):
        r = d.req("GET", d.API + "/files/" + f["id"], params={"alt": "media"})
        return r.text.splitlines() if r.status_code == 200 else []

    with ThreadPoolExecutor(8) as ex:
        for lines in ex.map(read, logs):
            for ln in lines:
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if e.get("skipped") or not e.get("file"):
                    continue
                entries[e["file"]] = e
    log("intake images: %d, log entries: %d (from %d logs)" % (len(imgs), len(entries), len(logs)))
    return imgs, entries


# ------------------------------------------------ repeats among downloads --
def intake_repeats(imgs, entries):
    """file id -> file id of the copy that is kept. Uses the intake's own
    fingerprint (same method for every download). Keeps the bigger image."""
    rows = []
    for f in imgs:
        e = entries.get(f["path"])
        if e and e.get("phash"):
            rows.append((f["id"], int(e["phash"], 16), int(e["w"]) * int(e["h"]),
                         intake.surname(e.get("artist")),
                         catalog_mod.title_words(intake.norm(e.get("title")))))
    if not rows:
        return {}
    ids = [r[0] for r in rows]
    h = np.array([r[1] for r in rows], dtype=np.uint64)
    px = np.array([r[2] for r in rows])
    sur = [r[3] for r in rows]
    words = [r[4] for r in rows]
    loser = {}
    for i in range(len(rows)):
        d = np.bitwise_count(h ^ h[i])
        for j in np.nonzero(d <= catalog_mod.NEAR_BITS)[0]:
            if j == i:
                continue
            if not catalog_mod.same_artwork(int(d[j]), sur[i], sur[j], words[i], words[j]):
                continue
            # i loses to j if j is bigger, or same size and j's id sorts first
            if px[j] > px[i] or (px[j] == px[i] and ids[j] < ids[i]):
                loser[ids[i]] = (ids[j], int(d[j]))
                break
    log("repeat candidates among downloads (before the detail check): %d" % len(loser))
    return loser


# --------------------------------------------------------------- checks --
STD_LUMA = np.array([16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
                     14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
                     18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
                     49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99], float)


def jpeg_quality(im):
    q = getattr(im, "quantization", None)
    if not q or 0 not in q:
        return None
    t = np.array(list(q[0])[:64], float)
    scale = float(np.sum(t) / np.sum(STD_LUMA) * 100)
    return round((200 - scale) / 2 if scale <= 100 else 5000 / scale, 1)


def psnr(a, b):
    m = float(np.mean((a - b) ** 2))
    return 99.0 if m < 1e-9 else 10 * np.log10(255.0 ** 2 / m)


def true_factor(gray, w, h):
    """How much the image was blown up (1 = not at all). Same test as the catalog scan."""
    c = min(1024, w, h)
    x0, y0 = (w - c) // 2, (h - c) // 2
    g = np.asarray(gray.crop((x0, y0, x0 + c, y0 + c)), float)
    src = Image.fromarray(g.astype(np.uint8))
    for f in (4.0, 3.0, 2.0):
        sw, sh = max(8, int(c / f)), max(8, int(c / f))
        back = src.resize((sw, sh), Image.LANCZOS).resize((c, c), Image.BICUBIC)
        if psnr(g, np.asarray(back, float)) >= 44:
            return f
    return 1.0


class FrameModel:
    """Zero-shot picture check: does the photo show a frame or a wall?"""
    FRAMED = ["a painting in a picture frame", "a framed picture hanging on a wall",
              "an ornate gilded frame around a painting", "a photograph of a framed artwork in a gallery"]
    PLAIN = ["a painting", "a flat scan of an artwork filling the whole image",
             "a drawing on paper", "a print on a sheet of paper with blank margins",
             "a poster"]
    # 17 Nov 2026 (Tobi): no watermarks, colour charts, rulers or museum labels in the picture
    MARKS = ["an image with a watermark written across it", "a stock photo with a repeated watermark",
             "a photo of an artwork next to a color calibration chart", "an artwork photographed with a ruler and color bars",
             "a scanned page with a museum stamp and catalogue number label"]

    def __init__(self):
        import torch
        import open_clip
        torch.set_num_threads(2)
        self.torch = torch
        self.model, _, self.pre = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="laion2b_s34b_b79k")
        self.model.eval()
        tok = open_clip.get_tokenizer("ViT-B-32")
        with torch.no_grad():
            t = self.model.encode_text(tok(self.FRAMED + self.PLAIN + self.MARKS))
            self.text = t / t.norm(dim=-1, keepdim=True)
        self.lock = threading.Lock()
        self.local = threading.local()

    def score(self, im):
        x = self.pre(im.convert("RGB")).unsqueeze(0)
        with self.lock, self.torch.no_grad():
            v = self.model.encode_image(x)
            v = v / v.norm(dim=-1, keepdim=True)
            logits = 100 * v @ self.text.T
        nf, npl = len(self.FRAMED), len(self.PLAIN)
        p = logits[0, :nf + npl].softmax(dim=-1)
        m = self.torch.cat([logits[0, nf + npl:], logits[0, nf:nf + npl]]).softmax(dim=-1)
        self.local.marks = round(float(m[:len(self.MARKS)].sum()), 3)
        return round(float(p[:nf].sum()), 3)


ARTVEE_NEW = set()


def check(d, f, e, cat, loser, missing, frames):
    row = {"drive_file_id": f["id"], "filename": f["name"], "filepath": f["path"],
           "file_size_bytes": int(f.get("size") or 0),
           "artist": (e or {}).get("artist") or f["path"].split("/")[0],
           "title": (e or {}).get("title") or f["name"],
           "source": (e or {}).get("src"), "source_id": (e or {}).get("id"),
           "source_url": (e or {}).get("page"), "license": (e or {}).get("license"),
           "status": "new", "hold_reason": None, "duplicate_of": None}
    q = {"kind": (e or {}).get("kind"), "date": (e or {}).get("date")}
    row["quality"] = q
    r = d.req("GET", d.API + "/files/" + f["id"], params={"alt": "media"})
    if r.status_code != 200:
        if r.status_code == 403:
            raise RuntimeError("drive refused download (%s)" % r.text[:120])
        row.update(status="held", hold_reason="broken")
        q["error"] = "download %s" % r.status_code
        return row, None
    try:
        im = Image.open(io.BytesIO(r.content))
        jq = jpeg_quality(im) if im.format == "JPEG" else None
        im.load()
        rgb = im.convert("RGB")
    except Exception as ex:
        row.update(status="held", hold_reason="broken")
        q["error"] = "open: %s" % str(ex)[:120]
        return row, None
    w, h = rgb.size
    ph = str(imagehash.phash(rgb))
    factor = true_factor(rgb.convert("L"), w, h)
    sharp_in = round(max(w, h) / factor / DPI, 1)
    small = rgb.copy()
    small.thumbnail((448, 448))
    fs = frames.score(small) if frames else None
    ms = getattr(frames.local, "marks", None) if frames else None
    row.update(width_px=w, height_px=h, phash=ph)
    q.update(jpeg_quality=jq, upscale_factor=factor, max_sharp_long_in=sharp_in, frame_score=fs, marks_score=ms)
    match = cat.best(ph, intake.surname(row["artist"]),
                     catalog_mod.title_words(intake.norm(row["title"])), rgb) if cat else None
    if match:
        q["catalog_match"] = match
    upgrade = catalog_mod.upgrade_over(match, w / factor, h / factor)
    if upgrade:
        q["upgrade_for"] = match["id"]
    t = intake.norm(row["title"])
    th = hashlib.sha1(t.encode()).hexdigest()[:12] if len(t.split()) >= 3 else None
    if th and th in missing:
        q["fills_missing_artvee"] = True
    if th and th in ARTVEE_NEW:
        q["artvee_new"] = True  # an Artvee artwork we don't have yet, got free from the museum
    if sharp_in < 8:
        row.update(status="held", hold_reason="too soft")
    elif fs is not None and fs >= FRAME_T:
        row.update(status="held", hold_reason="frame in picture")
    elif ms is not None and ms >= MARKS_T:
        row.update(status="held", hold_reason="watermark / colour chart")
    elif match and not upgrade:
        row.update(status="held", hold_reason="already in catalog", duplicate_of=match["id"])
    elif f["id"] in loser:
        other, dist = loser[f["id"]]
        cd = {"ok": True} if dist <= catalog_mod.SAME_BITS else catalog_mod.confirmed(
            cat.prev if cat else None, catalog_mod.small(rgb), other)
        q["repeat_of"] = {"id": other, "dist": dist, "details": cd}
        if cd and (cd["ok"] or os.environ.get("CALIBRATE_ALL") == "1"):
            row.update(status="held", hold_reason="repeat of download", duplicate_of=other)
    return row, small


# ------------------------------------------------------------ calibrate --
def sheet(items, title, path, catalog_imgs=None):
    """items: list of (thumb, caption). 6 per row, 220px cells."""
    if not items:
        return
    cw, ch, cols = 220, 250, 6
    rows = (len(items) + cols - 1) // cols
    out = Image.new("RGB", (cw * cols, ch * rows + 30), "white")
    dr = ImageDraw.Draw(out)
    dr.text((8, 8), title, fill="black")
    for k, (im, cap) in enumerate(items):
        t = im.copy()
        t.thumbnail((cw - 10, ch - 40))
        x, y = (k % cols) * cw + 5, (k // cols) * ch + 35
        out.paste(t, (x, y))
        dr.text((x, y + ch - 38), cap[:34], fill="black")
    out.save(path, quality=85)


def catalog_thumb(file_id):
    """Small view of a catalog image, read-only, for the calibration sheets."""
    import requests
    tok = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": CFG["client_id"], "client_secret": CFG["client_secret"],
        "refresh_token": CFG["refresh_token"], "grant_type": "refresh_token"}, timeout=60).json()["access_token"]
    j = requests.get("https://www.googleapis.com/drive/v3/files/" + file_id,
                     params={"fields": "thumbnailLink"},
                     headers={"Authorization": "Bearer " + tok}, timeout=60).json()
    link = (j.get("thumbnailLink") or "").replace("=s220", "=s400")
    if not link:
        return Image.new("RGB", (200, 200), "grey")
    r = requests.get(link, headers={"Authorization": "Bearer " + tok}, timeout=60)
    return Image.open(io.BytesIO(r.content)).convert("RGB")


# ----------------------------------------------------------------- main --
def main():
    d = intake.Drive()
    d.setup_root()
    imgs, entries = inventory(d)
    cat = catalog_mod.load(intake.surname, intake.keys, intake.norm)
    log("catalog fingerprints: %d" % len(cat))
    loser = intake_repeats(imgs, entries)
    with gzip.open(os.path.join(os.path.dirname(__file__), "missing_artvee_titles.txt.gz"), "rt") as fh:
        missing = set(fh.read().split())
    with gzip.open(os.path.join(os.path.dirname(__file__), "artvee_new_titles.txt.gz"), "rt") as fh:
        ARTVEE_NEW.update(fh.read().split())
    if SHARD == 0:
        hit = 0
        for f in imgs:
            t = intake.norm((entries.get(f["path"]) or {}).get("title") or f["name"])
            if len(t.split()) >= 3 and hashlib.sha1(t.encode()).hexdigest()[:12] in ARTVEE_NEW:
                hit += 1
        log("Artvee artworks we didn't have, found free among %d downloads: %d" % (len(imgs), hit))
    done = set() if REDO or CALIBRATE else {r["drive_file_id"] for r in catalog_mod.rpc_all(CFG, "intake_queue_ids")}
    mine = [f for f in imgs if f["id"] not in done
            and int(hashlib.md5(f["id"].encode()).hexdigest(), 16) % SHARDS == SHARD]
    if CALIBRATE:
        random.seed(SHARD)
        mine = random.sample(mine, min(CALIBRATE, len(mine)))
    log("shard %d/%d: %d already reviewed, %d to do now" % (SHARD, SHARDS, len(done), len(mine)))
    frames = FrameModel()
    pending, results, n = [], [], [0]
    stop = threading.Event()

    def flush(force=False):
        with LOCK:
            if not pending or (len(pending) < 200 and not force):
                return
            batch = pending[:]
            pending.clear()
        if not CALIBRATE:
            catalog_mod._rpc(CFG, "intake_submit", p_kind="candidate", p_rows=batch)

    def work(f):
        if stop.is_set() or time.time() - T0 > RUN_SECONDS:
            stop.set()
            return
        try:
            row, thumb = check(d, f, entries.get(f["path"]), cat, loser, missing, frames)
        except Exception as ex:
            log("  could not check", f["path"][:80], str(ex)[:100])
            return
        with LOCK:
            pending.append(row)
            n[0] += 1
            if CALIBRATE and thumb is not None:
                results.append((row, thumb))
            if n[0] % 500 == 0:
                log("reviewed", n[0], "of", len(mine))
        flush()

    with ThreadPoolExecutor(WORKERS) as ex:
        list(ex.map(work, mine))
    flush(force=True)
    counts = {}
    for r in ([x[0] for x in results] if CALIBRATE else []):
        counts[r["hold_reason"] or "ok"] = counts.get(r["hold_reason"] or "ok", 0) + 1
    log("done this run:", n[0], "stopped early (time)" if stop.is_set() else "", counts or "")

    if CALIBRATE:
        os.makedirs("calibration", exist_ok=True)
        by_frame = sorted(results, key=lambda x: -(x[0]["quality"].get("frame_score") or 0))
        sheet([(t, "%.2f %s" % (r["quality"]["frame_score"], r["title"])) for r, t in by_frame[:48]],
              "Highest frame scores (held at >= %.2f)" % FRAME_T, "calibration/s%d_frames_top.jpg" % SHARD)
        mid = [x for x in by_frame if 0.25 <= (x[0]["quality"].get("frame_score") or 0) < FRAME_T]
        sheet([(t, "%.2f %s" % (r["quality"]["frame_score"], r["title"])) for r, t in mid[:48]],
              "Frame scores 0.25 to %.2f (kept)" % FRAME_T, "calibration/s%d_frames_mid.jpg" % SHARD)
        dup = [x for x in results if x[0]["quality"].get("catalog_match")]
        dup.sort(key=lambda x: -x[0]["quality"]["catalog_match"]["dist"])
        items = []
        for r, t in dup[:24]:
            m = r["quality"]["catalog_match"]
            c = m.get("details") or {}
            items.append((t, "NEW d%d %s/%s %s" % (m["dist"], c.get("inliers", "-"), c.get("ratio", "-"), r["title"])))
            try:
                items.append((catalog_thumb(m["id"]), "OURS %s" % m["file"].split("/")[-1]))
            except Exception:
                items.append((Image.new("RGB", (200, 200), "grey"), "OURS (no preview)"))
        sheet(items, "Look-alike pairs, least similar first (new | ours)", "calibration/s%d_dupes.jpg" % SHARD)
        rep = [x for x in results if x[0]["hold_reason"] == "repeat of download"][:20]
        items = []
        path_of = {f["id"]: f["path"] for f in imgs}
        for r, t in rep:
            c = r["quality"]["repeat_of"]["details"] or {}
            items.append((t, "HELD %s/%s %s" % (c.get("inliers", "-"), c.get("ratio", "-"), r["title"])))
            try:
                j = d.req("GET", d.API + "/files/" + r["duplicate_of"], params={"fields": "thumbnailLink"}).json()
                tr = d.req("GET", j["thumbnailLink"].replace("=s220", "=s400"))
                items.append((Image.open(io.BytesIO(tr.content)).convert("RGB"),
                              "KEPT %s" % path_of.get(r["duplicate_of"], "?").split("/")[-1]))
            except Exception:
                items.append((Image.new("RGB", (200, 200), "grey"), "KEPT (no preview)"))
        sheet(items, "Repeats among downloads (held | kept)", "calibration/s%d_repeats.jpg" % SHARD)
        with open("calibration/s%d_rows.jsonl" % SHARD, "w") as fh:
            for r, _ in results:
                fh.write(json.dumps(r) + "\n")


if __name__ == "__main__":
    main()
