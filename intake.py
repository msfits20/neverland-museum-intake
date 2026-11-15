#!/usr/bin/env python3
"""
Neverland museum intake.

Downloads open-access wall art (paintings, drawings, prints, posters) from
museum collections into its own Google Drive folder, "Neverland Museum
Intake". The Google permission it uses (drive.file) only lets it see files
it created itself, so it cannot see or touch any other folder.

- Skips anything already in the Neverland Prints catalog (artist + title).
- Skips repeats among its own downloads (picture fingerprint).
- Keeps only images big enough to print, and shrinks huge ones to 9,000 px.
- Saves progress in Drive after every few images, so a stop never loses work.
- Stops by itself when Drive free space falls below FLOOR_GB.
- Pause: set the repository variable PAUSED to 1, or disable the workflow.
"""
import csv
import gzip
import hashlib
import io
import json
import os
import re
import signal
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image
import imagehash

Image.MAX_IMAGE_PIXELS = None

ROOT_NAME = "Neverland Museum Intake"
UA = "NeverlandMuseumIntake/1.0 (open-access download; contact via GitHub msfits20)"
TIER_W, TIER_H = 2900, 4060
MIN_LONG = int(os.environ.get("MIN_LONG_PX", "2400"))
MAX_LONG = int(os.environ.get("MAX_LONG_PX", "9000"))
FLOOR_GB = float(os.environ.get("FLOOR_GB", "150"))
RUN_SECONDS = int(os.environ.get("RUN_MINUTES", "340")) * 60
RUN_MAX_GB = float(os.environ.get("RUN_MAX_GB", "180"))
WORKERS = int(os.environ.get("WORKERS", "4"))
SOURCES = os.environ.get("SOURCES", "nga,met,cleveland,aic").split(",")
DRY = os.environ.get("DRY_RUN") == "1"
LIMIT = int(os.environ.get("LIMIT", "0"))  # 0 = no limit (test runs use 50)

T0 = time.time()
STOP = threading.Event()
LOCK = threading.Lock()
S = requests.Session()
S.headers["User-Agent"] = UA


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def on_signal(*_):
    log("Stop requested - finishing current images and saving progress")
    STOP.set()


signal.signal(signal.SIGTERM, on_signal)
signal.signal(signal.SIGINT, on_signal)


# ------------------------------------------------------------- matching --
def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"_\d+x\d+(\.\w+)?$", "", s)
    s = re.sub(r"\.(jpe?g|png|tiff?|webp)$", "", s)
    s = re.sub(r"\b(1[0-9]{3}|20[0-2][0-9])\b", "", s)
    return " ".join(re.findall(r"[a-z0-9]+", s))


SKIP_WORDS = {"after", "attributed", "to", "workshop", "of", "follower", "circle",
              "school", "style", "the", "elder", "younger", "i", "ii", "iii", "jr",
              "sr", "and", "imitator", "possibly", "manner", "copy", "studio"}


def surname(artist):
    t = [x for x in norm(re.sub(r"\(.*?\)", "", str(artist or ""))).split() if x not in SKIP_WORDS]
    return t[-1] if t else ""


def keys(artist, title):
    """Two checks: full artist name + title, and artist surname + title."""
    t = norm(title)
    return [hashlib.sha1((norm(artist) + "|" + t).encode()).hexdigest()[:12],
            hashlib.sha1((surname(artist) + "|" + t).encode()).hexdigest()[:12]]


def safe(s, n=150):
    s = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", str(s or "")).strip()
    s = re.sub(r"\s+", " ", s)
    return s[:n].strip() or "Untitled"


def year_of(s):
    m = re.search(r"\b(1[0-9]{3}|20[0-2][0-9])\b", str(s or ""))
    return m.group(1) if m else ""


