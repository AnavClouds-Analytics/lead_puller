"""Seamless contact-search filter definitions.

Single source of truth for the filter editor (served to the browser), the
Claude prompt-to-filters tool schema, and request validation.
Values come from Seamless's POST /search/contacts reference.
"""

INDUSTRIES = [
    "Aerospace & Defense", "Airlines & Aviation", "Aviation & Aerospace", "Defense & Space", "Military",
    "Agriculture", "Farming", "Horticulture", "Ranching", "Tobacco", "Apparel & Fashion", "Textiles",
    "Automotive", "Chemicals & Materials", "Chemicals", "Plastics", "Consumer Goods & Retail",
    "Consumer Goods", "Luxury Goods & Jewelry", "Retail", "Sporting Goods", "Education & Training",
    "E-Learning", "Education Management", "Higher Education", "Libraries", "Primary/Secondary Education",
    "Electronics & Hardware", "Computer Hardware", "Consumer Electronics",
    "Electrical & Electronic Manufacturing", "Semiconductors", "Energy & Utilities", "Oil & Energy",
    "Utilities", "Entertainment", "Animation", "Arts & Crafts", "Computer Games", "Fine Art",
    "Gambling & Casinos", "Mobile Games", "Motion Pictures & Film", "Music", "Performing Arts",
    "Photography", "Recreational Facilities & Services", "Sports", "Environmental",
    "Environmental Services", "Renewables & Environment", "Finance & Banking", "Banking",
    "Capital Markets", "Financial Services", "Investment Banking", "Investment Management",
    "Venture Capital & Private Equity", "Food & Beverage", "Dairy", "Fishery", "Food & Beverages",
    "Food Production", "Restaurants", "Supermarkets", "Wine & Spirits", "Government & Public Policy",
    "Executive Office", "Government Administration", "Government Relations", "Judiciary",
    "Law Enforcement", "Legislative Office", "Political Organization", "Public Policy", "Public Safety",
    "Health & Wellness", "Alternative Medicine", "Health, Wellness and Fitness", "Hospital & Health Care",
    "Medical Practice", "Mental Health Care", "Veterinary", "Hospitality & Tourism", "Events Services",
    "Hospitality", "Leisure, Travel & Tourism", "Museums & Institutions", "Household, Personal, & Beauty",
    "Consumer Services", "Cosmetics", "Furniture", "Individual & Family Services", "Insurance",
    "Internet & E-Commerce", "Internet", "Manufacturing & Engineering", "Civil Engineering",
    "Industrial Automation", "Machinery", "Mechanical or Industrial Engineering", "Railroad Manufacture",
    "Shipbuilding", "Marketing & Media", "Broadcast Media", "Graphic Design", "Marketing & Advertising",
    "Media Production", "Newspapers", "Online Media", "Printing", "Public Relations & Communications",
    "Publishing", "Writing & Editing", "Metals, Mining & Materials", "Building Materials",
    "Glass, Ceramics & Concrete", "Mining & Metals", "Paper & Forest Products", "Non-Profit",
    "Fund-Raising", "Non-Profit Organization Management", "Philanthropy", "Religious Institutions",
    "Pharmaceuticals & Medical Devices", "Biotechnology", "Medical Devices", "Nanotechnology",
    "Pharmaceuticals", "Professional Services & Consulting", "Accounting", "Alternative Dispute Resolution",
    "Civic & Social Organization", "Design", "Human Resources", "International Affairs",
    "International Trade & Development", "Law Practice", "Legal Services", "Management Consulting",
    "Market Research", "Outsourcing/Offshoring", "Professional Training & Coaching", "Program Development",
    "Research", "Security & Investigations", "Staffing & Recruiting", "Think Tanks",
    "Real Estate & Construction", "Architecture & Planning", "Commercial Real Estate", "Construction",
    "Facilities Services", "Real Estate", "Software & Information Technology",
    "Computer & Network Security", "Computer Software", "Information Services",
    "Information Technology & Services", "Software Development", "Telecommunications & Networking",
    "Computer Networking", "Telecommunications", "Wireless", "Transportation & Logistics",
    "Logistics & Supply Chain", "Maritime", "Package/Freight Delivery", "Packaging & Containers",
    "Translation & Localization", "Transportation/Trucking/Railroad", "Wholesale & Distribution",
    "Business Supplies & Equipment", "Import & Export", "Warehousing", "Wholesale",
]

