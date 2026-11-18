"""Probe museum APIs from a GitHub runner (read-only, prints status + a short sample)."""
import json, requests
UA = {"User-Agent": "NeverlandPrintsIntake/1.0 (hello@neverlandprints.com)"}
P = [
 ("aic_search", "https://api.artic.edu/api/v1/artworks/search?query[bool][must][0][term][is_public_domain]=true&fields=id,image_id&limit=2"),
 ("aic_search_p101", "https://api.artic.edu/api/v1/artworks/search?query[bool][must][0][term][is_public_domain]=true&fields=id&limit=100&page=101"),
 ("aic_iiif_info", "https://www.artic.edu/iiif/2/25c31d8d-21a4-9ea1-1d73-6a2eca4dda7e/info.json"),
 ("aic_iiif_full", "https://www.artic.edu/iiif/2/25c31d8d-21a4-9ea1-1d73-6a2eca4dda7e/full/3000,/0/default.jpg"),
 ("aic_iiif_tile", "https://www.artic.edu/iiif/2/25c31d8d-21a4-9ea1-1d73-6a2eca4dda7e/0,0,1024,1024/1024,/0/default.jpg"),
 ("yale_lux", "https://lux.collections.yale.edu/api/search/item?q=%7B%22AND%22%3A%5B%7B%22classification%22%3A%7B%22name%22%3A%22paintings%22%7D%7D%5D%7D&page=1&pageLength=2"),
 ("ycba_oai", "https://harvester-bl.britishart.yale.edu/oaicatmuseum/OAIHandler?verb=ListRecords&metadataPrefix=lido&set=ycba:ps"),
 ("wellcome_images", "https://api.wellcomecollection.org/catalogue/v2/images?locations.license=pdm,cc-0&pageSize=2"),
 ("smithsonian_demo", "https://api.si.edu/openaccess/api/v1.0/search?q=painting+AND+online_media_type%3AImages&rows=2&api_key=DEMO_KEY"),
 ("getty_sparql", "https://data.getty.edu/museum/collection/sparql?query=SELECT%20(COUNT(*)%20AS%20%3Fn)%20WHERE%20%7B%3Fs%20a%20%3Chttps%3A%2F%2Flinked.art%2Fns%2Fterms%2FHumanMadeObject%3E%7D"),
 ("getty_activity", "https://data.getty.edu/museum/collection/activity-stream"),
 ("mia_github", "https://raw.githubusercontent.com/artsmia/collection/main/objects/0/1.json"),
 ("mia_search", "https://search.artsmia.org/public_access:1%20classification:Paintings?size=2"),
 ("loc_posters", "https://www.loc.gov/collections/works-progress-administration-posters/?fo=json&c=2"),
 ("paris_musees", "https://apicollections.parismusees.paris.fr/graphql"),
 ("finnish_ng", "https://www.kansallisgalleria.fi/api/v1/objects?limit=1"),
 ("nationalmuseum_se", "https://api.nationalmuseum.se/api/objects?limit=1"),
 ("cma_count", "https://openaccess-api.clevelandart.org/api/artworks/?cc0=1&has_image=1&limit=1"),
 ("brooklyn", "https://www.brooklynmuseum.org/api/v2/object/?limit=1"),
 ("harvard", "https://api.harvardartmuseums.org/object?size=1"),
 ("europeana", "https://api.europeana.eu/record/v2/search.json?query=painting&reusability=open&rows=1&wskey=api2demo"),
 ("princeton", "https://data.artmuseum.princeton.edu/search/objects?q=painting&size=1"),
 ("philamuseum", "https://www.philamuseum.org/api/collection/object/104468"),
]
for name, url in P:
    try:
        r = requests.get(url, headers=UA, timeout=40)
        ct = r.headers.get("content-type", "")
        body = r.text[:500].replace("\n", " ") if "image" not in ct else "<image %d bytes>" % len(r.content)
        print("%-20s %s %s | %s" % (name, r.status_code, ct[:30], body))
    except Exception as e:
        print("%-20s ERR %s" % (name, str(e)[:200]))
