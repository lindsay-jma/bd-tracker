#!/usr/bin/env python3
"""
BD tracker: pulls SAM.gov opportunities matching JMA's profile, dedupes them
against a local SQLite file, scores/tags them, appends new ones to a Google
Sheet, and (if Gmail creds are set) emails a daily digest.

Designed to run once a day as a GitHub Actions job. Every external call is
budgeted to stay well under SAM.gov's personal API key rate limit.
"""

import argparse
import json
import os
import smtplib
import sqlite3
import sys
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText

import requests

import config

DB_PATH = "opportunities.db"
SAM_ENDPOINT = "https://api.sam.gov/opportunities/v2/search"
SHEET_TAB = "Pipeline"
PTYPES = "o,p,r,k"  # solicitation, presolicitation, sources sought, combined synopsis

SHEET_COLUMNS = [
    "Notice ID", "Title", "Agency", "Type", "Set-Aside", "NAICS", "Posted",
    "Deadline", "Days Left", "Score", "Eligibility", "Link", "CO Contact",
    "Status", "Bid Decision", "No-Bid Reason", "Notes",
]

TERMINAL_STATUSES = {"No-Bid", "Lost", "Won", "Expired"}


# ---------- SQLite ----------

def init_db(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS opportunities (
            notice_id TEXT PRIMARY KEY,
            title TEXT,
            agency TEXT,
            type TEXT,
            setaside TEXT,
            naics TEXT,
            posted TEXT,
            deadline TEXT,
            score INTEGER,
            eligibility TEXT,
            link TEXT,
            contact TEXT,
            last_seen TEXT,
            raw_json TEXT
        )
    """)
    conn.commit()
    return conn


def upsert(conn, notice_id, fields, raw_json):
    """Returns 'new', 'amended', or 'unchanged'."""
    cur = conn.execute(
        "SELECT type, deadline FROM opportunities WHERE notice_id = ?",
        (notice_id,),
    )
    row = cur.fetchone()
    today = date.today().isoformat()

    if row is None:
        conn.execute(
            """INSERT INTO opportunities
               (notice_id, title, agency, type, setaside, naics, posted,
                deadline, score, eligibility, link, contact, last_seen, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                notice_id, fields["title"], fields["agency"], fields["type"],
                fields["setaside"], fields["naics"], fields["posted"],
                fields["deadline"], fields["score"], fields["eligibility"],
                fields["link"], fields["contact"], today, raw_json,
            ),
        )
        conn.commit()
        return "new"

    old_type, old_deadline = row
    if old_type != fields["type"] or old_deadline != fields["deadline"]:
        conn.execute(
            """UPDATE opportunities SET type=?, deadline=?, last_seen=?, raw_json=?
               WHERE notice_id=?""",
            (fields["type"], fields["deadline"], today, raw_json, notice_id),
        )
        conn.commit()
        return "amended"

    conn.execute(
        "UPDATE opportunities SET last_seen=? WHERE notice_id=?",
        (today, notice_id),
    )
    conn.commit()
    return "unchanged"


# ---------- SAM.gov API ----------

