"""Seamless contact-search filters, with Include and Exclude.

Stored per pull as {"jobTitle": {"in": [...], "out": [...]}, "titlesExactMatch": true, ...}.

How an Exclude is applied, cheapest first:
  seamless    Seamless leaves them out itself ("-value"): no credits at all.
  complement  Exclude from a fixed list = include every other value, so Seamless leaves them out: no credits.
  tool        Seamless can't exclude this; the tool skips them when the page arrives (0.1 credit to see each,
              never researched).
  none        Can't be excluded through the API (the box is greyed out).
"""
import time

from .config import cfg

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
SIZES = ["0 - 1 (Self-employed)", "2 - 10", "11 - 50", "51 - 200", "201 - 500", "501 - 1,000",
         "1,001 - 5,000", "5,001 - 10,000", "10,001+"]
SENIORITY = ["C-Level", "VP", "Director", "Manager", "Senior", "Mid-Level", "Entry Level", "Other"]
DEPARTMENTS = ["Sales", "Marketing", "Engineering", "Human Resources", "Finance", "IT", "Operations",
               "Support", "Legal", "Project Management", "Other"]
TIMEZONES = ["Eastern (EST)", "Central (CST)", "Mountain (MST)", "Pacific (PST)", "Alaska (AKST)", "Hawaii (HST)",
             "New Zealand (NST)", "Solomon (SST)", "Australia Eastern (AET)", "Australia Central (ACT)", "Japan (JST)",
             "China Taiwan (CTT)", "Vietnam (VST)", "Bangladesh (BST)", "India (IST)", "Indiana Eastern (IET)",
             "Pakistan Lahore (PLT)", "Near East (NET)", "Middle East (MET)", "Eastern African (EAT)",
             "(Arabic) Egypt (ART)", "Eastern European (EET)", "European Central (ECT)", "Greenwich Mean (GMT)",
             "Central African (CAT)", "Argentina (AGT)", "Brazil Eastern (BET)", "Canada Newfoundland (CNT)",
             "Puerto Rico and US Virgin Islands (PRT)", "Midway Islands (MIT)"]
LOC_TYPES = {"contact": "Person's location", "company": "Company HQ", "bothOR": "Person or company HQ",
             "bothAND": "Person and company HQ"}

# Common technologies, so the box suggests names from day one. Names Seamless returns in your own pulls are
# added automatically (with how many companies they were seen at).
TECH_COMMON = [
    "Salesforce", "Salesforce Sales Cloud", "Salesforce Service Cloud", "Salesforce Marketing Cloud",
    "Salesforce Commerce Cloud", "Salesforce Experience Cloud", "Salesforce CPQ", "Pardot",
    "Salesforce Account Engagement", "MuleSoft", "Tableau", "Slack", "Heroku", "HubSpot", "HubSpot CRM",
    "Zoho CRM", "Microsoft Dynamics 365", "Microsoft Dynamics CRM", "Pipedrive", "SugarCRM", "Freshsales",
    "Zendesk", "Freshdesk", "Intercom", "ServiceNow", "Jira", "Confluence", "Asana", "Monday.com", "Trello",
    "Marketo", "Mailchimp", "Klaviyo", "ActiveCampaign", "Eloqua", "Outreach", "Salesloft", "Gong", "ZoomInfo",
    "Google Analytics", "Google Tag Manager", "Segment", "Mixpanel", "Amplitude", "Hotjar", "Optimizely",
    "Amazon Web Services", "AWS", "Microsoft Azure", "Google Cloud Platform", "Cloudflare", "Akamai",
    "DigitalOcean", "Heroku", "Vercel", "Netlify", "Kubernetes", "Docker", "Terraform", "GitHub", "GitLab",
    "Bitbucket", "Jenkins", "CircleCI", "Datadog", "New Relic", "Splunk", "Sentry", "PagerDuty",
    "Snowflake", "Databricks", "BigQuery", "Amazon Redshift", "MongoDB", "PostgreSQL", "MySQL", "Oracle Database",
    "Microsoft SQL Server", "Elasticsearch", "Redis", "Kafka", "Looker", "Power BI", "Qlik", "Domo",
    "SAP", "SAP S/4HANA", "Oracle NetSuite", "NetSuite", "Oracle ERP Cloud", "Microsoft Dynamics 365 Business Central",
    "Sage Intacct", "QuickBooks", "Xero", "Workday", "BambooHR", "ADP", "Gusto", "Rippling", "Greenhouse", "Lever",
    "Shopify", "Shopify Plus", "Magento", "Adobe Commerce", "BigCommerce", "WooCommerce", "Salesforce B2C Commerce",
    "WordPress", "Drupal", "Webflow", "Wix", "Squarespace", "Contentful", "Adobe Experience Manager", "Sitecore",
    "Stripe", "PayPal", "Braintree", "Adyen", "Square", "Chargebee", "Recurly", "Zuora",
    "Okta", "Auth0", "Microsoft 365", "Office 365", "Google Workspace", "Zoom", "Microsoft Teams", "Twilio",
    "SendGrid", "DocuSign", "Dropbox", "Box", "Atlassian", "Figma", "Miro", "Notion", "Airtable",
    "React", "Angular", "Vue.js", "Node.js", "Python", "Java", ".NET", "Ruby on Rails", "PHP", "Laravel",
    "OpenAI", "Anthropic", "TensorFlow", "PyTorch", "UiPath", "Automation Anywhere", "Zapier", "Workato",
    "Veeva", "Epic", "Cerner", "athenahealth", "Guidewire", "Duck Creek", "nCino", "Temenos", "FIS", "Fiserv",
]
TECH_BUNDLES = {
    "Uses Salesforce (any cloud)": ["Salesforce", "Salesforce Sales Cloud", "Salesforce Service Cloud",
                                     "Salesforce Marketing Cloud", "Pardot", "MuleSoft", "Tableau", "Salesforce CPQ",
                                     "Salesforce Experience Cloud", "Salesforce Commerce Cloud"],
    "Uses a competing CRM": ["HubSpot", "HubSpot CRM", "Zoho CRM", "Microsoft Dynamics 365", "Microsoft Dynamics CRM",
                             "Pipedrive", "SugarCRM", "Freshsales"],
    "AWS / Azure / Google Cloud": ["Amazon Web Services", "AWS", "Microsoft Azure", "Google Cloud Platform"],
    "Online stores": ["Shopify", "Shopify Plus", "Magento", "Adobe Commerce", "BigCommerce", "WooCommerce"],
}