# ---------------------------------------------------------------- drive --
class Drive:
    API = "https://www.googleapis.com/drive/v3"
    UP = "https://www.googleapis.com/upload/drive/v3/files"

    def __init__(self):
        auth = json.loads(os.environ["GOOGLE_AUTH"])
        self.auth = auth
        self.tok, self.exp = None, 0
        self.folders = {}
        self.flock = threading.Lock()

    def token(self):
        with self.flock:
            if time.time() < self.exp - 120:
                return self.tok
            r = requests.post("https://oauth2.googleapis.com/token", data={
                "client_id": self.auth["client_id"],
                "client_secret": self.auth["client_secret"],
                "refresh_token": self.auth["refresh_token"],
                "grant_type": "refresh_token"}, timeout=60)
            if r.status_code != 200:
                sys.exit("Google sign-in failed (%s). Run the setup again." % r.text[:200])
            j = r.json()
            self.tok, self.exp = j["access_token"], time.time() + j.get("expires_in", 3600)
            return self.tok

    def req(self, method, url, **kw):
        for i in range(6):
            h = kw.pop("headers", {})
            h["Authorization"] = "Bearer " + self.token()
            try:
                r = S.request(method, url, headers=h, timeout=300, **kw)
            except requests.RequestException:
                time.sleep(2 ** i)
                continue
            if r.status_code in (429, 500, 502, 503, 504) or (
                    r.status_code == 403 and "rate" in r.text.lower()):
                time.sleep(2 ** i + 1)
                continue
            if r.status_code == 403 and "uploadLimitExceeded" in r.text:
                log("Google's daily upload limit reached - stopping for today")
                STOP.set()
            return r
        return r

    def free_gb(self):
        q = self.req("GET", self.API + "/about", params={"fields": "storageQuota"}).json()["storageQuota"]
        if "limit" not in q:
            return 1e9
        return (int(q["limit"]) - int(q["usage"])) / 1e9

    def find(self, name, parent, folder=False):
        q = ["name = '%s'" % name.replace("\\", "\\\\").replace("'", "\\'"),
             "trashed = false", "'%s' in parents" % parent]
        if folder:
            q.append("mimeType = 'application/vnd.google-apps.folder'")
        r = self.req("GET", self.API + "/files", params={
            "q": " and ".join(q), "fields": "files(id)", "pageSize": 1}).json()
        f = r.get("files") or []
        return f[0]["id"] if f else None

    def folder(self, path):
        """path like ('Artist', 'Above 2900x4060'); created under the root."""
        with self.flock:
            if path in self.folders:
                return self.folders[path]
        parent = self.root if path else "root"
        cur = ()
        for part in path:
            cur = cur + (part,)
            with self.flock:
                fid = self.folders.get(cur)
            if not fid:
                fid = self.find(part, parent, folder=True)
                if not fid:
                    fid = self.req("POST", self.API + "/files", json={
                        "name": part, "parents": [parent],
                        "mimeType": "application/vnd.google-apps.folder"},
                        params={"fields": "id"}).json()["id"]
                with self.flock:
                    self.folders[cur] = fid
            parent = fid
        return parent

    def setup_root(self):
        r = self.req("GET", self.API + "/files", params={
            "q": "name = '%s' and 'root' in parents and trashed = false and "
                 "mimeType = 'application/vnd.google-apps.folder'" % ROOT_NAME,
            "fields": "files(id)"}).json()
        f = r.get("files") or []
        if f:
            self.root = f[0]["id"]
        else:
            self.root = self.req("POST", self.API + "/files", json={
                "name": ROOT_NAME, "mimeType": "application/vnd.google-apps.folder"},
                params={"fields": "id"}).json()["id"]
        self.folders[()] = self.root
        self.state_dir = self.folder(("_progress",))

    def upload(self, name, parent, data, mime="image/jpeg"):
        r = self.req("POST", self.UP, params={"uploadType": "resumable", "fields": "id"},
                     json={"name": name, "parents": [parent]},
                     headers={"X-Upload-Content-Type": mime,
                              "X-Upload-Content-Length": str(len(data))})
        loc = r.headers.get("Location")
        if not loc:
            raise RuntimeError("upload start failed: %s" % r.text[:200])
        for i in range(5):
            try:
                r2 = S.put(loc, data=data, headers={"Content-Type": mime}, timeout=900)
                if r2.status_code in (200, 201):
                    return r2.json()["id"]
            except requests.RequestException:
                pass
            time.sleep(2 ** i)
        raise RuntimeError("upload failed for %s" % name)

    def read_json(self, name, default):
        fid = self.find(name, self.state_dir)
        if not fid:
            return default
        r = self.req("GET", self.API + "/files/" + fid, params={"alt": "media"})
        return r.json() if r.status_code == 200 else default

    def write_json(self, name, obj):
        data = json.dumps(obj).encode()
        fid = self.find(name, self.state_dir)
        if fid:
            self.req("PATCH", self.UP + "/" + fid, params={"uploadType": "media"},
                     data=data, headers={"Content-Type": "application/json"})
        else:
            self.upload(name, self.state_dir, data, "application/json")


