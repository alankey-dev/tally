import re
from difflib import SequenceMatcher


def normalise(text):
    text = re.sub(r"\bseed\b", "seeed", text.casefold())
    text = re.sub(r"esp[\s-]+(32|8266)", r"esp\1", text)
    return re.findall(r"[a-z0-9]+", text)


def score(query, text):
    wanted, words = normalise(query), normalise(text)
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
