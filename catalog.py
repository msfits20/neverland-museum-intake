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
fooled that, so every match above SAME_BITS is confirmed by a second
fingerprint of the middle of both pictures (margins and paper cut away),
taken from Drive's small preview of the other file (read-only).
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
CENTER_BITS = int(os.environ.get("CENTER_BITS", "14"))


def center_hash(im):
    """pHash of the middle 60% of a 400px version (paper and margins cut away)."""
    t = im.convert("L")
    t.thumbnail((400, 400))
    w, h = t.size
    return imagehash.phash(t.crop((int(w * .2), int(h * .2), int(w * .8), int(h * .8))))


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
                r = requests.get(link.split("=s")[0] + "=s400", headers=h, timeout=60)
                if r.status_code == 200:
                    return Image.open(io.BytesIO(r.content)).convert("RGB")
            except Exception:
                pass
            time.sleep(2 ** i)
        return None

    def center(self, file_id):
        with self.lock:
            if file_id in self.cache:
                return self.cache[file_id]
        im = self.image(file_id)
        c = center_hash(im) if im is not None else None
        with self.lock:
            self.cache[file_id] = c
        return c


def confirmed(prev, new_center, other_id):
    """None when it can't be checked (no preview): treated as not confirmed."""
    if prev is None or new_center is None:
        return None
    c = prev.center(other_id)
    if c is None:
        return None
    return int(new_center - c)


def _rpc(cfg, fn, **args):
    args["p_token"] = cfg["db_token"]
    for i in range(6):
        try:
            r = requests.post(cfg["db_url"] + "/rest/v1/rpc/" + fn, json=args, timeout=120,
                              headers={"apikey": cfg["db_key"], "Content-Type": "application/json"})
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(2 ** i)
    raise RuntimeError("database unreachable (%s)" % fn)


def rpc_all(cfg, fn):
    out, after = [], ""
    while True:
        rows = _rpc(cfg, fn, p_after=after, p_limit=50000)
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
        rows = [r for r in rpc_all(cfg, "intake_catalog_index") if r.get("phash")]
        self.rows = rows
        self.h = np.array([int(r["phash"], 16) for r in rows], dtype=np.uint64)
        self.sur = [surname((r.get("path") or "").split("/")[0]) for r in rows]
        self.words = [title_words(norm(r.get("name") or "")) if norm else set() for r in rows]
        self.weak = np.array([r.get("verdict") in ("limit_sizes", "hide") for r in rows])
        self.weak_keys = set()
        for r, w in zip(rows, self.weak):
            if w:
                self.weak_keys.update(keys((r.get("path") or "").split("/")[0], r.get("name") or ""))

    def __len__(self):
        return len(self.rows)

    def best(self, ph, artist_surname, words=frozenset(), image=None):
        """Closest catalog match that counts as the same artwork, or None.
        Returns dict(id, dist, weak, sharp_in, kind, center).
        image: the new picture, for the middle-of-picture confirmation."""
        new_center = center_hash(image) if image is not None else None
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
                cd = confirmed(self.prev, new_center, r["drive_file_id"])
                if cd is None or cd > CENTER_BITS:
                    continue
            m = {"id": r["drive_file_id"], "dist": dist, "weak": bool(self.weak[i]),
                 "sharp_in": float(r.get("sharp_in") or 0), "kind": kind, "center": cd,
                 "file": "%s/%s" % (r.get("path"), r.get("name"))}
            if best is None:
                best = m
            if not m["weak"]:
                return m            # a good copy already sells -> duplicate
        return best


def upgrade_over(match, w, h):
    """True when a new w x h image prints clearly bigger than a weak catalog copy."""
    if not match or not match["weak"]:
        return False
    new_in = max(w, h) / DPI
    return new_in >= match["sharp_in"] * 1.3 + 4


def load(surname, keys, norm=None):
    raw = os.environ.get("NEVERLAND_READ")
    if not raw:
        return None
    return Catalog(json.loads(raw), surname, keys, norm)