# kind: list (in/out), bool, choice, int, text.  exclude: seamless | complement | tool | none
# row: search-result field the tool checks for a "tool" exclude.
FIELDS = [
    {"key": "jobTitle", "label": "Job title", "kind": "list", "group": "People", "max": 10, "exclude": "tool",
     "row": "title", "help": "Include: titles Seamless should match. Exclude: skip anyone whose title contains these words."},
    {"key": "titlesExactMatch", "label": "Exact title match only", "kind": "bool", "group": "People"},
    {"key": "seniority", "label": "Seniority", "kind": "list", "group": "People", "options": SENIORITY, "max": 5,
     "exclude": "complement", "row": "seniority"},
    {"key": "department", "label": "Department", "kind": "list", "group": "People", "options": DEPARTMENTS, "max": 5,
     "exclude": "complement", "row": "department"},
    {"key": "contactKeyword", "label": "Profile keywords", "kind": "list", "group": "People", "max": 10,
     "exclude": "none"},
    {"key": "keywordsIsOr", "label": "Keywords: match any (not all)", "kind": "bool", "group": "People"},

    {"key": "contactCountry", "label": "Country", "kind": "list", "group": "Location", "max": 10,
     "exclude": "seamless", "lookup": "country"},
    {"key": "contactState", "label": "State / region", "kind": "list", "group": "Location", "max": 10,
     "exclude": "seamless", "lookup": "state-country"},
    {"key": "locations", "label": "City", "kind": "list", "group": "Location", "max": 10,
     "exclude": "seamless", "lookup": "city-state-country,city-country"},
    {"key": "contactZipCode", "label": "Zip / postal code", "kind": "list", "group": "Location", "max": 10,
     "exclude": "seamless", "lookup": "postcode"},
    {"key": "locationRadius", "label": "Widen cities by (miles)", "kind": "choice", "group": "Location",
     "options": ["25", "50", "100", "250"]},
    {"key": "locationType", "label": "Location applies to", "kind": "choice", "group": "Location",
     "options": list(LOC_TYPES), "option_labels": LOC_TYPES},
    {"key": "timezones", "label": "Time zone", "kind": "list", "group": "Location", "options": TIMEZONES, "max": 10,
     "exclude": "none"},

    {"key": "industry", "label": "Industry", "kind": "list", "group": "Company", "options": INDUSTRIES, "max": 5,
     "exclude": "tool", "row": "industries"},
    {"key": "companySize", "label": "Company size (employees)", "kind": "list", "group": "Company", "options": SIZES,
     "max": 10, "exclude": "complement", "row": "size"},
    {"key": "companyRevenue", "label": "Company revenue", "kind": "list", "group": "Company", "options": MONEY,
     "max": 10, "exclude": "complement"},
    {"key": "technologies", "label": "Technologies", "kind": "list", "group": "Company", "max": 10,
     "exclude": "none", "suggest": "tech",
     "help": "Seamless matches technology names as written. Names seen in your own pulls are marked ✓."},
    {"key": "technologiesIsOr", "label": "Technologies: uses any (not all)", "kind": "bool", "group": "Company",
     "default": True},
    {"key": "companyName", "label": "Company name", "kind": "list", "group": "Company", "max": 100,
     "exclude": "tool", "row": "company"},
    {"key": "companyDomain", "label": "Company website", "kind": "list", "group": "Company", "max": 100,
     "exclude": "tool", "row": "domain"},
    {"key": "industrySicCodes", "label": "SIC code", "kind": "list", "group": "Company", "max": 5,
     "exclude": "seamless"},
    {"key": "industryNaicsCodes", "label": "NAICS code", "kind": "list", "group": "Company", "max": 5,
     "exclude": "seamless"},
    {"key": "companyType", "label": "Public or private", "kind": "choice", "group": "Company",
     "options": ["Public", "Private"]},
    {"key": "companyFoundedOn", "label": "Company founded", "kind": "list", "group": "Company",
     "options": ["Less than 1 Year", "Last 1-3 Years", "Last 4-10 Years", "10+ Years"], "max": 4,
     "exclude": "complement"},

    {"key": "companyLatestFundingClassifications", "label": "Latest funding round", "kind": "list",
     "group": "Signals", "max": 14, "exclude": "complement",
     "options": ["Angel", "Pre-Seed", "Seed", "Series A", "Series B", "Series C", "Series D", "Series E",
                 "Series F", "Series G", "Series H", "Series I", "Series J", "Other"]},
    {"key": "companyLatestFundingDates", "label": "Funded within (days)", "kind": "choice", "group": "Signals",
     "options": ["90", "180", "365", "1095"]},
    {"key": "companyLatestFundingTotals", "label": "Total funding raised", "kind": "list", "group": "Signals",
     "options": MONEY, "max": 9, "exclude": "complement"},
    {"key": "newsTypes", "label": "Recent company news", "kind": "list", "group": "Signals", "max": 8,
     "exclude": "none", "options": ["Acquisition", "Corporate Challenges", "Cost Cutting", "Expansion", "Investment",
                                    "Leadership", "Partnership", "Recognition"]},
    {"key": "newsTypeDates", "label": "News within (days)", "kind": "choice", "group": "Signals",
     "options": ["60", "90", "180", "365"]},
    {"key": "jobChangeType", "label": "Recent job change", "kind": "choice", "group": "Signals",
     "options": ["New Hire", "New Promotion", "New Hire or New Promotion"]},
    {"key": "jobChangeDays", "label": "Job change within", "kind": "choice", "group": "Signals",
     "options": ["Last 60 Days", "Last 90 Days", "Last 180 Days", "Last 365 Days"]},
    {"key": "pastCompany", "label": "Used to work at", "kind": "list", "group": "Signals", "max": 100,
     "exclude": "none"},

    {"key": "updatedWithinMonths", "label": "Only records updated in the last … months", "kind": "int",
     "group": "Quality", "min": 0, "max": 120, "help": "Fresher records = more emails found. 0 = any age."},
    {"key": "requireLinkedIn", "label": "Must have a LinkedIn profile", "kind": "bool", "group": "Quality"},
]
FIELD = {f["key"]: f for f in FIELDS}
LOCATION_KEYS = ("contactCountry", "contactState", "contactZipCode", "locations")