def fetch_opportunities(api_key, posted_from, posted_to):
    """Paginated fetch. Returns list of raw opportunity dicts, or None on
    a rate-limit / server error (caller should back off, not crash)."""
    results = []
    offset = 0
    limit = 1000

    while True:
        params = {
            "api_key": api_key,
            "postedFrom": posted_from,
            "postedTo": posted_to,
            "ptype": PTYPES,
            "limit": limit,
            "offset": offset,
        }
        try:
            resp = requests.get(SAM_ENDPOINT, params=params, timeout=30)
        except requests.RequestException as e:
            print(f"WARNING: request failed: {e}", file=sys.stderr)
            return None

        if resp.status_code == 429:
            print("WARNING: SAM.gov rate limit hit (429). Stopping for today.", file=sys.stderr)
            return None
        if resp.status_code >= 500:
            print(f"WARNING: SAM.gov server error {resp.status_code}. Stopping for today.", file=sys.stderr)
            return None
        if resp.status_code != 200:
            print(f"WARNING: SAM.gov returned {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
            return None

        data = resp.json()
        batch = data.get("opportunitiesData", [])
        results.extend(batch)

        total = data.get("totalRecords", len(results))
        offset += limit
        if offset >= total or not batch:
            break

    return results


def fetch_description(url, api_key):
    try:
        resp = requests.get(url, params={"api_key": api_key}, timeout=20)
        if resp.status_code == 200:
            text = resp.text
            try:
                parsed = resp.json()
                text = parsed.get("description", text)
            except ValueError:
                pass
            return text.strip()[:400]
    except requests.RequestException:
        pass
    return ""


# ---------- Filtering & scoring ----------

def passes_filter(item):
    title = (item.get("title") or "").lower()
    naics = item.get("naicsCode") or ""

    if any(kw.lower() in title for kw in config.KEYWORDS_EXCLUDE):
        return False
    if naics in config.NAICS_CODES:
        return True
    if any(kw.lower() in title for kw in config.KEYWORDS_INCLUDE):
        return True
    return False


def _place_of_performance_state(item):
    pop = item.get("placeOfPerformance") or {}
    state = pop.get("state")
    if isinstance(state, dict):
        return state.get("code", "")
    return state or ""


def score_and_tag(item):
    title = item.get("title") or ""
    title_lower = title.lower()
    setaside = item.get("typeOfSetAside") or ""
    ptype_desc = item.get("type") or ""
    naics = item.get("naicsCode") or ""
    deadline_raw = item.get("responseDeadLine") or ""

    if setaside in config.INELIGIBLE_SETASIDES:
        eligibility = "INELIGIBLE"
    elif setaside in ("WOSB", "EDWOSB", "SBA", "SBP"):
        eligibility = "ELIGIBLE"
    elif setaside == "":
        eligibility = "SUB-ONLY"
    else:
        eligibility = "REVIEW"

    score = 0
    if setaside in ("WOSB", "EDWOSB"):
        score += 30
    if any(t.lower() in ptype_desc.lower() for t in config.EARLY_VISIBILITY_TYPES):
        score += 20
    if naics in config.NAICS_CODES:
        score += 15

    kw_hits = sum(1 for kw in config.KEYWORDS_INCLUDE if kw.lower() in title_lower)
    score += min(kw_hits, 3) * 10

    state = _place_of_performance_state(item)
    if state in config.STATES_PRIORITY:
        score += 10

    days_left = None
    if deadline_raw:
        try:
            deadline_date = datetime.fromisoformat(deadline_raw.replace("Z", "+00:00")).date()
            days_left = (deadline_date - date.today()).days
            if 0 <= days_left < 7:
                score -= 20
        except ValueError:
            pass

    if any(kw.lower() in title_lower for kw in config.KEYWORDS_EXCLUDE):
        score = 0

    status = "Review" if score >= config.REVIEW_SCORE_THRESHOLD else "Logged"

    contacts = item.get("pointOfContact") or []
    contact_str = ""
    if contacts:
        c = contacts[0]
        contact_str = f"{c.get('fullName', '')} <{c.get('email', '')}>".strip()

    return {
        "title": title,
        "agency": item.get("fullParentPathName") or "",
        "type": ptype_desc,
        "setaside": setaside,
        "naics": naics,
        "posted": item.get("postedDate") or "",
        "deadline": deadline_raw,
        "days_left": days_left,
        "score": score,
        "eligibility": eligibility,
        "link": item.get("uiLink") or "",
        "contact": contact_str,
        "status": status,
        "description_url": item.get("description") or "",
    }


# ---------- Google Sheets ----------

def get_sheet(google_creds_json, sheet_id):
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    info = json.loads(google_creds_json)
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    client = gspread.authorize(creds)
    sh = client.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_TAB, rows=1000, cols=len(SHEET_COLUMNS))
        ws.append_row(SHEET_COLUMNS)
    return ws


