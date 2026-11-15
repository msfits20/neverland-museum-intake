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
6 hours in 4 parallel parts and skips files already checked.
`quality_mode.txt`: `sample N` = calibration run, `full` = everything, `off` = stop.