MONEY = ["$0 - $100K", "$100K - $1M", "$1M - $5M", "$5M - $20M", "$20M - $50M", "$50M - $100M",
         "$100M - $500M", "$500M - $1B", "$1B+"]

import time

TIMEZONES = ["Eastern (EST)", "Central (CST)", "Mountain (MST)", "Pacific (PST)", "Alaska (AKST)", "Hawaii (HST)",
             "New Zealand (NST)", "Solomon (SST)", "Australia Eastern (AET)", "Australia Central (ACT)", "Japan (JST)",
             "China Taiwan (CTT)", "Vietnam (VST)", "Bangladesh (BST)", "India (IST)", "Indiana Eastern (IET)",
             "Pakistan Lahore (PLT)", "Near East (NET)", "Middle East (MET)", "Eastern African (EAT)",
             "(Arabic) Egypt (ART)", "Eastern European (EET)", "European Central (ECT)", "Greenwich Mean (GMT)",
             "Central African (CAT)", "Argentina (AGT)", "Brazil Eastern (BET)", "Canada Newfoundland (CNT)",
             "Puerto Rico and US Virgin Islands (PRT)", "Midway Islands (MIT)"]
LOC_TYPES = {"bothOR": "Person or company HQ", "contact": "Person's location only",
             "company": "Company HQ only", "bothAND": "Both person and company HQ"}

SENIORITY = ["C-Level", "VP", "Director", "Manager", "Senior", "Mid-Level", "Entry Level", "Other"]

