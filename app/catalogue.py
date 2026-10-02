import csv
import io
import json

from app.matching import normalise, score

MAX_CATALOGUE_ITEMS = 5000
CATALOGUE_COLUMNS = {"name", "family", "manufacturer", "part_number"}


FAMILIES = {
    "microcontroller": {
        "label": "Microcontroller board",
        "keywords": "esp32 esp8266 arduino raspberry pi pico rp2040 microcontroller mcu development board seeed xiao",
        "fields": [
            ("chip", "Chip / family", "text", "e.g. ESP32-C3"),
            ("variant", "Board variant", "text", "e.g. XIAO"),
            ("connectivity", "Connectivity", "text", "e.g. Wi-Fi, Bluetooth, LoRa"),
            ("flash", "Flash", "text", "e.g. 4 MB"),
            ("form_factor", "Form factor", "text", "e.g. DevKit, Feather, XIAO"),
        ],
    },
    "capacitor": {
        "label": "Capacitor", "keywords": "capacitor ceramic electrolytic film tantalum",
        "fields": [("value", "Capacitance", "number", "e.g. 100"), ("value_unit", "Unit", "select", ["pF", "nF", "µF", "mF"]), ("voltage", "Voltage rating", "text", "e.g. 50 V"), ("capacitor_type", "Type", "select", ["Ceramic", "Electrolytic", "Film", "Tantalum", "Supercapacitor"]), ("tolerance", "Tolerance", "text", "e.g. ±10%"), ("package", "Package / size", "text", "e.g. 0805 or 5×11 mm")],
    },
    "resistor": {
        "label": "Resistor", "keywords": "resistor resistance ohm",
        "fields": [("value", "Resistance", "number", "e.g. 10"), ("value_unit", "Unit", "select", ["Ω", "kΩ", "MΩ"]), ("tolerance", "Tolerance", "select", ["±1%", "±2%", "±5%", "±10%"]), ("power", "Power rating", "select", ["0.125 W", "0.25 W", "0.5 W", "1 W", "2 W"]), ("package", "Package / size", "text", "e.g. axial or 0805")],
    },
    "led": {
        "label": "LED", "keywords": "led light emitting diode rgb neopixel",
        "fields": [("colour", "Colour", "text", "e.g. red or RGB"), ("package", "Package / size", "text", "e.g. 5 mm or 0603"), ("forward_voltage", "Forward voltage", "text", "e.g. 2.1 V"), ("current", "Rated current", "text", "e.g. 20 mA"), ("led_type", "Type", "select", ["Standard", "High power", "Addressable", "Infrared", "Ultraviolet"])],
    },
    "connector": {
        "label": "Connector", "keywords": "connector jst dupont header terminal socket plug usb",
        "fields": [("series", "Series", "text", "e.g. JST XH"), ("pitch", "Pitch", "text", "e.g. 2.50 mm"), ("positions", "Positions / pins", "number", "e.g. 4"), ("gender", "Gender", "select", ["Male", "Female", "Plug", "Socket", "Not applicable"]), ("mounting", "Mounting", "select", ["Cable", "Through-hole", "Surface-mount", "Panel"]), ("orientation", "Orientation", "select", ["Straight", "Right-angle"] )],
    },
    "sensor": {
        "label": "Sensor", "keywords": "sensor temperature humidity pressure imu motion pir distance proximity light colour current voltage rfid gps",
        "fields": [("model", "Model", "text", "e.g. BME280"), ("measures", "Measures", "text", "e.g. temperature and humidity"), ("interface", "Interface", "select", ["I²C", "SPI", "UART", "Analogue", "Digital"]), ("supply_voltage", "Supply voltage", "text", "e.g. 3.3–5 V"), ("range", "Measurement range", "text", "Optional")],
    },
    "module": {
        "label": "Electronic module", "keywords": "module breakout converter driver relay rtc adc dac programmer",
        "fields": [("model", "Model / chip", "text", "e.g. LM2596"), ("function", "Function", "text", "e.g. buck converter"), ("input", "Input", "text", "e.g. 4–35 V"), ("output", "Output", "text", "e.g. 1.25–30 V"), ("interface", "Interface", "text", "Optional")],
    },
    "semiconductor": {
        "label": "Semiconductor / IC", "keywords": "ic chip diode transistor mosfet regulator op amp logic",
        "fields": [("function", "Function", "text", "e.g. timer"), ("package", "Package", "text", "e.g. DIP-8 or SOIC-8"), ("voltage", "Voltage", "text", "Optional"), ("current", "Current", "text", "Optional")],
    },
    "cable": {
        "label": "Cable / adaptor", "keywords": "cable wire lead adaptor hdmi ethernet usb",
        "fields": [("connector_a", "End A", "text", "e.g. USB-C"), ("connector_b", "End B", "text", "e.g. USB-A"), ("length", "Length", "text", "e.g. 1 m"), ("cable_type", "Type / standard", "text", "e.g. USB 3.0"), ("colour", "Colour", "text", "Optional")],
    },
    "generic": {"label": "Other item", "keywords": "", "fields": []},
}


