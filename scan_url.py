#!/usr/bin/env python3
"""
scan_url.py — Copyright Scanner

Given a URL pointing to an item in a digital collection, fetches metadata
via OAI-PMH (MODS or Dublin Core) or HTML scraping, normalizes it, and
produces a copyright status determination using the Copyright Compass engine.

Supported inputs:
  - Handle URLs     (hdl.handle.net/...)
  - Islandora nodes (any-host.org/node/NNNN)
  - OAI identifiers (oai:host:path)
  - Any Islandora Modern (Drupal) item page

Metadata extraction order:
  1. OAI-PMH / MODS  — richest, preferred
  2. OAI-PMH / oai_dc — simpler fallback
  3. HTML scrape      — last resort (works on CTDA-style metadata tables)

Usage:
    py -3.12 scan_url.py "https://hdl.handle.net/11134/3932475"
    py -3.12 scan_url.py "https://ctdigitalarchive.org/node/3932475"
    py -3.12 scan_url.py  (interactive mode)

Prerequisites:
    pip install requests beautifulsoup4 lxml python-dotenv supabase==1.2.0 openai
"""

import os
import re
import sys
from datetime import date
import json
import requests
from urllib.parse import urlparse, urljoin
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

# ── HTTP session with a browser-like user agent ────────────────────────────────

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (compatible; CopyrightScanner/1.0; "
        "+https://copyrightnexus.netlify.app)"
    )
})
TIMEOUT = 20


# ═══════════════════════════════════════════════════════════════════════════════
# PART 1 — URL RESOLUTION
# ═══════════════════════════════════════════════════════════════════════════════

def resolve_url(raw_url: str) -> str:
    """
    Follow any redirects (e.g. handle.net → institution URL).
    Returns the final resolved URL.
    """
    try:
        r = SESSION.head(raw_url, allow_redirects=True, timeout=TIMEOUT)
        return r.url
    except Exception:
        # HEAD might fail; try GET
        try:
            r = SESSION.get(raw_url, allow_redirects=True, timeout=TIMEOUT)
            return r.url
        except Exception:
            return raw_url


def extract_node_id(url: str) -> str | None:
    """Extract Islandora/Drupal node ID from a URL like /node/3932475."""
    m = re.search(r'/node/(\d+)', url)
    return m.group(1) if m else None


def discover_oai_base(url: str) -> str | None:
    """
    Probe common OAI-PMH endpoint paths for this host.
    Returns the first working base URL, or None.

    Islandora Modern (rest_oai_pmh module): /oai  or  /oai/request
    Classic Islandora (Fedora):             /oai/request
    DSpace:                                 /oai/request  or  /dspace-oai/request
    Omeka:                                  /oai-pmh-repository/oai
    """
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    candidates = [
        f"{base}/oai",
        f"{base}/oai/request",
        f"{base}/oai2",
        f"{base}/oai-pmh-repository/oai",
        f"{base}/dspace-oai/request",
    ]

    for endpoint in candidates:
        try:
            r = SESSION.get(
                endpoint,
                params={"verb": "Identify"},
                timeout=TIMEOUT,
                allow_redirects=True,
            )
            if r.status_code == 200 and "<OAI-PMH" in r.text:
                return endpoint
        except Exception:
            continue

    return None


def discover_oai_identifier(oai_base: str, node_id: str, resolved_url: str) -> str | None:
    """
    Discover the real OAI identifier for a node by:
    1. Trying common constructed patterns first (fast)
    2. Falling back to ListIdentifiers harvest to find a matching record

    Returns the working OAI identifier string, or None.
    """
    parsed = urlparse(resolved_url)
    host   = parsed.netloc

    # Patterns used by different Islandora / Drupal OAI modules
    candidates = [
        f"oai:{host}:node-{node_id}",                   # CTDA / rest_oai_pmh (hyphen)
        f"oai:{host}:node/{node_id}",                   # some Islandora configs (slash)
        f"oai:{host}:{node_id}",                         # bare node id
        f"oai:{host}/node/{node_id}",                    # alternate separator
    ]

    for ident in candidates:
        try:
            r = SESSION.get(
                oai_base,
                params={"verb": "GetRecord", "identifier": ident, "metadataPrefix": "oai_dc"},
                timeout=TIMEOUT,
            )
            if r.status_code == 200 and "idDoesNotExist" not in r.text and "<record>" in r.text:
                return ident
        except Exception:
            continue

    return None