def default_filters():
    f = {"technologiesIsOr": True}
    if cfg.UPDATED_WITHIN_MONTHS:
        f["updatedWithinMonths"] = cfg.UPDATED_WITHIN_MONTHS
    return f


def clean(raw):
    """Keep only known filters with valid values; lists become {"in": [...], "out": [...]}."""
    out = {}
    for key, val in (raw or {}).items():
        spec = FIELD.get(key)
        if not spec or val in (None, "", [], {}):
            continue
        kind = spec["kind"]
        if kind == "list":
            if isinstance(val, (list, str)):
                val = {"in": val if isinstance(val, list) else [val], "out": []}
            if not isinstance(val, dict):
                continue
            res = {}
            for side in ("in", "out"):
                items = []
                for v in val.get(side) or []:
                    v = str(v).strip()
                    if not v or v in items:
                        continue
                    if spec.get("options") and v not in spec["options"]:
                        continue
                    items.append(v)
                if side == "out" and spec.get("exclude", "none") == "none":
                    items = []
                limit = spec.get("max", 100) if side == "in" else 200
                if items:
                    res[side] = items[:limit]
            if res:
                out[key] = res
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
            if str(val) in [str(o) for o in spec["options"]]:
                out[key] = str(val)
    return out


def months_ago_iso(months, now=None):
    """First day of the month N months back: the value only changes once a month, so saved search positions
    stay valid all month."""
    t = time.gmtime(now if now is not None else time.time())
    y, m = t.tm_year, t.tm_mon - int(months)
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}-01T00:00:00Z"


