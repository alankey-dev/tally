import json
import re
import unicodedata
from difflib import SequenceMatcher


def normalise(text):
    text = re.sub(r"\bseed\b", "seeed", text.casefold())
    text = re.sub(r"esp[\s-]+(32|8266)", r"esp\1", text)
    return re.findall(r"[a-z0-9]+", text)


def score(query, text):
    return score_tokens(normalise(query), normalise(text))


def score_tokens(wanted, words):
    if not wanted or not words:
        return 0
    matches = []
    for token in wanted:
        best = 0
        for word in words:
            if token == word:
                value = 1
            elif len(token) >= 3 and word.startswith(token):
                value = .9
            elif min(len(token), len(word)) >= 4:
                value = SequenceMatcher(None, token, word).ratio()
            else:
                value = 0
            best = max(best, value)
        matches.append(best)
    if min(matches) < .65:
        return 0
    return sum(matches) / len(matches)


def ranked(query, rows, text):
    scored = [(score(query, text(row)), dict(row)) for row in rows]
    return [row for value, row in sorted(scored, key=lambda pair: -pair[0]) if value >= .72]


def suggested_homes(query, locations):
    tokens = normalise(query)
    scored = []
    for row in locations:
        keywords = row["keywords"]
        # A family keyword is enough to suggest a home for an unrecognised variant.
        family = max((score(token, keywords) for token in tokens if len(token) >= 3), default=0)
        value = max(score(query, row["code"] + " " + row["label"]), family * .9)
        if value >= .75:
            scored.append((value, dict(row)))
    return [row for _, row in sorted(scored, key=lambda pair: -pair[0])[:3]]


PREFIXES = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1, "k": 1e3, "K": 1e3, "M": 1e6, "meg": 1e6, "G": 1e9}
TOKEN_PREFIXES = (("p", 1e-12), ("n", 1e-9), ("u", 1e-6), ("m", 1e-3), ("", 1), ("k", 1e3), ("meg", 1e6), ("g", 1e9))
NOT_AFTER, NOT_BEFORE = r"(?![A-Za-z0-9])", r"(?<![A-Za-z0-9.])"
UNIT = r"(?:F|Ω|ω|[Oo]hms?|R)"
PREFIX = r"(?:meg|[pnuμkKMG]|m(?=F))"
VALUE = re.compile(
    NOT_BEFORE + r"(?:(?P<rn>\d{1,3})(?P<rp>[pnuμkKMR])(?P<rf>\d{1,2})(?P<ru>F|Ω|ω|[Oo]hms?)?"
    r"|(?P<n>\d+(?:\.\d+)?)(?:(?P<p1>" + PREFIX + r")(?:\s?(?P<u1>" + UNIT + r"))?|\s?(?P<u2>" + UNIT + r")|\s(?P<p3>" + PREFIX + r")\s?(?P<u3>" + UNIT + r")))" + NOT_AFTER)
JOINED_UNITS = re.compile(r"(?<![\d.])(\d+)\s+(?=(?:m?[VAW]|[kMG]?Hz|mm|cm|mAh|Ah)\b)")


def values_in(text, unit=""):
    """The (start, end, number, unit) of each component value written in the text. A bare number is not one."""
    found = []
    for match in VALUE.finditer(unicodedata.normalize("NFKC", text)):
        if match["rn"]:
            number = float(f"{match['rn']}.{match['rf']}") * PREFIXES.get(match["rp"], 1)
            written = match["ru"] or ""
        else:
            number = float(match["n"]) * PREFIXES[match["p1"] or match["p3"] or ""]
            written = match["u1"] or match["u2"] or match["u3"] or ""
        kind = "F" if written == "F" else "Ω" if written else ""
        if unit and kind and kind != unit:
            continue
        found.append((match.start(), match.end(), float(f"{number:.6g}"), kind))
    return found


def parse_value(text, unit=""):
    """The number a component value means, so 4k7, 4.7k and 4700R are all 4700.0. None if the text is not one value."""
    text = unicodedata.normalize("NFKC", text).strip()
    found = values_in(text, unit)
    return found[0][2] if len(found) == 1 and (found[0][0], found[0][1]) == (0, len(text)) else None


def value_token(number, unit=""):
    """A short canonical token for a value, such as 4k7 or 100n, made only of a-z and 0-9."""
    mark = "f" if unit == "F" else "r"
    number = float(f"{number:.3g}")
    prefix, factor = next(((p, f) for p, f in reversed(TOKEN_PREFIXES) if number >= f * .9999), TOKEN_PREFIXES[0])
    whole, _, fraction = f"{number / factor:.3f}".rstrip("0").rstrip(".").partition(".")
    return whole + (prefix or mark) + fraction if number else "0" + mark


def rewrite_values(text):
    """The text with every component value written as its token, so 0.1uF and 100 nF read the same."""
    text = unicodedata.normalize("NFKC", text)
    for start, end, number, unit in reversed(values_in(text)):
        text = text[:start] + " " + value_token(number, unit) + " " + text[end:]
    return text


def split_query(query):
    """The value tokens in a query and the text left over, with a number and its unit joined (50 V becomes 50v)."""
    query = unicodedata.normalize("NFKC", query)
    found = values_in(query)
    for start, end, number, unit in reversed(found):
        query = query[:start] + " " + query[end:]
    return [value_token(number, unit) for _, _, number, unit in found], JOINED_UNITS.sub(r"\1", query)


def item_terms(row):
    """An item's value tokens, from its name and its value attributes, and its other attribute values as words."""
    try:
        attributes = json.loads(row["attributes"] or "{}")
    except (ValueError, TypeError):
        attributes = {}
    if not isinstance(attributes, dict):
        attributes = {}
    unit = {"capacitor": "F", "resistor": "Ω"}.get(row["family"], "")
    written = f"{attributes.get('value', '')} {attributes.get('value_unit', '')}"
    values = [value_token(number, kind) for _, _, number, kind in values_in(row["name"] or "") + values_in(written, unit)]
    other = " ".join(str(value) for key, value in attributes.items() if key not in ("value", "value_unit"))
    return values, normalise(JOINED_UNITS.sub(r"\1", other))


def value_ranked(query, rows):
    """Rows matching a query, best first. A value in the query must match exactly, other words match fuzzily."""
    wanted, rest = split_query(query)
    rest = normalise(JOINED_UNITS.sub(r"\1", rest))
    scored = []
    for row in rows:
        row = dict(row)
        values, words = item_terms(row)
        words = normalise(JOINED_UNITS.sub(r"\1", " ".join(row[key] or "" for key in ("name", "manufacturer", "part_number", "code")))) + words
        if wanted and values:
            value = 0 if not set(wanted) <= set(values) else score_tokens(rest, words) if rest else 1
        else:
            value = score_tokens(normalise(JOINED_UNITS.sub(r"\1", query)), words)
        scored.append((value, row))
    return [row for value, row in sorted(scored, key=lambda pair: -pair[0]) if value >= .72]
