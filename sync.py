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
    
    extracted_events = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Set browser locale and Accept-Language to English
        context = browser.new_context(
            locale="en-US",
            timezone_id="Europe/Paris",
            viewport={"width": 1600, "height": 1000},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"}
        )
        page = context.new_page()

        # Listen for any background JSON payloads containing course data
        def handle_response(response):
            if any(ext in response.url for ext in [".js", ".css", ".png", ".svg", ".woff"]):
                return
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct:
                    data = response.json()
                    # Check for lists of event objects
                    target_list = None
                    if isinstance(data, list) and len(data) > 0:
                        target_list = data
                    elif isinstance(data, dict):
                        for k in ["events", "data", "planning", "items"]:
                            if k in data and isinstance(data[k], list):
                                target_list = data[k]
                                break
                    
                    if target_list:
                        for item in target_list:
                            if isinstance(item, dict) and ("start" in item or "title" in item or "debut" in item):
                                extracted_events.append(item)
                                print(f"[NET] Extracted event from API: {item.get('title') or item.get('name')}")
            except Exception:
                pass

        page.on("response", handle_response)

        print("[INFO] Navigating to Auriga...")
        page.goto("https://auriga.isae-supaero.fr", wait_until="networkidle")

        # 1. Handle SSO
        sso_btn = page.locator("text=/Connexion SSO|SSO|Authentification/i").first
        if sso_btn.is_visible():
            sso_btn.click()
            page.wait_for_load_state("networkidle")

        # 2. Login via Eliot
        if "eliot.isae.fr" in page.url or page.locator("input[type='password']").count() > 0:
            print("[INFO] Authenticating via Eliot...")
            page.locator("input[type='text'], input[name*='username'], input[id*='username'], input[name='j_username']").first.fill(username)
            page.locator("input[type='password'], input[name='j_password']").first.fill(password)
            submit_btn = page.locator("button[type='submit'], input[type='submit'], button[name='_eventId_proceed']").first
            submit_btn.click()
            page.wait_for_load_state("networkidle")

        page.wait_for_timeout(3000)

        # 3. Switch Language to English if toggle exists
        try:
            lang_btn = page.locator("button:has-text('EN'), a:has-text('EN'), [aria-label*='English'], [title*='English']").first
            if lang_btn.is_visible():
                print("[INFO] Switching interface language to English...")
                lang_btn.click()
                page.wait_for_timeout(1500)
        except Exception:
            pass

        # 4. Open Planning view
        print("[INFO] Opening Planning view...")
        page.goto(PLANNING_URL, wait_until="networkidle")
        page.wait_for_timeout(6000)

        # 5. Extract colored event elements directly from the DOM
        # Modern schedulers wrap colored cards in identifiable classes
        print("[INFO] Scanning DOM for rendered colored boxes...")
        cards = page.locator(
            "[class*='event'], "
            "[class*='fc-time-grid-event'], "
            "[class*='planning-item'], "
            "[class*='appointment'], "
            "[style*='background-color']"
        ).all()

        dom_items = []
        for card in cards:
            try:
                # Filter out tiny elements or background containers
                box = card.bounding_box()
                if not box or box["width"] < 40 or box["height"] < 20:
                    continue
                
                text = card.inner_text().strip()
                if text and len(text) > 3 and not any(skip in text for skip in ["Mon planning", "Planning", "Aujourd'hui", "Today", "Semaine", "Week"]):
                    dom_items.append({
                        "raw_text": text,
                        "title": text.split("\n")[0],
                        "details": text.replace("\n", " - ")
                    })
            except Exception:
                pass

        print(f"[DOM] Located {len(dom_items)} visible schedule cards.")

        # If API interception didn't yield structured JSON, use DOM card text
        if len(extracted_events) == 0 and len(dom_items) > 0:
            print("[INFO] Using DOM card content for calendar sync.")
            for idx, item in enumerate(dom_items):
                extracted_events.append({
                    "id": f"card_{idx}_{hash(item['details'])}",
                    "title": item["title"],
                    "description": item["details"],
                    "dom_card": True
                })

        page.screenshot(path="debug_screen.png")
        browser.close()

    return extracted_events

def parse_event_details(raw_event):
    title = raw_event.get("title", "Course")
    desc = raw_event.get("description", raw_event.get("details", title))
    clean_title = re.sub(r"<.*?>", "", str(title)).strip()

    start_raw = raw_event.get("start")
    end_raw = raw_event.get("end")

    now = datetime.now(PARIS_TZ)

    # Parse timestamps if present
    if start_raw:
        if isinstance(start_raw, int) or (isinstance(start_raw, str) and start_raw.isdigit()):
            start_dt = datetime.fromtimestamp(int(start_raw) / 1000, tz=PARIS_TZ)
            end_dt = datetime.fromtimestamp(int(end_raw) / 1000, tz=PARIS_TZ) if end_raw else start_dt + timedelta(hours=2)
        else:
            start_dt = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00")).astimezone(PARIS_TZ)
            end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00")).astimezone(PARIS_TZ) if end_raw else start_dt + timedelta(hours=2)
    else:
        # Fallback for DOM cards without timestamps
        start_dt = now
        end_dt = now + timedelta(hours=2)

    # Extract room number (Salle / Room / Amphi)
    location = ""
    loc_match = re.search(r"(?:Room|Salle|Amphi)\s*[:\-]?\s*([A-Za-z0-9\.\-]+)", desc, re.IGNORECASE)
    if loc_match:
        location = loc_match.group(0)

    event_id = str(raw_event.get("id", f"{clean_title}_{start_dt.isoformat()}"))

    return {
        "id": event_id,
        "summary": clean_title,
        "description": desc,
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
                print(f"[UPDATE] {body['summary']}")
        else:
            service.events().insert(calendarId=calendar_id, body=body).execute()
            print(f"[ADD] {body['summary']}")

if __name__ == "__main__":
    raw = fetch_auriga_schedule()
    print(f"[INFO] Retrieved {len(raw)} events.")
    if raw:
        formatted = [parse_event_details(e) for e in raw]
        sync_to_google(formatted)
        print("[SUCCESS] Calendar sync complete.")