# House design (from Tally)

The look is quiet and tool-like: neutral greys, hairline borders, one blue accent, system fonts. Every
value below is already a token or class in `assets/static/app.css`; use those rather than restating them.

## Principles

- **Borders, not shadows.** Surfaces are separated by 1px `--border` hairlines. Shadows only on things that
  float: popovers (`--shadow-pop`) and dialogs/sheets (`--shadow-modal`).
- **One accent.** `--accent` (blue) is for links, focus rings, selection and checkbox ticks only. Primary
  buttons are neutral (`--primary`: near-black in light, near-white in dark), never blue.
- **Status colour means status.** `--success`, `--warn`, `--danger` and their `-bg` pairs are for state
  (flash messages, badges, low/out quantities, destructive buttons). Never decorative.
- **System fonts.** `--font` (SF/Segoe/Roboto stack) and `--mono` for codes and IDs. No web fonts.
- **Light and dark.** Every colour is a token with a dark value. Dark mode follows the OS unless the user
  picks one (`data-theme` on `<html>`, saved in localStorage). Never hard-code a hex in app CSS.
- **Accessible by default.** Colour pairs meet WCAG AA. Visible `:focus-visible` ring. Current page uses
  `aria-current="page"`. Icons are `aria-hidden` with text or `aria-label` beside them. Motion respects
  `prefers-reduced-motion`.
- **Phone first-class.** Under 960px the sidebar becomes a top bar + menu sheet + bottom tab bar (2-5 tabs);
  grids collapse to one column. Under 640px tables become labelled cards, so give every `<td>` a
  `data-label`. Inputs use 16px text on mobile so iOS doesn't zoom.

## Scale

| Thing | Value |
| --- | --- |
| Root font | 15px, line-height 1.5 |
| h1 | 1.75rem / 600 / -0.02em (1.5rem on phones) |
| h2, h3 | 1rem / 600, .9375rem / 600 |
| Secondary text | `small`, `.copy`: .8125-.875rem in `--text-2` |
| Radius | `--radius` 10px (cards, tables, lists), `--radius-sm` 7px (buttons, inputs, flashes) |
| Control height | 36px desktop, 40px under 960px; `.small` 30px |
| Spacing rhythm | 28px under page head, 20px card padding, 16px grid gaps, 8px between buttons |
| Widths | `--content-max` 1080px, `--form-max` 680px, `--sidebar-w` 224px |

## Building blocks

| Need | Markup |
| --- | --- |
| Page title + actions | `.page-head` > `div` (`h1`, `p.lede`) + `.page-actions` (buttons) |
| Back link / eyebrow | `a.back` with `#i-back` icon; `.eyebrow` above the h1 |
| Buttons | `.button` / `button`, modifiers `.primary`, `.quiet`, `.danger`, `.small`, `.block` |
| Segmented filter | `.segmented` > `a[aria-current="true"]` |
| Panel | `.card` (optional `.card-head` with h2 + link); stack with `.stack` |
| Headline numbers | `.metrics` > `.metric` (`span` label, `strong` value; `.warning`) |
| Two columns | `.split`, `.split.wide`, `.dashboard-grid` |
| List of things | `.record-list` > `a.record` (`div` > `strong` + `small`, then a badge or `.qty`) |
| Table | `.table-wrap` > `table`; `th.num`/`td.num` for numbers; `td.actions` for row buttons |
| Status pill | `.badge.neutral` / `.warn` / `.danger` |
| Messages | `.flash.success` (`role="status"`) / `.flash.error` (`role="alert"`) with an icon |
| Empty state | `.empty` with one sentence and, if useful, the action that fills it |
| Form | `form.form` > `label` (text then input inside); `.form-grid` for pairs; `.form-section` with h2; `.hint`; `.optional`; `.check` for checkboxes; `.form-actions` at the end |
| Search | `form.search` > `.search-field` (`#i-search` icon + input) + button; `.suggestions` for a dropdown |
| Toolbar | `.toolbar` with a search and buttons |
| Dialog | native `<dialog>` containing `form.form` |
| Sign-in page | `body.access-page` + `.access-panel` (hides the shell) |

Icons are 24×24 stroke line icons (stroke-width 1.75, round caps) in an inline `<svg hidden>` sprite,
used as `<svg class="icon"><use href="#i-name"/></svg>`. Add new ones in the same style.

## Writing

British English, sentence case everywhere (titles, buttons, nav). Labels are short verbs or nouns
("Save", "New item", "Download backup"). A page lede is one plain sentence. Empty states say what goes
here and how to add it. Errors say what went wrong and what to do.

## Extending

Put app-specific rules in a second stylesheet loaded after `app.css`, built only from the tokens and
the scale above. If something needs a new colour, add a token with both light and dark values (and
check AA contrast) rather than a one-off hex. Tailwind is optional; Tally only uses it for the odd
utility, and `app.css` must stay the source of truth.