def build_oai_identifier(url: str, node_id: str) -> str:
    """Fallback: construct OAI identifier from node URL (may not work)."""
    parsed = urlparse(url)
    host = parsed.netloc
    return f"oai:{host}:node/{node_id}"


# ═══════════════════════════════════════════════════════════════════════════════
# PART 2 — OAI-PMH FETCH
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_oai_record(oai_base: str, identifier: str, prefix: str) -> str | None:
    """
    Fetch a single OAI-PMH record. Returns raw XML string or None.
    """
    params = {
        "verb": "GetRecord",
        "identifier": identifier,
        "metadataPrefix": prefix,
    }
    try:
        r = SESSION.get(oai_base, params=params, timeout=TIMEOUT)
        if r.status_code == 200 and "<OAI-PMH" in r.text:
            return r.text
        return None
    except Exception as e:
        print(f"   OAI fetch error ({prefix}): {e}")
        return None


# ── MODS parser ────────────────────────────────────────────────────────────────

def parse_mods(xml_text: str) -> dict:
    """
    Parse MODS XML into a normalized metadata dict.
    Handles namespaced and un-namespaced MODS.
    """
    try:
        from lxml import etree
    except ImportError:
        from xml.etree import ElementTree as etree

    root = etree.fromstring(xml_text.encode())

    # Strip namespace for simpler XPath
    def strip_ns(tag):
        return re.sub(r'\{[^}]+\}', '', tag)

    def find_text(el, *paths):
        for path in paths:
            for child in el.iter():
                if strip_ns(child.tag) == path and child.text:
                    return child.text.strip()
        return None

    def find_all_text(el, tag):
        return [
            c.text.strip()
            for c in el.iter()
            if strip_ns(c.tag) == tag and c.text
        ]

    meta = {
        "title":         None,
        "creator":       None,
        "date":          None,
        "date_raw":      None,
        "type":          None,
        "genre":         None,
        "description":   None,
        "language":      None,
        "publisher":     None,
        "rights":        None,
        "identifier":    None,
        "holding_institution": None,
    }

    # Title — prefer titleInfo/title without type="alternative"
    for ti in root.iter():
        if strip_ns(ti.tag) == "titleInfo":
            if ti.get("type") not in ("alternative", "abbreviated"):
                t = find_text(ti, "title")
                if t:
                    meta["title"] = t
                    break

    # Creator / name
    names = []
    for n in root.iter():
        if strip_ns(n.tag) == "name":
            parts = find_all_text(n, "namePart")
            role_terms = find_all_text(n, "roleTerm")
            if parts:
                names.append({
                    "name": " ".join(parts),
                    "roles": [r.lower() for r in role_terms]
                })
    # Prefer creator/author roles; fall back to first name
    creator = None
    for n in names:
        if any(r in ("creator", "author", "aut", "cre") for r in n["roles"]):
            creator = n["name"]
            break
    if not creator and names:
        creator = names[0]["name"]
    meta["creator"] = creator

    # Dates — prefer dateCreated, dateIssued, then copyrightDate
    for date_tag in ("dateCreated", "dateIssued", "copyrightDate", "dateOther"):
        val = find_text(root, date_tag)
        if val:
            meta["date_raw"] = val
            yr = re.search(r'\b(1[0-9]{3}|20[0-2][0-9])\b', val)
            if yr:
                meta["date"] = int(yr.group(1))
            break

    # Type of resource
    meta["type"] = find_text(root, "typeOfResource")

    # Genre
    genres = find_all_text(root, "genre")
    meta["genre"] = genres[0] if genres else None

    # Description / abstract / note
    meta["description"] = find_text(root, "abstract") or find_text(root, "note")

    # Language
    meta["language"] = find_text(root, "languageTerm")

    # Publisher
    meta["publisher"] = find_text(root, "publisher")

    # Rights
    meta["rights"] = find_text(root, "accessCondition")

    # Holding institution
    meta["holding_institution"] = find_text(root, "institution") or find_text(root, "physicalLocation")

    return meta


