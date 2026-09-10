import os
import json
import re
from datetime import datetime, timedelta
import pytz
from playwright.sync_api import sync_playwright
from google.oauth2 import service_account
from googleapiclient.discovery import build

PARIS_TZ = pytz.timezone("Europe/Paris")
PLANNING_URL = "https://auriga.isae-supaero.fr/#/mainContent/menuEntry/227/planning"

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
            viewport={"width": 1600, "height": 1000}
        )
        page = context.new_page()

        # Log and inspect network traffic
        def handle_response(response):
            url = response.url
            # Filter out images, fonts, css
            if any(ext in url for ext in [".css", ".png", ".jpg", ".woff", ".svg", ".ico"]):
                return
            
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct or "xml" in ct or "text" in ct:
                    text = response.text()
                    # Catch events JSON
                    if '"events"' in text or '"start"' in text or 'events:' in text:
                        print(f"[NET DEBUG] Candidate event response from: {url[:100]}")
                        match = re.search(r'\{"events"\s*:\s*(\[.*?\])\}', text)
                        if match:
                            parsed = json.loads(match.group(1))
                            events.extend(parsed)
                            print(f"[INFO] Intercepted {len(parsed)} events via regex.")
                        else:
                            try:
                                data = response.json()
                                if isinstance(data, list) and len(data) > 0:
                                    events.extend(data)
                                    print(f"[INFO] Intercepted {len(data)} list items.")
                                elif isinstance(data, dict):
                                    for key in ["events", "data", "rows"]:
                                        if key in data and isinstance(data[key], list):
                                            events.extend(data[key])
                                            print(f"[INFO] Intercepted {len(data[key])} items from dict key '{key}'.")
                            except Exception:
                                pass
            except Exception:
                pass

        page.on("response", handle_response)

        print("[INFO] Opening Auriga...")
        page.goto("https://auriga.isae-supaero.fr", wait_until="networkidle")

        # 1. Click SSO
        sso_btn = page.locator("text=/Connexion SSO|SSO|Authentification/i").first
        if sso_btn.is_visible():
            sso_btn.click()
            page.wait_for_load_state("networkidle")

        # 2. Fill Eliot login
        if "eliot.isae.fr" in page.url or page.locator("input[type='password']").count() > 0:
            print("[INFO] Submitting Eliot credentials...")
            page.locator("input[type='text'], input[name*='username'], input[id*='username'], input[name='j_username']").first.fill(username)
            page.locator("input[type='password'], input[name='j_password']").first.fill(password)
            submit_btn = page.locator("button[type='submit'], input[type='submit'], button[name='_eventId_proceed']").first
            submit_btn.click()
            page.wait_for_load_state("networkidle")

        page.wait_for_timeout(2000)

        # 3. Direct navigation to menuEntry 227
        print(f"[INFO] Navigating to {PLANNING_URL}...")
        page.goto(PLANNING_URL, wait_until="networkidle")
        page.wait_for_timeout(6000)

        # 4. If network intercept caught 0 events, scrape the visible DOM calendar elements directly
        if len(events) == 0:
            print("[INFO] Interceptor caught 0 events. Checking DOM for rendered calendar cards...")
            
            # Common Auriga / FullCalendar event selectors
            card_selectors = [
                ".fc-event", 
                ".ui-schedule-event", 
                "div[class*='event-item']", 
                "div[class*='planning-event']",
                ".fc-time-grid-event",
                ".fc-daygrid-event"
            ]
            
            found_cards = []
            for sel in card_selectors:
                cards = page.locator(sel).all()
                if len(cards) > 0:
                    print(f"[INFO] Found {len(cards)} event elements matching selector '{sel}'.")
                    found_cards = cards
                    break

            for card in found_cards:
                try:
                    text_content = card.inner_text().strip()
                    if text_content:
                        events.append({
                            "id": f"dom_{hash(text_content)}",
                            "title": text_content,
                            "raw_dom": True
                        })
                except Exception:
                    pass

        page.screenshot(path="debug_screen.png")
        browser.close()

    # Deduplicate
    unique = []
    seen = set()
    for e in events:
        s = json.dumps(e, sort_keys=True)
        if s not in seen:
            seen.add(s)
            unique.append(e)

    return unique

def parse_event_details(raw_event):
    # Handle standard PrimeFaces event object
    title = raw_event.get("title", "Cours")
    clean_title = re.sub(r"<br\s*/?>", " - ", str(title))
    clean_title = re.sub(r"<.*?>", "", clean_title).strip()

    start_raw = raw_event.get("start")
    end_raw = raw_event.get("end")

    if not start_raw:
        # If event came from fallback DOM parser without timestamp
        now = datetime.now(PARIS_TZ)
        start_dt = now
        end_dt = now + timedelta(hours=1)
    elif isinstance(start_raw, int) or (isinstance(start_raw, str) and start_raw.isdigit()):
        start_dt = datetime.fromtimestamp(int(start_raw) / 1000, tz=PARIS_TZ)
        end_dt = datetime.fromtimestamp(int(end_raw) / 1000, tz=PARIS_TZ) if end_raw else start_dt + timedelta(hours=2)
    else:
        start_dt = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00")).astimezone(PARIS_TZ)
        end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00")).astimezone(PARIS_TZ) if end_raw else start_dt + timedelta(hours=2)

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

    print("[INFO] Fetching existing calendar items from Google...")
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
                print(f"[UPDATE] {body['summary']}")
        else:
            service.events().insert(calendarId=calendar_id, body=body).execute()
            print(f"[ADD] {body['summary']}")

if __name__ == "__main__":
    raw = fetch_auriga_schedule()
    print(f"[INFO] Retrieved {len(raw)} total events.")
    if raw:
        formatted = [parse_event_details(e) for e in raw]
        sync_to_google(formatted)
        print("[SUCCESS] Calendar sync complete.")