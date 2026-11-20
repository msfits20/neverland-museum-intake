"""
Look-alike check against the live Neverland Prints catalog.

Loads the picture fingerprint (pHash) of every catalog image from the store
database (filled by the read-only quality scan) and answers: "is this new
image an artwork we already sell, and is our copy of it weak?"

Rules (64-bit pHash, distance = number of differing bits), calibrated on
480 downloads by eye on 16 Nov 2026:
- distance <= SAME_BITS (4)                      -> same image, any names
- distance <= MID_BITS (10) and same artist surname OR a shared title word
                                                 -> same artwork
- distance <= NEAR_BITS (12) and same surname AND a shared title word
At 5-6 bits with nothing else in common, mostly-blank paper prints were
false matches, so a name or title clue is required there. Series by one
artist on blank paper (carriage designs, natural-history plates) still
fooled that, so every match above SAME_BITS is confirmed by matching
details: ORB feature points of both pictures (the other one from Drive's
preview, read-only) must line up under one shift/scale/rotation. On the
16 Nov calibration, true pairs lined up at 0.74-0.99 of matched points,
different pictures at 0.50 or less.
A catalog copy is "weak" when the quality scan marked it limit_sizes or
hide. A new copy that prints clearly bigger is an upgrade, not a duplicate.
"""
import json
import os
import time

import io
import threading

import numpy as np
import requests
from PIL import Image
import imagehash

SAME_BITS = int(os.environ.get("SAME_BITS", "4"))
MID_BITS = int(os.environ.get("MID_BITS", "10"))
NEAR_BITS = int(os.environ.get("NEAR_BITS", "12"))
STOP_WORDS = set("the and with from for after study studies portrait view untitled design plate pl figure figures "
                 "head woman man landscape scene part sketch title page der die das und von des les une with "
                 "dans sur zum zur".split())
DPI = 150
PREVIEW = 800


def small(im):
    t = im.convert("L")
    t.thumbnail((PREVIEW, PREVIEW))
    return t


def details_match(a, b):
    """a, b: small grayscale PIL images. Returns (inliers, ratio)."""
    import cv2
    A, B = np.asarray(a), np.asarray(b)
    orb = cv2.ORB_create(1500)
    ka, da = orb.detectAndCompute(A, None)
    kb, db = orb.detectAndCompute(B, None)
    if da is None or db is None or len(ka) < 8 or len(kb) < 8:
        return 0, 0.0
    m = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
    good = [x[0] for x in m if len(x) == 2 and x[0].distance < 0.8 * x[1].distance]
    if len(good) < 8:
        return 0, 0.0
    pa = np.float32([ka[g.queryIdx].pt for g in good])
    pb = np.float32([kb[g.trainIdx].pt for g in good])
    _, mask = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC, ransacReprojThreshold=4)
    inl = int(mask.sum()) if mask is not None else 0
    return inl, round(inl / len(good), 3)


def details_ok(inl, ratio):
    return (inl >= 40 and ratio >= 0.6) or (inl >= 12 and ratio >= 0.85)


class Previews:
    """Drive's small preview of any file, via the read-only Google sign-in."""
    def __init__(self, cfg):
        self.cfg, self.tok, self.exp = cfg, None, 0
        self.lock = threading.Lock()
        self.cache = {}

    def token(self):
        with self.lock:
            if time.time() > self.exp - 120:
                j = requests.post("https://oauth2.googleapis.com/token", data={
                    "client_id": self.cfg["client_id"], "client_secret": self.cfg["client_secret"],
                    "refresh_token": self.cfg["refresh_token"], "grant_type": "refresh_token"},
                    timeout=60).json()
                self.tok, self.exp = j["access_token"], time.time() + j.get("expires_in", 3600)
            return self.tok

    def image(self, file_id):
        h = {"Authorization": "Bearer " + self.token()}
        for i in range(4):
            try:
                j = requests.get("https://www.googleapis.com/drive/v3/files/" + file_id,
                                 params={"fields": "thumbnailLink"}, headers=h, timeout=60).json()
                link = j.get("thumbnailLink")
                if not link:
                    return None
                r = requests.get(link.split("=s")[0] + "=s%d" % PREVIEW, headers=h, timeout=60)
                if r.status_code == 200:
                    return Image.open(io.BytesIO(r.content)).convert("RGB")
            except Exception:
                pass
            time.sleep(2 ** i)
        return None



