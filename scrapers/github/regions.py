"""Map a GitHub profile's free-text location to a target region.

GitHub has no structured country field -- `location` is whatever the user typed
("Berlin", "SF Bay Area", "são paulo, brasil", "Remote"). This module turns that
string into a country + region, and supplies the `location:` search terms used
to enumerate users in the first place.

Four regions are targets: the US, the rest of North America (Canada, Mexico),
Europe, and South America. Anything that does not match (Remote, Bangalore,
Tokyo, blank, ...) classifies as ``None`` and is skipped by the scraper -- i.e.
the filter is an allow-list, so an unknown place is excluded rather than guessed
at.

Ambiguous place names ("Cambridge", "Birmingham", "Cordoba", "San Jose") are
deliberately left out of the tables: a wrong country is worse than a miss.
"""
from __future__ import annotations

import re
import unicodedata
from itertools import zip_longest

# ---- country tables -------------------------------------------------------
# terms:  extra strings (beyond the name) that identify the country in a
#         free-text location, e.g. native names and ISO codes.
# cities: place names unique enough to attribute to this country on their own.
#         They double as `location:` search terms, which is how we get past the
#         1000-results-per-query cap on the search API.
#
# Russia and Turkey are transcontinental and excluded. The US has a region of its
# own rather than sharing `north_america` with Canada and Mexico: it is the
# single biggest population here and worth targeting (and filtering) on its own.
COUNTRIES: list[dict] = [
    # --- United States ---
    {"name": "United States", "code": "US", "region": "us",
     "terms": ["usa", "u.s.a.", "u.s.", "us", "united states of america"],
     "cities": [
         "San Francisco", "Bay Area", "Silicon Valley", "Palo Alto",
         "Mountain View", "Sunnyvale", "Oakland", "Berkeley",
         "New York", "NYC", "Brooklyn", "Seattle", "Austin", "Los Angeles",
         "San Diego", "Boston", "Chicago", "Denver", "Boulder", "Portland",
         "Atlanta", "Miami", "Dallas", "Houston", "Philadelphia", "Phoenix",
         "Minneapolis", "Pittsburgh", "Salt Lake City", "Nashville", "Detroit",
         "Raleigh", "Charlotte", "Washington DC",
     ]},

    # --- North America (the US is its own region, above) ---
    # "Vancouver" and "Waterloo" are also US towns, and "London" is Ontario's as
    # well as England's -- all three are resolved by the postal-code and country
    # rules below ("Vancouver, WA" -> US, "London, ON" -> Canada), which outrank
    # a bare city name.
    {"name": "Canada", "code": "CA", "region": "north_america", "terms": [],
     "cities": ["Toronto", "Vancouver", "Montreal", "Montréal", "Ottawa",
                "Calgary", "Edmonton", "Winnipeg", "Quebec City", "Waterloo",
                "Kitchener", "Mississauga", "Burnaby"]},
    {"name": "Mexico", "code": "MX", "region": "north_america",
     "terms": ["méxico"],
     "cities": ["Mexico City", "Ciudad de México", "CDMX", "Guadalajara",
                "Monterrey", "Puebla", "Querétaro", "Queretaro", "Tijuana",
                "Mérida", "Merida"]},

    # --- Europe ---
    {"name": "United Kingdom", "code": "GB", "region": "europe",
     "terms": ["uk", "u.k.", "great britain", "england", "scotland", "wales",
               "northern ireland"],
     "cities": ["London", "Manchester", "Edinburgh", "Glasgow", "Bristol",
                "Leeds", "Sheffield", "Brighton"]},
    {"name": "Germany", "code": "DE", "region": "europe",
     "terms": ["deutschland"],
     "cities": ["Berlin", "Munich", "München", "Hamburg", "Cologne", "Köln",
                "Frankfurt", "Stuttgart", "Düsseldorf", "Leipzig", "Dresden",
                "Nuremberg", "Karlsruhe"]},
    {"name": "France", "code": "FR", "region": "europe", "terms": [],
     "cities": ["Paris", "Lyon", "Toulouse", "Nantes", "Bordeaux", "Marseille",
                "Lille", "Grenoble"]},
    {"name": "Spain", "code": "ES", "region": "europe", "terms": ["españa", "espana"],
     "cities": ["Madrid", "Barcelona", "Valencia", "Seville", "Sevilla",
                "Bilbao", "Malaga", "Zaragoza"]},
    {"name": "Italy", "code": "IT", "region": "europe", "terms": ["italia"],
     "cities": ["Milan", "Milano", "Rome", "Roma", "Turin", "Torino", "Naples",
                "Napoli", "Florence", "Firenze", "Bologna"]},
    {"name": "Netherlands", "code": "NL", "region": "europe",
     "terms": ["holland", "nederland", "the netherlands"],
     "cities": ["Amsterdam", "Rotterdam", "Utrecht", "Eindhoven", "The Hague",
                "Delft", "Groningen"]},
    {"name": "Portugal", "code": "PT", "region": "europe", "terms": [],
     "cities": ["Lisbon", "Lisboa", "Porto", "Braga", "Coimbra"]},
    {"name": "Ireland", "code": "IE", "region": "europe", "terms": ["éire", "eire"],
     "cities": ["Dublin", "Cork", "Galway"]},
    {"name": "Belgium", "code": "BE", "region": "europe", "terms": ["belgique", "belgië"],
     "cities": ["Brussels", "Bruxelles", "Antwerp", "Ghent", "Leuven"]},
    {"name": "Switzerland", "code": "CH", "region": "europe",
     "terms": ["schweiz", "suisse"],
     "cities": ["Zurich", "Zürich", "Geneva", "Lausanne", "Basel", "Bern"]},
    {"name": "Austria", "code": "AT", "region": "europe", "terms": ["österreich"],
     "cities": ["Vienna", "Wien", "Graz", "Linz", "Salzburg"]},
    {"name": "Poland", "code": "PL", "region": "europe", "terms": ["polska"],
     "cities": ["Warsaw", "Warszawa", "Krakow", "Kraków", "Wroclaw", "Wrocław",
                "Poznan", "Poznań", "Gdansk", "Gdańsk", "Lodz", "Katowice"]},
    {"name": "Czechia", "code": "CZ", "region": "europe",
     "terms": ["czech republic", "cesko", "česko"],
     "cities": ["Prague", "Praha", "Brno", "Ostrava"]},
    {"name": "Sweden", "code": "SE", "region": "europe", "terms": ["sverige"],
     "cities": ["Stockholm", "Gothenburg", "Göteborg", "Malmö", "Malmo",
                "Uppsala", "Linköping"]},
    {"name": "Norway", "code": "NO", "region": "europe", "terms": ["norge"],
     "cities": ["Oslo", "Bergen", "Trondheim", "Stavanger"]},
    {"name": "Denmark", "code": "DK", "region": "europe", "terms": ["danmark"],
     "cities": ["Copenhagen", "København", "Aarhus", "Odense"]},
    {"name": "Finland", "code": "FI", "region": "europe", "terms": ["suomi"],
     "cities": ["Helsinki", "Espoo", "Tampere", "Oulu", "Turku"]},
    {"name": "Iceland", "code": "IS", "region": "europe", "terms": ["ísland"],
     "cities": ["Reykjavik", "Reykjavík"]},
    {"name": "Ukraine", "code": "UA", "region": "europe", "terms": ["україна"],
     "cities": ["Kyiv", "Kiev", "Lviv", "Kharkiv", "Odesa", "Dnipro",
                "Vinnytsia"]},
    {"name": "Romania", "code": "RO", "region": "europe", "terms": ["românia"],
     "cities": ["Bucharest", "București", "Cluj", "Cluj-Napoca", "Timisoara",
                "Iasi", "Brasov"]},
    {"name": "Bulgaria", "code": "BG", "region": "europe", "terms": [],
     "cities": ["Sofia", "Plovdiv", "Varna", "Burgas"]},
    {"name": "Greece", "code": "GR", "region": "europe", "terms": ["hellas", "ελλάδα"],
     "cities": ["Athens", "Thessaloniki", "Patras", "Heraklion"]},
    {"name": "Hungary", "code": "HU", "region": "europe", "terms": ["magyarország"],
     "cities": ["Budapest", "Debrecen", "Szeged"]},
    {"name": "Serbia", "code": "RS", "region": "europe", "terms": ["srbija"],
     "cities": ["Belgrade", "Beograd", "Novi Sad", "Nis"]},
    {"name": "Croatia", "code": "HR", "region": "europe", "terms": ["hrvatska"],
     "cities": ["Zagreb", "Split", "Rijeka", "Osijek"]},
    {"name": "Slovenia", "code": "SI", "region": "europe", "terms": ["slovenija"],
     "cities": ["Ljubljana", "Maribor"]},
    {"name": "Slovakia", "code": "SK", "region": "europe", "terms": ["slovensko"],
     "cities": ["Bratislava", "Kosice", "Košice"]},
    {"name": "Estonia", "code": "EE", "region": "europe", "terms": ["eesti"],
     "cities": ["Tallinn", "Tartu"]},
    {"name": "Latvia", "code": "LV", "region": "europe", "terms": ["latvija"],
     "cities": ["Riga", "Rīga"]},
    {"name": "Lithuania", "code": "LT", "region": "europe", "terms": ["lietuva"],
     "cities": ["Vilnius", "Kaunas", "Klaipeda"]},
    {"name": "Belarus", "code": "BY", "region": "europe", "terms": ["беларусь"],
     "cities": ["Minsk", "Gomel"]},
    {"name": "Moldova", "code": "MD", "region": "europe", "terms": [],
     "cities": ["Chisinau", "Chișinău"]},
    {"name": "Albania", "code": "AL", "region": "europe", "terms": ["shqipëri"],
     "cities": ["Tirana", "Tiranë"]},
    {"name": "Bosnia and Herzegovina", "code": "BA", "region": "europe",
     "terms": ["bosnia", "herzegovina", "bosna"],
     "cities": ["Sarajevo", "Banja Luka", "Mostar"]},
    {"name": "North Macedonia", "code": "MK", "region": "europe",
     "terms": ["macedonia", "makedonija"], "cities": ["Skopje", "Bitola"]},
    {"name": "Montenegro", "code": "ME", "region": "europe", "terms": ["crna gora"],
     "cities": ["Podgorica"]},
    {"name": "Kosovo", "code": "XK", "region": "europe", "terms": ["kosovë"],
     "cities": ["Pristina", "Prishtina"]},
    {"name": "Cyprus", "code": "CY", "region": "europe", "terms": [],
     "cities": ["Nicosia", "Limassol"]},
    {"name": "Malta", "code": "MT", "region": "europe", "terms": [],
     "cities": ["Valletta", "Sliema"]},
    {"name": "Luxembourg", "code": "LU", "region": "europe", "terms": [], "cities": []},
    {"name": "Andorra", "code": "AD", "region": "europe", "terms": [], "cities": []},
    {"name": "Monaco", "code": "MC", "region": "europe", "terms": [], "cities": []},
    {"name": "Liechtenstein", "code": "LI", "region": "europe", "terms": [], "cities": []},

    # --- South America ---
    {"name": "Brazil", "code": "BR", "region": "south_america", "terms": ["brasil"],
     "cities": ["São Paulo", "Sao Paulo", "Rio de Janeiro", "Belo Horizonte",
                "Brasília", "Brasilia", "Curitiba", "Porto Alegre",
                "Florianópolis", "Florianopolis", "Recife", "Fortaleza",
                "Campinas", "Belém", "Goiânia", "Manaus"]},
    {"name": "Argentina", "code": "AR", "region": "south_america", "terms": [],
     "cities": ["Buenos Aires", "Rosario", "La Plata", "Mendoza",
                "Mar del Plata", "San Miguel de Tucumán"]},
    {"name": "Chile", "code": "CL", "region": "south_america", "terms": [],
     "cities": ["Valparaíso", "Valparaiso", "Concepción", "Viña del Mar"]},
    {"name": "Colombia", "code": "CO", "region": "south_america", "terms": [],
     "cities": ["Bogotá", "Bogota", "Medellín", "Medellin", "Barranquilla",
                "Bucaramanga"]},
    {"name": "Peru", "code": "PE", "region": "south_america", "terms": ["perú"],
     "cities": ["Lima", "Arequipa", "Trujillo", "Cusco"]},
    {"name": "Uruguay", "code": "UY", "region": "south_america", "terms": [],
     "cities": ["Montevideo", "Punta del Este"]},
    {"name": "Ecuador", "code": "EC", "region": "south_america", "terms": [],
     "cities": ["Quito", "Guayaquil", "Cuenca"]},
    {"name": "Paraguay", "code": "PY", "region": "south_america", "terms": [],
     "cities": ["Asunción", "Asuncion"]},
    {"name": "Bolivia", "code": "BO", "region": "south_america", "terms": [],
     "cities": ["Cochabamba", "Santa Cruz de la Sierra"]},
    {"name": "Venezuela", "code": "VE", "region": "south_america", "terms": [],
     "cities": ["Caracas", "Maracaibo"]},
    {"name": "Guyana", "code": "GY", "region": "south_america", "terms": [],
     "cities": []},
    {"name": "Suriname", "code": "SR", "region": "south_america", "terms": [],
     "cities": ["Paramaribo"]},
]