# kind: "enum_list" (pick from options), "text_list" (free text tags), "choice" (single),
#       "bool" (tick box), "int" (whole number, min..max)
# group: how the editor groups fields
FILTERS = [
    {"key": "savedSearchId", "label": "Run a Seamless saved search (ID)", "kind": "int", "min": 0, "max": 10 ** 12,
     "group": "Seamless saved search",
     "help": "Build the search in the Seamless app (it has exclude on every filter), save it, paste its ID here. "
             "When set, Seamless uses ONLY the saved search; the Seamless filters below are ignored. "
             "This tool's skips (blocked companies, cooldown, people already pulled, Better matches, Skip) still apply."},
    {"key": "jobTitle", "label": "Job titles", "kind": "text_list", "max": 10, "group": "People",
     "help": "Matched by relevance. Include common variations (CTO, Chief Technology Officer, VP Engineering)."},
    {"key": "titlesExactMatch", "label": "Exact title match (Seamless)", "kind": "bool", "group": "People",
     "help": "Only titles exactly as typed above. Stricter than “Title must contain”."},
    {"key": "seniority", "label": "Seniority", "kind": "enum_list", "max": 5, "group": "People",
     "options": SENIORITY},
    {"key": "department", "label": "Department", "kind": "enum_list", "max": 5, "group": "People",
     "options": ["Sales", "Marketing", "Engineering", "Human Resources", "Finance", "IT", "Operations",
                 "Support", "Legal", "Project Management", "Other"]},
    {"key": "contactKeyword", "label": "Profile keywords", "kind": "text_list", "max": 10, "group": "People",
     "help": "Words in the person's profile, skills or bio."},
    {"key": "keywordsIsOr", "label": "Keywords: match any (instead of all)", "kind": "bool", "group": "People"},
    {"key": "contactCountry", "label": "Countries", "kind": "text_list", "max": 10, "group": "Location",
     "help": "Full country names, e.g. United States, Canada, Israel, Germany."},
    {"key": "contactState", "label": "States / regions", "kind": "text_list", "max": 10, "group": "Location"},
    {"key": "contactZipCode", "label": "Zip / postal codes", "kind": "text_list", "max": 10, "group": "Location"},
    {"key": "locations", "label": "Cities / places", "kind": "text_list", "max": 10, "group": "Location",
     "help": "City level, e.g. “Austin, Texas”, “Toronto, Ontario”. Checked against Seamless's place list (free)."},
    {"key": "locationRadius", "label": "Widen cities by (miles)", "kind": "choice", "group": "Location",
     "options": ["25", "50", "100", "250"]},
    {"key": "timezones", "label": "Time zones", "kind": "enum_list", "max": 10, "group": "Location",
     "options": TIMEZONES},
    {"key": "locationType", "label": "Location applies to", "kind": "choice", "group": "Location",
     "options": ["bothOR", "contact", "company", "bothAND"],
     "option_labels": LOC_TYPES},
    {"key": "timezoneType", "label": "Time zone applies to", "kind": "choice", "group": "Location",
     "options": ["bothOR", "contact", "company", "bothAND"], "option_labels": LOC_TYPES},
    {"key": "industry", "label": "Industries", "kind": "enum_list", "max": 5, "group": "Company",
     "options": INDUSTRIES},
    {"key": "industrySicCodes", "label": "SIC codes", "kind": "text_list", "max": 5, "group": "Company",
     "help": "e.g. 7372 (software). Combined with Industries."},
    {"key": "industryNaicsCodes", "label": "NAICS codes", "kind": "text_list", "max": 5, "group": "Company",
     "help": "e.g. 541511."},
    {"key": "companySize", "label": "Company size (employees)", "kind": "enum_list", "max": 10, "group": "Company",
     "options": ["0 - 1 (Self-employed)", "2 - 10", "11 - 50", "51 - 200", "201 - 500", "501 - 1,000",
                 "1,001 - 5,000", "5,001 - 10,000", "10,001+"]},
    {"key": "companyRevenue", "label": "Company revenue", "kind": "enum_list", "max": 10, "group": "Company",
     "options": MONEY},
    {"key": "technologies", "label": "Technologies used", "kind": "text_list", "max": 10, "group": "Company",
     "help": "e.g. Salesforce, HubSpot, AWS."},
    {"key": "technologiesIsOr", "label": "Technologies match", "kind": "choice", "group": "Company",
     "options": [True, False], "option_labels": {"true": "Any of them", "false": "All of them"}},
    {"key": "companyName", "label": "Only these companies", "kind": "text_list", "max": 100, "group": "Company"},
    {"key": "companyDomain", "label": "Only these company domains", "kind": "text_list", "max": 100, "group": "Company"},
    {"key": "companyType", "label": "Public or private", "kind": "choice", "group": "Company",
     "options": ["Public", "Private"]},
    {"key": "companyFoundedOn", "label": "Company founded", "kind": "enum_list", "max": 4, "group": "Company",
     "options": ["Less than 1 Year", "Last 1-3 Years", "Last 4-10 Years", "10+ Years"]},
    {"key": "companyLatestFundingClassifications", "label": "Latest funding round", "kind": "enum_list",
     "max": 14, "group": "Signals",
     "options": ["Angel", "Pre-Seed", "Seed", "Series A", "Series B", "Series C", "Series D", "Series E",
                 "Series F", "Series G", "Series H", "Series I", "Series J", "Other"]},
    {"key": "companyLatestFundingDates", "label": "Funded within (days)", "kind": "enum_list", "max": 1,
     "group": "Signals", "options": ["90", "180", "365", "1095"]},
    {"key": "companyLatestFundingTotals", "label": "Total funding raised", "kind": "enum_list", "max": 9,
     "group": "Signals", "options": MONEY},
    {"key": "newsTypes", "label": "Recent company news", "kind": "enum_list", "max": 8, "group": "Signals",
     "options": ["Acquisition", "Corporate Challenges", "Cost Cutting", "Expansion", "Investment",
                 "Leadership", "Partnership", "Recognition"]},
    {"key": "newsTypeDates", "label": "News within (days)", "kind": "enum_list", "max": 1, "group": "Signals",
     "options": ["60", "90", "180", "365"]},
    {"key": "jobChangeType", "label": "Recent job change", "kind": "choice", "group": "Signals",
     "options": ["New Hire", "New Promotion", "New Hire or New Promotion"]},
    {"key": "jobChangeDays", "label": "Job change within", "kind": "choice", "group": "Signals",
     "options": ["Last 60 Days", "Last 90 Days", "Last 180 Days", "Last 365 Days"]},
    {"key": "updatedWithinMonths", "label": "Only contacts updated in the last … months", "kind": "int", "min": 0,
     "max": 120, "group": "Better matches",
     "help": "Fresher records mean more emails found and fewer bounces. 0 = any age."},
    {"key": "pastCompany", "label": "Used to work at", "kind": "text_list", "max": 100, "group": "Better matches",
     "help": "Company names from their job history."},
    {"key": "pastCompanyRecentOnly", "label": "Only their most recent previous job", "kind": "bool",
     "group": "Better matches"},
    {"key": "titleMustContain", "label": "Title must contain", "kind": "text_list", "max": 30,
     "group": "Better matches", "local": True,
     "help": "Seamless matches titles loosely. People whose title has none of these words are skipped before any research credit is spent."},
    {"key": "requireCompanyWebsite", "label": "Must have a company website", "kind": "bool",
     "group": "Better matches", "local": True, "help": "No company website means no work email to find."},
    {"key": "requireLinkedIn", "label": "Must have a LinkedIn profile", "kind": "bool", "group": "Better matches",
     "local": True, "help": "Records without LinkedIn are more often out of date."},
    {"key": "roleMinMonths", "label": "At least … months in current role", "kind": "int", "min": 0, "max": 600,
     "group": "Better matches", "local": True, "help": "People whose start date is unknown are kept."},
    {"key": "roleMaxYears", "label": "At most … years in current role", "kind": "int", "min": 0, "max": 60,
     "group": "Better matches", "local": True},
    # Local exclusions: Seamless search has no "exclude" filters, so these are applied by this tool
    # to every result before it is kept (and before any research credit is spent).
    {"key": "excludeTitleWords", "label": "Skip titles containing", "kind": "text_list", "max": 30,
     "group": "Skip", "local": True,
     "help": "e.g. intern, assistant, recruiter. Case-insensitive."},
    {"key": "excludeCompanyWords", "label": "Skip companies containing", "kind": "text_list", "max": 30,
     "group": "Skip", "local": True, "help": "e.g. staffing, consulting, agency."},
    {"key": "excludeIndustries", "label": "Skip industries", "kind": "enum_list", "max": 30,
     "group": "Skip", "local": True, "options": INDUSTRIES},
    {"key": "excludeDomains", "label": "Skip company domains", "kind": "text_list", "max": 200,
     "group": "Skip", "local": True},
    {"key": "excludeCountries", "label": "Skip countries", "kind": "text_list", "max": 10, "group": "Skip",
     "native": "contactCountry", "help": "Excluded by Seamless itself (no credits spent on them). e.g. “Europe but not the UK”."},
    {"key": "excludeStates", "label": "Skip states / regions", "kind": "text_list", "max": 10, "group": "Skip",
     "native": "contactState", "help": "Excluded by Seamless itself."},
    {"key": "excludeLocations", "label": "Skip cities / places", "kind": "text_list", "max": 10, "group": "Skip",
     "native": "locations", "help": "Excluded by Seamless itself."},
    {"key": "excludeZipCodes", "label": "Skip zip / postal codes", "kind": "text_list", "max": 10, "group": "Skip",
     "native": "contactZipCode", "help": "Excluded by Seamless itself."},
    {"key": "excludeSicCodes", "label": "Skip SIC codes", "kind": "text_list", "max": 5, "group": "Skip",
     "native": "industrySicCodes", "help": "Excluded by Seamless itself."},
    {"key": "excludeNaicsCodes", "label": "Skip NAICS codes", "kind": "text_list", "max": 5, "group": "Skip",
     "native": "industryNaicsCodes", "help": "Excluded by Seamless itself."},
    {"key": "excludeSeniority", "label": "Skip seniority", "kind": "enum_list", "max": 8,
     "group": "Skip", "local": True, "options": SENIORITY},
]

