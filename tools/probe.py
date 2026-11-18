"""Probe 2: data shapes of candidate sources (read-only)."""
import json, requests
UA = {"User-Agent": "NeverlandPrintsIntake/1.0 (hello@neverlandprints.com)"}
def show(name, url, n=1600, js=True):
    try:
        r = requests.get(url, headers=UA, timeout=60)
        print("=====", name, r.status_code)
        print(r.text[:n].replace("\n", " "))
    except Exception as e:
        print("=====", name, "ERR", e)
EU = "https://api.europeana.eu/record/v2/search.json?wskey=api2demo"
show("eu_count_pd_painting", EU + "&query=*&qf=TYPE:IMAGE&qf=proxy_dc_type.en:painting&qf=RIGHTS:*publicdomain*&media=true&qf=MEDIA:true&reusability=open&rows=1&profile=minimal", 900)
show("eu_facets", EU + "&query=*&qf=TYPE:IMAGE&reusability=open&media=true&rows=0&profile=facets&facet=proxy_dc_type.en&f.proxy_dc_type.en.facet.limit=40", 4000)
show("eu_item", EU + "&query=what:painting&qf=TYPE:IMAGE&reusability=open&media=true&qf=IMAGE_SIZE:extra_large&rows=1&profile=rich", 3500)
show("eu_cursor", EU + "&query=what:painting&qf=TYPE:IMAGE&reusability=open&media=true&rows=1&cursor=*&profile=minimal", 600)
show("eu_rights_facet", EU + "&query=*&qf=TYPE:IMAGE&reusability=open&media=true&rows=0&profile=facets&facet=RIGHTS", 2500)
show("si_cc0_paint", "https://api.si.edu/openaccess/api/v1.0/category/art_design/search?q=painting+AND+online_media_type%3AImages&rows=1&api_key=DEMO_KEY", 4000)
show("mia_obj", "https://search.artsmia.org/public_access:1%20image:valid%20classification:Paintings?size=1", 3000)
show("mia_total_pd", "https://search.artsmia.org/public_access:1%20image:valid?size=0", 500)
show("fng_obj", "https://www.kansallisgalleria.fi/api/v1/objects?limit=2", 4000)
show("wellcome_img", "https://api.wellcomecollection.org/catalogue/v2/images?locations.license=pdm,cc-0&pageSize=1&include=source.genres,source.contributors", 3000)
show("ycba_rec", "https://harvester-bl.britishart.yale.edu/oaicatmuseum/OAIHandler?verb=ListRecords&metadataPrefix=lido&set=ycba:ps", 6000)
