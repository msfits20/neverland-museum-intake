#!/usr/bin/env python3
"""
Neverland museum intake.

Downloads open-access wall art (paintings, drawings, prints, posters) from
museum collections into its own Google Drive folder, "Neverland Museum
Intake". The Google permission it uses (drive.file) only lets it see files
it created itself, so it cannot see or touch any other folder.

- Skips anything already in the Neverland Prints catalog (artist + title).
- Skips repeats among its own downloads (picture fingerprint).
- Skips look-alikes of catalog images (picture fingerprint vs the live
  catalog), except when our copy is weak and the new one prints bigger:
  those are kept as upgrades (logged with "upgrade_for").
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

import catalog as catalog_mod

Image.MAX_IMAGE_PIXELS = None

ROOT_NAME = "Neverland Museum Intake"
UA = "NeverlandMuseumIntake/1.0 (open-access download; contact via GitHub msfits20)"
TIER_W, TIER_H = 2900, 4060
MIN_LONG = int(os.environ.get("MIN_LONG_PX", "2400"))
MAX_LONG = int(os.environ.get("MAX_LONG_PX", "9000"))
FLOOR_GB = float(os.environ.get("FLOOR_GB", "150"))
RUN_SECONDS = int(os.environ.get("RUN_MINUTES", "340")) * 60
RUN_MAX_GB = float(os.environ.get("RUN_MAX_GB", "180"))
WORKERS = int(os.environ.get("WORKERS", "8"))
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
# 16 Nov 2026: each museum split into SHARD_N parts that run side by side (part SHARD_I of them)
SHARD_N = int(os.environ.get("SHARD_N", "1"))
SHARD_I = int(os.environ.get("SHARD_I", "0"))
PARENT_STATE = os.environ.get("PARENT_STATE", "")   # the museum's single-part state file, to start from
SOURCES = os.environ.get("SOURCES", "nga,met,cleveland,aic").split(",")
DRY = os.environ.get("DRY_RUN") == "1"
LIMIT = int(os.environ.get("LIMIT", "0"))  # 0 = no limit (test runs use 50)

T0 = time.time()
STOP = threading.Event()
AGAIN = {"yes": False}   # set when a run ends only because its time ran out
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


WARN_GB = float(os.environ.get("WARN_GB", "400"))


def warn_low_space(free):
    """Open one GitHub issue when Drive space runs low, so Tobi gets an email in time
    to upgrade storage before the job pauses at FLOOR_GB."""
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not tok or not repo or free >= WARN_GB or (len(SOURCES) == 1 and SOURCES[0] != "nga"):
        return
    title = "Drive space low: museum downloads will pause soon"
    h = {"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json"}
    try:
        r = requests.get("https://api.github.com/repos/%s/issues" % repo,
                         params={"state": "open", "per_page": 100}, headers=h, timeout=30)
        if any(i.get("title") == title for i in r.json()):
            return
        requests.post("https://api.github.com/repos/%s/issues" % repo, headers=h, timeout=30, json={
            "title": title,
            "body": ("Google Drive has %.0f GB free. The museum download pauses by itself at %.0f GB.\n\n"
                     "Nothing to do: it stops by itself at the floor. If you add storage later, the job "
                     "carries on at its next run." % (free, FLOOR_GB))})
        log("Low-space warning issue opened")
    except Exception as e:
        log("Low-space warning failed:", str(e)[:120])


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
            "url": None, "iiif": im["iiifurl"],
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
                       "url": None, "iiif": "https://www.artic.edu/iiif/2/%s" % r["image_id"],
                       "page": "https://www.artic.edu/artworks/%s" % r["id"],
                       "license": "CC0 (Art Institute of Chicago)"}
            page += 1
            time.sleep(1)


# ----------------------------------------------------------- Rijksmuseum --
# 17 Nov 2026. Data Services search API (no key) -> Linked Art object records.
# Images on iiif.micr.io, public domain (PDM/CC0). Order: paintings, posters,
# drawings, prints (the Artvee Dutch titles mostly come from here).
RIJKS_TYPES = [t for t in os.environ.get("RIJKS_TYPES", "painting,poster,drawing,print").split(",") if t]
EN = "http://vocab.getty.edu/aat/300388277"
LA = {"Accept": "application/ld+json", "Profile": "https://linked.art/ns/v1/linked-art.json"}
_rijks_actor = {}


def src_rijks():
    for t in RIJKS_TYPES:
        url = "https://data.rijksmuseum.nl/search/collection?type=%s&imageAvailable=true" % t
        while url and not STOP.is_set():
            j = get_json(url)
            if not j:
                break
            for o in j.get("orderedItems") or []:
                oid = o.get("id", "").rsplit("/", 1)[-1]
                if oid:
                    yield {"src": "rijks", "id": oid, "artist": None, "title": None, "date": "",
                           "kind": t, "w": 0, "h": 0, "url": None,
                           "page": "https://id.rijksmuseum.nl/" + oid,
                           "license": "Public domain (Rijksmuseum)"}
            nxt = j.get("next")
            url = nxt.get("id") if isinstance(nxt, dict) else nxt


def _walk(x):
    if isinstance(x, dict):
        yield x
        for v in x.values():
            yield from _walk(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk(v)


def _names(node):
    """Name contents in node.identified_by, English first."""
    out = []
    for n in (node or {}).get("identified_by") or []:
        if n.get("type") != "Name" or not n.get("content"):
            continue
        lang = [l.get("id") for l in n.get("language") or [] if isinstance(l, dict)]
        out.append((0 if EN in lang else 1, n["content"]))
    return [c for _, c in sorted(out, key=lambda x: x[0])]


def _label(node):
    for k in ("_label",):
        if isinstance(node.get(k), str):
            return node[k]
    for n in node.get("notation") or []:
        if isinstance(n, dict) and n.get("@value") and n.get("@language") in ("en", None):
            return n["@value"]
    nm = _names(node)
    return nm[0] if nm else None


def _actor_name(aid):
    if aid in _rijks_actor:
        return _rijks_actor[aid]
    nm = None
    j = get_json(aid, headers=LA)
    if j:
        nm = _label(j)
    _rijks_actor[aid] = nm
    return nm


MICRIO = re.compile(r"https://iiif\.micr\.io/([A-Za-z0-9]+)/")


def _find_micrio(j):
    for d in _walk(j):
        for v in d.values():
            if isinstance(v, str):
                m = MICRIO.match(v)
                if m:
                    return m.group(1)
    return None


def resolve_rijks(it):
    j = get_json("https://id.rijksmuseum.nl/" + it["id"], headers=LA)
    time.sleep(0.05)
    if not j:
        return None
    nm = _names(j)
    it["title"] = nm[0] if nm else None
    artist = None
    pb = j.get("produced_by") or {}
    if isinstance(pb, list):
        pb = pb[0] if pb else {}
    for d in _walk(pb):
        for a in d.get("carried_out_by") or []:
            if not isinstance(a, dict):
                continue
            artist = _label(a) or (_actor_name(a["id"]) if a.get("id") else None)
            if artist:
                break
        if artist:
            break
    it["artist"] = artist or "Anonymous"
    ts = pb.get("timespan") or {}
    if isinstance(ts, list):
        ts = ts[0] if ts else {}
    tn = _names(ts)
    it["date"] = tn[0] if tn else (ts.get("begin_of_the_begin") or "")[:4]
    # the image: in the record, or one hop away through the visual item / digital object
    mid = _find_micrio(j)
    hops = [v.get("id") for v in j.get("shows") or [] if isinstance(v, dict) and v.get("id")]
    while not mid and hops:
        h = get_json(hops.pop(0), headers=LA)
        if not h:
            continue
        mid = _find_micrio(h)
        for d in _walk(h):
            for v in d.get("digitally_shown_by") or []:
                if isinstance(v, dict) and v.get("id"):
                    hops.append(v["id"])
        if len(hops) > 6:
            break
    if not mid or not it["title"]:
        return None
    it["iiif"] = "https://iiif.micr.io/%s" % mid
    return it


# ------------------------------------------------------------------ SMK --
# 17 Nov 2026. Statens Museum for Kunst (Denmark) open API, no key; public-domain
# works with images (~39.5K). Wall art only: paintings, drawings, prints, watercolours.
SMK_OK = re.compile(r"paint|draw|print|etch|engrav|lithograph|woodcut|xylograph|watercol|gouache|pastel|poster|mezzotint|aquatint", re.I)


def src_smk():
    offset, rows = 0, 100
    while not STOP.is_set():
        j = get_json("https://api.smk.dk/api/v1/art/search/", params={
            "keys": "*", "filters": "[has_image:true],[public_domain:true]",
            "offset": offset, "rows": rows, "lang": "en"})
        items = (j or {}).get("items") or []
        if not items:
            break
        for r in items:
            names = " ".join(r.get("object_names") and [n.get("name", "") if isinstance(n, dict) else str(n)
                                                        for n in r["object_names"]] or [])
            if not SMK_OK.search(names) or not r.get("image_native"):
                continue
            t = (r.get("titles") or [{}])[0].get("title") or ""
            pr = (r.get("production") or [{}])[0]
            artist = " ".join(x for x in (pr.get("creator_forename"), pr.get("creator_surname")) if x) \
                or pr.get("creator") or "Anonymous"
            pd = (r.get("production_date") or [{}])[0]
            yield {"src": "smk", "id": str(r.get("object_number") or r.get("id")), "artist": artist,
                   "title": t, "date": pd.get("period") or "", "kind": names.split(";")[0].lower(),
                   "w": int(r.get("image_width") or 0), "h": int(r.get("image_height") or 0),
                   "url": r["image_native"], "page": r.get("frontend_url"),
                   "license": "Public domain (SMK, National Gallery of Denmark)"}
        offset += len(items)
        time.sleep(0.3)


# ------------------------------------------- Finnish National Gallery --
# 18 Nov 2026. One API call returns the whole collection (~89k records); keep artworks with a
# CC0 image of print size. Images: kokoelma.kansallisgalleria.fi/media-assets/... (4000 px max).
FNG_HOST = "https://kokoelma.kansallisgalleria.fi"


def src_fng():
    j = get_json("https://www.kansallisgalleria.fi/api/v1/objects") or []
    rows = []
    for o in j:
        if ((o.get("category") or {}).get("categoryId") or "") != "artwork":
            continue
        best = None
        for m in o.get("multimedia") or []:
            if m.get("license") != "CC0" or not (m.get("jpg") or {}).get("4000"):
                continue
            if not best or (m.get("width") or 0) * (m.get("height") or 0) > (best.get("width") or 0) * (best.get("height") or 0):
                best = m
        if not best or max(best.get("width") or 0, best.get("height") or 0) < MIN_LONG:
            continue
        t = o.get("title") or {}
        title = t.get("en") or t.get("fi") or t.get("sv") or ""
        ppl = [p for p in o.get("people") or [] if ((p.get("role") or {}).get("en") or "").lower() in ("artist", "")]
        artist = " ".join(x for x in ((ppl[0].get("firstName"), ppl[0].get("familyName")) if ppl else ()) if x) or "Anonymous"
        y1, y2 = o.get("yearFrom"), o.get("yearTo")
        date = str(y1) if y1 and (not y2 or y2 == y1) else ("%s-%s" % (y1, y2) if y1 and y2 else "")
        rows.append({"src": "fng", "id": str(o["objectId"]), "artist": artist, "title": title, "date": date,
                     "kind": "artwork", "w": int(best.get("width") or 0), "h": int(best.get("height") or 0),
                     "url": FNG_HOST + best["jpg"]["4000"],
                     "page": "https://www.kansallisgalleria.fi/en/object/%s" % o["objectId"],
                     "license": "CC0 (Finnish National Gallery)"})
    log("Finnish National Gallery: %d CC0 artworks of print size" % len(rows))
    for x in rows:
        yield x


# ------------------------------------------- Yale Center for British Art --
# 18 Nov 2026. OAI-PMH (LIDO) lists Paintings and Prints & Drawings; each record's IIIF manifest
# gives title, creator, date, copyright and the image service. Public Domain only.
YCBA_OAI = "https://harvester-bl.britishart.yale.edu/oaicatmuseum/OAIHandler"
YCBA_SETS = [x for x in os.environ.get("YCBA_SETS", "ycba:ps,ycba:pd").split(",") if x]


def src_ycba():
    for st in YCBA_SETS:
        url, params = YCBA_OAI, {"verb": "ListRecords", "metadataPrefix": "lido", "set": st}
        while not STOP.is_set():
            txt = None
            for i in range(6):
                try:
                    r = S.get(url, params=params, timeout=180)
                    if r.status_code == 200:
                        txt = r.text
                        break
                except requests.RequestException:
                    pass
                time.sleep(2 ** i)
            if not txt:
                break
            for rec in txt.split("<record>")[1:]:
                if 'status="deleted"' in rec:
                    continue
                oid = re.search(r"<identifier>oai:tms\.ycba\.yale\.edu:(\d+)</identifier>", rec)
                man = re.search(r"<lido:linkResource[^>]*>(https://manifests\.collections\.yale\.edu/v2/ycba/obj/\d+)</lido:linkResource>", rec)
                kind = re.search(r"<lido:classification>.*?<lido:term>([^<]+)</lido:term>", rec, re.S)
                if not (oid and man):
                    continue
                k = (kind.group(1) if kind else "").lower()
                if k and not re.search(r"paint|drawing|print|watercolo|pastel|poster", k):
                    continue
                yield {"src": "ycba", "id": oid.group(1), "artist": None, "title": None, "date": "",
                       "kind": k or st, "w": 0, "h": 0, "url": None, "manifest": man.group(1),
                       "page": "https://collections.britishart.yale.edu/catalog/tms:%s" % oid.group(1),
                       "license": "Public domain (Yale Center for British Art)"}
            tok = re.search(r"<resumptionToken[^>]*>([^<]+)</resumptionToken>", txt)
            if not tok:
                break
            # the token is already URL-encoded (e.g. ycba%3Aps); encoding it again makes the server fail
            url, params = YCBA_OAI + "?verb=ListRecords&resumptionToken=" + tok.group(1), None


def resolve_ycba(it):
    m = get_json(it["manifest"])
    if not m:
        return None
    md = {x.get("label"): x.get("value") for x in m.get("metadata") or [] if isinstance(x, dict)}
    if "public domain" not in str(md.get("Copyright Statement") or "").lower():
        return None
    try:
        res = m["sequences"][0]["canvases"][0]["images"][0]["resource"]
    except (KeyError, IndexError, TypeError):
        return None
    svc = (res.get("service") or {}).get("@id")
    if not svc:
        return None
    it["artist"] = re.sub(r",?\s*\d{4}\s*[-–]\s*\d{4}.*$", "", str(md.get("Creator") or "Anonymous")).strip()
    it["title"] = str(md.get("Title") or "")
    lab = str(m.get("label") or "")
    y = re.search(r"((?:ca\.\s*)?\d{4}(?:\s*[-–]\s*\d{2,4})?)\s*$", lab)
    it["date"] = y.group(1) if y else ""
    it["w"], it["h"] = int(res.get("width") or 0), int(res.get("height") or 0)
    it["iiif"] = svc
    return it


# ------------------------------------------------- Wellcome Collection --
# 18 Nov 2026. Images API, Public Domain Mark / CC0 only, wall-art genres (prints, paintings,
# drawings, posters). Paged per genre so no single listing gets too deep.
WELLCOME_GENRES = ["Paintings", "Watercolours", "Drawings", "Posters", "Woodcuts", "Engravings", "Etchings",
                   "Lithographs", "Chromolithographs", "Aquatints", "Mezzotints", "Colour prints",
                   "Line engravings", "Stipple engravings", "Gouaches", "Pastels", "Wood engravings"]


def src_wellcome():
    for gname in WELLCOME_GENRES:
        url = "https://api.wellcomecollection.org/catalogue/v2/images"
        params = {"locations.license": "pdm,cc-0", "source.genres.label": gname, "pageSize": 100,
                  "include": "source.genres,source.contributors"}
        while url and not STOP.is_set():
            j = get_json(url, params=params)
            params = None
            if not j:
                break
            for r in j.get("results") or []:
                loc = (r.get("locations") or [{}])[0]
                lic = ((loc.get("license") or {}).get("id") or "")
                if lic not in ("pdm", "cc-0") or not str(loc.get("url", "")).endswith("/info.json"):
                    continue
                src = r.get("source") or {}
                cons = src.get("contributors") or []
                artist = ((cons[0].get("agent") or {}).get("label") if cons else "") or "Anonymous"
                title = re.sub(r"\.\s*(Woodcut|Engraving|Etching|Lithograph|Colour lithograph|Watercolour|Oil painting|Drawing|Aquatint|Mezzotint)[^.]*\.?\s*$",
                               "", str(src.get("title") or "")).strip()
                yield {"src": "wellcome", "id": str(r["id"]), "artist": artist, "title": title,
                       "date": "", "kind": gname.lower(), "w": 0, "h": 0, "url": None,
                       "iiif": loc["url"][:-len("/info.json")],
                       "page": "https://wellcomecollection.org/works/%s" % src.get("id", ""),
                       "license": "Public domain (Wellcome Collection, %s)" % lic.upper()}
            url = j.get("nextPage")
            time.sleep(0.2)


# ----------------------------------------------------------- Smithsonian --
# 19 Nov 2026. Open Access API (api.data.gov key kept in the database: intake_config 'si_api_key').
# CC0 images only, art museums, wall-art object types; high-resolution JPEG download.
SI_UNITS = [x for x in os.environ.get("SI_UNITS", "SAAM,NPG,HMSG,FSG,CHNDM,NMAfA,NMAH,ACAH").split(",") if x]
SI_TYPES = ["Paintings", "Drawings", "Prints", "Watercolors", "Posters", "Pastels (visual works)",
            "Lithographs", "Etchings", "Woodcuts", "Engravings", "Screenprints", "Gouaches"]


def si_key():
    raw = os.environ.get("NEVERLAND_READ")
    if not raw:
        return None
    c = json.loads(raw)
    for i in range(6):
        try:
            r = requests.post(c["db_url"] + "/rest/v1/rpc/intake_get_config",
                              json={"p_token": c["db_token"], "p_key": "si_api_key"},
                              headers={"apikey": c["db_key"], "Content-Type": "application/json"}, timeout=60)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(2 ** i)
    return None


def src_si():
    key = si_key()
    if not key:
        log("Smithsonian: no API key - skipped")
        return
    for unit in SI_UNITS:
        for typ in SI_TYPES:
            q = 'online_media_type:"Images" AND media_usage:CC0 AND unit_code:%s AND object_type:"%s"' % (unit, typ)
            start, total = 0, None
            while not STOP.is_set():
                j = get_json("https://api.si.edu/openaccess/api/v1.0/search",
                             params={"q": q, "rows": 1000, "start": start, "api_key": key})
                resp = (j or {}).get("response") or {}
                rows = resp.get("rows") or []
                if total is None:
                    total = resp.get("rowCount") or 0
                    if total:
                        log("Smithsonian %s / %s: %d records" % (unit, typ, total))
                if not rows:
                    break
                for row in rows:
                    c = row.get("content") or {}
                    ft, dnr = c.get("freetext") or {}, c.get("descriptiveNonRepeating") or {}
                    media = [m for m in ((dnr.get("online_media") or {}).get("media") or [])
                             if m.get("type") == "Images" and (m.get("usage") or {}).get("access") == "CC0"]
                    best = None
                    for m in media:
                        for res in m.get("resources") or []:
                            if res.get("label") == "High-resolution JPEG" and res.get("url"):
                                if not best or (res.get("width") or 0) * (res.get("height") or 0) > (best.get("width") or 0) * (best.get("height") or 0):
                                    best = res
                    if not best or max(best.get("width") or 0, best.get("height") or 0) < MIN_LONG:
                        continue
                    names = [n.get("content", "") for n in ft.get("name") or [] if n.get("label") in ("Artist", "Maker", "Designer", "Printmaker", "Painter")]
                    artist = re.split(r",\s*(?:American|born|b\.|active|English|French|British|German|Dutch|Italian|Japanese|Chinese|\d)", names[0])[0].strip() if names else "Anonymous"
                    date = ((ft.get("date") or [{}])[0].get("content") or "")
                    date = "" if date.lower() in ("n.d.", "nd") else date
                    yield {"src": "si", "id": dnr.get("record_ID") or row.get("id"), "artist": artist or "Anonymous",
                           "title": (dnr.get("title") or {}).get("content") or row.get("title") or "",
                           "date": date, "kind": typ.lower(), "w": int(best.get("width") or 0), "h": int(best.get("height") or 0),
                           "url": best["url"], "page": dnr.get("record_link") or "",
                           "license": "CC0 (Smithsonian, %s)" % unit}
                start += len(rows)
                if start >= total:
                    break


IIIF_TILE = 3800


def iiif_image(base, w=0, h=0):
    """Full-resolution image from a IIIF server that caps output size:
    request it in pieces at full (or MAX_LONG-scaled) resolution and stitch."""
    hdr = {"AIC-User-Agent": UA}
    if not (w and h):
        j = get_json(base + "/info.json", headers=hdr)
        if not j:
            raise RuntimeError("no info.json")
        w, h = int(j["width"]), int(j["height"])
    s = min(1.0, MAX_LONG / max(w, h))
    R = int(IIIF_TILE / s)                    # source pixels per piece
    out = Image.new("RGB", (round(w * s), round(h * s)))
    for y in range(0, h, R):
        for x in range(0, w, R):
            rw, rh = min(R, w - x), min(R, h - y)
            ox, oy = round(x * s), round(y * s)
            ow = max(1, round((x + rw) * s) - ox)
            oh = max(1, round((y + rh) * s) - oy)
            u = "%s/%d,%d,%d,%d/%d,%d/0/default.jpg" % (base, x, y, rw, rh, ow, oh)
            for i in range(5):
                try:
                    r = S.get(u, headers=hdr, timeout=300)
                    if r.status_code == 200:
                        break
                except requests.RequestException:
                    pass
                time.sleep(2 ** i)
            else:
                raise RuntimeError("piece failed")
            piece = Image.open(io.BytesIO(r.content)).convert("RGB")
            if piece.size != (ow, oh):
                piece = piece.resize((ow, oh), Image.LANCZOS)
            out.paste(piece, (ox, oy))
    return out


SRC = {"nga": src_nga, "met": src_met, "cleveland": src_cleveland, "aic": src_aic, "rijks": src_rijks, "smk": src_smk,
       "fng": src_fng, "ycba": src_ycba, "wellcome": src_wellcome, "si": src_si}  # fng/ycba/wellcome added 18 Nov 2026


# ----------------------------------------------------------------- main --
def fetch_image(it):
    """Returns (image, original jpeg bytes or None)."""
    if it.get("iiif"):
        known_wh = it["src"] in ("nga", "ycba")   # sizes from the museum's own record
        return iiif_image(it["iiif"], it.get("w") if known_wh else 0,
                          it.get("h") if known_wh else 0), None
    r = S.get(it["url"], timeout=600)
    if r.status_code != 200:
        raise RuntimeError("HTTP %s" % r.status_code)
    im = Image.open(io.BytesIO(r.content))
    im.load()
    jpeg = r.content if r.headers.get("Content-Type", "").startswith("image/jpeg") else None
    return im, jpeg


def finish(im, data):
    """RGB, at most MAX_LONG px; re-encode only when needed."""
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
        data = None
    w, h = im.size
    if max(w, h) > MAX_LONG:
        s = MAX_LONG / max(w, h)
        im = im.resize((round(w * s), round(h * s)), Image.LANCZOS)
        data = None
    if data is None:
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92, optimize=True)
        data = buf.getvalue()
    return im, data


def tier_of(w, h):
    big = min(w, h) >= TIER_W and max(w, h) >= TIER_H
    return ("Above %dx%d" if big else "Below %dx%d") % (TIER_W, TIER_H)


def file_name(title, date, w, h):
    y = year_of(date)
    t = re.sub(r"\(\s*\)", "", safe(title, 140)).strip() or "Untitled"
    return "%s%s_%dx%d.jpg" % (t, (" " + y) if y and y not in t else "", w, h)


class Run:
    def __init__(self, drive, known, cat=None):
        self.d, self.known, self.cat = drive, known, cat
        self.catdupes = self.upgrades = 0
        self.state = {}
        self.phash = set()
        self.log = []
        self.bytes = 0
        self.saved = self.skipped = self.small = self.failed = self.dupes = 0
        self.since_save = 0

    def load(self):
        # One state file per parallel part (16 Nov 2026); the first time, start from the shared one.
        st = self.d.read_json(STATE_FILE, None)
        if st is None and PARENT_STATE:
            st = self.d.read_json(PARENT_STATE, None)
        if st is None:
            st = self.d.read_json("state.json", {})
        self.state = {k: set(v) for k, v in st.get("done", {}).items()}
        self.phash = set(st.get("phash", []))
        self.keys_new = set(st.get("keys", []))
        self.weakchk = set(st.get("weakchk", []))

    def save(self):
        with LOCK:
            obj = {"done": {k: sorted(v) for k, v in self.state.items()},
                   "phash": sorted(self.phash), "keys": sorted(self.keys_new),
                   "weakchk": sorted(self.weakchk),
                   "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
            logs, self.log = self.log, []
        self.d.write_json(STATE_FILE, obj)
        if logs:
            name = "log-%s-%s.jsonl" % (time.strftime("%Y%m%d-%H%M%S"), STATE_FILE.replace(".json", ""))
            self.d.upload(name, self.d.state_dir, "\n".join(json.dumps(x) for x in logs).encode(), "application/json")

    def should_stop(self):
        if STOP.is_set():
            return True
        if time.time() - T0 > RUN_SECONDS:
            log("Time is up for this run - the next run will carry on")
            AGAIN["yes"] = True
            STOP.set()
        elif self.bytes / 1e9 > RUN_MAX_GB:
            log("Run upload cap reached")
            STOP.set()
        elif LIMIT and self.saved >= LIMIT:
            STOP.set()
        return STOP.is_set()

    def handle(self, it):
        """One bad image (upload hiccup, odd file) must never stop the run."""
        try:
            self._handle(it)
        except Exception as e:
            with LOCK:
                self.failed += 1
            log("  failed", it.get("src"), it.get("id"), str(e)[:100])

    def _handle(self, it):
        if self.should_stop():
            return
        if SHARD_N > 1 and int(hashlib.md5((it["src"] + ":" + str(it["id"])).encode()).hexdigest(), 16) % SHARD_N != SHARD_I:
            return  # another part of this museum handles it
        done = self.state.setdefault(it["src"], set())
        if it["src"] == "rijks" and it["title"] is None:
            if it["id"] in done and not os.environ.get("RECHECK"):
                return
            it = resolve_rijks(it)
            if not it:
                return  # no public image (or record) right now; retried on a later run
        if it["src"] == "ycba" and it["title"] is None:
            if it["id"] in done and not os.environ.get("RECHECK"):
                return
            it = resolve_ycba(it)
            if not it:
                return  # not public domain / no image; retried on a later run
        ks = keys(it["artist"], it["title"])
        weak_hit = bool(self.cat) and any(x in self.cat.weak_keys for x in ks)
        tag = it["src"] + ":" + it["id"]
        if it["id"] in done:
            # skipped earlier by name only; look once more if our copy is weak
            if not (weak_hit and tag not in self.weakchk and not any(x in self.keys_new for x in ks)):
                return
            with LOCK:
                self.weakchk.add(tag)
        elif weak_hit:
            with LOCK:
                self.weakchk.add(tag)
        if any(x in self.keys_new for x in ks) or (
                not weak_hit and any(x in self.known for x in ks)):
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
            im, data = fetch_image(it)
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
        im, data = finish(im, data)
        w, h = im.size
        ph = str(imagehash.phash(im.convert("L").resize((256, 256))))
        cph = match = None
        if self.cat:
            try:
                cph = str(imagehash.phash(im))      # same method as the catalog scan
                match = self.cat.best(cph, surname(it["artist"]), catalog_mod.title_words(norm(it["title"])), im)
            except Exception as e:
                log("  catalog check skipped:", str(e)[:80])
        if match and not catalog_mod.upgrade_over(match, w, h):
            with LOCK:          # logged (not saved) so every skip can be checked later
                done.add(it["id"])
                self.catdupes += 1
                self.log.append({k2: it.get(k2) for k2 in ("src", "id", "artist", "title", "date", "kind", "page")} |
                                {"skipped": "lookalike", "w": w, "h": h, "cphash": cph, "match": match})
            return
        if weak_hit and not match and any(x in self.known for x in ks):
            with LOCK:          # same name as a weak catalog item, but not the same picture
                done.add(it["id"])
                self.skipped += 1
            return
        with LOCK:
            if ph in self.phash:
                done.add(it["id"])
                self.dupes += 1
                return
            self.phash.add(ph)
        artist = safe(it["artist"] or "Unknown Artist", 80)
        tier = tier_of(w, h)
        name = file_name(it["title"], it["date"], w, h)
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
                            {"file": "%s/%s/%s" % (artist, tier, name), "w": w, "h": h, "phash": ph, "bytes": len(data),
                             "cphash": cph, "upgrade_for": match["id"] if match else None})
            if match:
                self.upgrades += 1
            due = self.since_save >= 25
            if due:
                self.since_save = 0
        if due:
            self.save()
            log("  saved %d (upgrades %d) | skipped (already have) %d | look-alikes %d | too small %d | repeats %d | failed %d | %.1f GB"
                % (self.saved, self.upgrades, self.skipped, self.catdupes, self.small, self.dupes, self.failed, self.bytes / 1e9))


def upgrade_capped(d):
    """One-off: the first NGA test files came down capped at 4,096 px.
    Replace their content in place with the full-resolution version."""
    nga = {x["id"]: x for x in src_nga()}
    r = d.req("GET", d.API + "/files", params={
        "q": "'%s' in parents and name contains 'log-' and trashed = false" % d.state_dir,
        "fields": "files(id,name)", "pageSize": 1000}).json()
    fixed = 0
    for f in r.get("files", []):
        lines = d.req("GET", d.API + "/files/" + f["id"], params={"alt": "media"}).text.splitlines()
        out, changed = [], False
        for ln in lines:
            e = json.loads(ln)
            src = nga.get(e["id"]) if e["src"] == "nga" else None
            redo = src and max(src["w"], src["h"]) > 4096 and (
                max(e["w"], e["h"]) == 4096 or max(src["w"], src["h"]) > MAX_LONG)
            if redo:
                artist, tier, name = e["file"].split("/", 2)
                parent = d.folder((artist, tier))
                fid = d.find(name, parent)
                if fid:
                    im, data = finish(iiif_image(src["iiif"], src["w"], src["h"]), None)
                    w, h = im.size
                    nt, nn = tier_of(w, h), file_name(src["title"], src["date"], w, h)
                    np_ = d.folder((artist, nt))
                    params = {"uploadType": "media"}
                    if np_ != parent:
                        params.update(addParents=np_, removeParents=parent)
                    d.req("PATCH", d.UP + "/" + fid, params=params, data=data,
                          headers={"Content-Type": "image/jpeg"})
                    d.req("PATCH", d.API + "/files/" + fid, json={"name": nn})
                    e.update(file="%s/%s/%s" % (artist, nt, nn), w=w, h=h, bytes=len(data))
                    changed, fixed = True, fixed + 1
                    log("  upgraded", nn)
            out.append(json.dumps(e))
        if changed:
            d.req("PATCH", d.UP + "/" + f["id"], params={"uploadType": "media"},
                  data="\n".join(out).encode(), headers={"Content-Type": "application/json"})
    log("Upgraded %d capped files" % fixed)


def legacy_busy_with(d):
    """Source the old single (all-museums) run is still working on, or None.
    A part for that museum must wait, or both would download the same pictures."""
    try:
        r = d.req("GET", d.API + "/files", params={
            "q": "'%s' in parents and name = 'state.json' and trashed = false" % d.state_dir,
            "fields": "files(id,modifiedTime)"}).json().get("files") or []
        if not r:
            return None
        mod = time.mktime(time.strptime(r[0]["modifiedTime"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        if time.time() - mod > 20 * 60:
            return None
        logs = d.req("GET", d.API + "/files", params={
            "q": "'%s' in parents and name contains 'log-' and trashed = false" % d.state_dir,
            "orderBy": "createdTime desc", "pageSize": 20, "fields": "files(id,name)"}).json().get("files") or []
        for f in logs:
            if not re.match(r"^log-\d{8}-\d{6}\.jsonl$", f["name"]):
                continue  # a part's log, not the old run's
            body = d.req("GET", d.API + "/files/" + f["id"], params={"alt": "media"}).text.strip().splitlines()
            if body:
                return json.loads(body[-1]).get("src")
        return "unknown"
    except Exception as e:
        log("legacy check failed:", str(e)[:100])
        return None


SHARE_WITH = os.environ.get("SHARE_WITH", "neverland-drive-reader@neverland-prints.iam.gserviceaccount.com")


def share_with_backend(d):
    """The store's backend reads product images by Drive file id. Give its service account
    view access to this folder (Tobi, 16 Nov 2026), so new products show their pictures."""
    if not SHARE_WITH:
        return
    try:
        perms = d.req("GET", d.API + "/files/%s/permissions" % d.root,
                      params={"fields": "permissions(emailAddress,role)"}).json().get("permissions", [])
        if any((p.get("emailAddress") or "").lower() == SHARE_WITH.lower() for p in perms):
            return
        r = d.req("POST", d.API + "/files/%s/permissions" % d.root,
                  params={"sendNotificationEmail": "false"},
                  json={"type": "user", "role": "reader", "emailAddress": SHARE_WITH})
        log("Shared the intake folder with the store backend (view only):", r.status_code)
    except Exception as e:
        log("Sharing with the backend failed:", str(e)[:120])


def parent_busy(d):
    """True while an older single part of this museum is still writing its state file
    (it saves every few pictures), so a split part doesn't download the same pictures."""
    if not PARENT_STATE:
        return False
    try:
        r = d.req("GET", d.API + "/files", params={
            "q": "'%s' in parents and name = '%s' and trashed = false" % (d.state_dir, PARENT_STATE),
            "fields": "files(modifiedTime)"}).json().get("files") or []
        if not r:
            return False
        mod = time.mktime(time.strptime(r[0]["modifiedTime"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        return time.time() - mod < 150
    except Exception:
        return False


ORDER = ["nga", "met", "cleveland", "aic"]


def legacy_blocks(d):
    """The old run goes through the museums in ORDER; a part must wait if the old run is on
    its museum or hasn't reached it yet (it would download the same pictures again)."""
    busy = legacy_busy_with(d)
    if not busy:
        return None
    if busy == "unknown" or busy not in ORDER:
        return busy
    return busy if any(s in ORDER and ORDER.index(s) >= ORDER.index(busy) for s in SOURCES) else None


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
        warn_low_space(free)
        if free < FLOOR_GB:
            log("Not enough Drive space - not starting.")
            return
    d.setup_root()
    share_with_backend(d)
    if STATE_FILE != "state.json":
        busy = legacy_blocks(d)
        if busy:
            log("The earlier all-museums run is still downloading %s - this part waits for the next run." % busy)
            return
    if os.environ.get("UPGRADE") == "1":
        upgrade_capped(d)
        return
    cat = None
    try:
        cat = catalog_mod.load(surname, keys, norm)
        if cat:
            log("Catalog fingerprints: %d (weak copies: %d)" % (len(cat), int(cat.weak.sum())))
    except Exception as e:
        log("Catalog fingerprints unavailable, name check only:", str(e)[:120])
    if parent_busy(d):
        log("An earlier single %s part is still downloading - this part waits for the next run." % ",".join(SOURCES))
        return
    run = Run(d, known, cat)
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
                        if parent_busy(d):
                            log("An earlier single part of this museum is still running - stopping.")
                            STOP.set()
                        if STATE_FILE != "state.json" and legacy_blocks(d):
                            log("The earlier all-museums run reached this museum - stopping so nothing is downloaded twice.")
                            STOP.set()
                for f in futs:
                    f.result()
    finally:
        run.save()
        if AGAIN["yes"] and not DRY and not LIMIT:
            open("again", "w").write("1")
        log("DONE this run: saved %d (upgrades %d) | skipped (already have) %d | look-alikes %d | too small %d | repeats %d | failed %d | %.1f GB"
            % (run.saved, run.upgrades, run.skipped, run.catdupes, run.small, run.dupes, run.failed, run.bytes / 1e9))


if __name__ == "__main__":
    main()
