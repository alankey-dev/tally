import math
import re

GS, RS, EOT = "\x1d", "\x1e", "\x04"
LCSC = re.compile(r"^\{\s*(?:pbn|pc):", re.I)
TME_FIELD = re.compile(r"\s+(?=[A-Za-z]+:)")


def clean_quantity(value):
    """A bag quantity as a number; a missing, zero or unreadable one is 1."""
    try:
        number = float(str(value).strip())
    except ValueError:
        return 1
    if not math.isfinite(number) or number <= 0:
        return 1
    return int(number) if number == int(number) else number


def label(manufacturer="", part_number="", supplier="", supplier_sku="", quantity=""):
    return {"manufacturer": manufacturer.strip(), "part_number": part_number.strip(), "supplier": supplier,
            "supplier_sku": supplier_sku.strip(), "quantity": clean_quantity(quantity)}


def parse_bag_label(text):
    """Read a distributor bag label.

    Returns {manufacturer, part_number, supplier, supplier_sku, quantity}, None when the text is not a label,
    and raises ValueError when it has an ECIA header but no GS separators to split it on. Splits on explicit
    characters: str.split() with no argument would also treat \\x1c to \\x1f as whitespace.
    """
    text = text.strip(" \t\r\n")
    if text.startswith("[)>"):
        parts = text.split(GS)
        if len(parts) < 2:
            raise ValueError("This label has no GS separators.")
        fields = [part.strip(" \t\r\n") for part in parts[1:]]
        fields[-1] = fields[-1].rstrip(RS + EOT + " \t\r\n")
        found = {}
        for field in fields:
            for prefix in ("30P", "1P", "Q"):
                if field.startswith(prefix):
                    found.setdefault(prefix, field[len(prefix):])
                    break
        sku = found.get("30P", "")
        return label(part_number=found.get("1P", ""), supplier="Digi-Key" if sku else "", supplier_sku=sku, quantity=found.get("Q", ""))
    if LCSC.match(text):
        found = {}
        for field in text.strip("{} ").split(","):
            key, _, value = field.partition(":")
            found.setdefault(key.strip().lower(), value.strip())
        return label(part_number=found.get("pm", ""), supplier="LCSC", supplier_sku=found.get("pc", ""), quantity=found.get("qty", ""))
    if text.startswith("QTY:"):
        found = {}
        for field in TME_FIELD.split(text):
            key, _, value = field.partition(":")
            found.setdefault(key, value.strip())
        if "PN" in found or "MPN" in found:
            return label(manufacturer=found.get("MFR", ""), part_number=found.get("MPN", ""), supplier="TME", supplier_sku=found.get("PN", ""), quantity=found.get("QTY", ""))
    return None