REGIONS = {"us": "US", "north_america": "North America", "europe": "Europe",
           "south_america": "South America"}
ALL_REGIONS = tuple(REGIONS)

# US state names and postal codes. Names are matched like any other term; the
# two-letter codes are matched only in the "City, ST" shape (see _us_state),
# because bare "OR" / "IN" / "ME" are ordinary English words.
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "Washington DC",
}
# Georgia is also a country (Tbilisi), which is not a target region -- matching
# the bare word would mis-file Georgians as American. "Austin, GA" still lands
# via the postal-code rule below, which is unambiguous.
_AMBIGUOUS_STATES = {"Georgia"}

# Canadian provinces in the same "Toronto, ON" shape. Newfoundland (NL), Prince
# Edward Island (PE) and Saskatchewan (SK) are left out of the *code* table --
# they are also the ISO codes of the Netherlands, Peru and Slovakia, all of them
# targets, so ", NL" is not proof of Canada. Their names below still are.
CA_PROVINCES = {"ON": "Ontario", "QC": "Quebec", "BC": "British Columbia",
                "AB": "Alberta", "MB": "Manitoba", "NS": "Nova Scotia",
                "NB": "New Brunswick"}
# Spelled out, every province is safe to match -- "Hamilton, Ontario" is the
# common way to write a Canadian town we do not list. The one place this loses
# is "Ontario, CA", which is a real city in California: it comes out Canadian.
# That is the cheap side of the trade -- both are target regions, so the user is
# still scraped, only the country label is wrong; an unmatched location is
# skipped outright. "Ontario, California" spelled out still lands in the US, as
# the subdivision tier prefers the longer match.
CA_PROVINCE_NAMES = (*CA_PROVINCES.values(), "Québec", "Saskatchewan",
                     "Newfoundland", "Prince Edward Island")