def confirmed(prev, new_small, other_id):
    """{"inliers", "ratio", "ok"}; None when it can't be checked (no preview),
    which counts as not confirmed."""
    if prev is None or new_small is None:
        return None
    other = prev.image(other_id)
    if other is None:
        return None
    inl, ratio = details_match(new_small, small(other))
    return {"inliers": inl, "ratio": ratio, "ok": details_ok(inl, ratio)}


def _rpc(cfg, fn, _tries=6, **args):
    """_tries: 6 waits ~1 min in all; 12 waits up to ~15 min (each wait capped at 2 min)
    for when the database is busy (e.g. during the bulk launch, 20 Nov 2026)."""
    args["p_token"] = cfg["db_token"]
    for i in range(_tries):
        try:
            r = requests.post(cfg["db_url"] + "/rest/v1/rpc/" + fn, json=args, timeout=120,
                              headers={"apikey": cfg["db_key"], "Content-Type": "application/json"})
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(min(2 ** i, 120))
    raise RuntimeError("database unreachable (%s)" % fn)


def wait_for_store(cfg, log=print, max_wait=3 * 3600):
    """20 Nov 2026: shoppers first. While the launch watchdog reports the storefront catalog as
    slow/failing (sync_state.launch_health.catalog_ok = false), background jobs pause here."""
    t0, said = time.time(), False
    while time.time() - t0 < max_wait:
        try:
            r = requests.post(cfg["db_url"] + "/rest/v1/rpc/intake_launch_ok", json={"p_token": cfg["db_token"]},
                              headers={"apikey": cfg["db_key"], "Content-Type": "application/json"}, timeout=30)
            if r.status_code == 200 and r.json() is not False:
                if said:
                    log("store catalog healthy again; carrying on")
                return
        except requests.RequestException:
            pass
        if not said:
            log("store catalog is struggling; pausing database writes so shoppers come first")
            said = True
        time.sleep(120)


def rpc_offset_all(cfg, fn, page=1000, **args):
    """Every row of a set-returning RPC. The API returns at most 1,000 rows per request,
    so page with offset (the function must return rows in a fixed order)."""
    args["p_token"] = cfg["db_token"]
    out, off = [], 0
    while True:
        rows = None
        for i in range(6):
            try:
                r = requests.post(cfg["db_url"] + "/rest/v1/rpc/%s?limit=%d&offset=%d" % (fn, page, off),
                                  json=args, timeout=120,
                                  headers={"apikey": cfg["db_key"], "Content-Type": "application/json"})
                if r.status_code == 200:
                    rows = r.json()
                    break
            except requests.RequestException:
                pass
            time.sleep(2 ** i)
        if rows is None:
            raise RuntimeError("database unreachable (%s)" % fn)
        out += rows
        if len(rows) < page:
            return out
        off += page


INDEX_FILE = os.environ.get("CATALOG_INDEX_FILE", "catalog_index.json.gz")


def index_rows(cfg):
    """The catalog fingerprint list. 20 Nov 2026: every museum part used to download the whole list
    (~600K rows, 120+ calls) from the database when it started; with a dozen parts starting at once that
    overloaded the database during the bulk launch. The workflow now shares one copy per hour through the
    GitHub cache (CATALOG_INDEX_FILE); without the file this falls back to the database as before."""
    import gzip
    if INDEX_FILE and os.path.exists(INDEX_FILE):
        try:
            with gzip.open(INDEX_FILE, "rt") as f:
                rows = json.load(f)
            if rows:
                return rows
        except Exception:
            pass
    return rpc_all(cfg, "intake_catalog_index")


def dump_index(path=None):
    """Write the catalog fingerprint list to a file (run once per workflow run, before the parts)."""
    import gzip
    cfg = json.loads(os.environ["NEVERLAND_READ"])
    rows = rpc_all(cfg, "intake_catalog_index")
    path = path or INDEX_FILE
    with gzip.open(path + ".tmp", "wt") as f:
        json.dump(rows, f)
    os.replace(path + ".tmp", path)   # an older copy from the cache stays usable if this run fails
    print("catalog index: %d rows" % len(rows))