FILTER_BY_KEY = {f["key"]: f for f in FILTERS}
LOCAL_KEYS = {f["key"] for f in FILTERS if f.get("local")}


def clean_filters(raw: dict) -> dict:
    """Drop unknown keys, empty values and out-of-list options; enforce max counts."""
    out = {}
    for key, val in (raw or {}).items():
        spec = FILTER_BY_KEY.get(key)
        if spec is None or val is None or val == "" or val == []:
            continue
        kind = spec["kind"]
        if kind in ("enum_list", "text_list"):
            if isinstance(val, str):
                val = [val]
            items = []
            for v in val:
                v = str(v).strip()
                if not v or v in items:
                    continue
                if kind == "enum_list" and v not in spec["options"]:
                    continue
                items.append(v)
            items = items[: spec.get("max", 100)]
            if items:
                out[key] = items
        elif kind == "bool":
            if isinstance(val, str):
                val = val.strip().lower() in ("1", "true", "yes", "on")
            if val is True:
                out[key] = True
        elif kind == "int":
            try:
                n = int(float(val))
            except (TypeError, ValueError):
                continue
            n = max(spec.get("min", 0), min(n, spec.get("max", 10 ** 6)))
            if n > 0:
                out[key] = n
        elif kind == "choice":
            if isinstance(val, str) and val.lower() in ("true", "false") and True in spec["options"]:
                val = val.lower() == "true"
            if val in spec["options"]:
                out[key] = val
    if out.get("technologiesIsOr") is True and "technologies" not in out:
        out.pop("technologiesIsOr")
    return out


