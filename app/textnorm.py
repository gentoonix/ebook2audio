"""Turn written forms (numbers, years, money, times, abbreviations) into the words a narrator would say.

Done in the app so results don't depend on the TTS server's own normaliser.
"""
import re

from num2words import num2words

_SCALE = {"thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000, "trillion": 10**12}
_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}

ABBREV_WORDS = {
    "Mr": "Mister", "Mrs": "Missus", "Ms": "Miz", "Dr": "Doctor", "Prof": "Professor",
    "Capt": "Captain", "Col": "Colonel", "Gen": "General", "Lt": "Lieutenant", "Sgt": "Sergeant",
    "Cmdr": "Commander", "Rev": "Reverend", "Sen": "Senator", "Rep": "Representative",
    "Gov": "Governor", "Pres": "President", "Hon": "Honorable", "Jr": "Junior", "Sr": "Senior",
    "St": "Saint", "Mt": "Mount", "vs": "versus", "Inc": "Incorporated", "Ltd": "Limited",
    "Co": "Company", "Corp": "Corporation",
}


def _words(n: int) -> str:
    return num2words(n).replace(",", "").replace(" and ", " ")


def _int(s: str) -> str:
    s = s.replace(",", "")
    if len(s) > 15:                       # phone/serial-like: read digit by digit
        return " ".join(_words(int(c)) for c in s)
    if len(s) > 1 and s[0] == "0":        # 007 -> "zero zero seven"
        return " ".join(_words(int(c)) for c in s)
    return _words(int(s))


def _year(n: int) -> str:
    if 2000 <= n <= 2009:
        return _words(n)                  # two thousand five
    hi, lo = divmod(n, 100)
    if lo == 0:
        return f"{_words(hi)} hundred"    # nineteen hundred
    return f"{_words(hi)} {'oh ' + _words(lo) if lo < 10 else _words(lo)}"


def _decimal(whole: str, frac: str) -> str:
    return f"{_int(whole) if whole else 'zero'} point " + " ".join(_words(int(c)) for c in frac)


def _num(s: str) -> str:
    s = s.replace(",", "")
    if "." in s:
        w, f = s.split(".", 1)
        return _decimal(w, f) if f else _int(w)
    return _int(s)


def _roman(s: str) -> int | None:
    if not re.fullmatch(r"M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})", s) or not s:
        return None
    total = 0
    for i, ch in enumerate(s):
        v = _ROMAN[ch]
        total += -v if i + 1 < len(s) and _ROMAN[s[i + 1]] > v else v
    return total


def _money(m: re.Match) -> str:
    sym, amount, scale = m.group(1), m.group(2), (m.group(3) or "").lower()
    unit = {"$": "dollar", "£": "pound", "€": "euro"}[sym]
    if scale:
        return f"{_num(amount)} {scale} {unit}s"
    whole, _, cents = amount.replace(",", "").partition(".")
    n = int(whole or 0)
    out = f"{_int(whole or '0')} {unit}{'' if n == 1 else 's'}"
    if cents and int(cents[:2].ljust(2, "0")):
        c = int(cents[:2].ljust(2, "0"))
        sub = {"$": "cent", "£": "penny" if c == 1 else "pence", "€": "cent"}[sym]
        out += f" and {_words(c)} {sub}{'s' if sym != '£' and c != 1 else ''}"
    return out


def _time(m: re.Match) -> str:
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if h > 24 or mi > 59:
        return m.group(0)
    out = _words(h)
    if mi == 0:
        out += "" if ap else " o'clock"
    else:
        out += f" oh {_words(mi)}" if mi < 10 else f" {_words(mi)}"
    if ap:
        out += " " + " ".join(re.sub(r"[^apm]", "", ap.lower()))
    return out


