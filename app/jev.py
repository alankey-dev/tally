"""Classify a new part with TypeSafe's Jev model: which family it is and which storage location suits it."""
import json
import urllib.error
import urllib.request

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
TIMEOUT = 15
MAX_LOCATIONS = 300


class JevError(Exception):
    pass


def classify(api_key, part, families, locations):
    """Return {'family': (key, confidence), 'location': (code, confidence)} for a part description.

    `families` maps key -> label. `locations` is a list of (code, description). Jev picks one option for each question."""
    questions = {
        "family": {"type": "choice", "instructions": "Which kind of electronic component is this part?", "criteria": dict(families)},
        "location": {"type": "choice", "instructions": "Which storage location should this part be kept in? Prefer a location whose description names this kind of part.",
                     "criteria": dict(locations[:MAX_LOCATIONS])},
    }
    body = json.dumps({"state": part, "model": "jev-latest", "questions": questions}).encode()
    request = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": "Tally/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            answers = json.loads(response.read(1_000_000))["answers"]
    except urllib.error.HTTPError as error:
        raise JevError("Jev rejected the API key." if error.code in (401, 403) else f"Jev answered HTTP {error.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise JevError("Jev could not be reached.") from None
    except (ValueError, KeyError):
        raise JevError("Jev sent an answer Tally could not read.") from None
    try:
        return {name: (answers[name]["choice"], answers[name].get("confidence", 0)) for name in questions}
    except (KeyError, TypeError):
        raise JevError("Jev sent an answer Tally could not read.") from None
