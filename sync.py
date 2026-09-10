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

# Google Calendar Event Palette (excludes Tomato Red "11" reserved for exams)
# 1: Lavender, 2: Sage, 3: Grape, 4: Flamingo, 5: Banana, 6: Tangerine, 7: Peacock, 9: Blueberry, 10: Basil
COURSE_PALETTE = ["1", "2", "3", "4", "5", "6", "7", "9", "10"]


def get_google_service():
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise ValueError("Missing GOOGLE_SERVICE_ACCOUNT_JSON secret.")
    creds_dict = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=creds)


def get_event_color(summary, activity_name, is_exam=False):
    # Always highlight exams, tests, or graded evaluations in Tomato Red ("11")
    exam_keywords = ["exam", "graded", "contrôle", "partiel", "devoir", "test"]
    text_to_check = f"{summary} {activity_name}".lower()
    if is_exam or any(k in text_to_check for k in exam_keywords):
        return "11"

    # Assign a consistent, deterministic color to each course based on its name
    color_index = abs(hash(summary)) % len(COURSE_PALETTE)
    return COURSE_PALETTE[color_index]


def parse_date_value(val):
    if val is None:
        return None
    if isinstance(val, (int, float)) or (isinstance(val, str) and val.isdigit()):
        v = int(val)
        return datetime.fromtimestamp((v / 1000) if v > 1e11 else v, tz=PARIS_TZ)
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00")).astimezone(PARIS_TZ)
        except Exception:
            pass
    return None


def extract_caption(obj):
    if isinstance(obj, dict):
        cap = obj.get("caption") or obj.get("name") or obj.get("label") or obj.get("title")
        if isinstance(cap, dict):
            return cap.get("en") or cap.get("fr") or ""
        elif isinstance(cap, str):
            return cap
        elif "code" in obj:
            return str(obj["code"])
    elif isinstance(obj, str):
        return obj
    return ""


def extract_person_name(obj):
    if not isinstance(obj, dict):
        return str(obj) if obj else ""

    first = obj.get("firstName") or obj.get("prenom") or ""
    last = obj.get("lastName") or obj.get("nom") or ""
    if first or last:
        return f"{first} {last}".strip()

    for key in ["person", "individual", "instructor", "intervenant", "user"]:
        if key in obj and isinstance(obj[key], dict):
            nested_name = extract_person_name(obj[key])
            if nested_name:
                return nested_name

    cap = extract_caption(obj)
    if cap and not cap.isdigit() and len(cap) > 2:
        return cap
    return ""


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

        # Intercept backend timetable API payloads directly from Angular requests
        def handle_response(response):
            url = response.url
            if "/api/plannings/me" in url:
                try:
                    data = response.json()
                    if isinstance(data, dict) and "interventions" in data:
                        items = data["interventions"]
                        raw_interventions.extend(items)
                    elif isinstance(data, list):
                        raw_interventions.extend(data)
                except Exception:
                    pass

        page.on("response", handle_response)

        print("[INFO] Navigating to Auriga...")
        page.goto("https://auriga.isae-supaero.fr", wait_until="networkidle")

        # 1. SSO Button
        sso_btn = page.locator("text=/Connexion SSO|SSO|Authentification/i").first
        if sso_btn.is_visible():
            sso_btn.click()
            page.wait_for_load_state("networkidle")

        # 2. Authenticate via Eliot Shibboleth IDP
        if "eliot.isae.fr" in page.url or page.locator("input[type='password']").count() > 0:
            print("[INFO] Logging into Eliot IDP...")
            page.locator(
                "input[type='text'], input[name*='username'], input[id*='username'], input[name='j_username']"
            ).first.fill(username)
            page.locator(
                "input[type='password'], input[name='j_password']"
            ).first.fill(password)
            submit_btn = page.locator(
                "button[type='submit'], input[type='submit'], button[name='_eventId_proceed']"
            ).first
            submit_btn.click()
            page.wait_for_load_state("networkidle")

        page.wait_for_timeout(2000)

        # 3. Switch Language to English
        try:
            lang_btn = page.locator("text=/Français|Francais/i").first
            if lang_btn.is_visible():
                lang_btn.click()
                page.wait_for_timeout(800)
                opt = page.locator("text=/Anglais|English/i").first
                if opt.is_visible():
                    opt.click()
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(1500)
        except Exception:
            pass

        # 4. Open Planning view
        print(f"[INFO] Navigating to {PLANNING_URL}...")
        page.goto(PLANNING_URL, wait_until="networkidle")
        page.wait_for_timeout(3000)

        # 5. Switch to Month View to request month-wide payloads
        try:
            month_btn = page.locator(
                "button:has-text('Month'), button:has-text('Mois'), [aria-label*='Month'], [aria-label*='Mois']"
            ).first
            if month_btn.is_visible():
                print("[INFO] Switching to Month view...")
                month_btn.click()
                page.wait_for_timeout(2000)
        except Exception:
            pass

        # 6. Step forward 9 months to cover September 2026 through May 2027
        months_to_request = 9
        for i in range(months_to_request):
            print(f"[INFO] Fetching month #{i+1} of {months_to_request}...")
            page.wait_for_timeout(600)
            next_btn = page.locator(
                "button:has-text('>'), [aria-label*='next'], [aria-label*='suivant'], .fc-next-button"
            ).first
            if next_btn.is_visible():
                next_btn.click()
                page.wait_for_timeout(800)
            else:
                break

        page.wait_for_timeout(1500)
        browser.close()

    return raw_interventions


