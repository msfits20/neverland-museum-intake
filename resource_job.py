#!/usr/bin/env python3
"""
Targeted re-sourcing (Tobi, 17 Nov 2026: "source a better image instead of
hiding it").

For catalog copies that are unfit (quality scan: broken, hide, limit_sizes;
or the file is missing) and that the bulk museum download hasn't matched yet,
look the artwork up by artist + title in the open-access collections the
intake already uses (Met, AIC, NGA, Cleveland, Rijksmuseum, SMK) and on
Wikimedia Commons (public domain / CC0 files only).

A hit is accepted only when ALL of these hold:
- same artist (catalog name, intake_artist_map aliases, or surname + first name)
- the title matches closely
- it is the same picture: pHash distance <= MATCH_BITS (10) against our copy,
  confirmed by the ORB detail check above 4 bits (catalog.py); for broken /
  missing copies (no picture to compare) a strict title + artist + year match
- it prints sharper than ours (max_sharp_long_in, >= 5% and >= 1 in more),
  unless ours is broken / missing
- aspect within 2% of ours (the product's sizes stay the same)
- the picture shows the artwork alone (review.py frame / watermark / colour
  chart / label check)

Accepted copies are saved in "Neverland Museum Intake" (Artist/Above|Below
2900x4060, same 150 GB floor) and sent to intake_queue as status 'new' with
quality.upgrade_for = our drive_file_id. The brain's apply-upgrades.js swaps
them under the same product. This script never changes the store or the
Neverland Prints folders (read-only previews only).

Misses go to resource_misses.jsonl (committed by the workflow) and are
retried after RETRY_DAYS. Progress: _progress/resource-state.json in Drive.
"""
import difflib
import io
import hashlib
import json
import os
import re
import time
import urllib.parse

import numpy as np
from PIL import Image
import imagehash

import intake
import catalog as catalog_mod
import review

Image.MAX_IMAGE_PIXELS = None
log = intake.log
S = intake.S
get_json = intake.get_json
norm, surname, safe = intake.norm, intake.surname, intake.safe

CFG = json.loads(os.environ["NEVERLAND_READ"])
TARGETS = int(os.environ.get("TARGETS", "300"))
RUN_SECONDS = int(os.environ.get("RUN_MINUTES", "320")) * 60
FLOOR_GB = float(os.environ.get("FLOOR_GB", "150"))
MATCH_BITS = int(os.environ.get("MATCH_BITS", "10"))
TITLE_MIN = float(os.environ.get("TITLE_MIN", "0.85"))
STRICT_TITLE_MIN = float(os.environ.get("STRICT_TITLE_MIN", "0.92"))
ASPECT_TOL = 0.02
SHARPER = 1.05
RETRY_DAYS = float(os.environ.get("RETRY_DAYS", "30"))
DRY = os.environ.get("DRY_RUN") == "1"
SOURCES = [s for s in os.environ.get("RESOURCE_SOURCES", "met,aic,nga,cleveland,rijks,smk,commons").split(",") if s]
# 18 Nov 2026: runs in parallel parts (SHARD_N / SHARD_I); each part keeps its own state + misses file
SHARD_N = int(os.environ.get("SHARD_N", "1"))
SHARD_I = int(os.environ.get("SHARD_I", "0"))
_SFX = "" if SHARD_N <= 1 else "-%d" % SHARD_I
STATE = "resource-state%s.json" % _SFX
OLD_STATE = "resource-state.json"  # the single-part run's history (read so old misses keep their retry date)
MISSES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resource_misses%s.jsonl" % _SFX)
T0 = time.time()
DPI = 150
# 19 Nov 2026 (Tobi): MODE=wanted looks for famous works the store shows or wants but doesn't sell yet
# (table intake_wanted, e.g. the homepage hero paintings). No catalog copy to compare against, so the
# match is by name: same artist, the original's year (+/- WANTED_YEAR_TOL), never an engraving/copy "after" it.
# A search_hint naming a Wikimedia Commons file is looked up directly and trusted for title and year.
WANTED = os.environ.get("MODE") == "wanted"
WANTED_YEAR_TOL = int(os.environ.get("WANTED_YEAR_TOL", "3"))
NOT_ORIGINAL = re.compile(r"\b(after|engrav\w*|etching|lithograph\w*|mezzotint|woodcut|copy|copies|reproduction|"
                          r"print(?:ed)? by|stud(?:y|ies)|sketch|detail|fragment|tapestry|photograph)\b", re.I)


