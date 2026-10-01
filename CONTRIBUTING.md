# Contributing

Thanks for helping. Bug reports, small fixes and new storage layouts are all welcome. For bigger changes, open an issue first so we can agree on the approach.

## Setup

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
TALLY_LAYOUT=example STORAGE_DB=data/dev.db .venv/bin/flask --app app.main run --debug
.venv/bin/python -m unittest
```

Styles live in `static/app.css`. Tailwind only supplies the reset; `npm run build:css` regenerates it.

## Guidelines

- Keep it one container and one SQLite file, with no build step needed to run it.
- Add or update a test in `tests/` for behaviour changes.
- `CONTEXT.md` defines the words the app uses (item, variant, storage location). Please stick to them in the UI.
- Pages must work on a phone, by keyboard, and in light and dark themes.

## Sharing a storage layout

Layouts are JSON files in `app/layouts/`. Each location needs a `code` and a `label`, and can also have a `kind` and search `keywords`. `groups` names the code prefixes, and `family_homes` maps a component family to the location where new parts of that family go by default. See `example.json`.