# ── Dublin Core parser ─────────────────────────────────────────────────────────

def parse_oai_dc(xml_text: str) -> dict:
    """Parse OAI Dublin Core into normalized dict."""
    try:
        from lxml import etree
    except ImportError:
        from xml.etree import ElementTree as etree

    root = etree.fromstring(xml_text.encode())

    def strip_ns(tag):
        return re.sub(r'\{[^}]+\}', '', tag)

    def find_all(tag):
        return [
            c.text.strip()
            for c in root.iter()
            if strip_ns(c.tag) == tag and c.text
        ]

    dates = find_all("date")
    date_raw = dates[0] if dates else None
    date_year = None
    if date_raw:
        yr = re.search(r'\b(1[0-9]{3}|20[0-2][0-9])\b', date_raw)
        if yr:
            date_year = int(yr.group(1))

    creators = find_all("creator")
    types    = find_all("type")
    rights   = find_all("rights")

    return {
        "title":         (find_all("title") or [None])[0],
        "creator":       creators[0] if creators else None,
        "date":          date_year,
        "date_raw":      date_raw,
        "type":          types[0] if types else None,
        "genre":         None,
        "description":   (find_all("description") or [None])[0],
        "language":      (find_all("language") or [None])[0],
        "publisher":     (find_all("publisher") or [None])[0],
        "rights":        rights[0] if rights else None,
        "identifier":    (find_all("identifier") or [None])[0],
        "holding_institution": None,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PART 3 — HTML SCRAPE FALLBACK
# ═══════════════════════════════════════════════════════════════════════════════

def scrape_html_metadata(url: str) -> dict:
    """
    Scrape metadata from an Islandora Modern item page.
    Works on CTDA-style metadata tables (bootstrap-panel layout).
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("   beautifulsoup4 not installed — HTML scrape unavailable")
        return {}

    try:
        r = SESSION.get(url, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        print(f"   HTML fetch error: {e}")
        return {}

    soup = BeautifulSoup(r.text, "lxml")

    # CTDA / Islandora Modern: metadata lives in a two-column table
    # First column = label, second column = value
    meta_raw = {}
    for row in soup.select("table tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2:
            label = cells[0].get_text(strip=True).lower()
            value = cells[1].get_text(separator=" ", strip=True)
            if label and value:
                meta_raw[label] = value

    # Also check definition lists (some Islandora themes use <dl>)
    for dt in soup.select("dl dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            meta_raw[dt.get_text(strip=True).lower()] = dd.get_text(separator=" ", strip=True)

    # Also try JSON-LD embedded in <script type="application/ld+json">
    ld_meta = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            if isinstance(data, dict):
                ld_meta = data
                break
        except Exception:
            pass

    def clean_value(label: str, value: str) -> str:
        """
        CTDA and some Islandora themes repeat the label text at the start of
        the value cell, e.g. label="resource type", value="Resource Type Manuscript"
        Strip ALL label words from the start of the value, then strip role codes.
        """
        label_words = label.strip().split()
        for word in label_words:
            value = re.sub(
                rf'^{re.escape(word)}\s*',
                '', value, flags=re.IGNORECASE
            ).strip()
        # Strip role codes e.g. "Signer (sgn): " or "(sgn): "
        value = re.sub(r'^[^:]+\([a-z]{2,4}\):\s*', '', value).strip()
        return value.strip()

    def get(*keys):
        for k in keys:
            for mk, mv in meta_raw.items():
                if k in mk:
                    return clean_value(mk, mv)
        return None

    date_raw = get("date created", "date issued", "date", "origin")
    date_year = None
    if date_raw:
        yr = re.search(r'\b(1[0-9]{3}|20[0-2][0-9])\b', date_raw)
        if yr:
            date_year = int(yr.group(1))

    # Title: prefer <h1> (cleanest source on CTDA pages)
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else get("title")

    creator = get("persons", "creator", "author", "contributor")

    rtype = get("resource type", "type")

    rights = get("rights statement", "rights")
    if rights:
        # Strip any leading "Rights Statement" or "Statement" prefix
        rights = re.sub(r'^(rights\s+)?statement\s*', '', rights, flags=re.IGNORECASE).strip()

    return {
        "title":         title or ld_meta.get("name"),
        "creator":       creator,
        "date":          date_year,
        "date_raw":      date_raw,
        "type":          rtype,
        "genre":         get("genre"),
        "description":   get("description", "abstract"),
        "language":      get("language"),
        "publisher":     get("publisher"),
        "rights":        rights,
        "identifier":    get("handle", "identifier", "local identifier"),
        "holding_institution": get("held by", "physical location", "institution"),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PART 4 — METADATA NORMALIZER
# ═══════════════════════════════════════════════════════════════════════════════

def normalize_metadata(raw: dict, source_url: str, platform: str) -> dict:
    """
    Produce a clean, standardized metadata object regardless of source.
    """
    return {
        "title":               raw.get("title"),
        "creator":             raw.get("creator"),
        "date":                raw.get("date"),        # int year or None
        "date_raw":            raw.get("date_raw"),    # original string
        "type":                raw.get("type"),        # text/image/sound/manuscript
        "genre":               raw.get("genre"),
        "description":         raw.get("description"),
        "language":            raw.get("language"),
        "publisher":           raw.get("publisher"),
        "rights_existing":     raw.get("rights"),      # what institution already has
        "holding_institution": raw.get("holding_institution"),
        "source_url":          source_url,
        "source_platform":     platform,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PART 5 — PUBLICATION STATUS INFERENCE
# ═══════════════════════════════════════════════════════════════════════════════

# MODS typeOfResource values that typically mean unpublished
UNPUBLISHED_TYPES = {
    "manuscript", "mixed material", "still image",
    "three dimensional object", "notated music",
}

def infer_publication_status(meta: dict) -> str:
    """
    Return 'published', 'unpublished', or 'unknown' based on metadata signals.
    """
    t = (meta.get("type") or "").lower()
    g = (meta.get("genre") or "").lower()

    if any(u in t for u in UNPUBLISHED_TYPES):
        return "unpublished"

    unpublished_genres = {"letters", "manuscripts", "diaries", "drafts",
                          "correspondence", "accounts", "maps"}
    if any(u in g for u in unpublished_genres):
        return "unpublished"

    published_types = {"text", "sound recording", "moving image",
                       "cartographic", "notated music"}
    if any(p in t for p in published_types):
        # Text could go either way; need more signals
        if t == "text":
            # If no publisher, likely unpublished
            return "unknown"
        return "published"

    return "unknown"


# ═══════════════════════════════════════════════════════════════════════════════
# PART 6 — COPYRIGHT DETERMINATION
# ═══════════════════════════════════════════════════════════════════════════════

def build_determination_prompt(meta: dict) -> str:
    """
    Build a natural-language question from normalized metadata,
    suitable for the Copyright Compass determination engine.
    """
    parts = []

    title   = meta.get("title", "Unknown title")
    creator = meta.get("creator")
    year    = meta.get("date")
    rtype   = meta.get("type")
    genre   = meta.get("genre")
    pub     = infer_publication_status(meta)

    parts.append(f'What is the copyright status of "{title}"?')

    if creator:
        parts.append(f"Creator: {creator}.")

    if year:
        parts.append(f"Date: {year}.")

    if rtype:
        parts.append(f"Resource type: {rtype}.")

    if genre:
        parts.append(f"Genre: {genre}.")

    if pub == "unpublished":
        parts.append("This appears to be an unpublished manuscript or archival item.")
    elif pub == "published":
        parts.append("This appears to be a published work.")

    return " ".join(parts)


SYSTEM_PROMPT = """You are Copyright Scanner, an expert copyright analysis tool for archivists and catalogers working in cultural heritage institutions.

Your audience is the archivist or cataloger who is processing this item and needs to know exactly what to do next in their cataloging workflow.

Determine the copyright status based ONLY on the metadata provided. Always respond with:

1. COPYRIGHT STATUS — Public Domain / In Copyright / Undetermined

2. RIGHTS STATEMENT
   Label: [exact RightsStatements.org label]
   URI:   [exact URI — ready to paste into metadata record]

3. CONFIDENCE — High / Medium / Low
   One sentence explaining why.

4. REASONING — One to two sentences citing the specific legal rule that applies.

5. RECOMMENDED ACTION
   Write this directly for the archivist or cataloger processing this record. Use this format:

   Start with one of:
   - "✓ Existing statement is correct. No change needed." — if the current rights statement already matches.
   - "→ Update rights statement:" — if the existing statement is wrong, missing, or weaker than it should be.
   - "⚠ Research required before assigning a statement:" — if the determination is undetermined.

   Then provide ALL of the following that apply:
   a) Exact field values to enter — Label and URI on separate lines, ready to copy.
   b) If replacing an existing statement, name the old statement and explain in one sentence why the new one is more appropriate.
   c) ALWAYS end with a suggested rights note. This is mandatory. Format it as a ready-to-paste string:
      Rights note: "[material type], [date]. [Legal basis for determination]. [Statement assigned] [YYYY-MM-DD]."
      Example: "Rights note: Unpublished manuscript, 1778. Creator (William Aylett) died prior to 1955; life+70 term has expired. NoC-US assigned 2026-03-14."
   d) If death date research is needed: "Search VIAF (viaf.org) or SNAC (snaccooperative.org) for: [creator name]"
   e) If renewal research is needed: "Search Stanford Renewal DB (exhibits.stanford.edu/copyrightrenewals) for title: [title], pub year: [year]"
   f) If the work is in copyright, note the earliest possible expiration year if calculable.