# ------------------------------------------------------------- matching --
DROP = {"ca", "c", "circa", "no", "nr", "the", "a", "an", "of", "and", "le", "la", "les", "de", "du", "der", "die", "das"}


def tnorm(title):
    return " ".join(w for w in norm(title).split() if w not in DROP and not w.isdigit())


def title_score(a, b):
    a, b = tnorm(a), tnorm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ra = difflib.SequenceMatcher(None, a, b).ratio()
    wa, wb = set(a.split()), set(b.split())
    jac = len(wa & wb) / len(wa | wb)
    return round(max(ra, jac), 3)


def name_tokens(n):
    n = re.sub(r"\(.*?\)", "", str(n or ""))
    if "," in n:   # "Monet, Claude" -> "Claude Monet"
        p = [x.strip() for x in n.split(",", 1)]
        n = p[1] + " " + p[0]
    return [t for t in norm(n).split() if t not in intake.SKIP_WORDS]


def artist_ok(cand, target, aliases):
    c = name_tokens(cand)
    if not c:
        return False
    names = [target] + [a.split("|")[0] for a in aliases or []]
    for n in names:
        t = name_tokens(n)
        if not t:
            continue
        if c == t:
            return True
        # same surname, and the first name (or its initial) agrees
        if t[-1] in c and (len(t) == 1 or t[0] in c or any(x[0] == t[0][0] for x in c[:-1] if x)):
            return True
    return False


def year_of(*parts):
    for p in parts:
        y = intake.year_of(p)
        if y:
            return y
    return ""


def aspect_ok(w, h, tw, th):
    if not (w and h and tw and th):
        return None
    return abs((w / h) / (tw / th) - 1) < ASPECT_TOL


# --------------------------------------------------------------- sources --
def _q(s):
    return urllib.parse.quote(s)


def c_met(t):
    j = get_json("https://collectionapi.metmuseum.org/public/collection/v1/search",
                 params={"q": t["q_title"], "hasImages": "true"})
    out = []
    for oid in ((j or {}).get("objectIDs") or [])[:25]:
        o = get_json("https://collectionapi.metmuseum.org/public/collection/v1/objects/%s" % oid)
        time.sleep(0.05)
        if not o or not o.get("isPublicDomain") or not o.get("primaryImage"):
            continue
        out.append({"src": "met", "id": str(oid), "artist": o.get("artistDisplayName") or "",
                    "title": o.get("title") or "", "date": o.get("objectDate") or "",
                    "kind": o.get("classification") or "", "w": 0, "h": 0,
                    "url": o["primaryImage"], "preview": o.get("primaryImageSmall") or o["primaryImage"],
                    "page": o.get("objectURL"), "license": "CC0 (Met Open Access)"})
    return out


def c_aic(t):
    j = get_json("https://api.artic.edu/api/v1/artworks/search", params={
        "q": "%s %s" % (t["artist"], t["q_title"]), "limit": 25,
        "fields": "id,title,artist_title,date_display,image_id,is_public_domain,thumbnail,artwork_type_title"},
        headers={"AIC-User-Agent": intake.UA})
    out = []
    for r in (j or {}).get("data") or []:
        if not r.get("is_public_domain") or not r.get("image_id"):
            continue
        th = r.get("thumbnail") or {}
        base = "https://www.artic.edu/iiif/2/%s" % r["image_id"]
        out.append({"src": "aic", "id": str(r["id"]), "artist": r.get("artist_title") or "",
                    "title": r.get("title") or "", "date": r.get("date_display") or "",
                    "kind": (r.get("artwork_type_title") or "").lower(),
                    "w": int(th.get("width") or 0), "h": int(th.get("height") or 0),
                    "url": None, "iiif": base, "preview": base + "/full/843,/0/default.jpg",
                    "page": "https://www.artic.edu/artworks/%s" % r["id"],
                    "license": "CC0 (Art Institute of Chicago)"})
    return out


_NGA = {}