# -------------------------------------------------------------- sources --
def get_json(url, **kw):
    for i in range(6):
        try:
            r = S.get(url, timeout=120, **kw)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
        except (requests.RequestException, ValueError):
            pass
        time.sleep(2 ** i)
    return None


def download_csv(url, path):
    if not os.path.exists(path):
        log("Downloading list", url)
        with S.get(url, stream=True, timeout=600) as r:
            r.raise_for_status()
            with open(path, "wb") as fh:
                for c in r.iter_content(1 << 20):
                    fh.write(c)
    return path


WALL_RANK = {"painting": 0, "drawing": 1, "watercolor": 1, "pastel": 1,
             "poster": 2, "print": 3}


def src_nga():
    csv.field_size_limit(10 ** 9)
    o = download_csv("https://github.com/NationalGalleryOfArt/opendata/raw/main/data/objects.csv", "/tmp/nga_o.csv")
    i = download_csv("https://github.com/NationalGalleryOfArt/opendata/raw/main/data/published_images.csv", "/tmp/nga_i.csv")
    imgs = {}
    for r in csv.DictReader(open(i, encoding="utf-8")):
        if r["viewtype"] == "primary" and r["openaccess"] == "1":
            imgs[r["depictstmsobjectid"]] = r
    rows = []
    for r in csv.DictReader(open(o, encoding="utf-8")):
        c = r["classification"].lower()
        if c not in ("painting", "drawing", "print"):
            continue
        im = imgs.get(r["objectid"])
        if not im:
            continue
        w, h = int(float(im["width"] or 0)), int(float(im["height"] or 0))
        rows.append((WALL_RANK[c], {
            "src": "nga", "id": r["objectid"], "artist": r["attribution"],
            "title": r["title"], "date": r["displaydate"], "kind": c,
            "w": w, "h": h,
            "url": im["iiifurl"] + "/full/!%d,%d/0/default.jpg" % (MAX_LONG, MAX_LONG),
            "page": "https://www.nga.gov/collection/art-object-page.%s.html" % r["objectid"],
            "license": "CC0 (NGA open access)"}))
    rows.sort(key=lambda x: x[0])
    for _, x in rows:
        yield x


MET_OK = re.compile(r"^(Paintings|Drawings|Prints|Watercolors|Pastels|Posters|Miniatures|Ukiyo-e)", re.I)


def src_met():
    p = download_csv("https://media.githubusercontent.com/media/metmuseum/openaccess/master/MetObjects.csv", "/tmp/met.csv")
    rows = []
    for r in csv.DictReader(open(p, encoding="utf-8-sig")):
        if r.get("Is Public Domain") != "True":
            continue
        c = (r.get("Classification") or "").split("|")[0].strip()
        if not MET_OK.match(c):
            continue
        k = c.lower()
        rank = 0 if k.startswith("painting") else 1 if k.startswith(("drawing", "watercolor", "pastel", "miniature")) else 2 if k.startswith("poster") else 3
        rows.append((rank, r["Object ID"], r.get("Artist Display Name") or "", r.get("Title") or "", r.get("Object Date") or "", c))
    rows.sort(key=lambda x: x[0])
    for rank, oid, artist, title, date, c in rows:
        yield {"src": "met", "id": oid, "artist": artist, "title": title, "date": date,
               "kind": c, "w": 0, "h": 0, "url": None,
               "page": "https://www.metmuseum.org/art/collection/search/%s" % oid,
               "license": "CC0 (Met Open Access)"}


def resolve_met(it):
    j = get_json("https://collectionapi.metmuseum.org/public/collection/v1/objects/" + it["id"])
    time.sleep(0.05)
    if not j or not j.get("isPublicDomain") or not j.get("primaryImage"):
        return None
    it["url"] = j["primaryImage"]
    return it


