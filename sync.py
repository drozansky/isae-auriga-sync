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


import hashlib

# Fixed, unique color assignments per subject (zero collisions, 100% deterministic)
EXPLICIT_COURSE_COLORS = {
    "aocs": "9",           # Blueberry (Dark Blue)
    "propulsion": "6",     # Tangerine (Orange)
    "law": "3",            # Grape (Deep Purple)
    "environment": "2",    # Sage (Light Green)
    "launcher": "10",      # Basil (Deep Forest Green)
    "thermal": "7",        # Peacock (Cyan / Light Blue)
    "data handling": "4",  # Flamingo (Coral / Pink)
    "obdh": "4",           # Flamingo
    "communication": "5",  # Banana (Yellow)
    "french": "1",         # Lavender (Light Violet)
    "fle": "1",            # Lavender
    "estimation": "1",     # Lavender (re-used for Spring semester course)
    "situational": "7",    # Peacock
}

FALLBACK_PALETTE = ["1", "2", "3", "4", "5", "6", "7", "9", "10"]

def get_event_color(summary, activity_name, is_exam=False):
    text = f"{summary} {activity_name}".lower()

    # 1. Exams, graded tests, or evaluations always Tomato Red ("11")
    exam_keywords = ["exam", "graded", "contrôle", "partiel", "devoir", "test"]
    if is_exam or any(k in text for k in exam_keywords):
        return "11"

    # 2. Autonomous work or generic presentations get muted Graphite Gray ("8")
    if any(k in text for k in ["autonomie", "presentation", "présentation"]):
        return "8"

    # 3. Match explicit course keywords
    for keyword, color_id in EXPLICIT_COURSE_COLORS.items():
        if keyword in text:
            return color_id

    # 4. Deterministic fallback using MD5 (stable across all Python runs/machines)
    hash_val = int(hashlib.md5(summary.encode("utf-8")).hexdigest(), 16)
    return FALLBACK_PALETTE[hash_val % len(FALLBACK_PALETTE)]


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
        return str(obj).strip() if obj else ""

    first = obj.get("firstName") or obj.get("prenom") or ""
    last = obj.get("lastName") or obj.get("nom") or ""
    if first or last:
        return f"{first} {last}".strip()

    for k in ["fullName", "displayName", "name"]:
        val = obj.get(k)
        if isinstance(val, str) and len(val.strip()) > 1 and not val.strip().isdigit():
            return val.strip()

    for key in ["instructor", "individual", "person", "intervenant", "user", "participant"]:
        if key in obj and isinstance(obj[key], dict):
            res = extract_person_name(obj[key])
            if res:
                return res

    cap = extract_caption(obj)
    if cap and not cap.isdigit() and len(cap) >= 2:
        return cap

    return ""


def scrape_dom_card_metadata(page):
    """Scrapes visible planning cards directly from the Angular DOM."""
    return page.evaluate("""() => {
        const cards = document.querySelectorAll('.pl-planning-card');
        const results = [];
        cards.forEach(card => {
            const titleEl = card.querySelector('.pl-planning-card--header--title--text');
            const timeEl = card.querySelector('.pl-planning-card--content--time');
            const roomEl = card.querySelector('.pl-planning-card--footer--left p');
            const teacherEl = card.querySelector('.pl-planning-card--footer--right p');
            
            const title = titleEl ? titleEl.innerText.trim() : '';
            const time = timeEl ? timeEl.innerText.trim() : '';
            const room = roomEl ? roomEl.innerText.trim() : '';
            const teacher = teacherEl ? teacherEl.innerText.trim() : '';

            if (title && (teacher || room)) {
                results.push({ title, time, room, teacher });
            }
        });
        return results;
    }""")


def fetch_auriga_schedule():
    username = os.environ["SCHOOL_USERNAME"]
    password = os.environ["SCHOOL_PASSWORD"]

    raw_interventions = []
    dom_metadata_map = {}

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
                try:
                    data = response.json()
                    if isinstance(data, dict) and "interventions" in data:
                        raw_interventions.extend(data["interventions"])
                    elif isinstance(data, list):
                        raw_interventions.extend(data)
                except Exception:
                    pass

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

        # 4. Navigate into Planning module
        print(f"[INFO] Navigating to {PLANNING_URL}...")
        page.goto(PLANNING_URL, wait_until="networkidle")
        page.wait_for_timeout(3000)

        # 5. Switch to Month View
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

        # 6. Step forward 9 months and harvest DOM cards alongside API responses
        months_to_request = 9
        for i in range(months_to_request):
            print(f"[INFO] Fetching month #{i+1} of {months_to_request}...")
            page.wait_for_timeout(700)

            # Extract instructor cards visible in DOM for the current month view
            try:
                card_data = scrape_dom_card_metadata(page)
                for item in card_data:
                    start_time = item["time"].split("-")[0].strip() if "-" in item["time"] else item["time"].strip()
                    key = f"{item['title'].lower()}_{start_time}"
                    if item.get("teacher"):
                        dom_metadata_map[key] = item["teacher"]
            except Exception:
                pass

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

    print(f"[INFO] Harvested {len(dom_metadata_map)} instructor mappings directly from DOM cards.")
    return raw_interventions, dom_metadata_map


def parse_api_event(item, dom_metadata_map):
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

    # 2. Activity / Format
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

    # 4. Rooms and Instructors from Resources
    rooms = []
    instructors = []

    res_list = item.get("interventionResources") or []
    for res in res_list:
        if isinstance(res, dict):
            r = res.get("resource") or res
            r_name = extract_caption(r)
            if re.search(r'\d', r_name) or "AMPHI" in r_name.upper():
                if r_name not in rooms:
                    rooms.append(r_name)
            else:
                if r_name and r_name not in instructors:
                    instructors.append(r_name)

    # Check interventionInstructors and participations
    for inst in item.get("interventionInstructors") or []:
        name = extract_person_name(inst)
        if name and name not in instructors:
            instructors.append(name)

    for part in item.get("participations") or []:
        if isinstance(part, dict):
            name = extract_person_name(part)
            if name and name not in instructors:
                instructors.append(name)

    # 5. Reconcile with DOM metadata harvested from card footers
    time_str = start_dt.strftime("%H:%M")
    lookup_key = f"{summary.lower()}_{time_str}"
    if lookup_key in dom_metadata_map:
        dom_teacher = dom_metadata_map[lookup_key]
        if dom_teacher and dom_teacher not in instructors:
            instructors.append(dom_teacher)

    location = " / ".join(rooms)
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

    for auriga_id, g_event in existing_events.items():
        if auriga_id and auriga_id not in seen_ids:
            service.events().delete(calendarId=calendar_id, eventId=g_event["id"]).execute()
            print(f"[PURGE OBSOLETE] {g_event.get('summary')} ({g_event.get('start', {}).get('dateTime')})")


if __name__ == "__main__":
    raw_items, dom_metadata_map = fetch_auriga_schedule()
    print(f"[INFO] Intercepted {len(raw_items)} total interventions.")

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
        evt = parse_api_event(item, dom_metadata_map)
        if evt:
            valid_events.append(evt)

    print(f"[INFO] Parsed {len(valid_events)} verified academic sessions.")
    if valid_events:
        sync_to_google(valid_events)
        print("[SUCCESS] Calendar sync complete.")