# ...and conversely, these US postal codes double as the ISO code of a country
# people really do write ("Berlin, DE", "Toronto, CA", "Cali, CO"), so they are
# not allowed to *outrank* a recognised city -- only to act as the last-resort
# US signal, exactly as they did before.
_ISO_LOOKALIKE_STATES = {"AL", "AR", "CA", "CO", "DE", "ID", "IL", "IN", "MA",
                         "MD", "ME", "MT", "PA"}

_US = next(c for c in COUNTRIES if c["code"] == "US")
_CA = next(c for c in COUNTRIES if c["code"] == "CA")


def fold(text: str) -> str:
    """Lowercase, strip accents, collapse whitespace -- 'São Paulo' -> 'sao paulo'."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _build_index() -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
    """(countries, subdivisions, cities): folded term -> country, in three tiers.

    They are ranked, not merged, because the tiers make claims of different
    strength about the same string. A country name settles it outright
    ("Waterloo, Belgium" is Belgian, not Canadian); failing that a state or
    province name does ("Vancouver, Washington" is not British Columbia); only
    then does a city name get a say. Merging them and taking the longest match
    would let "Waterloo" outvote "Belgium" on sheer length.
    """
    countries: dict[str, dict] = {}
    subdivisions: dict[str, dict] = {}
    cities: dict[str, dict] = {}
    for country in COUNTRIES:
        for name in [country["name"], *country["terms"]]:
            countries.setdefault(fold(name), country)
        for name in country["cities"]:
            cities.setdefault(fold(name), country)
    for name in US_STATES.values():
        if name not in _AMBIGUOUS_STATES:
            subdivisions.setdefault(fold(name), _US)
    for name in CA_PROVINCE_NAMES:
        subdivisions.setdefault(fold(name), _CA)
    return countries, subdivisions, cities


_COUNTRIES_IDX, _SUBDIVISIONS_IDX, _CITIES_IDX = _build_index()
# Longest first within a tier, so "united states" wins over "us", and
# "california" over "ontario" in "Ontario, California".
_COUNTRY_TERMS = sorted(_COUNTRIES_IDX, key=len, reverse=True)
_SUBDIVISION_TERMS = sorted(_SUBDIVISIONS_IDX, key=len, reverse=True)
_CITY_TERMS = sorted(_CITIES_IDX, key=len, reverse=True)
_TERM_RES = {t: re.compile(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])")
             for t in (*_COUNTRIES_IDX, *_SUBDIVISIONS_IDX, *_CITIES_IDX)}
_STATE_RE = re.compile(r",\s*([A-Z]{2})\b")

# Cities the US has one of too. On its own the name means the foreign one (a
# bare "Vancouver" is British Columbia), but a US postal code in the same string
# is the more specific statement and wins: "Vancouver, WA", "Waterloo, IA".
_SHARED_WITH_US = {"vancouver", "waterloo"}


def _codes(raw_location: str) -> list[str]:
    """Two-letter codes in the 'Austin, TX' shape -- uppercase, after a comma."""
    return [m.group(1) for m in _STATE_RE.finditer(raw_location or "")]


def _us_state(codes: list[str]) -> bool:
    """True if a code can only be a US state -- i.e. is not also a country's."""
    return any(c in US_STATES and c not in _ISO_LOOKALIKE_STATES for c in codes)


