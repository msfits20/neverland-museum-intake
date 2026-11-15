#!/usr/bin/env python3
"""
Neverland print-quality scan (read-only).

Looks at every image in the "Neverland Prints" Drive folder and records, per
file: whether it opens, its picture fingerprint (pHash), real pixel size,
sharpness, JPEG compression, signs of having been blown up from a smaller
image, plain borders, and the largest size it can print sharp at 150 DPI.

Results go to the store database (asset_quality). It never writes, moves,
renames or deletes anything in Drive: its Google permission is read-only.
Already-scanned files are skipped, so runs pick up where the last stopped.

Mode comes from quality_mode.txt:  "sample N" (calibration) or "full".
"""
import hashlib
import io
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests
from PIL import Image, ImageFile
import imagehash

Image.MAX_IMAGE_PIXELS = None
ImageFile.LOAD_TRUNCATED_IMAGES = False

ROOT_NAME = "Neverland Prints"
DPI = 150
SHARD = int(os.environ.get("SHARD", "0"))
SHARDS = int(os.environ.get("SHARDS", "1"))
WORKERS = int(os.environ.get("WORKERS", "6"))
RUN_SECONDS = int(os.environ.get("RUN_MINUTES", "330")) * 60
T0 = time.time()
STOP = threading.Event()
LOCK = threading.Lock()
S = requests.Session()

CFG = json.loads(os.environ["NEVERLAND_READ"])
mode = open(os.path.join(os.path.dirname(__file__), "quality_mode.txt")).read().split()
SAMPLE = int(mode[1]) if mode and mode[0] == "sample" else 0
if mode and mode[0] == "off":
    print("quality scan switched off"); sys.exit(0)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ------------------------------------------------------------ google ----
class Drive:
    API = "https://www.googleapis.com/drive/v3"

    def __init__(self):
        self.tok, self.exp = None, 0
        self.lock = threading.Lock()

    def token(self):
        with self.lock:
            if time.time() < self.exp - 120:
                return self.tok
            r = requests.post("https://oauth2.googleapis.com/token", data={
                "client_id": CFG["client_id"], "client_secret": CFG["client_secret"],
                "refresh_token": CFG["refresh_token"], "grant_type": "refresh_token"}, timeout=60)
            if r.status_code != 200:
                sys.exit("Google sign-in failed: %s" % r.text[:200])
            j = r.json()
            self.tok, self.exp = j["access_token"], time.time() + j.get("expires_in", 3600)
            return self.tok

    def get(self, url, stream=False, **params):
        r = None
        for i in range(7):
            try:
                r = S.get(url, params=params, stream=stream, timeout=300,
                          headers={"Authorization": "Bearer " + self.token()})
            except requests.RequestException:
                time.sleep(2 ** i); continue
            if r.status_code in (429, 500, 502, 503, 504) or (
                    r.status_code == 403 and ("rate" in r.text.lower() or "quota" in r.text.lower())):
                if "downloadQuotaExceeded" in r.text:
                    log("Google download limit reached - stopping"); STOP.set(); return r
                time.sleep(2 ** i + 1); continue
            return r
        return r

    def list_all(self, q, fields):
        out, page = [], None
        while True:
            p = {"q": q, "fields": "nextPageToken,files(%s)" % fields, "pageSize": 1000,
                 "spaces": "drive"}
            if page:
                p["pageToken"] = page
            j = self.get(self.API + "/files", **p).json()
            out += j.get("files", [])
            page = j.get("nextPageToken")
            if not page:
                return out


def tree_images(d):
    roots = d.list_all("name = '%s' and mimeType = 'application/vnd.google-apps.folder' "
                       "and trashed = false and 'root' in parents" % ROOT_NAME, "id,name")
    if len(roots) != 1:
        sys.exit("Expected one '%s' folder at the top of Drive, found %d" % (ROOT_NAME, len(roots)))
    root = roots[0]["id"]
    folders = d.list_all("mimeType = 'application/vnd.google-apps.folder' and trashed = false",
                         "id,name,parents")
    parent = {f["id"]: (f.get("parents") or [None])[0] for f in folders}
    name = {f["id"]: f["name"] for f in folders}
    inside = {}

    def under(fid):
        chain = []
        while fid and fid not in inside:
            if fid == root:
                inside[fid] = True; break
            chain.append(fid); fid = parent.get(fid)
        ok = inside.get(fid, False) if fid else False
        for c in chain:
            inside[c] = ok
        return ok

    imgs = d.list_all("mimeType contains 'image/' and trashed = false",
                      "id,name,parents,size,mimeType")
    out = []
    for f in imgs:
        p = (f.get("parents") or [None])[0]
        if p and under(p):
            path, x = [], p
            while x and x != root:
                path.append(name.get(x, "?")); x = parent.get(x)
            f["path"] = "/".join(reversed(path))
            out.append(f)
    log("folders:", len(folders), "images in %s:" % ROOT_NAME, len(out))
    return out


# ------------------------------------------------------------ database --
def rpc(fn, **args):
    args["p_token"] = CFG["db_token"]
    for i in range(6):
        try:
            r = S.post(CFG["db_url"] + "/rest/v1/rpc/" + fn, json=args, timeout=120,
                       headers={"apikey": CFG["db_key"], "Content-Type": "application/json"})
            if r.status_code == 200:
                return r.json()
            log("db", fn, r.status_code, r.text[:200])
        except requests.RequestException as e:
            log("db error", e)
        time.sleep(2 ** i)
    sys.exit("database unreachable")