def c_nga(t):
    if not _NGA:
        log("loading the NGA open-data list")
        for it in intake.src_nga():
            _NGA.setdefault(surname(it["artist"]), []).append(it)
    out = []
    for it in _NGA.get(surname(t["artist"]), []):
        x = dict(it)
        x["preview"] = x["iiif"] + "/full/!800,800/0/default.jpg"
        out.append(x)
    return out


def c_cleveland(t):
    j = get_json("https://openaccess-api.clevelandart.org/api/artworks/", params={
        "q": t["q_title"], "cc0": 1, "has_image": 1, "limit": 25,
        "fields": "id,title,creators,creation_date,images,url,type"})
    out = []
    for r in (j or {}).get("data") or []:
        im = r.get("images") or {}
        best = im.get("full") or im.get("print") or {}
        if not best.get("url"):
            continue
        cr = r.get("creators") or []
        out.append({"src": "cleveland", "id": str(r["id"]),
                    "artist": re.sub(r"\s*\(.*$", "", cr[0]["description"]) if cr else "",
                    "title": r.get("title") or "", "date": r.get("creation_date") or "",
                    "kind": (r.get("type") or "").lower(),
                    "w": int(best.get("width") or 0), "h": int(best.get("height") or 0),
                    "url": best["url"], "preview": (im.get("web") or best).get("url"),
                    "page": r.get("url"), "license": "CC0 (Cleveland Museum of Art)"})
    return out


def c_rijks(t):
    j = get_json("https://data.rijksmuseum.nl/search/collection", params={
        "title": t["q_title"], "creator": t["artist"], "imageAvailable": "true"})
    out = []
    for o in ((j or {}).get("orderedItems") or [])[:15]:
        oid = o.get("id", "").rsplit("/", 1)[-1]
        if not oid:
            continue
        it = intake.resolve_rijks({"src": "rijks", "id": oid, "artist": None, "title": None, "date": "",
                                   "kind": "", "w": 0, "h": 0, "url": None,
                                   "page": "https://id.rijksmuseum.nl/" + oid,
                                   "license": "Public domain (Rijksmuseum)"})
        if it:
            it["preview"] = it["iiif"] + "/full/!800,800/0/default.jpg"
            out.append(it)
    return out


def c_smk(t):
    j = get_json("https://api.smk.dk/api/v1/art/search/", params={
        "keys": "%s %s" % (t["artist"], t["q_title"]),
        "filters": "[has_image:true],[public_domain:true]", "rows": 25, "lang": "en"})
    out = []
    for r in (j or {}).get("items") or []:
        if not r.get("image_native"):
            continue
        pr = (r.get("production") or [{}])[0]
        artist = " ".join(x for x in (pr.get("creator_forename"), pr.get("creator_surname")) if x) \
            or pr.get("creator") or ""
        pd = (r.get("production_date") or [{}])[0]
        out.append({"src": "smk", "id": str(r.get("object_number") or r.get("id")), "artist": artist,
                    "title": (r.get("titles") or [{}])[0].get("title") or "", "date": pd.get("period") or "",
                    "kind": "", "w": int(r.get("image_width") or 0), "h": int(r.get("image_height") or 0),
                    "url": r["image_native"], "preview": r.get("image_thumbnail") or r["image_native"],
                    "page": r.get("frontend_url"),
                    "license": "Public domain (SMK, National Gallery of Denmark)"})
    return out


PD_LICENSE = re.compile(r"^(public domain|pd\b|pd-|cc0|cc-zero)", re.I)
NOT_ALONE = re.compile(r"\b(frame[ds]?|framed|gallery|exhibition|installation|museum view|in situ|"
                       r"room|wall|colou?r (chart|checker|bar)|ruler|label|detail)\b", re.I)


def _strip(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(html or ""))).strip()


