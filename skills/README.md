# Skills

## site-kit

Starts a site in Tally's design (the same `app.css`, app shell and theme toggle) and, if you want,
deploys it behind [edge-proxy](https://github.com/alankey-dev/edge-proxy) with GitHub Actions on the
self-hosted runner. See [site-kit/SKILL.md](site-kit/SKILL.md).

Install it once to use it everywhere:

- **Claude app (web, desktop, mobile):** zip the folder (`cd skills && zip -r site-kit.zip site-kit`),
  then Settings → Capabilities → Skills → Upload skill.
- **Claude Code on your machine:** `cp -r skills/site-kit ~/.claude/skills/`.

`site-kit/assets/static/app.css` is derived from `static/app.css` with Tally-only page styles removed.
When the design changes here, copy the change across.
