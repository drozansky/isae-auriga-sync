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
            locale="en-US",
            timezone_id="Europe/Paris",
            viewport={"width": 1600, "height": 1000}
        )
        page = context.new_page()

        print("[INFO] Navigating to Auriga...")
        page.goto("https://auriga.isae-supaero.fr", wait_until="networkidle")

        # 1. SSO
        sso_btn = page.locator("text=/Connexion SSO|SSO|Authentification/i").first
        if sso_btn.is_visible():
            sso_btn.click()
            page.wait_for_load_state("networkidle")

        # 2. Login via Eliot
        if "eliot.isae.fr" in page.url or page.locator("input[type='password']").count() > 0:
            print("[INFO] Logging into Eliot IDP...")
            page.locator("input[type='text'], input[name*='username'], input[id*='username'], input[name='j_username']").first.fill(username)
            page.locator("input[type='password'], input[name='j_password']").first.fill(password)
            submit_btn = page.locator("button[type='submit'], input[type='submit'], button[name='_eventId_proceed']").first
            submit_btn.click()
            page.wait_for_load_state("networkidle")

        page.wait_for_timeout(2500)

        # 3. Switch Language to English
        try:
            lang_btn = page.locator("text=/Français|Francais/i").first
            if lang_btn.is_visible():
                lang_btn.click()
                page.wait_for_timeout(1000)
                opt = page.locator("text=/Anglais|English/i").first
                if opt.is_visible():
                    opt.click()
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(2000)
        except Exception as e:
            print(f"[WARN] Language toggle: {e}")

        # 4. Query the Auriga planning API directly using the authenticated session
        # Range: Start of semester to end of February
        start_date = "2026-09-01"
        end_date = "2027-02-28"
        api_url = (
            f"https://auriga.isae-supaero.fr/api/plannings/me?"
            f"days=1&days=2&days=3&days=4&days=5&days=6&days=7&"
            f"startDate={start_date}&endDate={end_date}"
        )

        print(f"[INFO] Fetching full semester schedule from API: {api_url}")
        response = page.evaluate(f"""
            async () => {{
                const res = await fetch('{api_url}', {{
                    headers: {{
                        'Accept': 'application/json',
                        'Accept-Language': 'en-US,en;q=0.9'
                    }}
                }});
                return await res.json();
            }}
        """)

        # Extract interventions list
        if isinstance(response, dict) and "interventions" in response:
            events = response["interventions"]
        elif isinstance(response, list):
            events = response
        else:
            print(f"[WARN] Unexpected API response keys: {list(response.keys()) if isinstance(response, dict) else type(response)}")

        print(f"[INFO] Received {len(events)} raw events from Auriga API.")
        browser.close()

    return events

def parse_api_event(item):
    # Extract title
    course_obj = item.get("course") or item.get("subject") or {}
    title = ""
    if isinstance(course_obj, dict):
        title = course_obj.get("caption", {}).get("en") or course_obj.get("caption", {}).get("fr") or course_obj.get("name", "")
    
    if not title:
        act_type = item.get("activityType", {}).get("caption", {})
        title = act_type.get("en") or act_type.get("fr") or item.get("name") or "Course"

    # Extract start and end datetimes
    start_str = item.get("startDate") or item.get("start")
    end_str = item.get("endDate") or item.get("end")

    if not start_str:
        return None

    # Parse ISO dates (e.g. '2026-09-22T09:15:00Z' or '2026-09-22T11:15:00+02:00')
    start_dt = datetime.fromisoformat(str(start_str).replace("Z", "+00:00")).astimezone(PARIS_TZ)
    if end_str:
        end_dt = datetime.fromisoformat(str(end_str).replace("Z", "+00:00")).astimezone(PARIS_TZ)
    else:
        duration_sec = item.get("actualDuration", 7200)
        end_dt = start_dt + timedelta(seconds=duration_sec)

    # Extract room/location
    rooms = []
    for r in item.get("rooms", []):
        if isinstance(r, dict):
            r_name = r.get("caption") or r.get("name") or r.get("code")
            if r_name:
                rooms.append(str(r_name))
    location = " / ".join(rooms) if rooms else item.get("room", "")

    # Extract teacher
    teachers = []
    for t in item.get("teachers", []):
        if isinstance(t, dict):
            t_name = f"{t.get('firstName', '')} {t.get('lastName', '')}".strip() or t.get("name", "")
            if t_name:
                teachers.append(t_name)
    teacher_str = ", ".join(teachers)

    # Build description
    desc_lines = []
    if item.get("description"):
        desc_lines.append(item["description"])
    if teacher_str:
        desc_lines.append(f"Instructor: {teacher_str}")
    if location:
        desc_lines.append(f"Room: {location}")

    # Unique ID per session
    raw_id = f"auriga_{item.get('id', start_dt.strftime('%Y%m%d%H%M'))}"
    clean_id = re.sub(r'[^a-zA-Z0-9]', '', raw_id)[:64]

    return {
        "id": clean_id,
        "summary": title,
        "description": "\n".join(desc_lines),
        "location": location,
        "start": {"dateTime": start_dt.isoformat()},
        "end": {"dateTime": end_dt.isoformat()}
    }

def sync_to_google(parsed_events):
    calendar_id = os.environ["CALENDAR_ID"]
    service = get_google_service()

    now = datetime.now(PARIS_TZ)
    time_min = (now - timedelta(days=14)).isoformat()
    time_max = (now + timedelta(days=200)).isoformat()

    print("[INFO] Fetching existing Google Calendar items...")
    existing_call = service.events().list(
        calendarId=calendar_id,
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        maxResults=2500,
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
                curr.get("location") != body["location"] or
                curr.get("description") != body["description"]):
                service.events().patch(calendarId=calendar_id, eventId=curr["id"], body=body).execute()
                print(f"[UPDATE] {body['summary']} ({item['start']['dateTime']})")
        else:
            service.events().insert(calendarId=calendar_id, body=body).execute()
            print(f"[ADD] {body['summary']} ({item['start']['dateTime']}) - {body['location']}")

    # Purge the previous incorrectly-dated events
    for auriga_id, g_event in existing_events.items():
        if auriga_id and auriga_id not in seen_ids:
            service.events().delete(calendarId=calendar_id, eventId=g_event["id"]).execute()
            print(f"[PURGE WRONG DATE] {g_event.get('summary')} ({g_event.get('start', {}).get('dateTime')})")

if __name__ == "__main__":
    raw_items = fetch_auriga_schedule()
    valid_events = []
    
    for item in raw_items:
        evt = parse_api_event(item)
        if evt:
            valid_events.append(evt)

    print(f"[INFO] Parsed {len(valid_events)} verified API sessions.")
    if valid_events:
        sync_to_google(valid_events)
        print("[SUCCESS] API sync and calendar cleanup complete.")