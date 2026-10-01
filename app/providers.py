"""Part search providers: look up parts that are not in the local catalogue.

Each provider turns a search query into entries shaped like catalogue entries
(name, family, manufacturer, part_number, attributes) plus a description and link.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from app.catalogue import classify

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TIMEOUT = 8


class ProviderError(Exception):
    pass


def _request(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": "Tally/1.0", "Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=TIMEOUT) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise ProviderError(f"The provider answered HTTP {error.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProviderError("The provider could not be reached.") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ProviderError("The provider's answer was too large.")
    try:
        return json.loads(raw)
    except ValueError:
        raise ProviderError("The provider sent an answer Tally could not read.") from None


def _entry(name, manufacturer, part_number, description, url, category, attributes):
    return {
        "name": (name or part_number or "").strip()[:200], "family": classify(category) if classify(category) != "generic" else classify(description),
        "manufacturer": (manufacturer or "").strip()[:200], "part_number": (part_number or "").strip()[:200],
        "description": (description or "").strip()[:200], "url": url if str(url).startswith(("http://", "https://")) else "",
        "attributes": {key: str(value).strip()[:200] for key, value in attributes.items() if value not in (None, "")},
    }


def search_jlcpcb(query, config, limit=8):
    """JLCPCB / LCSC parts through the public jlcsearch index (no account needed)."""
    url = "https://jlcsearch.tscircuit.com/components/list.json?" + urllib.parse.urlencode({"search": query, "limit": limit})
    results = []
    for part in _request(url).get("components", [])[:limit]:
        lcsc = f"C{part['lcsc']}" if part.get("lcsc") else ""
        results.append(_entry(
            part.get("mfr"), "", part.get("mfr"), part.get("description") or f"{part.get('subcategory', '')}",
            f"https://jlcpcb.com/partdetail/{lcsc}" if lcsc else "", f"{part.get('category', '')} {part.get('subcategory', '')}",
            {"package": (part.get("package") or "").split(",")[0], "lcsc": lcsc},
        ))
    return results


def search_mouser(query, config, limit=8):
    """Mouser keyword search. Needs a free Mouser Search API key."""
    url = "https://api.mouser.com/api/v1/search/keyword?" + urllib.parse.urlencode({"apiKey": config["api_key"]})
    body = {"SearchByKeywordRequest": {"keyword": query, "records": limit, "startingRecord": 0, "searchOptions": "", "searchWithYourSignUpLanguage": ""}}
    answer = _request(url, body)
    if answer.get("Errors"):
        raise ProviderError("Mouser rejected the search. Check the API key.")
    results = []
    for part in ((answer.get("SearchResults") or {}).get("Parts") or [])[:limit]:
        results.append(_entry(
            part.get("ManufacturerPartNumber"), part.get("Manufacturer"), part.get("ManufacturerPartNumber"),
            part.get("Description"), part.get("ProductDetailUrl"), part.get("Category") or "",
            {"mouser": part.get("MouserPartNumber")},
        ))
    return results


PROVIDERS = {
    "jlcpcb": {
        "name": "JLCPCB / LCSC",
        "description": "Parts stocked by JLCPCB and LCSC, searched through the public jlcsearch index. No account needed.",
        "fields": [],
        "default_enabled": True,
        "search": search_jlcpcb,
    },
    "mouser": {
        "name": "Mouser",
        "description": "Mouser's catalogue through its Search API. Request a free API key from your Mouser account.",
        "fields": [("api_key", "API key", True)],
        "default_enabled": False,
        "search": search_mouser,
    },
}


def is_configured(provider_id, config):
    return all(config.get(key) for key, _label, _secret in PROVIDERS[provider_id]["fields"])


def search_all(query, configs):
    """Search every enabled, configured provider. Returns (results by provider id, errors by provider id)."""
    results, errors = {}, {}
    for provider_id, provider in PROVIDERS.items():
        config = configs.get(provider_id, {})
        if not (config.get("enabled") and is_configured(provider_id, config)):
            continue
        try:
            results[provider_id] = provider["search"](query, config)
        except ProviderError as error:
            errors[provider_id] = str(error)
    return results, errors