def src_cleveland():
    types = ["Painting", "Drawing", "Print"]
    for t in types:
        skip = 0
        while not STOP.is_set():
            j = get_json("https://openaccess-api.clevelandart.org/api/artworks/", params={
                "cc0": 1, "has_image": 1, "type": t, "limit": 500, "skip": skip,
                "fields": "id,title,creators,creation_date,images,url,type"})
            data = (j or {}).get("data") or []
            if not data:
                break
            for r in data:
                im = (r.get("images") or {})
                best = im.get("full") or im.get("print") or {}
                if not best.get("url"):
                    continue
                cr = r.get("creators") or []
                artist = re.sub(r"\s*\(.*$", "", cr[0]["description"]) if cr else ""
                yield {"src": "cleveland", "id": str(r["id"]), "artist": artist,
                       "title": r.get("title"), "date": r.get("creation_date") or "",
                       "kind": t.lower(), "w": int(best.get("width") or 0),
                       "h": int(best.get("height") or 0), "url": best["url"],
                       "page": r.get("url"), "license": "CC0 (Cleveland Museum of Art)"}
            skip += len(data)


def src_aic():
    types = ["Painting", "Drawing and Watercolor", "Print", "Poster"]
    for t in types:
        page = 1
        while not STOP.is_set() and page <= 1000:
            j = get_json("https://api.artic.edu/api/v1/artworks/search", params={
                "query[bool][must][0][term][is_public_domain]": "true",
                "query[bool][must][1][term][artwork_type_title.keyword]": t,
                "fields": "id,title,artist_title,date_display,image_id,thumbnail",
                "limit": 100, "page": page},
                headers={"AIC-User-Agent": UA})
            data = (j or {}).get("data") or []
            if not data:
                break
            for r in data:
                if not r.get("image_id"):
                    continue
                th = r.get("thumbnail") or {}
                yield {"src": "aic", "id": str(r["id"]), "artist": r.get("artist_title") or "",
                       "title": r.get("title"), "date": r.get("date_display") or "",
                       "kind": t.lower(), "w": int(th.get("width") or 0), "h": int(th.get("height") or 0),
                       "url": "https://www.artic.edu/iiif/2/%s/full/full/0/default.jpg" % r["image_id"],
                       "page": "https://www.artic.edu/artworks/%s" % r["id"],
                       "license": "CC0 (Art Institute of Chicago)"}
            page += 1
            time.sleep(1)


SRC = {"nga": src_nga, "met": src_met, "cleveland": src_cleveland, "aic": src_aic}


