"""
tools/enterprise_dictionary.py
==============================
High-speed deterministic enterprise, corporate, brand, and vendor entity
scrubbing engine for Sabrina's context and bulk sanitizers.

Contains curated, open-source enterprise categories (Fortune 500, Global 2000,
retail conglomerates, tech/cloud providers, defense, banking, healthcare,
and corporate subsidiaries) to deterministically scrub brand and company
identities without incurring heavy neural network latency.
"""

import os
import re
import json
from typing import List, Dict, Set, Optional, Tuple

# ---------------------------------------------------------------------------
# Curated Enterprise Entities & Brand Categories
# ---------------------------------------------------------------------------

DEFAULT_ENTERPRISE_ENTITIES: List[str] = [
    # --- Retail, E-Commerce & Consumer Conglomerates ---
    "Jet.com", "jet.com", "Bonobos", "bonobos", "Flipkart", "flipkart",
    "Asda", "asda", "Hayneedle", "Moosejaw", "Shoebuy", "ModCloth",
    "Target", "target", "Target Brands", "Target Corp",
    "Costco", "costco", "Costco Wholesale", "Kirkland Signature",
    "Amazon", "amazon", "Amazon.com", "Whole Foods", "Zappos", "PillPack",
    "Kroger", "kroger", "Ralphs", "Fred Meyer", "King Soopers",
    "Home Depot", "The Home Depot", "homedepot",
    "Lowe's", "Lowes", "lowes",
    "Best Buy", "bestbuy", "Geek Squad",
    "Walgreens", "walgreens", "Walgreens Boots Alliance",
    "CVS", "CVS Health", "cvs", "CVS Pharmacy", "Aetna", "aetna",
    "Dollar General", "Dollar Tree", "Family Dollar",
    "Albertsons", "Safeway", "Vons", "Jewel-Osco", "Shaw's",
    "Publix", "publix", "H-E-B", "HEB", "Meijer", "meijer",
    "Aldi", "aldi", "Trader Joe's", "Trader Joes",
    "Carrefour", "Tesco", "Lidl", "Metro AG",
    "Macy's", "Macys", "Nordstrom", "Kohl's", "Kohls", "TJX", "T.J. Maxx", "TJ Maxx",
    "Marshalls", "HomeGoods", "Ross Stores", "Ross Dress for Less",
    "Nike", "Adidas", "Lululemon", "Under Armour", "Gap Inc", "Old Navy",

    # --- Tech Giants, Cloud Providers & Software Vendors ---
    "Microsoft", "microsoft", "MSFT", "Azure", "azure", "Office 365", "Office365",
    "GitHub", "github", "LinkedIn", "linkedin", "OpenAI", "openai",
    "Google", "google", "Alphabet", "alphabet", "Google Cloud", "GCP", "DeepMind", "deepmind",
    "Apple", "apple", "AAPL", "Apple Inc", "iCloud",
    "Meta", "meta", "Facebook", "facebook", "Instagram", "instagram", "WhatsApp", "whatsapp", "Oculus",
    "Amazon Web Services", "AWS", "aws",
    "Oracle", "oracle", "Oracle Cloud", "OCI", "NetSuite", "netsuite",
    "Salesforce", "salesforce", "Slack", "slack", "Tableau", "MuleSoft",
    "SAP", "sap", "SAP SE", "SAP HANA",
    "IBM", "ibm", "Red Hat", "RedHat", "redhat",
    "Cisco", "cisco", "Cisco Systems", "Webex", "Meraki",
    "VMware", "vmware", "Broadcom", "broadcom",
    "Intel", "intel", "AMD", "amd", "Nvidia", "nvidia", "Qualcomm", "qualcomm",
    "Adobe", "adobe", "Creative Cloud",
    "ServiceNow", "servicenow", "Workday", "workday",
    "Atlassian", "atlassian", "Jira", "jira", "Confluence", "confluence", "Trello", "trello",
    "Snowflake", "snowflake", "Databricks", "databricks",
    "Splunk", "splunk", "CrowdStrike", "crowdstrike", "Falcon",
    "Palo Alto Networks", "paloaltonetworks", "Fortinet", "fortinet",
    "Cloudflare", "cloudflare", "Akamai", "akamai", "Fastly", "fastly",
    "Datadog", "datadog", "New Relic", "Dynatrace",
    "Twilio", "twilio", "SendGrid", "sendgrid",
    "Zoom", "zoom", "Dropbox", "dropbox", "Box.com", "box.com",
    "Shopify", "shopify", "Stripe", "stripe", "Square", "square", "Block Inc",
    "PayPal", "paypal", "Venmo", "Braintree",

    # --- Financial Institutions & Banks ---
    "JPMorgan", "JPMorgan Chase", "jpmorgan", "Chase Bank", "J.P. Morgan",
    "Bank of America", "bankofamerica", "BofA", "Merrill Lynch",
    "Wells Fargo", "wellsfargo",
    "Citigroup", "Citibank", "citibank", "citi",
    "Goldman Sachs", "goldmansachs", "Marcus by Goldman Sachs",
    "Morgan Stanley", "morganstanley", "E*TRADE", "ETrade",
    "US Bancorp", "U.S. Bank", "PNC Financial", "PNC Bank", "Truist", "truist",
    "Capital One", "capitalone", "Discover Financial", "discover",
    "American Express", "americanexpress", "Amex", "amex",
    "Visa", "visa", "Mastercard", "mastercard",
    "Fidelity", "Fidelity Investments", "Vanguard", "vanguard",
    "BlackRock", "blackrock", "State Street", "Charles Schwab", "schwab",
    "BNY Mellon", "Bank of New York Mellon", "Northern Trust",
    "Barclays", "HSBC", "Deutsche Bank", "UBS", "Credit Suisse", "BNP Paribas",

    # --- Healthcare, Pharma & Insurance ---
    "UnitedHealth Group", "UnitedHealthcare", "UnitedHealth", "Optum", "optum",
    "Elevance Health", "Anthem", "anthem", "Cigna", "cigna", "Evernorth",
    "Humana", "humana", "Centene", "centene", "Molina Healthcare",
    "McKesson", "mckesson", "AmerisourceBergen", "Cencora", "cencora",
    "Cardinal Health", "cardinalhealth",
    "Pfizer", "pfizer", "Johnson & Johnson", "Johnson and Johnson", "J&J", "jnj",
    "Moderna", "moderna", "Merck", "merck", "AbbVie", "abbvie", "Abbott Laboratories", "Abbott",
    "Eli Lilly", "elililly", "Bristol Myers Squibb", "BMS", "Amgen", "Gilead Sciences",
    "Sanofi", "Novartis", "Roche", "AstraZeneca", "GSK", "GlaxoSmithKline",
    "Kaiser Permanente", "HCA Healthcare", "Tenet Healthcare",

    # --- Aerospace, Defense & Government Contractors ---
    "Lockheed Martin", "lockheedmartin", "Lockheed",
    "Boeing", "boeing",
    "Raytheon", "raytheon", "RTX", "rtx", "Pratt & Whitney", "Collins Aerospace",
    "General Dynamics", "generaldynamics",
    "Northrop Grumman", "northropgrumman", "Northrop",
    "L3Harris", "l3harris", "BAE Systems", "baesystems",
    "Honeywell", "honeywell", "Leidos", "leidos",
    "Booz Allen Hamilton", "Booz Allen", "boozallen", "SAIC", "CACI",

    # --- Telecom, Energy & Industrial ---
    "AT&T", "att", "Verizon", "verizon", "T-Mobile", "tmobile",
    "Comcast", "comcast", "Xfinity", "xfinity", "Charter Communications", "Spectrum",
    "ExxonMobil", "Exxon", "Mobil", "exxonmobil", "exxon",
    "Chevron", "chevron", "ConocoPhillips", "conocophillips",
    "Shell", "shell", "BP", "British Petroleum", "TotalEnergies",
    "General Electric", "GE", "Siemens", "Caterpillar", "Deere & Company", "John Deere",
    "Tesla", "tesla", "General Motors", "GM", "Ford", "ford", "Stellantis",
    "Toyota", "toyota", "Honda", "honda", "BMW", "Mercedes-Benz", "Volkswagen",
]

