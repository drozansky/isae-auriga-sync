import os
import json
import re
from datetime import datetime, timedelta
import pytz
from playwright.sync_api import sync_playwright
from google.oauth2 import service_account
from googleapiclient.discovery import build

PARIS_TZ = pytz.timezone("Europe/Paris")

def get_google_service():
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise ValueError("Missing GOOGLE_SERVICE_ACCOUNT_JSON secret.")
    creds_dict = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=creds)

def fetch_auriga_schedule():
    username = os.environ["SCHOOL_USERNAME"]
    password = os.environ["SCHOOL_PASSWORD"]
    
    events = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="fr-FR", timezone_id="Europe/Paris")
        page = context.new_page()

        # Intercept background PrimeFaces schedule XML/JSON updates
        def handle_response(response):
            if "Planning" in response.url or "schedule" in response.url:
                try:
                    text = response.text()
                    # Check for PrimeFaces JSON schedule payloads
                    if '"events":' in text:
                        match = re.search(r'\{"events"\s*:\s*(\[.*?\])\}', text)
                        if match:
                            parsed = json.loads(match.group(1))
                            events.extend(parsed)
                except Exception:
                    pass

        page.on("response", handle_response)

        # 1. Access portal - will redirect to CAS
        page.goto("https://auriga.isae-supaero.fr", wait_until="networkidle")

        # 2. Complete CAS Login if redirected
        if "cas" in page.url.lower() or page.locator("input[type='password']").count() > 0:
            page.locator("input[type='text'], input[name*='username'], input[id*='username']").first.fill(username)
            page.locator("input[type='password']").first.fill(password)
            page.locator("button[type='submit'], input[type='submit']").first.click()
            page.wait_for_load_state("networkidle")

        # 3. Navigate to Planning / Mon planning
        # Clicks the agenda link in the Aurion/Auriga navigation tree
        planning_btn = page.locator("text=/Planning|Emploi du temps|Mon planning/i").first
        if planning_btn.is_visible():
            planning_btn.click()
            page.wait_for_load_state("networkidle")

        page.wait_for_timeout(4000)
        browser.close()

    return events

def parse_event_details(raw_event):
    """
    Parses PrimeFaces schedule objects into structured calendar entries.
    Handles HTML tags commonly present in course event descriptions.
    """
    title = raw_event.get("title", "Cours")
    clean_title = re.sub(r"<br\s*/?>", " - ", title)
    clean_title = re.sub(r"<.*?>", "", clean_title).strip()

    # PrimeFaces timestamps are typically ISO or epoch milliseconds
    start_raw = raw_event.get("start")
    end_raw = raw_event.get("end")

    if isinstance(start_raw, int) or (isinstance(start_raw, str) and start_raw.isdigit()):
        start_dt = datetime.fromtimestamp(int(start_raw) / 1000, tz=PARIS_TZ)
        end_dt = datetime.fromtimestamp(int(end_raw) / 1000, tz=PARIS_TZ)
    else:
        start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00")).astimezone(PARIS_TZ)
        end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00")).astimezone(PARIS_TZ)

    # Attempt to extract room/hall from description if formatted as 'Salle: ...'
    location = ""
    loc_match = re.search(r"(?:Salle|Amphi|Room)\s*[:\-]?\s*([A-Za-z0-9\.\-]+)", clean_title, re.IGNORECASE)
    if loc_match:
        location = loc_match.group(0)

    event_id = str(raw_event.get("id", f"{clean_title}_{start_dt.isoformat()}"))

    return {
        "id": event_id,
        "summary": clean_title.split(" - ")[0] if " - " in clean_title else clean_title,
        "description": clean_title,
        "location": location,
        "start": {"dateTime": start_dt.isoformat()},
        "end": {"dateTime": end_dt.isoformat()},
    }

def sync_to_google(parsed_events):
    calendar_id = os.environ["CALENDAR_ID"]
    service = get_google_service()

    # Query existing events synced by this script in the active window (-7 days to +60 days)
    now = datetime.now(PARIS_TZ)
    time_min = (now - timedelta(days=7)).isoformat()
    time_max = (now + timedelta(days=60)).isoformat()

    existing_call = service.events().list(
        calendarId=calendar_id,
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        privateExtendedProperty="app=auriga_sync"
    ).execute()
    
    existing_events = {
        item.get("extendedProperties", {}).get("private", {}).get("auriga_id"): item
        for item in existing_call.get("items", [])
    }

    seen_ids = set()

    for item in parsed_events:
        auriga_id = item["id"]
        seen_ids.add(auriga_id)

        body = {
            "summary": item["summary"],
            "description": item["description"],
            "location": item["location"],
            "start": item["start"],
            "end": item["end"],
            "extendedProperties": {
                "private": {
                    "app": "auriga_sync",
                    "auriga_id": auriga_id
                }
            }
        }

        if auriga_id in existing_events:
            curr = existing_events[auriga_id]
            # Check for changes in timing, title, or room
            if (curr.get("summary") != body["summary"] or
                curr.get("start", {}).get("dateTime") != body["start"]["dateTime"] or
                curr.get("end", {}).get("dateTime") != body["end"]["dateTime"] or
                curr.get("location") != body["location"]):
                service.events().patch(calendarId=calendar_id, eventId=curr["id"], body=body).execute()
                print(f"Updated: {body['summary']}")
        else:
            service.events().insert(calendarId=calendar_id, body=body).execute()
            print(f"Added: {body['summary']}")

    # Handle canceled / removed events
    for auriga_id, g_event in existing_events.items():
        if auriga_id and auriga_id not in seen_ids:
            # Delete if the event was scheduled in the future
            start_iso = g_event.get("start", {}).get("dateTime")
            if start_iso and datetime.fromisoformat(start_iso) > now:
                service.events().delete(calendarId=calendar_id, eventId=g_event["id"]).execute()
                print(f"Removed canceled class: {g_event.get('summary')}")

if __name__ == "__main__":
    raw = fetch_auriga_schedule()
    print(f"Retrieved {len(raw)} events from Auriga.")
    if raw:
        formatted = [parse_event_details(e) for e in raw]
        sync_to_google(formatted)
        print("Calendar sync complete.")