Key copyright rules:
- Unpublished works by known individual creator: life + 70 years. Creator died before 1955 → Public Domain.
- If a work was created before 1905, any individual creator is definitively dead before 1955 — do not hedge on this. State confidently that the life+70 term has expired.
- Unpublished works with unknown creator: if created before 1905 → Public Domain (120 years elapsed).
- Unpublished works with unknown creator: if created 1905–1955 → Undetermined (research needed).
- Published before 1930 → Public Domain.
- Published 1930–1963 with copyright notice: renewal was required. No renewal found → Public Domain. Renewal found → In Copyright (expires pub year + 95).
- Published 1930–1963 without copyright notice → Public Domain (failed formalities).
- Published 1964–1977 with notice → In Copyright (automatic renewal, 95 year term).
- Published 1978–Feb 1989: notice still required; without notice may be PD unless cured within 5 years.
- Published March 1989 or later → In Copyright (no formalities required).
- Federal government works → Public Domain.
- Do NOT invent rules not listed above. Base reasoning only on these rules and the metadata provided.

RightsStatements.org URIs — listed from strongest/most specific to weakest:
- NoC-US:   https://rightsstatements.org/vocab/NoC-US/1.0/   — Confirmed Public Domain in US. Use when you can cite the specific legal rule.
- InC:      https://rightsstatements.org/vocab/InC/1.0/      — Confirmed In Copyright.
- UND:      https://rightsstatements.org/vocab/UND/1.0/      — Status researched but genuinely unresolvable without more information.
- InC-RUU:  https://rightsstatements.org/vocab/InC-RUU/1.0/  — Believed in copyright but rights-holder cannot be located after reasonable search.
- NKC:      https://rightsstatements.org/vocab/NKC/1.0/      — Probably not in copyright but not confirmed. WEAKER than NoC-US.
- NoC-CR:   https://rightsstatements.org/vocab/NoC-CR/1.0/   — Public Domain but contractual restrictions apply.