# ----------------------------------------------------------------- main --
class Run:
    def __init__(self, drive, known):
        self.d, self.known = drive, known
        self.state = {}
        self.phash = set()
        self.log = []
        self.bytes = 0
        self.saved = self.skipped = self.small = self.failed = self.dupes = 0
        self.since_save = 0

    def load(self):
        st = self.d.read_json("state.json", {})
        self.state = {k: set(v) for k, v in st.get("done", {}).items()}
        self.phash = set(st.get("phash", []))
        self.keys_new = set(st.get("keys", []))

    def save(self):
        with LOCK:
            obj = {"done": {k: sorted(v) for k, v in self.state.items()},
                   "phash": sorted(self.phash), "keys": sorted(self.keys_new),
                   "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
            logs, self.log = self.log, []
        self.d.write_json("state.json", obj)
        if logs:
            name = "log-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S")
            self.d.upload(name, self.d.state_dir, "\n".join(json.dumps(x) for x in logs).encode(), "application/json")

    def should_stop(self):
        if STOP.is_set():
            return True
        if time.time() - T0 > RUN_SECONDS:
            log("Time is up for this run")
            STOP.set()
        elif self.bytes / 1e9 > RUN_MAX_GB:
            log("Run upload cap reached")
            STOP.set()
        elif LIMIT and self.saved >= LIMIT:
            STOP.set()
        return STOP.is_set()

    def handle(self, it):
        if self.should_stop():
            return
        done = self.state.setdefault(it["src"], set())
        if it["id"] in done:
            return
        ks = keys(it["artist"], it["title"])
        k = ks[1]
        if any(x in self.known or x in self.keys_new for x in ks):
            with LOCK:
                done.add(it["id"])
                self.skipped += 1
            return
        if it["w"] and it["h"] and max(it["w"], it["h"]) < MIN_LONG:
            with LOCK:
                done.add(it["id"])
                self.small += 1
            return
        if it["src"] == "met":
            it = resolve_met(it)
            if not it:
                return  # no public image right now; retried on a later run
        if DRY:
            with LOCK:
                self.saved += 1
            return
        try:
            r = S.get(it["url"], timeout=600)
            if r.status_code != 200:
                raise RuntimeError("HTTP %s" % r.status_code)
            im = Image.open(io.BytesIO(r.content))
            im.load()
        except Exception as e:
            with LOCK:
                self.failed += 1
            log("  failed", it["src"], it["id"], str(e)[:80])
            return
        w, h = im.size
        if max(w, h) < MIN_LONG:
            with LOCK:
                done.add(it["id"])
                self.small += 1
            return
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        data = r.content
        if max(w, h) > MAX_LONG or not r.headers.get("Content-Type", "").startswith("image/jpeg"):
            if max(w, h) > MAX_LONG:
                s = MAX_LONG / max(w, h)
                im = im.resize((round(w * s), round(h * s)), Image.LANCZOS)
                w, h = im.size
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=92, optimize=True)
            data = buf.getvalue()
        ph = str(imagehash.phash(im.convert("L").resize((256, 256))))
        with LOCK:
            if ph in self.phash:
                done.add(it["id"])
                self.dupes += 1
                return
            self.phash.add(ph)
        artist = safe(it["artist"] or "Unknown Artist", 80)
        tier = "Above %dx%d" % (TIER_W, TIER_H) if (min(w, h) >= TIER_W and max(w, h) >= TIER_H) else "Below %dx%d" % (TIER_W, TIER_H)
        y = year_of(it["date"])
        title = safe(it["title"], 140)
        name = "%s%s_%dx%d.jpg" % (title, (" " + y) if y and y not in title else "", w, h)
        parent = self.d.folder((artist, tier))
        if not self.d.find(name, parent):
            self.d.upload(name, parent, data)
        with LOCK:
            done.add(it["id"])
            self.keys_new.update(ks)
            self.bytes += len(data)
            self.saved += 1
            self.since_save += 1
            self.log.append({k2: it[k2] for k2 in ("src", "id", "artist", "title", "date", "kind", "page", "license")} |
                            {"file": "%s/%s/%s" % (artist, tier, name), "w": w, "h": h, "phash": ph, "bytes": len(data)})
            due = self.since_save >= 25
            if due:
                self.since_save = 0
        if due:
            self.save()
            log("  saved %d | skipped (already have) %d | too small %d | repeats %d | failed %d | %.1f GB"
                % (self.saved, self.skipped, self.small, self.dupes, self.failed, self.bytes / 1e9))


def main():
    if os.environ.get("PAUSED") == "1":
        log("Paused (repository variable PAUSED=1). Nothing to do.")
        return
    with gzip.open(os.path.join(os.path.dirname(__file__), "known_keys.txt.gz"), "rt") as fh:
        known = set(fh.read().split())
    log("Catalog check list: %d artworks" % len(known))
    d = Drive()
    if not DRY:
        free = d.free_gb()
        log("Drive free space: %.0f GB (stops at %.0f GB)" % (free, FLOOR_GB))
        if free < FLOOR_GB:
            log("Not enough Drive space - not starting.")
            return
    d.setup_root()
    run = Run(d, known)
    run.load()
    last_check = time.time()
    try:
        with ThreadPoolExecutor(WORKERS) as ex:
            for s in SOURCES:
                if STOP.is_set():
                    break
                log("Source:", s)
                futs = []
                for it in SRC[s]():
                    if run.should_stop():
                        break
                    futs.append(ex.submit(run.handle, it))
                    if len(futs) > WORKERS * 4:
                        futs.pop(0).result()
                    if not DRY and time.time() - last_check > 600:
                        last_check = time.time()
                        if d.free_gb() < FLOOR_GB:
                            log("Drive free space below the floor - stopping.")
                            STOP.set()
                for f in futs:
                    f.result()
    finally:
        run.save()
        log("DONE this run: saved %d | skipped (already have) %d | too small %d | repeats %d | failed %d | %.1f GB"
            % (run.saved, run.skipped, run.small, run.dupes, run.failed, run.bytes / 1e9))


if __name__ == "__main__":
    main()
