# Neverland museum intake

Downloads open-access wall art (paintings, drawings, prints, posters) from the
National Gallery of Art, the Met, the Cleveland Museum of Art and the Art
Institute of Chicago into the Google Drive folder **Neverland Museum Intake**.

- Runs on GitHub every 6 hours and picks up where it stopped.
- Skips artworks already in the Neverland Prints catalog and repeats.
- Keeps print-size images only (2,400 px or more), shrinks anything over 9,000 px.
- Stops by itself when Drive free space drops below 150 GB.
- Its Google permission only covers files it created itself.

## Pause / resume
- **Pause:** Settings → Secrets and variables → Actions → Variables → set `PAUSED` to `1`.
  (Or Actions → Museum intake → ⋯ → Disable workflow.)
- **Resume:** set `PAUSED` to `0` (or Enable workflow).
- A run in progress can be stopped with **Cancel workflow**; nothing is lost.

## Quality scan (read-only)
`quality_scan.py` checks every image in the **Neverland Prints** Drive folder
(opens? sharp? blown up? heavy compression? borders? biggest sharp print size)
and records the results in the store database. Its Google permission is
read-only: it cannot change, move or delete anything in Drive. It runs every
6 hours in 8 parallel parts and skips files already checked.
`quality_mode.txt`: `sample N` = calibration run, `full` = everything, `off` = stop.

## Look-alike check (catalog fingerprints)
`catalog.py` loads the picture fingerprint of every catalog image (from the
quality scan) so the intake skips artworks we already sell, even under a
different title or from another museum. Exception: when our copy is weak
(quality scan said limit_sizes or hide) and the museum copy prints clearly
bigger, it is downloaded as an upgrade (`upgrade_for` in the log). Every
skipped look-alike is logged too, so skips can be checked.

## Intake review (read-only)
`review.py` checks every downloaded file before it can join the catalog and
writes one row per file to `intake_queue`: `new` (fine) or `held` with the
reason (broken, too soft, frame in picture, already in catalog, repeat of
download). It also notes upgrades and titles that fill Artvee items the old
scraper claimed but never saved (`missing_artvee_titles.txt.gz`). It never
changes Drive. Run with `calibrate` > 0 to get contact sheets on the
`review-samples` branch instead of writing to the database.

## Wanted works (19 Nov 2026)
Famous pieces the store shows or wants but doesn't sell yet (for example the homepage paintings) are listed in
the database table `intake_wanted`. Every 6 hours, part 0 of "Re-source unfit catalog copies" searches all the
sources above for each one (`MODE=wanted`): same artist, the original's year (within 3 years), and never an
engraving, copy, study or detail made after it. A Wikimedia Commons file named in `search_hint` is tried first.
Found files go into **Neverland Museum Intake** and `intake_queue` as `new` with `quality.wanted_id`; the brain
lists them before anything else. Add a work: insert a row into `intake_wanted` (status `wanted`).