def _ins(f, key):
    return list((f.get(key) or {}).get("in") or [])


def _outs(f, key):
    return list((f.get(key) or {}).get("out") or [])


def search_body(f):
    """Seamless POST /search/contacts body (no paging, no slice). Excludes Seamless can apply are put in here."""
    body = {}
    for spec in FIELDS:
        key, kind = spec["key"], spec["kind"]
        if key not in f or key in ("updatedWithinMonths", "requireLinkedIn", "pastCompany", "jobChangeType",
                                   "jobChangeDays"):
            continue
        if kind == "list":
            inc, out = _ins(f, key), _outs(f, key)
            mode = spec.get("exclude")
            if mode == "seamless" and out:
                room = spec.get("max", 10) - len(inc)
                inc = inc + ["-" + v for v in out[:max(0, room)]]
            elif mode == "complement" and out:
                base = inc or spec["options"]
                left = [o for o in base if o not in out]
                if len(left) <= spec.get("max", 10):
                    inc = left
                elif not inc:
                    inc = []                      # too many to list: the tool skips them instead
            if inc:
                body[key] = inc
        elif kind == "bool":
            if key == "technologiesIsOr":
                if f.get("technologies"):
                    body[key] = True
            elif f[key] is True:
                body[key] = True
        elif kind == "choice":
            body[key] = f[key]
        elif kind == "int":
            body[key] = f[key]
    if f.get("technologies") and "technologiesIsOr" not in f:
        body["technologiesIsOr"] = False
    if any(body.get(k) for k in LOCATION_KEYS) and not body.get("locationType"):
        body["locationType"] = cfg.LOCATION_APPLIES_TO
    if body.get("locationRadius") and not body.get("locations"):
        body.pop("locationRadius")
    if f.get("updatedWithinMonths"):
        body["lastModifiedAfter"] = months_ago_iso(f["updatedWithinMonths"])
    if f.get("pastCompany"):
        body["pastCompany"] = {"names": _ins(f, "pastCompany"), "onlyMostRecentDeparture": False}
    if f.get("jobChangeType"):
        jc = {"changeType": f["jobChangeType"]}
        if f.get("jobChangeDays"):
            jc["dayRange"] = f["jobChangeDays"]
        body["jobChanges"] = jc
    return body


def tool_excludes(f):
    """Excludes Seamless can't apply: [(key, row field, values, label)] checked by the tool on each result."""
    out = []
    for spec in FIELDS:
        if spec["kind"] != "list":
            continue
        vals = _outs(f, spec["key"])
        if not vals:
            continue
        mode = spec.get("exclude")
        if mode == "tool":
            out.append((spec["key"], spec["row"], vals, spec["label"]))
        elif mode == "complement":
            base = _ins(f, spec["key"]) or spec["options"]
            if len([o for o in base if o not in vals]) > spec.get("max", 10) and spec.get("row"):
                out.append((spec["key"], spec["row"], vals, spec["label"]))
    return out


def size_slices(f):
    """The company sizes this search covers, one slice each (None = no sizes chosen, so all of them)."""
    inc, out = _ins(f, "companySize"), _outs(f, "companySize")
    sizes = inc or list(SIZES)
    sizes = [s for s in sizes if s not in out]
    return sizes, bool(inc or out)


def summary(f):
    parts = []
    for key in ("jobTitle", "seniority", "contactCountry", "locations", "industry", "companySize", "technologies"):
        inc = _ins(f, key)
        if inc:
            parts.append(", ".join(inc[:3]) + ("…" if len(inc) > 3 else ""))
    return " · ".join(parts) or "No filters"


def meaningful(f):
    """At least one real search filter (not just switches), so a pull never asks Seamless for 'everyone'."""
    body = search_body(f)
    return any(k for k in body if k not in ("locationType", "technologiesIsOr", "lastModifiedAfter",
                                            "titlesExactMatch", "keywordsIsOr", "locationRadius"))