def rpc_all(cfg, fn):
    out, after = [], ""
    while True:
        rows = _rpc(cfg, fn, p_after=after, p_limit=5000)  # small pages stay under the 3s DB timeout
        if not rows:
            return out
        out += rows
        after = rows[-1]["drive_file_id"]


def title_words(norm_title):
    return {w for w in norm_title.split() if len(w) >= 4 and not w.isdigit() and w not in STOP_WORDS}


def same_artwork(dist, sur_a, sur_b, words_a, words_b):
    if dist <= SAME_BITS:
        return "same image"
    s = bool(sur_a) and sur_a == sur_b
    t = bool(words_a & words_b)
    if dist <= MID_BITS and (s or t):
        return "same artwork"
    if dist <= NEAR_BITS and s and t:
        return "same artwork"
    return None


def hex64(h):
    return np.uint64(int(h, 16))


class Catalog:
    def __init__(self, cfg, surname, keys, norm=None):
        self.prev = Previews(cfg)
        rows = [r for r in index_rows(cfg) if r.get("phash")]
        self.rows = rows
        self.h = np.array([int(r["phash"], 16) for r in rows], dtype=np.uint64)
        self.sur = [surname((r.get("path") or "").split("/")[0]) for r in rows]
        self.words = [title_words(norm(r.get("name") or "")) if norm else set() for r in rows]
        # "weak" = worth replacing with a bigger copy: the scan marked it limit_sizes/hide, or
        # (Tobi, 16 Nov 2026) it can't print as big as our largest sizes (UPGRADE_BELOW_IN).
        self.weak = np.array([r.get("verdict") in ("limit_sizes", "hide")
                              or float(r.get("sharp_in") or 0) < UPGRADE_BELOW_IN for r in rows])
        self.weak_keys = set()
        for r, w in zip(rows, self.weak):
            if w:
                self.weak_keys.update(keys((r.get("path") or "").split("/")[0], r.get("name") or ""))

    def __len__(self):
        return len(self.rows)

    def best(self, ph, artist_surname, words=frozenset(), image=None):
        """Closest catalog match that counts as the same artwork, or None.
        Returns dict(id, dist, weak, sharp_in, kind, details).
        image: the new picture, for the middle-of-picture confirmation."""
        new_small = small(image) if image is not None else None
        d = np.bitwise_count(self.h ^ hex64(ph))
        cand = np.nonzero(d <= NEAR_BITS)[0]
        if not len(cand):
            return None
        best = None
        for i in cand[np.argsort(d[cand])]:
            dist = int(d[i])
            kind = same_artwork(dist, artist_surname, self.sur[i], set(words), self.words[i])
            if not kind:
                continue
            r = self.rows[i]
            cd = None
            if dist > SAME_BITS:
                cd = confirmed(self.prev, new_small, r["drive_file_id"])
                if not (cd and (cd["ok"] or os.environ.get("CALIBRATE_ALL") == "1")):
                    continue
            m = {"id": r["drive_file_id"], "dist": dist, "weak": bool(self.weak[i]),
                 "sharp_in": float(r.get("sharp_in") or 0), "kind": kind, "details": cd,
                 "file": "%s/%s" % (r.get("path"), r.get("name"))}
            if best is None:
                best = m
            if not m["weak"]:
                return m            # a good copy already sells -> duplicate
        return best


UPGRADE_BELOW_IN = float(os.environ.get("UPGRADE_BELOW_IN", "48"))
MAX_SALE_IN = 60.0   # biggest size we sell (40x60 in); printing bigger than that gains nothing


def upgrade_over(match, w, h):
    """True when a new w x h image prints clearly bigger than our copy (sizes we can sell)."""
    if not match or not match["weak"]:
        return False
    new_in = min(max(w, h) / DPI, MAX_SALE_IN)
    return new_in >= match["sharp_in"] * 1.3 + 4


def load(surname, keys, norm=None):
    raw = os.environ.get("NEVERLAND_READ")
    if not raw:
        return None
    return Catalog(json.loads(raw), surname, keys, norm)