# Common corporate legal entity suffixes to help identify formal company mentions
LEGAL_SUFFIXES = [
    r"\bInc\.?\b", r"\bLLC\.?\b", r"\bCorp\.?\b", r"\bCorporation\b",
    r"\bLtd\.?\b", r"\bLimited\b", r"\bCo\.?\b", r"\bCompany\b",
    r"\bHoldings?\b", r"\bGroup\b", r"\bEnterprises?\b", r"\bInternational\b",
    r"\bServices?\b", r"\bSolutions?\b", r"\bTechnologies?\b", r"\bSystems?\b"
]


class EnterpriseDictionaryMatcher:
    """
    High-speed, memory-efficient enterprise entity matcher.
    
    Uses optimized regex pattern compilation with non-alphanumeric boundaries
    `(?<![a-zA-Z0-9])` and `(?![a-zA-Z0-9])` to match enterprise names,
    subsidiaries, brands, and identifiers without incurring heavy neural network latency.
    """

    def __init__(
        self,
        custom_terms: Optional[List[str]] = None,
        custom_dict_file: Optional[str] = None,
        include_defaults: bool = True,
    ):
        self.terms: Set[str] = set()
        
        if include_defaults:
            for item in DEFAULT_ENTERPRISE_ENTITIES:
                if item.strip():
                    self.terms.add(item.strip())
                    
        if custom_terms:
            for item in custom_terms:
                if item.strip():
                    self.terms.add(item.strip())
                    
        if custom_dict_file and os.path.isfile(custom_dict_file):
            self.load_from_file(custom_dict_file)

        self._compiled_regex: Optional[re.Pattern] = None
        self._build_pattern()

    def add_terms(self, terms: List[str]):
        """Add new terms to the matcher and recompile."""
        added = False
        for t in terms:
            t_clean = t.strip()
            if t_clean and t_clean not in self.terms:
                self.terms.add(t_clean)
                added = True
        if added:
            self._build_pattern()

    def load_from_file(self, filepath: str):
        """Load external entity dictionary from JSON, CSV, or TXT file."""
        if not os.path.exists(filepath):
            return
        ext = os.path.splitext(filepath)[1].lower()
        try:
            if ext == ".json":
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self.add_terms([str(x) for x in data])
                    elif isinstance(data, dict):
                        self.add_terms(list(data.keys()))
            else:
                # Text or CSV line-by-line
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
                    self.add_terms(lines)
        except Exception:
            pass

    def _build_pattern(self):
        """Compile an optimized regex matching all terms with boundary guards."""
        if not self.terms:
            self._compiled_regex = None
            return

        # Sort terms longest-first so compound terms (e.g. 'Target Brands') match before 'Target'
        sorted_terms = sorted(self.terms, key=lambda s: -len(s))
        escaped = [re.escape(t) for t in sorted_terms if t]

        # Use non-alphanumeric lookbehind and lookahead so terms match even when adjacent
        # to characters like '_', '-', '/', '.', or quotes.
        pattern_str = r"(?<![a-zA-Z0-9])(?:" + "|".join(escaped) + r")(?![a-zA-Z0-9])"
        self._compiled_regex = re.compile(pattern_str, re.IGNORECASE)

    def scrub(
        self,
        text: str,
        manifest: Dict[str, str],
        counts: Dict[str, int],
        placeholder_prefix: str = "ORG",
        next_placeholder_fn=None,
    ) -> str:
        """
        Scrub all enterprise entities from text in a single fast pass.
        
        Args:
            text: Input text string
            manifest: Redaction manifest dictionary
            counts: Redaction counts dictionary
            placeholder_prefix: Tag prefix (default 'ORG')
            next_placeholder_fn: Optional function to generate unique placeholder
            
        Returns:
            Scrubbed text
        """
        if not self._compiled_regex or not text:
            return text

        def _replace_match(m: re.Match) -> str:
            original = m.group(0)
            counts[placeholder_prefix] = counts.get(placeholder_prefix, 0) + 1
            if next_placeholder_fn:
                return next_placeholder_fn(manifest, placeholder_prefix, original)
            
            # Default placeholder logic
            for ph, orig in manifest.items():
                if orig == original:
                    return ph
            idx = sum(1 for k in manifest if k.startswith(f"[{placeholder_prefix}_")) + 1
            ph = f"[{placeholder_prefix}_{idx}]"
            manifest[ph] = original
            return ph

        return self._compiled_regex.sub(_replace_match, text)

    def contains_candidate(self, text: str) -> bool:
        """Quick check if text contains any dictionary terms."""
        if not self._compiled_regex or not text:
            return False
        return bool(self._compiled_regex.search(text))


# ---------------------------------------------------------------------------
# Global Singleton
# ---------------------------------------------------------------------------
_ENTERPRISE_DICT_INSTANCE: Optional[EnterpriseDictionaryMatcher] = None

def get_enterprise_dictionary(custom_terms: Optional[List[str]] = None) -> EnterpriseDictionaryMatcher:
    """Return singleton EnterpriseDictionaryMatcher."""
    global _ENTERPRISE_DICT_INSTANCE
    if _ENTERPRISE_DICT_INSTANCE is None:
        _ENTERPRISE_DICT_INSTANCE = EnterpriseDictionaryMatcher(custom_terms=custom_terms)
    elif custom_terms:
        _ENTERPRISE_DICT_INSTANCE.add_terms(custom_terms)
    return _ENTERPRISE_DICT_INSTANCE