def append_new_rows(ws, notice_ids, fields_by_id):
    rows = []
    for nid in notice_ids:
        f = fields_by_id[nid]
        row_num_placeholder = "{row}"  # gspread fills actual row via append; formula uses relative ref workaround below
        rows.append([
            nid, f["title"], f["agency"], f["type"], f["setaside"], f["naics"],
            f["posted"], f["deadline"], "",  # Days Left formula patched below
            f["score"], f["eligibility"], f["link"], f["contact"],
            f["status"], "", "", "",
        ])
    if not rows:
        return
    start_row = len(ws.get_all_values()) + 1
    ws.append_rows(rows, value_input_option="USER_ENTERED")
    # Patch in the Days Left formula per appended row (column I)
    updates = []
    for i in range(len(rows)):
        r = start_row + i
        updates.append({
            "range": f"I{r}",
            "values": [[f'=IF(H{r}="","",H{r}-TODAY())']],
        })
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")


def apply_amendments(ws, notice_ids, fields_by_id):
    if not notice_ids:
        return
    all_values = ws.get_all_values()
    header = all_values[0] if all_values else SHEET_COLUMNS
    try:
        id_col = header.index("Notice ID")
        type_col = header.index("Type")
        deadline_col = header.index("Deadline")
        notes_col = header.index("Notes")
    except ValueError:
        print("WARNING: sheet header doesn't match expected columns, skipping amendments", file=sys.stderr)
        return

    id_to_row = {row[id_col]: i + 1 for i, row in enumerate(all_values) if len(row) > id_col}

    for nid in notice_ids:
        if nid not in id_to_row:
            continue
        r = id_to_row[nid]
        f = fields_by_id[nid]
        today_str = date.today().isoformat()
        existing_notes = all_values[r - 1][notes_col] if len(all_values[r - 1]) > notes_col else ""
        new_notes = (existing_notes + f"; AMENDED {today_str}").strip("; ")
        ws.update_cell(r, type_col + 1, f["type"])
        ws.update_cell(r, deadline_col + 1, f["deadline"])
        ws.update_cell(r, notes_col + 1, new_notes)


def read_pipeline_for_digest(ws):
    return ws.get_all_records()


# ---------- Email digest ----------

def send_digest(gmail_address, gmail_app_password, new_eligible, deadline_warnings):
    if not new_eligible and not deadline_warnings:
        return

    lines = []
    if new_eligible:
        lines.append(f"NEW ELIGIBLE OPPORTUNITIES ({len(new_eligible)})")
        lines.append("-" * 40)
        for f in new_eligible:
            lines.append(f"[{f['score']}] {f['title']}")
            lines.append(f"  Agency: {f['agency']}")
            lines.append(f"  Deadline: {f['deadline'] or 'n/a'}")
            lines.append(f"  {f['link']}")
            lines.append("")

    if deadline_warnings:
        lines.append(f"DEADLINES WITHIN {config.DEADLINE_WARNING_DAYS} DAYS ({len(deadline_warnings)})")
        lines.append("-" * 40)
        for row in deadline_warnings:
            lines.append(f"{row.get('Title', '')} — due {row.get('Deadline', '')} — status: {row.get('Status', '')}")
            lines.append(f"  {row.get('Link', '')}")
            lines.append("")

    body = "\n".join(lines)
    msg = MIMEText(body)
    msg["Subject"] = f"BD Tracker Digest — {date.today().isoformat()}"
    msg["From"] = gmail_address
    msg["To"] = gmail_address

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, [gmail_address], msg.as_string())