CRITICAL — statement comparison rules:
- NKC is NOT equivalent to NoC-US. NKC means "probably PD, unconfirmed." NoC-US means "confirmed PD." If your determination is confirmed Public Domain, the correct statement is NoC-US, and NKC should be upgraded.
- If the existing statement is NKC and your determination is confident Public Domain → this is an UPGRADE. Use "→ Update rights statement" and explain that NoC-US is stronger because public domain status is now confirmed.
- If the existing statement is UND and you have reached a confident determination → always recommend updating.
- Only use "✓ Existing statement is correct" when the existing statement exactly matches your determination."""


def determine_copyright(meta: dict) -> str:
    """
    Call GPT-4 Turbo to make the copyright determination.
    Returns the full determination text.
    """
    question = build_determination_prompt(meta)

    # Build a rich context block from all available metadata
    today = date.today()
    today_str = today.strftime("%Y-%m-%d")
    pd_cutoff = today.year - 96  # works published before this year are public domain
    context_lines = [
        f"TODAY'S DATE: {today_str} — use this exact date in any rights note you generate.",
        f"PUBLIC DOMAIN CUTOFF: Works published before January 1, {pd_cutoff} are in the public domain in the US (95-year term). Do not use any other year as the cutoff.",
        "METADATA FROM DIGITAL COLLECTION RECORD:",
        f"  Title:               {meta.get('title', 'unknown')}",
        f"  Creator:             {meta.get('creator', 'unknown')}",
        f"  Date (raw):          {meta.get('date_raw', 'unknown')}",
        f"  Date (year parsed):  {meta.get('date', 'unknown')}",
        f"  Resource type:       {meta.get('type', 'unknown')}",
        f"  Genre:               {meta.get('genre', 'unknown')}",
        f"  Description:         {meta.get('description', 'none')}",
        f"  Publisher:           {meta.get('publisher', 'none')}",
        f"  Holding institution: {meta.get('holding_institution', 'unknown')}",
        f"  Existing rights:     {meta.get('rights_existing', 'none assigned')}",
        f"  Publication status:  {infer_publication_status(meta)}",
        f"  Source URL:          {meta.get('source_url', 'unknown')}",
    ]
    context = "\n".join(context_lines)

    response = openai_client.chat.completions.create(
        model="gpt-4-turbo-preview",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": f"{context}\n\nQUESTION: {question}"}
        ],
        temperature=0.2,
        max_tokens=600,
    )

    return response.choices[0].message.content


# ═══════════════════════════════════════════════════════════════════════════════
# PART 7 — MAIN SCANNER PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

def scan(input_url: str) -> dict:
    """
    Full pipeline: URL → metadata → copyright determination.
    Returns a result dict with keys: meta, determination, source_method.
    """
    print(f"\n{'═' * 65}")
    print(f"  COPYRIGHT SCANNER")
    print(f"{'═' * 65}")
    print(f"  Input:  {input_url}")

    # ── Step 1: Resolve URL ────────────────────────────────────────────────────
    print("\n  [1/4] Resolving URL...")
    resolved = resolve_url(input_url)
    if resolved != input_url:
        print(f"        → {resolved}")
    else:
        print(f"        → (no redirect)")

    node_id  = extract_node_id(resolved)
    platform = "islandora" if node_id else "unknown"

    print(f"        Platform:  {platform}")
    if node_id:
        print(f"        Node ID:   {node_id}")

    print(f"        Probing OAI endpoint...", end="  ", flush=True)
    oai_base = discover_oai_base(resolved)
    if oai_base:
        print(f"✅ {oai_base}")
    else:
        print("not found — will fall back to HTML scrape")

    # ── Step 2: Fetch metadata ─────────────────────────────────────────────────
    print("\n  [2/4] Fetching metadata...")

    raw_meta    = None
    source_method = None

    if node_id and oai_base:
        print(f"        Discovering OAI identifier...", end="  ", flush=True)
        oai_id = discover_oai_identifier(oai_base, node_id, resolved)
        if oai_id:
            print(f"✅ {oai_id}")

            # Try MODS first
            print(f"        Trying OAI-PMH / MODS...", end="  ", flush=True)
            xml = fetch_oai_record(oai_base, oai_id, "mods")
            if xml and "<mods" in xml.lower():
                print("✅ success")
                raw_meta      = parse_mods(xml)
                source_method = "OAI-PMH/MODS"
            else:
                print("not available")

            # Try Dublin Core
            if not raw_meta:
                print(f"        Trying OAI-PMH / oai_dc...", end=" ", flush=True)
                xml = fetch_oai_record(oai_base, oai_id, "oai_dc")
                if xml and "<dc:" in xml.lower():
                    print("✅ success")
                    raw_meta      = parse_oai_dc(xml)
                    source_method = "OAI-PMH/Dublin Core"
                else:
                    print("not available")
        else:
            print("not found — skipping OAI, using HTML scrape")

    # HTML scrape fallback
    if not raw_meta:
        print(f"        Falling back to HTML scrape...", end=" ", flush=True)
        raw_meta      = scrape_html_metadata(resolved)
        source_method = "HTML scrape"
        if raw_meta.get("title"):
            print("✅ success")
        else:
            print("⚠  limited data")

    # ── Step 3: Normalize ──────────────────────────────────────────────────────
    print("\n  [3/4] Normalizing metadata...")
    meta = normalize_metadata(raw_meta or {}, resolved, platform)

    print(f"        Title:    {meta.get('title', 'NOT FOUND')}")
    print(f"        Creator:  {meta.get('creator', 'NOT FOUND')}")
    print(f"        Date:     {meta.get('date_raw', 'NOT FOUND')}")
    print(f"        Type:     {meta.get('type', 'NOT FOUND')}")
    print(f"        Pub status: {infer_publication_status(meta)}")

    # ── Step 4: Determine copyright ────────────────────────────────────────────
    print("\n  [4/4] Determining copyright status...")
    determination = determine_copyright(meta)

    # ── Output ─────────────────────────────────────────────────────────────────
    print(f"\n{'─' * 65}")
    print("  DETERMINATION")
    print(f"{'─' * 65}")
    print(determination)
    print(f"{'─' * 65}")

    if meta.get("rights_existing"):
        print(f"\n  NOTE: Existing rights statement: {meta['rights_existing']}")

    print(f"  Metadata source: {source_method}")
    print(f"  Source URL: {resolved}\n")

    return {
        "meta":           meta,
        "determination":  determination,
        "source_method":  source_method,
        "resolved_url":   resolved,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PART 8 — CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) > 1:
        url = sys.argv[1].strip()
        scan(url)
    else:
        print("=" * 65)
        print("  COPYRIGHT SCANNER — Interactive Mode")
        print("  Paste a digital collection item URL to get a rights determination.")
        print("  Type 'quit' to exit.")
        print("=" * 65)
        print("\nExamples:")
        print("  https://hdl.handle.net/11134/3932475")
        print("  https://ctdigitalarchive.org/node/3932475")
        print()

        while True:
            try:
                url = input("\n🔗 Item URL: ").strip()
                if not url:
                    continue
                if url.lower() in ("quit", "exit", "q"):
                    print("\nGoodbye!")
                    break
                scan(url)
            except KeyboardInterrupt:
                print("\n\nGoodbye!")
                break


if __name__ == "__main__":
    main()
