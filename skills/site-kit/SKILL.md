---
name: site-kit
description: Start or restyle a website or web app in the house style taken from Tally (neutral greys, hairline borders, one blue accent, system fonts, light/dark, sidebar on desktop and tab bar on phones), then optionally deploy it to the VPS behind the shared Caddy edge proxy (alankey-dev/edge-proxy) with GitHub Actions on the self-hosted runner. Use when the user wants a new site, app, dashboard or landing page, says "Tally style", "house style" or "same look as Tally", or asks to deploy/host a project with edge-proxy, Caddy, cwtches.co.uk or the self-hosted runner.
---

# Site kit

Two jobs: give the project Tally's design, then (if wanted) ship it through the edge proxy.

Bundled files (paths relative to this skill):

- `assets/static/app.css`, `app.js`, `favicon.svg`: the design system and shell behaviour. Copy, don't rewrite.
- `assets/index.html`: the app shell (sidebar, mobile top bar, menu sheet, tab bar, theme toggle, icon sprite) with example content.
- `references/design.md`: principles, scale, the class for each building block, writing style. Read before designing pages.
- `assets/deploy/`: `docker-compose.yml`, `docker-compose.dev.yml`, `.github/workflows/deploy.yml`, `.github/dependabot.yml`, `.dockerignore`, `Dockerfile.static`, `Dockerfile.python`.
- `scripts/register-site.sh`: runs edge-proxy's "Add site" workflow with `gh`.

## 1. Gather

Ask only what isn't already clear: the site **name**, a lowercase **slug** (`[a-z0-9-]`, used for the
container, network alias and compose project), a two-letter **mark** for the logo tile (e.g. "Ta"),
and the **stack**. Default to plain static HTML for a site with no server logic, and Flask + Jinja +
gunicorn (as Tally) when it needs one. Follow the user's choice if they name another stack.

## 2. Apply the design

1. Copy `assets/static/*` into the project's static folder. Replace `{{MARK}}` in `favicon.svg`.
2. Turn `assets/index.html` into the project's base layout (a Jinja `base.html` with `{% block content %}`,
   a React/Svelte layout component, or the static page itself). Keep the shell markup, the inline
   theme script in `<head>`, the icon sprite, and `aria-current` on the active nav link and tab.
   Replace `{{NAME}}` and `{{MARK}}`; set the real nav items (tab bar: 2-5 most-used pages).
3. Build pages from the classes in `references/design.md`. App-specific CSS goes in a separate file
   loaded after `app.css`, using tokens only. Don't add a CSS framework, web fonts, shadows on cards,
   or a second accent colour.
4. Check it: open the page (or run the app) at desktop and ~390px widths, in light and dark.

## 3. Ask about deployment

Ask: **"Deploy this behind the edge proxy on the VPS?"** If no, stop here (offer the plain CI or Docker
files from `assets/deploy/` if useful). If yes, confirm:

- **Hostname**, default `<slug>.cwtches.co.uk`.
- **Port** the app listens on inside the container: 8080 for `Dockerfile.static`, 8000 for `Dockerfile.python`.
- **Upstream** is then `<slug>:<port>`.

Before going further, check the hostname isn't already routed: look for `sites/<hostname>.caddy` in
alankey-dev/edge-proxy. If it exists and points elsewhere, ask before replacing it.

## 4. Add the deploy files

Copy from `assets/deploy/` into the project root, replacing `{{SLUG}}`, `{{PORT}}`, `{{HOSTNAME}}` and
`{{DEV_PORT}}` (a free local port, e.g. 8000):

- the matching `Dockerfile.*` as `Dockerfile` (or write one for another stack: listen on `0.0.0.0:<port>`,
  run as non-root, keep state in `/data`);
- `docker-compose.yml` (joins the external `edge` network with alias `<slug>`, publishes **no** host ports)
  and `docker-compose.dev.yml` for local runs;
- `.github/workflows/deploy.yml` (runs on `[self-hosted, edge-proxy]` on push to `main`, writes `.env` from
  an optional `APP_ENV` secret, builds, starts, then checks `http://<slug>:<port>/` answers over `edge`);
- `.dockerignore` and `.github/dependabot.yml`, merging with any that exist.

If the project has tests, add a `test` job on `ubuntu-latest` and make `deploy` depend on it with
`needs: test`. If the health path isn't `/` (e.g. `/` redirects to a sign-in page that 401s), change the
URL in the check step to one that returns 200.

## 5. Ship it

1. **Prerequisites** (tell the user; you can't do these for them):
   - DNS A/AAAA record for the hostname pointing at the VPS. Caddy can't get a certificate until it resolves.
   - The self-hosted runner with label `edge-proxy` must be available to this repo. It's an
     organisation-level runner; if the repo isn't in its runner group, the job waits forever as "queued"
     (GitHub → org Settings → Actions → Runner groups).
   - Optional `APP_ENV` repository secret holding the `.env` contents.
2. **Deploy the app**: commit and push to `main` (or open a PR and merge it). Watch the Deploy run.
3. **Add the route** (once per hostname), using the first of these that's available:
   - GitHub tools/connector: trigger workflow `add-site.yml` in `alankey-dev/edge-proxy` on ref `main`
     with inputs `hostname` and `upstream`.
   - `gh` CLI: `scripts/register-site.sh <hostname> <slug>:<port>` (it validates input and watches the run).
   - Neither: tell the user to open edge-proxy → Actions → **Add site** → Run workflow with those two values.
4. **Verify**: both runs green, then `curl -I https://<hostname>/` returns 200 (allow a minute for the
   certificate). If it's 502, the app container isn't on `edge` or the port is wrong; check the Deploy
   run's health-check step.

Finish by telling the user the URL, which runs passed, and anything left for them (DNS, runner access, secrets).

## Where this runs

- **Claude Code** (CLI, desktop, web): work in the repo directly; use `gh` or GitHub tools for step 5.
- **Claude app chat** (web, desktop, mobile) without a checkout: write files to the repo with the GitHub
  connector if one is attached (create a branch, commit, open a PR), otherwise hand the files back as a
  zip and give the step 5 instructions. Never claim a deploy happened without seeing the run succeed.
