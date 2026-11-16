"""
Look-alike check against the live Neverland Prints catalog.

Loads the picture fingerprint (pHash) of every catalog image from the store
database (filled by the read-only quality scan) and answers: "is this new
image an artwork we already sell, and is our copy of it weak?"

Rules (64-bit pHash, distance = number of differing bits):
- distance <= SAME_BITS            -> same image (any artist name)
- distance <= NEAR_BITS and the same artist surname -> same artwork,
  e.g. another museum's photo of the same painting
A catalog copy is "weak" when the quality scan marked it limit_sizes or
hide. A new copy that prints clearly bigger is an upgrade, not a duplicate.
"""
import json
import os
import time

import numpy as np
import requests

SAME_BITS = int(os.environ.get("SAME_BITS", "6"))
NEAR_BITS = int(os.environ.get("NEAR_BITS", "12"))
DPI = 150


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


def hex64(h):
    return np.uint64(int(h, 16))


class Catalog:
    def __init__(self, cfg, surname, keys):
        rows = [r for r in rpc_all(cfg, "intake_catalog_index") if r.get("phash")]
        self.rows = rows
        self.h = np.array([int(r["phash"], 16) for r in rows], dtype=np.uint64)
        self.sur = [surname((r.get("path") or "").split("/")[0]) for r in rows]
        self.weak = np.array([r.get("verdict") in ("limit_sizes", "hide") for r in rows])
        self.weak_keys = set()
        for r, w in zip(rows, self.weak):
            if w:
                self.weak_keys.update(keys((r.get("path") or "").split("/")[0], r.get("name") or ""))

    def __len__(self):
        return len(self.rows)

    def best(self, ph, artist_surname):
        """Closest catalog match that counts as the same artwork, or None.
        Returns dict(id, dist, weak, sharp_in, kind)."""
        d = np.bitwise_count(self.h ^ hex64(ph))
        cand = np.nonzero(d <= NEAR_BITS)[0]
        if not len(cand):
            return None
        best = None
        for i in cand[np.argsort(d[cand])]:
            dist = int(d[i])
            if dist <= SAME_BITS:
                kind = "same image"
            elif artist_surname and self.sur[i] == artist_surname:
                kind = "same artwork"
            else:
                continue
            r = self.rows[i]
            m = {"id": r["drive_file_id"], "dist": dist, "weak": bool(self.weak[i]),
                 "sharp_in": float(r.get("sharp_in") or 0), "kind": kind,
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


def load(surname, keys):
    raw = os.environ.get("NEVERLAND_READ")
    if not raw:
        return None
    return Catalog(json.loads(raw), surname, keys)
