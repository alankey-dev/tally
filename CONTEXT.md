# Tally

Tally records electronic parts and the permanent storage homes used to find them.

## Language

**Item**:
One independently counted stock entry, such as “Seeed Studio XIAO ESP32-C3” or “100 nF ceramic capacitor, 50 V”.
_Avoid_: Product, component type

**Variant**:
The specific identity of an item within a broader family, distinguished by its meaningful attributes.

**Component family**:
A broad class such as microcontroller board, capacitor, resistor, connector, sensor or cable. The family determines which attributes are useful during entry.
_Avoid_: Category when referring to a physical storage group

**Attribute**:
A family-specific fact used to distinguish variants, such as capacitance, voltage rating, package, interface or connector pitch.

**Storage location**:
A permanently coded drawer, box or other physical home where items are normally returned.
_Avoid_: Bin, category

**Bag label**:
A distributor's 2D code on a bag of parts. It carries the manufacturer part number, the quantity and the supplier SKU.

**Label**:
A printed sticker for a storage location or item, carrying its code and a QR code that opens it in Tally.
_Avoid_: Tag, sticker
A distributor's code on a parts bag is a **Bag label**, not a Label. The database column `locations.label` is the location's description, which the UI calls "Description".

**Supplier SKU**:
The distributor's order code for an item, kept for reordering.

**Order entry**:
An item and a quantity to buy, which is to order, on order or received.
_Avoid_: Line (that means a bill of materials row), purchase order

**On order**:
An order entry that has been placed but not yet received.

**Attachment**:
A labelled file or link kept with an item or shared through its catalogue entry.
_Avoid_: Document, file

**Catalogue entry**:
A known part that quick add prefills from. Attachments are shared per catalogue entry, not per component family.

**Stock movement**:
A dated increase or decrease to an item’s on-hand quantity.

**Designator**:
A board reference such as R12 in a CAD BOM. Tally groups designators into one line and does not store them.
_Avoid_: Reference

**Line**:
An item a project’s bill of materials needs, with the quantity one build takes. Adding a line never moves stock.
_Avoid_: Allocation

**Build**:
One recorded consumption of a project’s whole bill of materials, a number of boards at a time. It writes a stock movement per line and can be undone, which returns exactly what it took.
