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
    
    raw_interventions = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="en-US",
            timezone_id="Europe/Paris",
            viewport={"width": 1600, "height": 1000}
        )
        page = context.new_page()

        def handle_response(response):
            url = response.url
            if "/api/plannings/me" in url:
                print(f"[API INTERCEPT] Caught response from: {url}")
                try:
                    data = response.json()
                    if isinstance(data, dict) and "interventions" in data:
                        items = data["interventions"]
                        raw_interventions.extend(items)
                        print(f"[API INTERCEPT] Added {len(items)} official interventions.")
                    elif isinstance(data, list):
                        raw_interventions.extend(data)
                        print(f"[API INTERCEPT] Added {len(data)} list items.")
                except Exception as e:
                    print(f"[API WARN] Failed to parse JSON: {e}")

        page.on("response", handle_response)

        print("[INFO] Navigating to Auriga...")
        page.goto("https://auriga.isae-supaero.fr", wait_until="networkidle")

        # 1. SSO Click
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
        except Exception:
            pass

        # 4. Navigate into Planning module
        print(f"[INFO] Navigating to {PLANNING_URL}...")
        page.goto(PLANNING_URL, wait_until="networkidle")
        page.wait_for_timeout(4000)

        # 5. Switch to Month View
        try:
            month_btn = page.locator("button:has-text('Month'), button:has-text('Mois'), [aria-label*='Month'], [aria-label*='Mois']").first
            if month_btn.is_visible():
                print("[INFO] Switching to Month view...")
                month_btn.click()
                page.wait_for_timeout(3000)
        except Exception:
            pass

        # 6. Step forward month by month
        for i in range(4):
            print(f"[INFO] Requesting month #{i+1}...")
            page.wait_for_timeout(2500)
            next_btn = page.locator("button:has-text('>'), [aria-label*='next'], [aria-label*='suivant'], .fc-next-button").first
            if next_btn.is_visible():
                next_btn.click()
                page.wait_for_timeout(3000)
            else:
                break

        page.wait_for_timeout(3000)
        browser.close()

    return raw_interventions

def parse_date_value(val):
    if val is None:
        return None
    # Numeric timestamp (milliseconds or seconds)
    if isinstance(val, (int, float)) or (isinstance(val, str) and val.isdigit()):
        v = int(val)
        if v > 1e11:  # milliseconds
            return datetime.fromtimestamp(v / 1000, tz=PARIS_TZ)
        return datetime.fromtimestamp(v, tz=PARIS_TZ)
    # ISO string format
    if isinstance(val, str):
        cleaned = val.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(cleaned).astimezone(PARIS_TZ)
        except Exception:
            pass
    return None

def parse_api_event(item):
    # 1. Inspect title from all common Auriga entity keys
    title = ""
    for k in ["course", "subject", "matter", "discipline", "module", "intervention"]:
        sub = item.get(k)
        if isinstance(sub, dict):
            cap = sub.get("caption") or sub.get("name") or sub.get("label")
            if isinstance(cap, dict):
                title = cap.get("en") or cap.get("fr") or ""
            elif isinstance(cap, str):
                title = cap
        elif isinstance(sub, str):
            title = sub
        if title:
            break

    if not title:
        act = item.get("activityType") or item.get("type") or {}
        if isinstance(act, dict):
            cap = act.get("caption") or act.get("name")
            if isinstance(cap, dict):
                title = cap.get("en") or cap.get("fr") or ""
            elif isinstance(cap, str):
                title = cap
        elif isinstance(act, str):
            title = act

    if not title:
        title = item.get("name") or item.get("title") or item.get("code") or "Course"

    # 2. Extract start and end datetimes across possible field names
    start_dt = None
    for k in ["start", "startDate", "startDateTime", "beginDate", "begin", "dateDebut", "debut"]:
        if k in item and item[k] is not None:
            start_dt = parse_date_value(item[k])
            if start_dt:
                break

    end_dt = None
    for k in ["end", "endDate", "endDateTime", "finishDate", "finish", "dateFin", "fin"]:
        if k in item and item[k] is not None:
            end_dt = parse_date_value(item[k])
            if end_dt:
                break

    if not start_dt:
        return None

    if not end_dt:
        duration_sec = item.get("actualDuration") or item.get("duration") or 7200
        end_dt = start_dt + timedelta(seconds=duration_sec)

    # 3. Extract room/location
    rooms = []
    r_list = item.get("rooms") or item.get("salles") or []
    if isinstance(r_list, list):
        for r in r_list:
            if isinstance(r, dict):
                r_name = r.get("caption") or r.get("name") or r.get("code")
                if r_name:
                    rooms.append(str(r_name))
            elif isinstance(r, str):
                rooms.append(r)
    location = " / ".join(rooms) if rooms else str(item.get("room") or item.get("salle") or "")

    # 4. Extract teacher
    teachers = []
    t_list = item.get("teachers") or item.get("intervenants") or []
    if isinstance(t_list, list):
        for t in t_list:
            if isinstance(t, dict):
                name = f"{t.get('firstName', '')} {t.get('lastName', '')}".strip() or t.get("name", "")
                if name:
                    teachers.append(name)
            elif isinstance(t, str):
                teachers.append(t)
    teacher_str = ", ".join(teachers)

    # 5. Build description
    desc_lines = []
    if item.get("description"):
        desc_lines.append(str(item["description"]))
    if teacher_str:
        desc_lines.append(f"Instructor: {teacher_str}")
    if location:
        desc_lines.append(f"Room: {location}")

    item_id = item.get("id") or f"{title}_{start_dt.strftime('%Y%m%d%H%M')}"
    clean_id = re.sub(r'[^a-zA-Z0-9]', '', f"auriga_{item_id}")[:64]

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

    # Purge any misplaced/weekend events from earlier DOM scraper runs
    for auriga_id, g_event in existing_events.items():
        if auriga_id and auriga_id not in seen_ids:
            service.events().delete(calendarId=calendar_id, eventId=g_event["id"]).execute()
            print(f"[PURGE WRONG DATE] {g_event.get('summary')} ({g_event.get('start', {}).get('dateTime')})")

if __name__ == "__main__":
    raw_items = fetch_auriga_schedule()
    print(f"[INFO] Intercepted {len(raw_items)} total interventions.")

    # Deduplicate interventions by ID
    seen = set()
    unique_items = []
    for it in raw_items:
        it_id = it.get("id")
        if it_id and it_id not in seen:
            seen.add(it_id)
            unique_items.append(it)
        elif not it_id:
            unique_items.append(it)

    if unique_items:
        # Print first item keys to verify schema
        sample = unique_items[0]
        print(f"[SCHEMA DEBUG] Sample intervention keys: {list(sample.keys())}")

    valid_events = []
    for item in unique_items:
        evt = parse_api_event(item)
        if evt:
            valid_events.append(evt)

    print(f"[INFO] Parsed {len(valid_events)} verified API sessions.")
    if valid_events:
        sync_to_google(valid_events)
        print("[SUCCESS] API sync and calendar cleanup complete.")