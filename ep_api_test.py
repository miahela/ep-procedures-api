"""
EP API Explorer
===============
Fetches European Parliament legislative procedures, MEP details, and committee
meetings from the EP Open Data API (v2), then saves structured CSV/JSON output
and prints a pass/fail check report.

Requests are spaced by the configured delay (0.5 seconds by default).
"""

import json
import time
import csv
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from pathlib import Path

try:
    import requests
    from config import Settings, load_settings
except ImportError:
    raise SystemExit(
        "Missing dependency. Run: python -m pip install -r requirements.txt"
    ) from None

# ── Config ────────────────────────────────────────────────────────────────────

DEFAULTS    = Settings()
BASE        = DEFAULTS.base_url
FEED_BASE   = f"{BASE}/procedures/feed"
HEADERS     = {"Accept": "application/json"}
HEADERS_XML = {"Accept": "application/atom+xml, application/xml, */*"}
TIMEOUT = DEFAULTS.timeout
REQ_DELAY = DEFAULTS.request_delay

# Test scope
MAX_PROCEDURES = DEFAULTS.max_procedures
LOOKBACK_MONTHS = DEFAULTS.lookback_months
MEETINGS_HORIZON = DEFAULTS.meetings_horizon
MAX_MEETINGS = DEFAULTS.max_meetings

OUTPUT_DIR = Path(DEFAULTS.output_dir)

MEP_CACHE_FILE = OUTPUT_DIR / "mep_cache.json"


def configure():
    """Apply settings at run time, without creating files on import."""
    global BASE, FEED_BASE, TIMEOUT, REQ_DELAY
    global MAX_PROCEDURES, LOOKBACK_MONTHS, MEETINGS_HORIZON, MAX_MEETINGS
    global OUTPUT_DIR, MEP_CACHE_FILE
    settings = load_settings()
    BASE = settings.base_url.rstrip("/")
    FEED_BASE = f"{BASE}/procedures/feed"
    TIMEOUT, REQ_DELAY = settings.timeout, settings.request_delay
    MAX_PROCEDURES = settings.max_procedures
    LOOKBACK_MONTHS = settings.lookback_months
    MEETINGS_HORIZON = settings.meetings_horizon
    MAX_MEETINGS = settings.max_meetings
    OUTPUT_DIR = Path(settings.output_dir)
    MEP_CACHE_FILE = OUTPUT_DIR / "mep_cache.json"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Committees we care about (all known EP committees)
# Used to sweep meetings endpoint — extend as needed
ALL_COMMITTEES = [
    "AFCO", "AFET", "AGRI", "BUDG", "CONT", "CULT", "DEVE", "DROI",
    "ECON", "EMPL", "ENVI", "FEMM", "IMCO", "INTA", "ITRE", "JURI",
    "LIBE", "PECH", "PETI", "REGI", "SANT", "SEDE", "TRAN",
]

# ── State ─────────────────────────────────────────────────────────────────────

request_count = 0
mep_cache: dict[str, dict] = {}          # person_id -> {full_name, ...}
test_results: list[dict]   = []          # accumulated check results


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def get_json(url, params=None, *, empty_ok=False):
    global request_count
    time.sleep(REQ_DELAY)
    request_count += 1
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=TIMEOUT)
        print(f"  [HTTP {r.status_code}] {r.url}")
        if empty_ok:
            # Server failures still fail even when their body is empty.
            if r.status_code >= 500:
                print("  [ERROR] Server failure")
                return None
            if r.status_code in (204, 404) or not r.content.strip():
                return {"data": []}
            if r.status_code != 200:
                print("  [SKIP] Non-200 response; no agenda items processed")
                return {"data": []}
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ValueError("Expected a JSON object with a data array")
        if any(not isinstance(item, dict) for item in data["data"]):
            raise ValueError("Expected objects inside the data array")
        return data
    except requests.exceptions.Timeout:
        print(f"  [TIMEOUT] {url}")
        return None
    except requests.RequestException as e:
        print(f"  [ERROR] {url} -> {e}")
        return {"data": []} if empty_ok else None
    except ValueError as e:
        print(f"  [ERROR] {url} -> {e}")
        return None