def item_summary(row):
    """A short description of a variant, such as 4.7 kΩ · 0805, from its value, voltage and package."""
    try:
        attributes = json.loads(row["attributes"] or "{}")
    except (ValueError, TypeError):
        attributes = {}
    if not isinstance(attributes, dict):
        return ""
    get = lambda key: str(attributes.get(key) or "").strip()
    return " · ".join(part for part in (f"{get('value')} {get('value_unit')}".strip(), get("voltage"), get("package")) if part)


def classify(query):
    best_key, best_score = "generic", 0
    for key, family in FAMILIES.items():
        value = max(
            (score(token, family["keywords"]) for token in normalise(query) if len(token) >= 3),
            default=0,
        )
        if value > best_score:
            best_key, best_score = key, value
    return best_key if best_score >= .68 else "generic"


def common_suggestions(query, entries, limit=12):
    scored = [(score(query, entry["name"] + " " + entry["manufacturer"] + " " + entry["part_number"]), entry) for entry in entries]
    return [entry for value, entry in sorted(scored, key=lambda row: -row[0]) if value >= .65][:limit]


def parse_catalogue(raw):
    """Read catalogue entries from JSON (a list, or an object with "items") or CSV.

    CSV needs a name column; family, manufacturer and part_number are optional and
    every other non-empty column becomes an attribute.
    """
    try:
        text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    except UnicodeDecodeError:
        raise ValueError("Catalogue files must be UTF-8 text.") from None
    try:
        data = json.loads(text)
    except ValueError:
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows or "name" not in rows[0]:
            raise ValueError("Use a JSON catalogue or a CSV file with a name column.")
        data = [{
            "name": row.get("name"), "family": row.get("family"), "manufacturer": row.get("manufacturer"), "part_number": row.get("part_number"),
            "attributes": {key: value for key, value in row.items() if key and key not in CATALOGUE_COLUMNS and value},
        } for row in rows]
    if isinstance(data, dict):
        data = data.get("items")
    if not isinstance(data, list):
        raise ValueError("A JSON catalogue must be a list of items or an object with an items list.")
    if len(data) > MAX_CATALOGUE_ITEMS:
        raise ValueError(f"A catalogue can hold at most {MAX_CATALOGUE_ITEMS} items.")
    entries = {}
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
            raise ValueError("Every catalogue item needs a name.")
        attributes = item.get("attributes") or {}
        if not isinstance(attributes, dict):
            raise ValueError(f"Attributes for {item['name'].strip()} must be an object.")
        family = item.get("family") if item.get("family") in FAMILIES else "generic"
        entry = {
            "name": item["name"].strip()[:200], "family": family,
            "manufacturer": str(item.get("manufacturer") or "").strip()[:200],
            "part_number": str(item.get("part_number") or "").strip()[:200],
            "attributes": {str(key)[:64]: str(value).strip()[:200] for key, value in attributes.items() if value not in (None, "")},
        }
        entries[entry["name"].casefold()] = entry
    return list(entries.values())