def normalize_numbers(t: str) -> str:
    # Roman numerals after a heading word: "Chapter IV" -> "Chapter four"
    t = re.sub(r"\b(Chapter|Part|Book|Act|Volume|Section|Canto)\s+([IVXLCDM]+)\b",
               lambda m: f"{m.group(1)} {_words(_roman(m.group(2)))}" if _roman(m.group(2)) else m.group(0), t)
    # Titles/abbreviations followed by a period
    t = re.sub(r"\b(" + "|".join(ABBREV_WORDS) + r")\.(?=\s|$)",
               lambda m: ABBREV_WORDS[m.group(1)] + ("," if m.group(1) in ("Jr", "Sr") and False else ""), t)
    t = re.sub(r"\b(Mr|Mrs|Ms|Dr)(?=\s+[A-Z])", lambda m: ABBREV_WORDS[m.group(1)], t)
    t = re.sub(r"\be\.g\.", "for example", t, flags=re.I)
    t = re.sub(r"\bi\.e\.", "that is", t, flags=re.I)
    t = re.sub(r"\betc\.", "et cetera.", t)
    t = t.replace("&", " and ")
    # Money
    t = re.sub(r"([$£€])\s?(\d+(?:,\d{3})*(?:\.\d+)?)(?:\s?(thousand|million|billion|trillion)\b)?", _money, t, flags=re.I)
    # Percent, degrees
    t = re.sub(r"(\d+(?:,\d{3})*(?:\.\d+)?)\s?%", lambda m: f"{_num(m.group(1))} percent", t)
    t = re.sub(r"(\d)\s?°\s?([CF])\b", lambda m: f"{m.group(1)} degrees {'Celsius' if m.group(2)=='C' else 'Fahrenheit'}", t)
    t = re.sub(r"(\d)\s?°", r"\1 degrees", t)
    # Clock times
    t = re.sub(r"\b(\d{1,2}):(\d{2})(?:\s?([ap]\.?m\.?))?(?![\d:])", _time, t, flags=re.I)
    # Ordinals
    t = re.sub(r"\b(\d+(?:,\d{3})*)(st|nd|rd|th)\b",
               lambda m: num2words(int(m.group(1).replace(",", "")), to="ordinal").replace(",", "").replace(" and ", " "), t)
    # Decades: 1980s / 80s
    t = re.sub(r"\b(1[0-9]|20)(\d)0s\b", lambda m: _decade(m), t)
    # Phone numbers: read digit by digit
    t = re.sub(r"\b(?:\d{3}[-. ])?\d{3}[-.]\d{4}\b",
               lambda m: ", ".join(" ".join(_words(int(c)) for c in g) for g in re.findall(r"\d+", m.group(0))), t)
    t = re.sub(r"\b(\d)\s?[x×]\s?(\d)\b", lambda m: f"{_words(int(m.group(1)))} by {_words(int(m.group(2)))}", t)
    # Ranges: 10-12 -> 10 to 12
    t = re.sub(r"\b(\d+(?:,\d{3})*)\s?[-–]\s?(\d+(?:,\d{3})*)\b", r"\1 to \2", t)
    # Years: four digits, no comma, 1100-2099, not part of a larger number
    t = re.sub(r"(?<![\d,.])(1[1-9]\d\d|20\d\d)(?![\d]|,\d|\.\d)", lambda m: _year(int(m.group(1))), t)
    t = re.sub(r"\b(\d+(?:,\d{3})*)s\b", lambda m: _int(m.group(1)) + "s", t)   # 2500s
    # Everything else numeric
    t = re.sub(r"(?<![\w.])\d+(?:,\d{3})*(?:\.\d+)?(?![\w])|(?<![\w.])\.\d+", lambda m: _num(m.group(0).rstrip(",")), t)
    t = re.sub(r"\b(\d+)(?=[A-Za-z])", lambda m: _int(m.group(1)) + " ", t)  # 3D, 4x4
    return re.sub(r"\s+", " ", t).strip()


