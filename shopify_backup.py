"""Shopify unused files -> Tobi's Drive backup (Claude, 21 Nov 2026; Tobi: "move them to a folder in my drive
before you delete"). Step 2 of the relay in neverland-prints-brain src/scripts/storage-free.js:
Render stages each unused file's ORIGINAL (size-checked) in Supabase Storage with a signed link; this copies it into
Drive "Claude Files (Neverland Prints)/Shopify unused files backup 2026-11-21" (file name keeps the Shopify id),
checks the Drive copy's byte size and reports it (RPC shopify_backup_done). Render deletes in Shopify only files
whose Drive copy has the same size. Also keeps manifest.csv (Shopify id, Drive id, bytes, alt, created) in that folder.
"""
import csv, io, json, os, sys, time
import requests
from intake import Drive

FOLDER = os.environ.get("BACKUP_FOLDER", "18VC7AuSvz3ym4DIwLNBum9SUQI2iFVdL")
CFG = json.loads(os.environ["NEVERLAND_READ"])


def rpc(fn, **args):
    args["p_token"] = CFG["db_token"]
    for i in range(6):
        try:
            r = requests.post(CFG["db_url"] + "/rest/v1/rpc/" + fn, json=args, timeout=120,
                              headers={"apikey": CFG["db_key"], "Content-Type": "application/json"})
            if r.status_code == 200:
                return r.json()
            print("rpc", fn, r.status_code, r.text[:200])
        except requests.RequestException:
            pass
        time.sleep(2 ** i)
    raise RuntimeError("database unreachable (%s)" % fn)


def main():
    d = Drive()
    # names already in the folder (a rerun never uploads a file twice)
    have, tok = {}, None
    while True:
        q = d.req("GET", d.API + "/files", params={"q": "'%s' in parents and trashed=false" % FOLDER,
                  "fields": "nextPageToken,files(id,name,size)", "pageSize": 1000, "pageToken": tok or ""}).json()
        for f in q.get("files", []):
            have[f["name"]] = f
        tok = q.get("nextPageToken")
        if not tok:
            break
    done = bad = 0
    t0 = time.time()
    while time.time() - t0 < 40 * 60:
        rows = rpc("shopify_backup_todo", p_limit=100)
        if not rows:
            break
        for r in rows:
            name = r["fname"]
            f = have.get(name)
            if not f:
                g = requests.get(r["signed_url"], timeout=120)
                if g.status_code != 200 or len(g.content) != int(r["bytes"]):
                    print("staged copy wrong:", name, g.status_code, len(g.content)); bad += 1; continue
                fid = d.upload(name, FOLDER, g.content, "image/webp" if name.endswith(".webp") else "image/jpeg")
                f = d.req("GET", d.API + "/files/" + fid, params={"fields": "id,name,size"}).json()
                have[name] = f
            res = rpc("shopify_backup_done", p_id=r["id"], p_drive_id=f["id"], p_drive_bytes=int(f.get("size") or -1))
            if res == "ok":
                done += 1
            else:
                bad += 1; print(name, res)
        if bad > 50:
            break
    # manifest (replaced each run)
    rows = rpc("shopify_backup_manifest")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["shopify_id", "drive_id", "bytes", "file_name", "alt", "created_at", "kind", "state", "deleted_at"])
    for r in rows:
        w.writerow([r["id"], r["drive_id"], r["bytes"], r["fname"], r["alt"], r["created_at"], r["kind"], r["state"], r["deleted_at"]])
    data = buf.getvalue().encode()
    old = have.get("manifest.csv")
    if old:
        d.req("PATCH", d.UP + "/" + old["id"], params={"uploadType": "media"}, data=data, headers={"Content-Type": "text/csv"})
    else:
        d.upload("manifest.csv", FOLDER, data, "text/csv")
    print("backed up %d, problems %d, manifest rows %d" % (done, bad, len(rows)))
    if bad and not done:
        sys.exit(1)


if __name__ == "__main__":
    main()
