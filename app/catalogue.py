from app.matching import normalise, score


FAMILIES = {
    "microcontroller": {
        "label": "Microcontroller board",
        "keywords": "esp32 esp8266 arduino raspberry pi pico rp2040 microcontroller mcu development board seeed xiao",
        "fields": [
            ("manufacturer", "Manufacturer", "text", "e.g. Seeed Studio"),
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
        "fields": [("part_number", "Part number", "text", "e.g. NE555P"), ("function", "Function", "text", "e.g. timer"), ("package", "Package", "text", "e.g. DIP-8 or SOIC-8"), ("voltage", "Voltage", "text", "Optional"), ("current", "Current", "text", "Optional")],
    },
    "cable": {
        "label": "Cable / adaptor", "keywords": "cable wire lead adaptor hdmi ethernet usb",
        "fields": [("connector_a", "End A", "text", "e.g. USB-C"), ("connector_b", "End B", "text", "e.g. USB-A"), ("length", "Length", "text", "e.g. 1 m"), ("cable_type", "Type / standard", "text", "e.g. USB 3.0"), ("colour", "Colour", "text", "Optional")],
    },
    "generic": {"label": "Other item", "keywords": "", "fields": []},
}

COMMON_ITEMS = [
    {"name": "Seeed Studio XIAO ESP32-C3", "family": "microcontroller", "manufacturer": "Seeed Studio", "part_number": "XIAO ESP32-C3", "attributes": {"chip": "ESP32-C3", "variant": "XIAO", "connectivity": "Wi-Fi, Bluetooth 5", "flash": "4 MB", "form_factor": "XIAO"}},
    {"name": "Seeed Studio XIAO ESP32-S3", "family": "microcontroller", "manufacturer": "Seeed Studio", "part_number": "XIAO ESP32-S3", "attributes": {"chip": "ESP32-S3", "variant": "XIAO", "connectivity": "Wi-Fi, Bluetooth 5", "flash": "8 MB", "form_factor": "XIAO"}},
    {"name": "ESP32 DevKit V1", "family": "microcontroller", "manufacturer": "Espressif-compatible", "part_number": "ESP32 DevKit V1", "attributes": {"chip": "ESP32", "variant": "DevKit V1", "connectivity": "Wi-Fi, Bluetooth", "form_factor": "DevKit"}},
    {"name": "ESP32-CAM", "family": "microcontroller", "manufacturer": "AI-Thinker-compatible", "part_number": "ESP32-CAM", "attributes": {"chip": "ESP32", "variant": "ESP32-CAM", "connectivity": "Wi-Fi, Bluetooth", "form_factor": "Camera module"}},
    {"name": "ESP32-S3 DevKitC-1", "family": "microcontroller", "manufacturer": "Espressif", "part_number": "ESP32-S3-DevKitC-1", "attributes": {"chip": "ESP32-S3", "variant": "DevKitC-1", "connectivity": "Wi-Fi, Bluetooth 5", "form_factor": "DevKit"}},
    {"name": "Raspberry Pi Pico", "family": "microcontroller", "manufacturer": "Raspberry Pi", "part_number": "Pico", "attributes": {"chip": "RP2040", "variant": "Pico", "flash": "2 MB", "form_factor": "Pico"}},
    {"name": "Arduino Uno R3", "family": "microcontroller", "manufacturer": "Arduino", "part_number": "Uno R3", "attributes": {"chip": "ATmega328P", "variant": "Uno R3", "form_factor": "Arduino Uno"}},
]


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


def common_suggestions(query, limit=6):
    scored = [(score(query, item["name"] + " " + item["manufacturer"] + " " + item["part_number"]), index, item) for index, item in enumerate(COMMON_ITEMS)]
    return [{**item, "catalogue_id": index} for value, index, item in sorted(scored, key=lambda row: -row[0]) if value >= .65][:limit]


def catalogue_item(item_id):
    try:
        return COMMON_ITEMS[int(item_id)]
    except (ValueError, TypeError, IndexError):
        return None
