# Future specs

These are proposals a contributor can pick up later. Most of them are not built yet. They came from research across PartsBox, Part-DB, Binner, InvenTree, PartKeepr, Homebox and maker forums. Three judges then scored eight candidates out of 30 for demand, fit and impact. The build planner came first and has shipped, and so has scan to receive. These are the other seven. Each one builds on what is in `main` today, including the build planner.

| Spec | Score | Summary | Depends on |
| --- | --- | --- | --- |
| [Scan bag labels to receive stock](scan-bag-receive.md) | 26/30 (shipped) | Scan a distributor bag's 2D code into Quick add to receive stock or start a new component. | Nothing new |
| [Printable QR labels](qr-labels.md) | 25/30 | Print QR labels for storage locations and items. A phone camera then opens that drawer's stock. | Nothing new |
| [Import a CAD bill of materials](bom-import.md) | 23/30 | Upload a KiCad, EasyEDA or JLCPCB BOM CSV, review the matches and add the lines to a project. | Nothing new |
| [Order list](reorder-list.md) | 23/30 | Turn low stock and project shortages into order entries. Mark them ordered, then receive them. | Nothing new |
| [Attachments on items](attachments.md) | 22/30 | Attach datasheets, pinout photos and links to items, and share them through a catalogue entry. | Nothing new |
| [Stocktake: count a storage location](stocktake.md) | 21/30 | Count a drawer on your phone. Each difference becomes a stock movement, and the dashboard shows what is due. | Nothing new |
| [Parametric search on component attributes](parametric-search.md) | 21/30 | Search by value and package, such as `10k 0603`, and filter Components by attribute chips. | Nothing new |

Every spec can ship on its own. Where two specs touch the same code, both say so, and whichever ships first adds the shared piece. The shared pieces are `exports_enabled`, `drawer_order(row)`, `item_column_names()`, the supplier SKU columns and the value parser.

## Suggested order

1. **Scan bag labels to receive stock** (shipped). It has the top score and makes putting stock away much faster. It also adds `items.supplier_sku`, which the order list can use later.
2. **Printable QR labels.** PR 1 needs no schema change. It adds `/stock?location=`, `exports_enabled` and `drawer_order(row)`, which later specs reuse.
3. **Import a CAD bill of materials.** It feeds the build planner directly and needs no schema change. It also sets the value rules that parametric search will reuse.
4. **Order list.** It works best once projects have full bills of materials and items carry supplier SKUs, so it comes after both.
5. **Attachments on items.** It stands alone. Its catalogue link and backup zip are larger changes, so it is better reviewed once the smaller specs are in.
6. **Stocktake.** It reuses `drawer_order(row)` from the labels work, and a printed drawer label makes a natural place to start a count.
7. **Parametric search.** It has the lowest score, and it can take over the BOM import's value rules, so there is still only one set.

## Picking one up

Open an issue first, as `CONTRIBUTING.md` asks, so the approach is agreed before you write code. Settle the spec's open questions in that issue. Then change the spec's **Status** from Proposed to In progress, and link the issue. When the work merges, set it to Shipped and update `README.md` and `CONTEXT.md` as the spec's "Docs to update" section lists.