# ---------- Main ----------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill-days", type=int, default=1,
                         help="How many days back to search (default 1 = last 24h)")
    args = parser.parse_args()

    api_key = os.environ.get("SAM_API_KEY")
    google_creds_json = os.environ.get("GOOGLE_CREDS_JSON")
    sheet_id = os.environ.get("SHEET_ID")
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD")

    missing = [name for name, val in [
        ("SAM_API_KEY", api_key), ("GOOGLE_CREDS_JSON", google_creds_json),
        ("SHEET_ID", sheet_id),
    ] if not val]
    if missing:
        print(f"ERROR: missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    posted_to = date.today()
    posted_from = posted_to - timedelta(days=args.backfill_days)
    posted_from_str = posted_from.strftime("%m/%d/%Y")
    posted_to_str = posted_to.strftime("%m/%d/%Y")

    print(f"Fetching SAM.gov opportunities posted {posted_from_str} to {posted_to_str}...")
    raw_items = fetch_opportunities(api_key, posted_from_str, posted_to_str)

    if raw_items is None:
        print("No data retrieved this run (rate limit or server error). Exiting cleanly; "
              "tomorrow's run will widen its lookback window automatically if you pass --backfill-days.")
        sys.exit(0)

    print(f"Retrieved {len(raw_items)} raw postings. Filtering...")
    filtered = [item for item in raw_items if passes_filter(item)]
    print(f"{len(filtered)} passed the NAICS/keyword filter.")

    conn = init_db()
    new_ids, amended_ids = [], []
    fields_by_id = {}

    for item in filtered:
        nid = item.get("noticeId")
        if not nid:
            continue
        fields = score_and_tag(item)
        fields_by_id[nid] = fields
        result = upsert(conn, nid, fields, json.dumps(item))
        if result == "new":
            new_ids.append(nid)
        elif result == "amended":
            amended_ids.append(nid)

    print(f"{len(new_ids)} new, {len(amended_ids)} amended, "
          f"{len(filtered) - len(new_ids) - len(amended_ids)} unchanged.")

    # Budgeted description fetch for the highest-scoring new items only
    top_new = sorted(new_ids, key=lambda i: fields_by_id[i]["score"], reverse=True)
    for nid in top_new[:config.MAX_DESCRIPTION_FETCHES]:
        url = fields_by_id[nid]["description_url"]
        if url:
            fields_by_id[nid]["description"] = fetch_description(url, api_key)

    if not new_ids and not amended_ids:
        print("Nothing new to write to the sheet. Done.")
        conn.close()
        return

    ws = get_sheet(google_creds_json, sheet_id)
    if new_ids:
        append_new_rows(ws, new_ids, fields_by_id)
        print(f"Appended {len(new_ids)} new rows to the sheet.")
    if amended_ids:
        apply_amendments(ws, amended_ids, fields_by_id)
        print(f"Applied {len(amended_ids)} amendments to the sheet.")

    # Digest
    if gmail_address and gmail_app_password:
        new_eligible = [fields_by_id[nid] for nid in new_ids
                         if fields_by_id[nid]["eligibility"] == "ELIGIBLE"]

        deadline_warnings = []
        try:
            pipeline_rows = read_pipeline_for_digest(ws)
            today = date.today()
            for row in pipeline_rows:
                status = row.get("Status", "")
                deadline_str = row.get("Deadline", "")
                if status in TERMINAL_STATUSES or not deadline_str:
                    continue
                try:
                    d = datetime.fromisoformat(deadline_str.replace("Z", "+00:00")).date()
                except ValueError:
                    continue
                days_out = (d - today).days
                if 0 <= days_out <= config.DEADLINE_WARNING_DAYS:
                    deadline_warnings.append(row)
        except Exception as e:
            print(f"WARNING: couldn't build deadline warnings: {e}", file=sys.stderr)

        send_digest(gmail_address, gmail_app_password, new_eligible, deadline_warnings)
        print("Digest email sent." if (new_eligible or deadline_warnings) else "Nothing digest-worthy today.")
    else:
        print("Gmail credentials not set, skipping digest email.")

    conn.close()


if __name__ == "__main__":
    main()
