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
        context = browser.new_context(
            locale="fr-FR", 
            timezone_id="Europe/Paris",
            viewport={"width": 1440, "height": 900}
        )
        page = context.new_page()

        # Listen for PrimeFaces calendar schedule payloads
        def handle_response(response):
            if "Planning" in response.url or "schedule" in response.url:
                try:
                    text = response.text()
                    if '"events"' in text or 'events:' in text:
                        match = re.search(r'\{"events"\s*:\s*(\[.*?\])\}', text)
                        if match:
                            parsed = json.loads(match.group(1))
                            events.extend(parsed)
                            print(f"[INFO] Intercepted {len(parsed)} events in response.")
                except Exception:
                    pass

        page.on("response", handle_response)

        print("[INFO] Navigating to Auriga landing page...")
        page.goto("https://auriga.isae-supaero.fr", wait_until="networkidle")

        # 1. Click SSO button if present on Auriga landing screen
        sso_btn = page.locator("text=/Connexion SSO|SSO|Authentification/i").first
        if sso_btn.is_visible():
            print("[INFO] Clicking Auriga SSO button...")
            sso_btn.click()
            page.wait_for_load_state("networkidle")

        # 2. Handle Eliot IDP login screen
        print(f"[INFO] Current URL: {page.url}")
        if "eliot.isae.fr" in page.url or page.locator("input[type='password']").count() > 0:
            print("[INFO] Eliot IDP detected. Submitting login credentials...")
            page.locator("input[type='text'], input[name*='username'], input[id*='username'], input[name='j_username']").first.fill(username)
            page.locator("input[type='password'], input[name='j_password']").first.fill(password)
            
            # Click submit button
            submit_btn = page.locator("button[type='submit'], input[type='submit'], button[name='_eventId_proceed']").first
            submit_btn.click()
            page.wait_for_load_state("networkidle")
            print(f"[INFO] Post-login URL: {page.url}")

        # 3. Direct navigation to the Planning page
        print("[INFO] Navigating to Planning...")
        page.goto("https://auriga.isae-supaero.fr/faces/Planning.xhtml", wait_until="networkidle")

        # Wait for the calendar widget to mount and load data
        try:
            page.wait_for_selector(".fc-view, .ui-schedule, div[id*='schedule']", timeout=10000)
            print("[INFO] Calendar UI mounted.")
        except Exception:
            print("[WARN] Timed out waiting for schedule selector.")
            page.screenshot(path="debug_screen.png")

        # Give PrimeFaces AJAX a few seconds to return all schedule events
        page.wait_for_timeout(4000)
        browser.close()

    return events

def parse_event_details(raw_event):
    title = raw_event.get("title", "Cours")
    clean_title = re.sub(r"<br\s*/?>", " - ", title)
    clean_title = re.sub(r"<.*?>", "", clean_title).strip()

    start_raw = raw_event.get("start")
    end_raw = raw_event.get("end")

    if isinstance(start_raw, int) or (isinstance(start_raw, str) and start_raw.isdigit()):
        start_dt = datetime.fromtimestamp(int(start_raw) / 1000, tz=PARIS_TZ)
        end_dt = datetime.fromtimestamp(int(end_raw) / 1000, tz=PARIS_TZ)
    else:
        start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00")).astimezone(PARIS_TZ)
        end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00")).astimezone(PARIS_TZ)

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

    now = datetime.now(PARIS_TZ)
    time_min = (now - timedelta(days=7)).isoformat()
    time_max = (now + timedelta(days=60)).isoformat()

    print("[INFO] Fetching current Google Calendar events...")
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
            if (curr.get("summary") != body["summary"] or
                curr.get("start", {}).get("dateTime") != body["start"]["dateTime"] or
                curr.get("end", {}).get("dateTime") != body["end"]["dateTime"] or
                curr.get("location") != body["location"]):
                service.events().patch(calendarId=calendar_id, eventId=curr["id"], body=body).execute()
                print(f"[UPDATE] {body['summary']} ({item['start']['dateTime']})")
        else:
            service.events().insert(calendarId=calendar_id, body=body).execute()
            print(f"[ADD] {body['summary']} ({item['start']['dateTime']})")

    for auriga_id, g_event in existing_events.items():
        if auriga_id and auriga_id not in seen_ids:
            start_iso = g_event.get("start", {}).get("dateTime")
            if start_iso and datetime.fromisoformat(start_iso) > now:
                service.events().delete(calendarId=calendar_id, eventId=g_event["id"]).execute()
                print(f"[REMOVE] {g_event.get('summary')}")

if __name__ == "__main__":
    raw = fetch_auriga_schedule()
    print(f"[INFO] Retrieved {len(raw)} events from Auriga.")
    if raw:
        formatted = [parse_event_details(e) for e in raw]
        sync_to_google(formatted)
        print("[SUCCESS] Calendar sync complete.")