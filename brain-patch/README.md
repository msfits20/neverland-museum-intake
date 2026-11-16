# Brain repo change waiting to be applied

`0001-intake-promotion.patch` is for github.com/msfits20/neverland-prints-brain (branch `intake-promotion`).
It adds `src/scripts/promote-intake.js` (reviewed downloads -> catalog), an hourly cron entry that does
nothing until `INTAKE_PROMOTE_ON=1` on Render, and keeps promoted files out of the full scan's
missing-file check. Kept here because this session couldn't push to the brain repo.

Apply: `git checkout -b intake-promotion && git am brain-patch/0001-intake-promotion.patch`
