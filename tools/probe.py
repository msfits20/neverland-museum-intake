import re, time, requests
UA = {"User-Agent": "NeverlandMuseumIntake/1.0 (open-access download; contact via GitHub msfits20)"}
U = "https://harvester-bl.britishart.yale.edu/oaicatmuseum/OAIHandler"
for st in ["ycba:ps", "ycba:pd"]:
    p = {"verb": "ListRecords", "metadataPrefix": "lido", "set": st}
    for page in range(6):
        t0 = time.time()
        try:
            r = requests.get(U, params=p, headers=UA, timeout=180)
        except Exception as e:
            print("YC", st, page, "ERR", str(e)[:120]); break
        t = r.text
        tok = re.search(r"<resumptionToken([^>]*)>([^<]*)</resumptionToken>", t)
        man = len(re.findall(r"manifests\.collections\.yale\.edu/v2/ycba/obj/\d+", t))
        cls = re.findall(r"<lido:classification>.*?<lido:term>([^<]+)</lido:term>", t, re.S)[:5]
        print("YC", st, page, r.status_code, "%.1fs" % (time.time() - t0), "records", t.count("<record>"), "manifests", man, "token", tok.group(1)[:80] + " | " + tok.group(2)[:80] if tok else None, cls, t[:200].replace("\n", " ") if r.status_code != 200 or "<record>" not in t else "")
        if not tok or not tok.group(2):
            break
        p = {"verb": "ListRecords", "resumptionToken": tok.group(2)}