def _decade(m: re.Match) -> str:
    n = int(m.group(0)[:-1])
    w = _year(n)
    return (w[:-1] + "ies") if w.endswith("y") else w + "s"


# ---- second pass: things that make TTS sound robotic ----
_KEEP_CAPS = {"US", "USA", "UK", "UN", "EU", "FBI", "CIA", "NSA", "NYC", "LA", "DC", "TV", "DNA", "AI", "PC", "ID", "OK",
              "CEO", "CFO", "CIO", "VIP", "UFO", "GPS", "USB", "HIV", "AIDS", "NBA", "NFL", "BBC", "CNN", "IRS", "FDA",
              "DJ", "MP", "PhD", "MD", "RN", "ER", "ICU", "SUV", "RV", "ATM", "PIN", "HR", "IT", "WWII", "WWI", "NATO", "NASA"}
_UNITS = {"km": "kilometers", "kg": "kilograms", "mph": "miles per hour", "ft": "feet", "lb": "pounds", "lbs": "pounds",
          "oz": "ounces", "cm": "centimeters", "mm": "millimeters", "ml": "milliliters", "mi": "miles", "kph": "kilometers per hour",
          "hr": "hours", "hrs": "hours", "min": "minutes", "sec": "seconds", "gb": "gigabytes", "mb": "megabytes", "kb": "kilobytes"}
_FRAC = {"½": "one half", "⅓": "one third", "⅔": "two thirds", "¼": "one quarter", "¾": "three quarters"}


def _caps(m: re.Match) -> str:
    w = m.group(0)
    return w if w in _KEEP_CAPS else w.lower()


def tidy(t: str) -> str:
    t = t.replace("\u00ad", "")                                   # soft hyphens
    t = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", t)                  # hyphenated line breaks
    if not re.search(r"[A-Za-z0-9]", t):                         # "* * *", "---", "~~~": scene break, not speech
        return ""
    letters = re.sub(r"[^A-Za-z]", "", t)
    if len(letters) > 6 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        t = t.lower()                                             # all-caps heading/shouting: don't spell it out
        t = re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), t)
    t = re.sub(r"\b[A-Z]{3,}\b", _caps, t)                        # SHOUTED words are read, not spelled
    t = re.sub(r"\b(THE|AND|BUT|YOU|NOT|ALL|NOW|WHY|HOW|WHO|YES|HEY|RUN|NO|OH|GO|SO|DO|IF|IT|ME|MY|WE|HE|OR|ON|IN|AT|TO|OF|BE)\b",
               lambda m: m.group(0).lower(), t)
    for k, v in _FRAC.items():
        t = t.replace(k, f" {v} ")
    t = re.sub(r"\b[Nn]o\.\s?(\d)", r"number \1", t)
    t = re.sub(r"\b(\d+)\s?'\s?(\d{1,2})\s?\"", lambda m: f"{m.group(1)} feet {m.group(2)} inches", t)   # 6'2"
    t = re.sub(r"\b(\d+(?:\.\d+)?)\s?(" + "|".join(_UNITS) + r")\b", lambda m: f"{m.group(1)} {_UNITS[m.group(2).lower()]}", t, flags=re.I)
    t = re.sub(r"https?://(?:www\.)?([\w.-]+)[^\s]*", lambda m: m.group(1).replace(".", " dot "), t)
    t = re.sub(r"\b([\w.+-]+)@([\w-]+)\.(\w+)\b", r"\1 at \2 dot \3", t)
    t = re.sub(r"\b([A-Za-z]{2,})\.com\b", r"\1 dot com", t)
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    t = re.sub(r"([!?.])\1{3,}", r"\1\1\1", t)                    # "!!!!!!" -> "!!!"
    return t


def speakable(t: str) -> str:
    """Full text normalisation: tidy first (caps, scene breaks, units), then numbers."""
    return normalize_numbers(tidy(t)) if tidy(t) else ""
