# Security policy

Please report vulnerabilities privately from this repository's **Security** tab (**Report a vulnerability**), not in a public issue. You should get a reply within a week.

Only the latest release receives fixes.

## Running Tally safely

- Tally has a single shared access password, not user accounts. Set one in **Settings** and require sign-in before anyone else can reach the app.
- Serve it over HTTPS through a reverse proxy (Caddy, Traefik, nginx) if it is reachable from outside your network.
- The data volume holds the database, uploaded images and, unless you set `SECRET_KEY`, the session signing key. Back it up and keep it private.