def parse_api_event(item):
    # 1. Course Name (Clean Title from Pedagogical Units)
    course_name = ""
    pus = item.get("interventionPedagogicalUnits") or []
    for pu in pus:
        candidate = ""
        if isinstance(pu, dict):
            unit = pu.get("pedagogicalUnit") or pu
            candidate = extract_caption(unit)
        if candidate and candidate not in course_name:
            course_name = candidate if not course_name else f"{course_name} · {candidate}"

    # 2. Activity / Format (Lecture, Tutorials, Exam, etc.)
    activity_name = extract_caption(item.get("activityType"))

    if course_name:
        summary = course_name
    elif activity_name:
        summary = activity_name
    else:
        summary = item.get("name") or "Course"

    # 3. Datetimes
    start_dt = parse_date_value(item.get("startDateTime") or item.get("startDate"))
    end_dt = parse_date_value(item.get("endDateTime") or item.get("endDate"))

    if not start_dt:
        return None
    if not end_dt:
        dur = item.get("actualDuration") or 7200
        end_dt = start_dt + timedelta(seconds=dur)

    # 4. Rooms / Locations
    rooms = []
    res_list = item.get("interventionResources") or []
    for res in res_list:
        if isinstance(res, dict):
            r = res.get("resource") or res
            r_name = extract_caption(r)
            if r_name and r_name not in rooms:
                rooms.append(r_name)
    location = " / ".join(rooms)

    # 5. Instructors
    instructors = []
    for inst in item.get("interventionInstructors") or []:
        name = extract_person_name(inst)
        if name and name not in instructors:
            instructors.append(name)

    for part in item.get("participations") or []:
        if isinstance(part, dict):
            role = str(part.get("role", "")).upper()
            if any(k in role for k in ["TEACH", "ENS", "PROF", "INTERV"]) or not role:
                name = extract_person_name(part)
                if name and name not in instructors:
                    instructors.append(name)

    instructor_str = ", ".join(instructors)

    # 6. Description / Notes
    desc_lines = []
    if activity_name:
        desc_lines.append(f"Format: {activity_name}")
    if instructor_str:
        desc_lines.append(f"Instructor: {instructor_str}")
    if location:
        desc_lines.append(f"Room: {location}")
    if item.get("description") and item["description"] != activity_name:
        desc_lines.append(f"Details: {item['description']}")

    # 7. Unique ID & Color
    item_id = str(item.get("id") or f"{summary}_{start_dt.strftime('%Y%m%d%H%M')}")
    clean_id = re.sub(r"[^a-zA-Z0-9]", "", f"auriga_{item_id}")[:64]
    is_exam_flag = bool(item.get("isExam"))
    event_color = get_event_color(summary, activity_name, is_exam=is_exam_flag)

    return {
        "id": clean_id,
        "summary": summary,
        "description": "\n".join(desc_lines),
        "location": location,
        "colorId": event_color,
        "start": {"dateTime": start_dt.isoformat()},
        "end": {"dateTime": end_dt.isoformat()}
    }


def sync_to_google(parsed_events):
    calendar_id = os.environ["CALENDAR_ID"]
    service = get_google_service()

    now = datetime.now(PARIS_TZ)
    time_min = (now - timedelta(days=14)).isoformat()
    time_max = (now + timedelta(days=300)).isoformat()

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
            "colorId": item["colorId"],
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
                curr.get("description") != body["description"] or
                curr.get("colorId") != body["colorId"]):
                service.events().patch(calendarId=calendar_id, eventId=curr["id"], body=body).execute()
                print(f"[UPDATE] {body['summary']} ({item['start']['dateTime']})")
        else:
            service.events().insert(calendarId=calendar_id, body=body).execute()
            print(f"[ADD] {body['summary']} ({item['start']['dateTime']}) - {body['location']}")

    # Automatically delete cancelled or stale calendar entries
    for auriga_id, g_event in existing_events.items():
        if auriga_id and auriga_id not in seen_ids:
            service.events().delete(calendarId=calendar_id, eventId=g_event["id"]).execute()
            print(f"[PURGE OBSOLETE] {g_event.get('summary')} ({g_event.get('start', {}).get('dateTime')})")


if __name__ == "__main__":
    raw_items = fetch_auriga_schedule()
    print(f"[INFO] Intercepted {len(raw_items)} total interventions.")

    # Deduplicate raw items by Auriga ID
    seen = set()
    unique_items = []
    for it in raw_items:
        it_id = it.get("id")
        if it_id and it_id not in seen:
            seen.add(it_id)
            unique_items.append(it)
        elif not it_id:
            unique_items.append(it)

    valid_events = []
    for item in unique_items:
        evt = parse_api_event(item)
        if evt:
            valid_events.append(evt)

    print(f"[INFO] Parsed {len(valid_events)} verified academic sessions.")
    if valid_events:
        sync_to_google(valid_events)
        print("[SUCCESS] Calendar sync complete.")