def _commons_pages(params, hint=False):
    j = get_json("https://commons.wikimedia.org/w/api.php", params=dict({
        "action": "query", "format": "json", "prop": "imageinfo|categories", "cllimit": 50,
        "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 800}, **params))
    out = []
    for p in ((j or {}).get("query") or {}).get("pages", {}).values():
        ii = (p.get("imageinfo") or [{}])[0]
        if ii.get("mime") not in ("image/jpeg", "image/png", "image/tiff"):
            continue
        md = ii.get("extmetadata") or {}
        lic = _strip((md.get("LicenseShortName") or {}).get("value"))
        lic2 = _strip((md.get("License") or {}).get("value"))
        if not (PD_LICENSE.match(lic) or PD_LICENSE.match(lic2)):
            continue
        cats = " ".join(c.get("title", "") for c in p.get("categories") or [])
        desc = _strip((md.get("ImageDescription") or {}).get("value"))
        name = p.get("title", "")
        if not hint and (NOT_ALONE.search(name) or NOT_ALONE.search(desc[:400]) or re.search(
                r"framed|frames|in situ|exhibitions?|installation views?|color ?checker|ruler", cats, re.I)):
            continue
        title = _strip((md.get("ObjectName") or {}).get("value")) or \
            re.sub(r"\.\w+$", "", name.split(":", 1)[-1])
        out.append({"src": "commons", "id": str(p.get("pageid")), "artist": _strip((md.get("Artist") or {}).get("value")),
                    "title": title, "date": _strip((md.get("DateTimeOriginal") or {}).get("value")),
                    "kind": "", "w": int(ii.get("width") or 0), "h": int(ii.get("height") or 0),
                    "url": ii.get("url"), "preview": ii.get("thumburl") or ii.get("url"),
                    "page": ii.get("descriptionurl"), "hint": hint,
                    "license": "Public domain (Wikimedia Commons: %s)" % (lic or lic2)[:60]})
    time.sleep(0.5)
    return out


def c_commons(t):
    out = []
    hint = t.get("search_hint") or ""
    m = re.match(r"\s*Wikimedia Commons:\s*(.+?)\s*$", hint)
    if m:   # the exact file we picked for a wanted work
        out += _commons_pages({"titles": "File:" + m.group(1).strip('"')}, hint=True)
    out += _commons_pages({"generator": "search", "gsrnamespace": 6, "gsrlimit": 20,
                           "gsrsearch": '%s "%s" filetype:bitmap' % (t["artist"], t["q_title"])})
    return out


SEARCH = {"met": c_met, "aic": c_aic, "nga": c_nga, "cleveland": c_cleveland,
          "rijks": c_rijks, "smk": c_smk, "commons": c_commons}


# ------------------------------------------------------------- pictures --
def fetch(url, hdr=None):
    for i in range(4):
        try:
            r = S.get(url, timeout=120, headers=hdr or {})
            if r.status_code == 200:
                im = Image.open(io.BytesIO(r.content))
                im.load()
                return im.convert("RGB")
            if r.status_code in (403, 404):
                return None
        except Exception:
            pass
        time.sleep(2 ** i)
    return None


def hd(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count("1")


# ----------------------------------------------------------------- target --
def prepare(t):
    raw = t.get("title") or re.sub(r"_\d+x\d+\.\w+$", "", t.get("filename") or "")
    t["year"] = year_of(raw, t.get("filename"))
    q = re.sub(r"\b(1[0-9]{3}|20[0-2][0-9])\b", " ", raw)
    q = re.sub(r"\b(ca|c\.|circa)\b\.?", " ", q, flags=re.I)
    t["q_title"] = re.sub(r"\s+", " ", q).strip(" -,.")
    t["strict"] = t["verdict"] in ("broken", "missing")
    if t.get("wanted"):
        t["year"] = year_of(t.get("wanted_year"))
        t["strict"] = True
    return t


def judge(t, c, ours_ph, ours_small):
    """Cheap checks on one candidate. Returns (ok, info)."""
    info = {"src": c["src"], "id": c["id"], "title": c["title"], "artist": c["artist"]}
    if not artist_ok(c["artist"], t["artist"], t.get("catalog_artist_aliases")):
        return False, dict(info, why="artist")
    ts = title_score(t["q_title"], c["title"])
    info["title_score"] = ts
    if t.get("wanted"):
        if NOT_ORIGINAL.search(" ".join([c.get("title") or "", c.get("kind") or "", c.get("artist") or ""])):
            return False, dict(info, why="not the original (copy/print/study)")
        if not c.get("hint"):
            if ts < STRICT_TITLE_MIN:
                return False, dict(info, why="title")
            cy = year_of(c.get("date"), c.get("title"))
            if not (t["year"] and cy and abs(int(cy) - int(t["year"])) <= WANTED_YEAR_TOL):
                return False, dict(info, why="year %s vs %s" % (cy or "?", t["year"] or "?"))
        pv = fetch(c["preview"], {"AIC-User-Agent": intake.UA}) if c.get("preview") else None
        if pv is None:
            return False, dict(info, why="no preview")
        c["_pv"] = pv
        return True, info
    strict = t["strict"] or not ours_ph
    if ts < (STRICT_TITLE_MIN if strict else TITLE_MIN):
        return False, dict(info, why="title")
    cy = year_of(c.get("date"), c.get("title"))
    if strict:
        if t["year"] and cy and t["year"] != cy:
            return False, dict(info, why="year")
        if not t["year"] and ts < 1.0:
            return False, dict(info, why="strict title (no year)")
        if t["year"] and not cy:
            return False, dict(info, why="no year to compare")
    a = aspect_ok(c["w"], c["h"], t["width_px"], t["height_px"])
    if a is False:
        return False, dict(info, why="aspect")
    if c["w"] and c["h"] and not t["strict"]:
        cand_in = min(max(c["w"], c["h"], ) / DPI, intake.MAX_LONG / DPI, catalog_mod.MAX_SALE_IN)
        if cand_in < (t["sharp_in"] or 0) * SHARPER:
            return False, dict(info, why="not bigger")
    pv = fetch(c["preview"], {"AIC-User-Agent": intake.UA}) if c.get("preview") else None
    if pv is None:
        return False, dict(info, why="no preview")
    if aspect_ok(pv.width, pv.height, t["width_px"], t["height_px"]) is False:
        return False, dict(info, why="aspect")
    if ours_ph:
        d = hd(str(imagehash.phash(pv)), ours_ph)
        info["dist"] = d
        if d > MATCH_BITS:
            return False, dict(info, why="different picture (%d bits)" % d)
        if d > catalog_mod.SAME_BITS:
            if ours_small is None:
                return False, dict(info, why="can't confirm details")
            inl, ratio = catalog_mod.details_match(catalog_mod.small(pv), ours_small)
            info["details"] = {"inliers": inl, "ratio": ratio}
            if not catalog_mod.details_ok(inl, ratio):
                return False, dict(info, why="details differ")
    c["_pv"] = pv
    return True, info


class Job:
    def __init__(self):
        self.d = intake.Drive()
        self.d.setup_root()
        self.prev = catalog_mod.Previews(CFG)
        self.frames = review.FrameModel()
        self.state = self.d.read_json(STATE, {}) if not DRY else {}
        self.done = self.state.get("done", {})
        if not DRY and STATE != OLD_STATE:
            for k, v in self.d.read_json(OLD_STATE, {}).get("done", {}).items():
                self.done.setdefault(k, v)
        self.n = {"targets": 0, "found": 0, "miss": 0, "error": 0}
        self.misses = []
        self.hits = []

    def save(self):
        if DRY:
            return
        self.state["done"] = self.done
        self.state["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.d.write_json(STATE, self.state)
        if self.hits:
            name = "log-%s-resource.jsonl" % time.strftime("%Y%m%d-%H%M%S")
            self.d.upload(name, self.d.state_dir, "\n".join(json.dumps(x) for x in self.hits).encode(),
                          "application/json")
            self.hits = []

    def due(self, t):
        e = self.done.get(t["drive_file_id"])
        if not e:
            return True
        return e[1] != "found" and time.time() - e[0] > RETRY_DAYS * 86400

    def one(self, t):
        t = prepare(t)
        # broken / missing: our file doesn't open, so nothing to compare -> strict name match only
        ours_ph = None if t["strict"] else t.get("phash")
        ours_small = None
        ours = None if t["strict"] else self.prev.image(t["drive_file_id"])
        if ours is not None:
            ours_small = catalog_mod.small(ours)
            if not ours_ph:
                ours_ph = str(imagehash.phash(ours))
        cands, tried = [], []
        for s in SOURCES:
            try:
                found = SEARCH[s](t)
            except Exception as ex:
                log("  search failed", s, str(ex)[:80])
                found = []
            for c in found:
                if "preview" not in c:
                    continue
                ok, info = judge(t, c, ours_ph, ours_small)
                tried.append(info)
                if ok:
                    cands.append((c, info))
        # biggest first; download and check the full picture
        cands.sort(key=lambda x: (not x[0].get("hint"), -max(x[0]["w"], x[0]["h"])))
        for c, info in cands[:(5 if t.get("wanted") else 3)]:
            res = self.take(t, c, info, ours_ph)
            if res:
                return "found", res
        return "miss", [i for i in tried if i.get("why") not in ("artist",)][:8]

    def take(self, t, c, info, ours_ph):
        try:
            im, data = intake.fetch_image(c)
        except Exception as ex:
            info["why"] = "download: %s" % str(ex)[:60]
            return None
        im, data = intake.finish(im, data)
        rgb = im.convert("RGB")
        w, h = rgb.size
        if aspect_ok(w, h, t["width_px"], t["height_px"]) is False:
            info["why"] = "aspect (full)"
            return None
        factor = review.true_factor(rgb.convert("L"), w, h)
        sharp = round(max(w, h) / factor / DPI, 1)
        if not t["strict"] and sharp < max((t["sharp_in"] or 0) * SHARPER, (t["sharp_in"] or 0) + 1):
            info["why"] = "not sharper (%.1f in vs %.1f)" % (sharp, t["sharp_in"] or 0)
            return None
        if sharp < 8:
            info["why"] = "too soft"
            return None
        ph = str(imagehash.phash(rgb))
        if ours_ph:
            d = hd(ph, ours_ph)
            if d > MATCH_BITS:
                info["why"] = "different picture (full, %d bits)" % d
                return None
            info["dist_full"] = d
        sm = rgb.copy()
        sm.thumbnail((448, 448))
        fs = self.frames.score(sm)
        ms = getattr(self.frames.local, "marks", None)
        if fs >= review.FRAME_T or (ms is not None and ms >= review.MARKS_T):
            info["why"] = "not the artwork alone (frame %.2f, marks %s)" % (fs, ms)
            return None
        artist = safe(t["artist"] or "Unknown Artist", 80)
        tier = intake.tier_of(w, h)
        title = t["title"] if t.get("wanted") else (c["title"] or t["q_title"])  # wanted: the recognised name
        name = intake.file_name(title, c.get("date") or t["year"], w, h)
        q = {"kind": c.get("kind"), "date": c.get("date"), "jpeg_quality": None, "upscale_factor": factor,
             "max_sharp_long_in": sharp, "frame_score": fs, "marks_score": ms,
             "upgrade_for": None if t.get("wanted") else t["drive_file_id"],
             "wanted_id": t.get("wanted_id"),
             "resource": {"target_verdict": t["verdict"], "our_sharp_in": t["sharp_in"],
                          "match": {k: info.get(k) for k in ("title_score", "dist", "dist_full", "details")},
                          "strict": t["strict"]}}
        if DRY:
            log("  [dry] would save %s/%s/%s (%.1f in, ours %s)" % (artist, tier, name, sharp, t["sharp_in"]))
            return {"file": "%s/%s/%s" % (artist, tier, name), "dry": True, "sharp": sharp}
        parent = self.d.folder((artist, tier))
        fid = self.d.find(name, parent) or self.d.upload(name, parent, data)
        row = {"drive_file_id": fid, "filename": name, "filepath": "%s/%s/%s" % (artist, tier, name),
               "file_size_bytes": len(data), "artist": t["artist"], "title": title,
               "source": c["src"], "source_id": c["id"], "source_url": c.get("page"),
               "license": c.get("license"), "width_px": w, "height_px": h, "phash": ph,
               "quality": q, "status": "new", "hold_reason": None, "duplicate_of": None}
        catalog_mod._rpc(CFG, "intake_submit", p_kind="candidate", p_rows=[row])
        self.hits.append({k2: c.get(k2) for k2 in ("src", "id", "artist", "title", "date", "kind", "page", "license")} |
                         {"file": row["filepath"], "w": w, "h": h, "bytes": len(data), "cphash": ph,
                          "upgrade_for": None if t.get("wanted") else t["drive_file_id"],
                         "wanted_id": t.get("wanted_id"), "resource": True})
        log("  saved %s (%.1f in, ours %s %s)" % (row["filepath"], sharp, t["verdict"], t["sharp_in"]))
        return {"file": row["filepath"], "id": fid, "sharp": sharp}

    def run(self):
        if not DRY:
            free = self.d.free_gb()
            log("Drive free space: %.0f GB (stops at %.0f GB)" % (free, FLOOR_GB))
            if free < FLOOR_GB:
                log("Not enough Drive space - not starting.")
                return
        targets = catalog_mod.rpc_offset_all(CFG, "intake_resource_targets", p_limit=20000)  # all of them, not just the first 1,000
        by = {}
        for t in targets:
            by[t["verdict"]] = by.get(t["verdict"], 0) + 1
        if SHARD_N > 1:
            targets = [t for t in targets
                       if int(hashlib.md5(t["drive_file_id"].encode()).hexdigest(), 16) % SHARD_N == SHARD_I]
        todo = [t for t in targets if self.due(t)][:TARGETS]
        log("unfit copies without a better one: %d %s | this run: %d" % (len(targets), by, len(todo)))
        for k, t in enumerate(todo):
            if intake.STOP.is_set() or time.time() - T0 > RUN_SECONDS:
                log("Time is up - the next run carries on")
                break
            if k and k % 50 == 0 and not DRY:
                self.save()
                if self.d.free_gb() < FLOOR_GB:
                    log("Drive free space below the floor - stopping.")
                    break
            self.n["targets"] += 1
            try:
                res, detail = self.one(t)
            except Exception as ex:
                res, detail = "error", str(ex)[:120]
                log("  error", t.get("title"), detail)
            self.n["found" if res == "found" else res] += 1
            self.done[t["drive_file_id"]] = [int(time.time()), res]
            if res != "found":
                self.misses.append({"drive_file_id": t["drive_file_id"], "artist": t["artist"],
                                    "title": t["title"], "verdict": t["verdict"], "result": res,
                                    "at": time.strftime("%Y-%m-%d"), "near": detail})
        self.save()
        with open(MISSES, "a") as fh:
            for m in self.misses:
                fh.write(json.dumps(m, ensure_ascii=False) + "\n")
        log("DONE: targeted %(targets)d | better copy found %(found)d | no copy %(miss)d | errors %(error)d" % self.n)
        with open(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a") as fh:
            fh.write("Re-source: targeted %(targets)d, better copy found %(found)d, no copy %(miss)d, errors %(error)d\n" % self.n)


def run_wanted(job):
    """Every still-wanted work, every run (the list is short). Found -> intake_queue 'new' with quality.wanted_id;
    the brain's promote-intake.js lists those first, even while bulk promotion is off."""
    if not DRY and job.d.free_gb() < FLOOR_GB:
        log("Not enough Drive space - not starting.")
        return
    rows = catalog_mod._rpc(CFG, "intake_wanted_targets") or []
    log("wanted works still to find: %d" % len(rows))
    found = 0
    for w in rows:
        t = {"wanted": True, "wanted_id": w["id"], "wanted_year": w.get("year"), "search_hint": w.get("search_hint"),
             "drive_file_id": "wanted-%s" % w["id"], "artist": w["artist"], "title": w["title"], "filename": "",
             "catalog_artist_aliases": w.get("catalog_artist_aliases") or [], "width_px": None, "height_px": None,
             "verdict": "missing", "sharp_in": None, "phash": None}
        try:
            res, detail = job.one(t)
        except Exception as ex:
            res, detail = "error", str(ex)[:160]
        ok = res == "found" and not (isinstance(detail, dict) and detail.get("dry"))
        log("  %s - %s: %s" % (w["artist"], w["title"], res))
        if ok:
            found += 1
        if not DRY:
            catalog_mod._rpc(CFG, "intake_wanted_report", p_id=w["id"], p_found=ok,
                             p_drive_file_id=(detail or {}).get("id") if ok else None,
                             p_result=detail if isinstance(detail, (dict, list)) else {"error": str(detail)})
    job.save()
    log("WANTED DONE: %d of %d found" % (found, len(rows)))
    with open(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a") as fh:
        fh.write("Wanted works: %d of %d found\n" % (found, len(rows)))


def main():
    if os.environ.get("PAUSED") == "1":
        log("Paused (repository variable PAUSED=1). Nothing to do.")
        return
    if WANTED:
        run_wanted(Job())
        return
    Job().run()


if __name__ == "__main__":
    main()