NATIVE_EXCLUDES = {f["key"]: f["native"] for f in FILTERS if f.get("native")}
NATIVE_LIMITS = {"contactCountry": 10, "contactState": 10, "locations": 10, "contactZipCode": 10,
                 "industrySicCodes": 5, "industryNaicsCodes": 5}


def to_search_body(filters: dict) -> dict:
    """Turn cleaned filters into a Seamless POST /search/contacts body (no paging fields).
    Exclusions Seamless supports natively are sent as "-value" in the matching include list."""
    if filters.get("savedSearchId"):
        return {"savedSearchId": int(filters["savedSearchId"])}
    body = {}
    special = ("jobChangeType", "jobChangeDays", "updatedWithinMonths", "pastCompany", "pastCompanyRecentOnly",
               "savedSearchId")
    for key, val in filters.items():
        if key in LOCAL_KEYS or key in special or key in NATIVE_EXCLUDES:
            continue
        if key in ("titlesExactMatch", "keywordsIsOr") and val is not True:
            continue
        body[key] = val
    for ex_key, target in NATIVE_EXCLUDES.items():
        if filters.get(ex_key):
            inc = list(body.get(target, []))
            room = NATIVE_LIMITS[target] - len(inc)
            body[target] = inc + ["-" + v.lstrip("-") for v in filters[ex_key][:max(0, room)]]
    if body.get("locationRadius") and not body.get("locations"):
        body.pop("locationRadius")
    if body.get("timezoneType") and not body.get("timezones"):
        body.pop("timezoneType")
    if filters.get("updatedWithinMonths"):
        body["lastModifiedAfter"] = months_ago_iso(filters["updatedWithinMonths"])
    if filters.get("pastCompany"):
        body["pastCompany"] = {"names": filters["pastCompany"],
                               "onlyMostRecentDeparture": bool(filters.get("pastCompanyRecentOnly"))}
    if filters.get("jobChangeType"):
        jc = {"changeType": filters["jobChangeType"]}
        if filters.get("jobChangeDays"):
            jc["dayRange"] = filters["jobChangeDays"]
        body["jobChanges"] = jc
    return body


def months_ago_iso(months, now=None):
    """First day of the month N months back, so the value (and the saved search position) only changes monthly."""
    t = time.gmtime(now if now is not None else time.time())
    y, m = t.tm_year, t.tm_mon - int(months)
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}-01T00:00:00Z"


NOT_ENOUGH_ALONE = ("locationType", "technologiesIsOr", "updatedWithinMonths", "pastCompanyRecentOnly",
                    "titlesExactMatch", "keywordsIsOr", "locationRadius", "timezoneType") + tuple(NATIVE_EXCLUDES)


def search_filter_count(filters: dict) -> int:
    return len([k for k in filters if k not in LOCAL_KEYS and k not in NOT_ENOUGH_ALONE])