def scanned_ids():
    done, after = set(), ""
    while True:
        rows = rpc("intake_scanned_ids", p_after=after, p_limit=50000)
        if not rows:
            return done
        done.update(r["drive_file_id"] for r in rows)
        after = rows[-1]["drive_file_id"]


PENDING = []


def submit(row, force=False):
    with LOCK:
        if row:
            PENDING.append(row)
        if len(PENDING) >= 200 or (force and PENDING):
            batch = PENDING[:]
            PENDING.clear()
        else:
            return
    rpc("intake_submit", p_kind="quality", p_rows=batch)


# ------------------------------------------------------------ checks ----
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


def upscale_probe(g):
    """g: grayscale float crop at native res. Returns {factor: psnr} of
    shrinking by the factor and blowing back up. A blown-up image loses
    almost nothing (high PSNR) at its true factor."""
    out = {}
    h, w = g.shape
    src = Image.fromarray(g.astype(np.uint8))
    for f in (1.5, 2.0, 3.0, 4.0):
        sw, sh = max(8, int(w / f)), max(8, int(h / f))
        back = src.resize((sw, sh), Image.LANCZOS).resize((w, h), Image.BICUBIC)
        out[str(f)] = round(psnr(g, np.asarray(back, float)), 2)
    return out


def laplacian_var(g):
    lap = (-4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:])
    return float(lap.var())


def border_frac(g):
    """Fraction of each side that is a flat uniform strip."""
    h, w = g.shape
    res = {}
    for side, lines in (("top", g), ("bottom", g[::-1]), ("left", g.T), ("right", g.T[::-1])):
        n, lim = 0, int(len(lines) * 0.25)
        ref = lines[0].mean()
        while n < lim and lines[n].std() < 6 and abs(lines[n].mean() - ref) < 8:
            n += 1
        res[side] = round(n / len(lines), 4)
    return res


def check(f):
    row = {"drive_file_id": f["id"], "flags": {"name": f["name"], "path": f["path"],
                                               "bytes": int(f.get("size") or 0)}}
    r = D.get(D.API + "/files/" + f["id"], alt="media")
    if r is None or r.status_code != 200:
        if STOP.is_set():
            return None
        row.update(broken=True, verdict="broken")
        row["flags"]["error"] = "download %s" % (r.status_code if r is not None else "failed")
        return row
    data = r.content
    try:
        im = Image.open(io.BytesIO(data))
        jq = jpeg_quality(im) if im.format == "JPEG" else None
        im.load()
    except Exception as e:
        row.update(broken=True, verdict="broken")
        row["flags"]["error"] = "open: %s" % str(e)[:120]
        return row
    w, h = im.size
    rgb = im.convert("RGB")
    row.update(width_px=w, height_px=h, jpeg_quality=jq, broken=False,
               phash=str(imagehash.phash(rgb)))
    small = rgb.copy(); small.thumbnail((1200, 1200))
    gs = np.asarray(small.convert("L"), float)
    bf = border_frac(gs)
    gray = rgb.convert("L")
    c = min(1024, w, h)
    x0, y0 = (w - c) // 2, (h - c) // 2
    g = np.asarray(gray.crop((x0, y0, x0 + c, y0 + c)), float)
    probe = upscale_probe(g)
    sharp = laplacian_var(g)
    # true-detail factor: largest shrink that loses (almost) nothing
    factor = 1.0
    for k in ("4.0", "3.0", "2.0"):
        if probe[k] >= float(os.environ.get("UPSCALE_PSNR", "44")):
            factor = float(k); break
    long_px = max(w, h)
    sharp_in = round(long_px / factor / DPI, 1)
    upscaled = factor >= 2.0
    has_border = max(bf.values()) >= 0.06
    verdict = "ok"
    if sharp_in < 8:
        verdict = "hide"
    elif upscaled or (jq is not None and jq < 50):
        verdict = "limit_sizes"
    row.update(sharpness=round(sharp, 2), upscaled=upscaled, has_border=has_border,
               has_color_bar=None, max_sharp_long_in=sharp_in, verdict=verdict)
    row["flags"].update(probe=probe, border=bf, factor=factor, format=im.format, mode=im.mode)
    return row


def work(f):
    if STOP.is_set() or time.time() - T0 > RUN_SECONDS:
        STOP.set(); return
    try:
        row = check(f)
    except Exception as e:
        row = {"drive_file_id": f["id"], "broken": True, "verdict": "broken",
               "flags": {"name": f["name"], "path": f["path"], "error": "check: %s" % str(e)[:120]}}
    if row:
        submit(row)


def main():
    global D
    D = Drive()
    files = tree_images(D)
    done = scanned_ids()
    mine = [f for f in files
            if int(hashlib.md5(f["id"].encode()).hexdigest(), 16) % SHARDS == SHARD
            and f["id"] not in done]
    mine.sort(key=lambda f: hashlib.md5(f["id"].encode()).hexdigest())
    if SAMPLE:
        want = max(0, SAMPLE // SHARDS - sum(
            1 for f in files if f["id"] in done
            and int(hashlib.md5(f["id"].encode()).hexdigest(), 16) % SHARDS == SHARD))
        mine = mine[:want]
    log("shard %d/%d: %d already scanned overall, %d to do now" % (SHARD, SHARDS, len(done), len(mine)))
    n = 0
    with ThreadPoolExecutor(WORKERS) as ex:
        for _ in ex.map(work, mine):
            n += 1
            if n % 500 == 0:
                log("checked", n, "of", len(mine))
    submit(None, force=True)
    log("done this run:", n, "stopped early" if STOP.is_set() else "")


if __name__ == "__main__":
    main()