def get_xml(url, params=None):
    global request_count
    time.sleep(REQ_DELAY)
    request_count += 1
    try:
        r = requests.get(url, headers=HEADERS_XML, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return ET.fromstring(r.content)
    except requests.exceptions.Timeout:
        print(f"  [TIMEOUT] {url}")
        return None
    except ET.ParseError as e:
        print(f"  [XML PARSE ERROR] {e}")
        return None
    except (requests.RequestException, ValueError) as e:
        print(f"  [ERROR] {url} -> {e}")
        return None


# ── Normalisation helpers ─────────────────────────────────────────────────────

def strip_prefix(value, prefix):
    if value and isinstance(value, str) and value.startswith(prefix):
        return value[len(prefix):]
    return value or ""

def normalise_activity_type(raw): return strip_prefix(raw, "def/ep-activities/")
def normalise_stage(raw):
    if raw and "/" in raw: return raw.rsplit("/", 1)[-1]
    return raw or ""
def normalise_role(raw): return strip_prefix(raw, "def/ep-roles/")

def as_list(value):
    if isinstance(value, list): return value
    if value in (None, ""): return []
    return [value]

def first_str(value, default=""):
    vals = as_list(value)
    return vals[0] if vals and isinstance(vals[0], str) else default

def clean_id(raw, prefix=""):
    if isinstance(raw, str):
        s = raw.replace(prefix, "").rstrip("/").rsplit("/", 1)[-1]
        return s
    return ""


# ── Test/check helpers ────────────────────────────────────────────────────────

def record_check(name: str, passed: bool, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    test_results.append({"check": name, "status": status, "detail": detail})
    msg = f"    [{status}] {name}"
    if detail: msg += f"  ->  {detail}"
    print(msg)


# ── Layer 1: Feed ─────────────────────────────────────────────────────────────

def fetch_feed(timeframe="today", start_date=None):
    params = {"process-type": "DEA,COD", "timeframe": timeframe}
    if start_date:
        params["timeframe"] = "custom"
        params["start-date"] = start_date

    print(f"  Calling feed: params={params}")
    root = get_xml(FEED_BASE, params)
    if root is None:
        return []

    NS = {"atom": "http://www.w3.org/2005/Atom"}
    proc_ids = []
    for entry in root.findall("atom:entry", NS):
        id_el = entry.find("atom:id", NS)
        if id_el is not None and id_el.text:
            pid = id_el.text.strip().rstrip("/").rsplit("/", 1)[-1]
            proc_ids.append(pid)
    return proc_ids


def fetch_procedure(proc_id):
    url = f"{BASE}/procedures/{proc_id}"
    data = get_json(url, {"format": "application/ld+json", "language": "en"})
    if data:
        items = data.get("data", [])
        return items[0] if items else None
    return None


# ── Layer 2: MEP resolution ───────────────────────────────────────────────────

def load_mep_cache():
    global mep_cache
    if MEP_CACHE_FILE.exists():
        with open(MEP_CACHE_FILE, encoding="utf-8") as f:
            try:
                cached = json.load(f)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON in MEP cache {MEP_CACHE_FILE}: {e}") from e
        if not isinstance(cached, dict) or any(not isinstance(v, dict) for v in cached.values()):
            raise ValueError(f"MEP cache {MEP_CACHE_FILE} must map person IDs to objects")
        mep_cache = cached
        print(f"  Loaded MEP cache: {len(mep_cache)} entries")

def save_mep_cache():
    with open(MEP_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(mep_cache, f, indent=2)

def resolve_mep(person_id: str) -> dict:
    """
    Fetch MEP details. Returns dict with at minimum: person_id, full_name.
    Uses in-memory + file cache to avoid repeated calls.
    """
    if not person_id:
        return {"person_id": "", "full_name": ""}

    if person_id in mep_cache:
        return mep_cache[person_id]

    url = f"{BASE}/meps/{person_id}"
    data = get_json(url, {"format": "application/ld+json", "language": "en"})

    result = {"person_id": person_id, "full_name": "", "nationality": "", "political_group": ""}

    if data:
        items = data.get("data", [])
        if items:
            mep = items[0]
            # Full name: try label or composed from given/family name
            label = mep.get("label", "")
            given = mep.get("given_name", "")
            family = mep.get("family_name", "")
            full_name = label or f"{given} {family}".strip()
            result["full_name"] = full_name
            result["nationality"] = mep.get("nationality", "")
            # Political group from membership
            for m in mep.get("has_membership", []) if isinstance(mep.get("has_membership"), list) else []:
                if isinstance(m, dict) and "politicalGroup" in m:
                    result["political_group"] = m["politicalGroup"].replace("org/", "")
                    break

    mep_cache[person_id] = result
    return result


# ── Layer 3: Meetings + foreseen activities ───────────────────────────────────

def fetch_meetings(body_id: str, start_date: str, end_date: str) -> list[dict] | None:
    """
    Use documented year/paging parameters, then enforce date and committee locally.
    EP v2 documents plenary meetings, not a committee meeting calendar.
    """
    url = f"{BASE}/meetings"
    data = get_json(url, {
        "format": "application/ld+json",
        "year": ",".join(str(y) for y in range(int(start_date[:4]), int(end_date[:4]) + 1)),
        "offset": 0,
        "limit": 50,
    })
    if data is None:
        return None
    start, end = datetime.fromisoformat(start_date).date(), datetime.fromisoformat(end_date).date()
    meetings = []
    for meeting in data["data"]:
        sitting_id = extract_sitting_id(meeting)
        # Never relabel a plenary meeting as a committee meeting.
        if re.match(r"G?MTG-PL-", sitting_id):
            continue
        match = re.search(r"\d{4}-\d{2}-\d{2}", sitting_id)
        if not match:
            continue
        try:
            day = datetime.fromisoformat(match.group()).date()
        except ValueError:
            continue
        bodies = as_list(meeting.get("had_participant_organization")) + as_list(meeting.get("body_id"))
        committee_matches = body_id in sitting_id.split("-") or any(
            clean_id(body) == body_id for body in bodies if isinstance(body, str)
        )
        if start <= day <= end and committee_matches:
            meetings.append(meeting)
    meetings.sort(key=extract_sitting_id)
    print(f"    Filtered {len(data['data'])} returned meetings -> {len(meetings)} for {body_id} in {start_date}..{end_date}")
    return meetings


def fetch_foreseen_activities(sitting_id: str) -> list[dict] | None:
    """
    GET /meetings/{sitting-id}/foreseen-activities
    Returns planned activities for a sitting.
    """
    url = f"{BASE}/meetings/{sitting_id}/foreseen-activities"
    data = get_json(url, {"format": "application/ld+json"}, empty_ok=True)
    if data:
        return data.get("data", [])
    return None


def extract_sitting_id(meeting: dict) -> str:
    """Pull the sitting/meeting ID from a meeting object."""
    mid = meeting.get("activity_id") or meeting.get("id", "")
    return clean_id(mid)


def extract_foreseen_proc_refs(activity: dict) -> list[str]:
    """
    Extract any procedure references from a foreseen activity.
    They may be in: based_on_a_realization_of, documented_by_a_realization_of,
    or a direct process_id field.
    """
    refs = []
    for field in ["based_on_a_realization_of", "documented_by_a_realization_of",
                  "recorded_in_a_realization_of"]:
        for doc_id in as_list(activity.get(field, [])):
            refs.append(str(doc_id))
    if activity.get("process_id"):
        refs.append(str(activity["process_id"]))
    return refs


# ── Participation extraction (from your v1, with empty-role guard) ─────────────

def extract_participations(proc):
    rows = []
    for p in proc.get("had_participation", []):
        if not isinstance(p, dict):
            continue
        # Guard: skip rows where 'id' looks like a field name, not a real ID
        pid = p.get("id", "")
        if pid in ("id", "type", "had_participant_organization",
                   "had_participant_person", "participation_role"):
            continue

        person       = first_str(p.get("had_participant_person", []), "").replace("person/", "")
        organization = first_str(p.get("had_participant_organization", []), "").replace("org/", "")
        role         = normalise_role(p.get("participation_role", ""))
        pg           = p.get("politicalGroup", "")
        in_name_of   = p.get("participation_in_name_of", "")
        parl_term    = p.get("parliamentary_term", "")

        rows.append({
            "participation_id":   pid,
            "role":               role,
            "person":             person,
            "organization":       organization,
            "political_group":    pg.replace("org/", "") if isinstance(pg, str) else "",
            "activity_date":      p.get("activity_date", ""),
            "stage":              normalise_stage(p.get("occured_at_stage", "")),
            "in_name_of":         in_name_of.replace("org/", "") if isinstance(in_name_of, str) else "",
            "parliamentary_term": parl_term.replace("org/", "") if isinstance(parl_term, str) else "",
        })
    return rows


def extract_events(event_list, source):
    rows = []
    for e in event_list:
        if not isinstance(e, dict):
            continue
        rows.append({
            "source":           source,
            "event_id":         e.get("activity_id") or e.get("id", ""),
            "type":             e.get("type", ""),
            "activity_type":    normalise_activity_type(e.get("had_activity_type", "")),
            "activity_date":    e.get("activity_date", ""),
            "stage":            normalise_stage(e.get("occured_at_stage", "")),
            "based_on_docs":    "|".join(as_list(e.get("based_on_a_realization_of", []))),
            "recorded_in_docs": "|".join(as_list(e.get("recorded_in_a_realization_of", []))),
            "decided_on_docs":  "|".join(as_list(e.get("decided_on_a_realization_of", []))),
        })
    return rows


def extract_scheduled(proc) -> list[dict]:
    """Extract foreseen/scheduled activities from was_scheduled_in."""
    rows = []
    for e in as_list(proc.get("was_scheduled_in", [])):
        if not isinstance(e, dict):
            continue
        rows.append({
            "event_id":        e.get("activity_id") or e.get("id", ""),
            "activity_type":   normalise_activity_type(e.get("had_activity_type", "")),
            "activity_date":   e.get("activity_date", ""),
            "stage":           normalise_stage(e.get("occured_at_stage", "")),
            "agenda_point":    e.get("notation_agendaPoint", ""),
            "documented_by":   "|".join(as_list(e.get("documented_by_a_realization_of", []))),
        })
    return rows


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global request_count, mep_cache
    configure()
    request_count, mep_cache = 0, {}
    test_results.clear()
    print("=" * 60)
    print("EP API Test Script v2")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    load_mep_cache()

    # ── Step 1: Fetch procedure list ──────────────────────────────────────────
    print("\n[1] Fetching procedure list...")
    proc_ids = fetch_feed(timeframe="today")
    print(f"    -> {len(proc_ids)} procedures updated today")

    if not proc_ids:
        start = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_MONTHS * 30)).strftime("%Y-%m-%d")
        print(f"    -> Falling back to last {LOOKBACK_MONTHS} months (from {start})...")
        proc_ids = fetch_feed(start_date=start)
        print(f"    -> {len(proc_ids)} procedures found")

    record_check("Feed returns procedures", len(proc_ids) > 0, f"{len(proc_ids)} found")

    if not proc_ids:
        print("    [FATAL] No procedures found.")
        sys.exit(1)

    proc_ids = list(dict.fromkeys(proc_ids))[:MAX_PROCEDURES]
    print(f"    -> Processing {len(proc_ids)} (cap={MAX_PROCEDURES})")

    # ── Step 2: Per-procedure fetch ───────────────────────────────────────────
    all_procedure_meta      = []
    all_participations      = []
    all_participations_resolved = []   # with MEP names
    all_events              = []
    all_scheduled           = []
    activity_type_inventory = defaultdict(int)
    proc_type_counts        = defaultdict(int)
    committees_seen         = set()    # to drive meetings sweep later

    print(f"\n[2] Fetching procedure details...")

    for i, proc_id in enumerate(proc_ids, 1):
        print(f"\n  [{i}/{len(proc_ids)}] {proc_id}")

        proc = fetch_procedure(proc_id)
        if not proc:
            record_check(f"{proc_id}: procedure fetchable", False, "No data returned")
            continue

        record_check(f"{proc_id}: procedure fetchable", True)

        pt         = proc.get("process_title", {})
        proc_title = pt.get("en", "") if isinstance(pt, dict) else str(pt)
        proc_type  = strip_prefix(proc.get("process_type", ""), "def/ep-procedure-types/")
        proc_type_counts[proc_type] += 1
        print(f"    {proc_type}  |  {proc_title[:65]}")

        # Events
        events_from_proc = as_list(proc.get("consists_of", []))
        for e in events_from_proc:
            if isinstance(e, dict):
                at = normalise_activity_type(e.get("had_activity_type", ""))
                activity_type_inventory[at] += 1

        proc_events = extract_events(events_from_proc, "procedure_object")
        for e in proc_events:
            e["proc_id"] = proc_id
        all_events.extend(proc_events)

        record_check(f"{proc_id}: has events", len(proc_events) > 0, f"{len(proc_events)} events")

        # Scheduled/foreseen from procedure
        scheduled = extract_scheduled(proc)
        for s in scheduled:
            s["proc_id"] = proc_id
        all_scheduled.extend(scheduled)

        # Participations
        participations = extract_participations(proc)
        for p in participations:
            p["proc_id"] = proc_id
        all_participations.extend(participations)

        record_check(f"{proc_id}: has participations", len(participations) > 0,
                     f"{len(participations)} rows")

        # Collect committees for meetings sweep
        for p in participations:
            org = p.get("organization") or p.get("in_name_of")
            if org:
                committees_seen.add(org)

        # MEP resolution
        person_ids = list({p["person"] for p in participations if p["person"]})
        resolved_count = 0
        for pid in person_ids:
            mep = resolve_mep(pid)
            if mep["full_name"]:
                resolved_count += 1

        record_check(f"{proc_id}: MEP names resolved",
                     resolved_count > 0 or not person_ids,
                     f"{resolved_count}/{len(person_ids)} resolved")

        # Build resolved participation rows
        for p in participations:
            row = dict(p)
            if p["person"]:
                mep = mep_cache.get(p["person"], {})
                row["full_name"] = mep.get("full_name", "")
                row["mep_nationality"] = mep.get("nationality", "")
            else:
                row["full_name"] = ""
                row["mep_nationality"] = ""
            all_participations_resolved.append(row)

        # Meta
        meta = {
            "proc_id":            proc_id,
            "proc_type":          proc_type,
            "title_en":           proc_title,
            "current_stage":      normalise_stage(proc.get("current_stage", "")),
            "identifier_year":    proc.get("identifierYear", ""),
            "label":              proc.get("label", ""),
            "n_participations":   len(participations),
            "n_events":           len(proc_events),
            "n_scheduled":        len(scheduled),
            "n_docs":             len(as_list(proc.get("created_a_realization_of", []))),
            "mep_ids_seen":       "|".join(person_ids),
        }
        all_procedure_meta.append(meta)

        print(f"    events={len(proc_events)} | scheduled={len(scheduled)} | participations={len(participations)} | meps_resolved={resolved_count}/{len(person_ids)}")

    save_mep_cache()

    # ── Step 3: Meetings + foreseen activities ────────────────────────────────
    print(f"\n[3] Fetching meetings + foreseen activities...")
    today     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    end_date  = (datetime.now(timezone.utc) + timedelta(days=MEETINGS_HORIZON)).strftime("%Y-%m-%d")

    # Use committees seen in procedures, fallback to full list
    target_committees = sorted(committees_seen & set(ALL_COMMITTEES)) or ALL_COMMITTEES[:5]
    print(f"    Committees to sweep: {target_committees}")

    all_meetings         = []
    all_foreseen         = []
    proc_id_set          = set(proc_ids)  # for cross-reference
    foreseen_cross_refs  = []             # foreseen items that mention a tracked procedure
    seen_meetings = set()
    print(f"    Global meeting/agenda cap: {MAX_MEETINGS}")
    print("    EP v2 documents plenary meetings; no committee-calendar endpoint is advertised.")
    print("    Strict local committee/date filters may therefore return zero meetings.")

    for committee in target_committees:
        if len(all_meetings) >= MAX_MEETINGS:
            print(f"    Meeting cap reached ({MAX_MEETINGS}); stopping the sweep.")
            break
        print(f"    Meetings for {committee} ({today} -> {end_date})...")
        meetings = fetch_meetings(committee, today, end_date)
        print(f"      -> {len(meetings or [])} meetings")

        record_check(f"Meetings endpoint: {committee}",
                     meetings is not None,
                     f"{len(meetings)} meetings returned" if meetings is not None else "Request failed")

        for meeting in meetings or []:
            sitting_id = extract_sitting_id(meeting)
            if not sitting_id or sitting_id in seen_meetings:
                continue
            if len(all_meetings) >= MAX_MEETINGS:
                print(f"    Meeting cap reached ({MAX_MEETINGS}); remaining meetings skipped.")
                break
            seen_meetings.add(sitting_id)

            m_row = {
                "committee":    committee,
                "sitting_id":   sitting_id,
                "meeting_date": re.search(r"\d{4}-\d{2}-\d{2}", sitting_id).group(),
                "meeting_type": normalise_activity_type(meeting.get("had_activity_type", "")),
                "location":     meeting.get("label", ""),
            }
            all_meetings.append(m_row)

            # Fetch foreseen activities for this sitting
            foreseen = fetch_foreseen_activities(sitting_id)
            print(f"        {sitting_id}: {len(foreseen or [])} foreseen activities")

            record_check(f"Foreseen activities: {sitting_id}",
                         foreseen is not None,
                         f"{len(foreseen)} items" if foreseen is not None else "Request failed")

            for fa in foreseen or []:
                if not isinstance(fa, dict):
                    continue
                proc_refs = extract_foreseen_proc_refs(fa)
                at = normalise_activity_type(fa.get("had_activity_type", ""))
                fa_row = {
                    "committee":     committee,
                    "sitting_id":    sitting_id,
                    "meeting_date":  m_row["meeting_date"],
                    "activity_type": at,
                    "activity_date": fa.get("activity_date", ""),
                    "agenda_point":  fa.get("notation_agendaPoint", ""),
                    "proc_refs":     "|".join(proc_refs),
                    "raw_id":        fa.get("activity_id") or fa.get("id", ""),
                }
                all_foreseen.append(fa_row)

                # Cross-reference: does this foreseen activity mention a tracked proc?
                for ref in proc_refs:
                    for pid in proc_id_set:
                        if pid in ref:
                            foreseen_cross_refs.append({
                                **fa_row,
                                "matched_proc_id": pid,
                            })

    record_check("Foreseen activities collected (informational)", True,
                 f"{len(all_foreseen)} total")
    record_check("Foreseen cross-refs to tracked procs",
                 len(foreseen_cross_refs) >= 0,   # informational, not a failure
                 f"{len(foreseen_cross_refs)} matches")

    # ── Step 4: Save outputs ──────────────────────────────────────────────────
    print(f"\n[4] Saving outputs to ./{OUTPUT_DIR}/...")

    def save_json(obj, name):
        p = OUTPUT_DIR / name
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, default=str)
        print(f"    OK {name}  ({p.stat().st_size // 1024} KB)")

    def save_csv(rows, name):
        if not rows:
            print(f"    SKIP {name} - no data")
            return
        p = OUTPUT_DIR / name
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"    OK {name}  ({len(rows)} rows)")

    save_json(all_procedure_meta,   "procedures_meta.json")
    save_json(dict(activity_type_inventory), "activity_type_inventory.json")
    save_csv(all_procedure_meta,             "procedures_meta.csv")
    save_csv(all_participations_resolved,    "participations_resolved.csv")
    save_csv(all_events,                     "events.csv")
    save_csv(all_scheduled,                  "scheduled_from_procedure.csv")
    save_csv(all_meetings,                   "meetings.csv")
    save_csv(all_foreseen,                   "foreseen_activities.csv")
    save_csv(foreseen_cross_refs,            "foreseen_cross_refs.csv")
    save_json(mep_cache,                     "mep_cache.json")

    # ── Step 5: Test report ───────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("TEST / CHECK REPORT")
    print("=" * 60)

    passed = [t for t in test_results if t["status"] == "PASS"]
    failed = [t for t in test_results if t["status"] == "FAIL"]

    print(f"  PASS: {len(passed)}  |  FAIL: {len(failed)}  |  TOTAL: {len(test_results)}")
    print()

    if failed:
        print("  FAILURES:")
        for t in failed:
            print(f"    FAIL {t['check']}  ->  {t['detail']}")
        print()

    print("  FINDINGS SUMMARY:")
    print(f"    Procedures processed     : {len(all_procedure_meta)}")
    print(f"    API requests made        : {request_count}")
    print(f"    Procedure types          : {dict(proc_type_counts)}")
    print(f"    Unique MEPs cached       : {len(mep_cache)}")
    print(f"    Committees swept         : {target_committees}")
    print(f"    Meetings found           : {len(all_meetings)}")
    print(f"    Foreseen activities      : {len(all_foreseen)}")
    print(f"    Foreseen -> proc matches  : {len(foreseen_cross_refs)}")
    print(f"    Scheduled (from proc)    : {len(all_scheduled)}")
    print()

    print("  ACTIVITY TYPES SEEN:")
    for at, cnt in sorted(activity_type_inventory.items(), key=lambda x: -x[1]):
        print(f"    {at:<60} {cnt:>4}x")
    print()

    print("  PARTICIPATION ROLES SEEN:")
    role_counts = defaultdict(int)
    for p in all_participations:
        role_counts[p["role"]] += 1
    for role, cnt in sorted(role_counts.items(), key=lambda x: -x[1]):
        print(f"    {role:<60} {cnt:>4}x")
    print()

    print("  MEP RESOLUTION SAMPLE (first 10):")
    shown = 0
    for pid, data in mep_cache.items():
        if shown >= 10: break
        print(f"    {pid:<10} -> {data.get('full_name','?'):<35} ({data.get('nationality','?')})")
        shown += 1

    print()
    print("  FORESEEN ACTIVITY TYPES SEEN:")
    fa_types = defaultdict(int)
    for fa in all_foreseen:
        fa_types[fa["activity_type"]] += 1
    if fa_types:
        for at, cnt in sorted(fa_types.items(), key=lambda x: -x[1]):
            print(f"    {at:<60} {cnt:>4}x")
    else:
        print("    (none — meetings endpoint may return empty for this horizon)")

    save_json(test_results, "test_report.json")
    save_csv(test_results,  "test_report.csv")

    print(f"\nAll outputs in: ./{OUTPUT_DIR}/")
    print("Done.")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    try:
        main()
    except (OSError, UnicodeError) as e:
        raise SystemExit(f"File error: {e}. Check the output path and permissions.") from None
    except ValueError as e:
        raise SystemExit(f"Configuration or data error: {e}") from None
    except KeyboardInterrupt:
        raise SystemExit("Interrupted. Existing exports and cache remain in the output folder.") from None