def _match(terms: list[str], folded: str) -> tuple[str, tuple[int, int]] | None:
    """The first (longest) term of this tier in the location, with where it hit."""
    for term in terms:
        found = _TERM_RES[term].search(folded)
        if found:
            return term, found.span()
    return None


def _within(inner: tuple[int, int], outer: tuple[int, int]) -> bool:
    """True if one match is merely part of the other -- 'Mexico' in 'New Mexico'."""
    return inner != outer and outer[0] <= inner[0] and inner[1] <= outer[1]


def _hit(country: dict) -> dict:
    return {"name": country["name"], "code": country["code"],
            "region": country["region"]}


def classify(location: str) -> dict | None:
    """Return {name, code, region} for a free-text location, or None if off-target.

    None means "not one of the target regions" -- including locations we simply
    do not recognise ("Remote", "/dev/null", ""). The caller skips those users.
    """
    if not location or not location.strip():
        return None
    folded = fold(location)
    codes = _codes(location)

    # 1. A country name outranks everything -- except when the words it matched
    #    are only part of a state's name: "New Mexico" is a US state, not Mexico.
    country = _match(_COUNTRY_TERMS, folded)
    subdivision = _match(_SUBDIVISION_TERMS, folded)
    if country and not (subdivision and _within(country[1], subdivision[1])):
        return _hit(_COUNTRIES_IDX[country[0]])

    # 2. Then a state or province name: "Vancouver, Washington" is not the BC one.
    if subdivision:
        return _hit(_SUBDIVISIONS_IDX[subdivision[0]])

    # 3. "Toronto, ON" -- a province code, which is unambiguous (see CA_PROVINCES).
    if any(code in CA_PROVINCES for code in codes):
        return _hit(_CA)

    # 4. City names, except that a city the US shares yields to a US postal code.
    city = _match(_CITY_TERMS, folded)
    if city and not (city[0] in _SHARED_WITH_US and _us_state(codes)):
        return _hit(_CITIES_IDX[city[0]])

    # 5. Last resort: any state code, including the ones that double as country
    #    codes -- ", CA" is California far more often than it is Canada, but it
    #    only gets to say so once no city has spoken.
    if any(code in US_STATES for code in codes):
        return _hit(_US)
    return None


