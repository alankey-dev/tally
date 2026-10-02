"""Reading a CAD bill of materials and matching its lines to items. Stdlib only."""
import codecs
import csv
import io
import json
import math
import re
from app.matching import normalise, rewrite_values, score_tokens

ALIASES = {
    "designator": ("reference", "references", "designator"),
    "quantity": ("qty", "quantity"),
    "value": ("value", "comment", "name"),
    "footprint": ("footprint", "package"),
    "mpn": ("mpn", "manufacturer part", "manufacturer part number"),
    "supplier_code": ("lcsc part #", "lcsc", "supplier part", "jlcpcb part #"),
    "dnp": ("dnp", "exclude from bom", "exclude from board"),
    "populate": ("populate",),
}
COLUMNS = {alias: field for field, aliases in ALIASES.items() for alias in aliases}
TEXT_FIELDS = ("designators", "value", "footprint", "mpn", "supplier_code")
MAX_TEXT = 200
IMPERIAL = re.compile(r"(?<![0-9])(0201|0402|0603|0805|1206|1210|1812|2010|2512)(?![0-9])")
LEADING_PACKAGE = re.compile(r"^(?:[A-Za-z]+_)?((?:SOT|SOIC)-?\d+)", re.I)


def decode(raw):
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            raise ValueError("That file is not a readable CSV.")
    if b"\x00" in raw:
        raise ValueError("That file is not a CSV.")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError("That file is not a CSV.")


def header_of(rows):
    """The first of the first 10 rows that names a designator column and a value, MPN or supplier code column."""
    for index, row in enumerate(rows[:10]):
        columns = {}
        for position, cell in enumerate(row):
            field = COLUMNS.get(cell.strip().casefold())
            if field and field not in columns:
                columns[field] = position
        if "designator" in columns and columns.keys() & {"value", "mpn", "supplier_code"}:
            return index, columns
    return None


def expand_designators(text):
    """'R1-R4, R7' becomes R1, R2, R3, R4, R7."""
    found = []
    for part in re.split(r"[,;\s]+", re.sub(r"\s*-\s*", "-", text.strip())):
        span = re.fullmatch(r"([A-Za-z]+)(\d+)-([A-Za-z]*)(\d+)", part)
        if span and span[3] in ("", span[1]) and 0 <= int(span[4]) - int(span[2]) < 1000:
            found += [f"{span[1]}{number}" for number in range(int(span[2]), int(span[4]) + 1)]
        elif part:
            found.append(part)
    return found


def parse_bom(raw):
    """The grouped lines of a BOM file and how many DNP rows were left out. Raises ValueError for a file that is not one."""
    text = decode(raw)
    found = None
    for delimiter in (",", ";", "\t"):
        try:
            rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
        except csv.Error:
            raise ValueError("That file is not a readable CSV.")
        found = header_of(rows)
        if found:
            break
    if not found:
        raise ValueError("No BOM header found. The file needs a Reference or Designator column and a Value, MPN or LCSC column.")
    start, columns = found
    groups, skipped = {}, 0
    for row in rows[start + 1:]:
        def cell(field):
            return row[columns[field]].strip() if field in columns and columns[field] < len(row) else ""
        designators, quantity = expand_designators(cell("designator")), cell("quantity")
        if not designators and not quantity:
            continue
        if cell("dnp").casefold() not in ("", "0", "no", "false", "n") or cell("populate").casefold() in ("0", "no", "false", "n"):
            skipped += 1
            continue
        try:
            number = float(quantity) if quantity else len(designators)
            if not math.isfinite(number) or number <= 0:
                raise ValueError()
        except ValueError:
            number = len(designators) or 1
        key = tuple(cell(field) for field in ("value", "footprint", "mpn", "supplier_code"))
        group = groups.setdefault(key, {"designators": [], "quantity": 0, "warn": False})
        group["designators"] += designators
        group["quantity"] += number
        group["warn"] = group["warn"] or bool(quantity and designators and number != len(designators))
    lines = []
    for (value, footprint, mpn, supplier_code), group in groups.items():
        names = ", ".join(group["designators"])
        lines.append({"designators": names if len(names) <= MAX_TEXT else names[:MAX_TEXT - 1] + "…",
                      "value": value[:MAX_TEXT], "footprint": footprint[:MAX_TEXT], "mpn": mpn[:MAX_TEXT],
                      "supplier_code": supplier_code[:MAX_TEXT], "quantity": group["quantity"], "warn": group["warn"]})
    return lines, skipped


def package_of(footprint):
    """A package a footprint name carries, such as 0805 from R_0805_2012Metric, or ''."""
    found = IMPERIAL.search(footprint) or LEADING_PACKAGE.search(footprint)
    return found[1] if found else ""


def prepare_items(rows):
    """Item rows ready for matching, with each item's match text rewritten and normalised once."""
    items, by_word, by_part = [], {}, {}
    for row in rows:
        try:
            attributes = json.loads(row["attributes"] or "{}")
        except ValueError:
            attributes = {}
        values = attributes.values() if isinstance(attributes, dict) else ()
        text = " ".join(str(part or "") for part in (row["name"], row["manufacturer"], row["part_number"], *values))
        item = {"id": row["id"], "name": row["name"], "quantity": row["quantity"], "unit": row["unit"], "code": row["code"], "words": normalise(rewrite_values(text))}
        items.append(item)
        for word in set(item["words"]):
            by_word.setdefault(word, []).append(item)
        if (row["part_number"] or "").strip():
            by_part.setdefault(row["part_number"].strip().casefold(), []).append(item)
    return {"items": items, "by_word": by_word, "by_part": by_part}


def match_line(line, index, search=""):
    """How a line was matched, 'part', 'name' or 'none', and up to five candidate items, best first."""
    if not search:
        codes = [code.strip().casefold() for code in (line["mpn"], line["supplier_code"]) if code.strip() not in ("", "~", "-")]
        found = {item["id"]: item for code in codes for item in index["by_part"].get(code, [])}
        if found:
            return "part", list(found.values())[:5]
    wanted = normalise(rewrite_values(search or line["value"]))
    number = next((token for token in wanted if any(char.isdigit() for char in token)), None)
    pool = index["by_word"].get(number, []) if number else index["items"]
    package = re.findall(r"[a-z0-9]+", package_of(line["footprint"]).casefold())
    scored = []
    for item in pool:
        value = score_tokens(wanted, item["words"])
        if value >= .72:
            scored.append((not (package and set(package) <= set(item["words"])), -value, item["name"].casefold(), item))
    scored.sort(key=lambda entry: entry[:3])
    candidates = [entry[3] for entry in scored[:5]]
    return ("name" if candidates else "none"), candidates