def search_terms(regions=ALL_REGIONS) -> list[tuple[str, dict]]:
    """`location:` query terms for the requested regions, as (term, country).

    Each country contributes its own name plus its cities: GitHub's search API
    caps any single query at 1000 results, so a country-sized query alone would
    leave most of a big country unseen. Cities are the cheapest way to shard it.

    The terms are *interleaved* by country rather than grouped: every country's
    name first, then every country's biggest city, then their second city, and
    so on. Grouped, one country's terms would swallow an entire run -- Brazil is
    a name plus 16 cities, each of which is six follower shards of up to 1000
    users, so a walk that starts there does not reach Europe for days. Round
    robin means a run stopped at any point has covered every country to a
    similar depth, and the deepest (smallest-city) terms are the ones that go
    unwalked -- which is the right thing to lose.
    """
    wanted = set(regions)
    per_country: list[list[tuple[str, dict]]] = []
    for country in COUNTRIES:
        if country["region"] not in wanted:
            continue
        meta = {"name": country["name"], "code": country["code"],
                "region": country["region"]}
        per_country.append([(term, meta)
                            for term in [country["name"], *country["cities"]]])
    # zip_longest over the per-country queues == one pass per "depth" of term.
    return [pair for depth in zip_longest(*per_country) for pair in depth
            if pair is not None]
