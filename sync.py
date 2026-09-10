import os
import json
import re
from datetime import datetime, date, time, timedelta
import pytz
from playwright.sync_api import sync_playwright
from google.oauth2 import service_account
from googleapiclient.discovery import build

PARIS_TZ = pytz.timezone("Europe/Paris")
PLANNING_URL = "https://auriga.isae-supaero.fr/#/mainContent/menuEntry/227/planning"

MONTH_NAMES = {
    "jan": 1, "fév": 2, "fev": 2, "mar": 3, "avr": 4, "mai": 5, "juin": 6,
    "juil": 7, "aoû": 8, "aou": 8, "sep": 9, "oct": 10, "nov": 11, "déc": 12, "dec": 12
}

def get_google_service():
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise ValueError("Missing GOOGLE_SERVICE_ACCOUNT_JSON secret.")
    creds_dict = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=creds)

def extract_cards_from_page(page):
    """
    Evaluates JavaScript in the browser to extract every <pl-planning-card-header>
    and associates it with the date of its column or day container.
    """
    return page.evaluate("""
        () => {
            const results = [];
            
            // Find all planning card containers
            const headers = document.querySelectorAll('pl-planning-card-header, .pl-planning-card--header');
            
            headers.forEach((headerEl) => {
                const parent = headerEl.closest('div[class*="planning-card"], [class*="card"], li, td, div') || headerEl.parentElement;
                
                // 1. Title
                const titleEl = headerEl.querySelector('.pl-planning-card--header--title--text, p, div');
                const title = titleEl ? titleEl.innerText.trim() : 'Course';
                
                // 2. Time string (e.g., '09:00 - 12:15')
                const timeEl = parent.querySelector('.pl-planning-card--content--time');
                const timeText = timeEl ? timeEl.innerText.trim() : '';
                
                // 3. Room
                const roomEl = parent.querySelector('.pl-planning-card--footer--left, [pl-planning-card-footer-left]');
                const room = roomEl ? roomEl.innerText.trim() : '';
                
                // 4. Teacher
                const teacherEl = parent.querySelector('.pl-planning-card--footer--right, [pl-planning-card-footer-right]');
                const teacher = teacherEl ? teacherEl.innerText.trim() : '';
                
                // 5. Course Cohort/Group
                const contentEl = parent.querySelector('.pl-planning-card--content');
                const contentText = contentEl ? contentEl.innerText.replace(timeText, '').trim() : '';
                
                // 6. Find date context from parent column header
                let dateStr = '';
                let col = parent.closest('[class*="col"], [class*="day"], [class*="column"], td');
                if (col) {
                    const colHeader = col.querySelector('[class*="header"], [class*="date"], [class*="title"]') || col;
                    dateStr = colHeader ? colHeader.innerText.slice(0, 50) : '';
                }

                results.push({
                    title: title,
                    time: timeText,
                    room: room,
                    teacher: teacher,
                    content: contentText,
                    dateContext: dateStr
                });
            });
            
            return results;
        }
    """)

def fetch_auriga_schedule():
    username = os.environ["SCHOOL_USERNAME"]
    password = os.environ["SCHOOL_PASSWORD"]
    
    raw_cards = []
    
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

        page.wait_for_timeout(2000)

        # 3. Switch Language to English
        print("[INFO] Checking language toggle...")
        try:
            lang_toggle = page.locator("button:has-text('EN'), a:has-text('EN'), [aria-label*='English'], img[alt*='en'], img[alt*='English'], span:has-text('EN')").first
            if lang_toggle.is_visible():
                print("[INFO] Clicking English language toggle...")
                lang_toggle.click()
                page.wait_for_timeout(1500)
        except Exception as e:
            print(f"[WARN] Language toggle not clicked: {e}")

        # 4. Open Planning view
        print(f"[INFO] Navigating to {PLANNING_URL}...")
        page.goto(PLANNING_URL, wait_until="networkidle")
        page.wait_for_timeout(4000)

        # Wait for the custom card elements to be present
        try:
            page.wait_for_selector("pl-planning-card-header, .pl-planning-card--header", timeout=12000)
            print("[INFO] Found <pl-planning-card-header> in DOM.")
        except Exception:
            print("[WARN] Card selector timeout, proceeding with scan...")

        # Switch to Month view if available to load more weeks at once
        try:
            month_btn = page.locator("button:has-text('Month'), button:has-text('Mois'), [aria-label*='Month'], [aria-label*='Mois']").first
            if month_btn.is_visible():
                print("[INFO] Switching to Month view...")
                month_btn.click()
                page.wait_for_timeout(3000)
        except Exception:
            pass

        # Extract cards for the current view
        cards_view1 = extract_cards_from_page(page)
        raw_cards.extend(cards_view1)
        print(f"[INFO] Extracted {len(cards_view1)} cards from current view.")

        # Click the 'Next' arrow to fetch the following week/month as well
        try:
            next_btn = page.locator("button:has-text('>'), button[aria-label*='next'], button[aria-label*='suivant'], .fc-next-button, [class*='next']").first
            if next_btn.is_visible():
                print("[INFO] Clicking Next view arrow...")
                next_btn.click()
                page.wait_for_timeout(3000)
                cards_view2 = extract_cards_from_page(page)
                raw_cards.extend(cards_view2)
                print(f"[INFO] Extracted {len(cards_view2)} additional cards from next view.")
        except Exception as e:
            print(f"[INFO] Next view skipped: {e}")

        page.screenshot(path="debug_screen.png")
        browser.close()

    return raw_cards

def parse_card_to_event(card, fallback_date):
    title = card.get("title", "Course")
    time_str = card.get("time", "")
    room = card.get("room", "")
    teacher = card.get("teacher", "")
    content = card.get("content", "")
    date_ctx = card.get("dateContext", "")

    # Attempt to extract explicit day/month from column context
    event_date = fallback_date
    date_match = re.search(r'(\d{1,2})[\s/\.-]+([a-zA-Zà-üÀ-Ü]{3,}|\d{1,2})', date_ctx)
    if date_match:
        try:
            day = int(date_match.group(1))
            month_token = date_match.group(2).lower()[:3]
            month = MONTH_NAMES.get(month_token, int(month_token) if month_token.isdigit() else fallback_date.month)
            year = fallback_date.year
            event_date = date(year, month, day)
        except Exception:
            event_date = fallback_date

    # Parse Start and End times from string (e.g., '09:00 - 12:15')
    start_dt = PARIS_TZ.localize(datetime.combine(event_date, time(8, 0)))
    end_dt = start_dt + timedelta(hours=2)

    times = re.findall(r'(\d{1,2})[:h](\d{2})', time_str)
    if len(times) >= 2:
        sh, sm = int(times[0][0]), int(times[0][1])
        eh, em = int(times[1][0]), int(times[1][1])
        start_dt = PARIS_TZ.localize(datetime.combine(event_date, time(sh, sm)))
        end_dt = PARIS_TZ.localize(datetime.combine(event_date, time(eh, em)))

    description_parts = []
    if content:
        description_parts.append(content)
    if teacher:
        description_parts.append(f"Instructor: {teacher}")
    if room:
        description_parts.append(f"Room: {room}")

    uid = f"{title}_{start_dt.strftime('%Y%m%dT%H%M%S')}"

    return {
        "id": re.sub(r'[^a-zA-Z0-9]', '', uid)[:64],
        "summary": title,
        "description": "\n".join(description_parts),
        "location": room,
        "start": {"dateTime": start_dt.isoformat()},
        "end": {"dateTime": end_dt.isoformat()}
    }

def sync_to_google(parsed_events):
    calendar_id = os.environ["CALENDAR_ID"]
    service = get_google_service()

    now = datetime.now(PARIS_TZ)
    time_min = (now - timedelta(days=7)).isoformat()
    time_max = (now + timedelta(days=60)).isoformat()

    print("[INFO] Fetching existing Google Calendar items...")
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
                curr.get("location") != body["location"] or
                curr.get("description") != body["description"]):
                service.events().patch(calendarId=calendar_id, eventId=curr["id"], body=body).execute()
                print(f"[UPDATE] {body['summary']} ({item['start']['dateTime']})")
        else:
            service.events().insert(calendarId=calendar_id, body=body).execute()
            print(f"[ADD] {body['summary']} ({item['start']['dateTime']}) - {body['location']}")

    for auriga_id, g_event in existing_events.items():
        if auriga_id and auriga_id not in seen_ids:
            start_iso = g_event.get("start", {}).get("dateTime")
            if start_iso and datetime.fromisoformat(start_iso) > now:
                service.events().delete(calendarId=calendar_id, eventId=g_event["id"]).execute()
                print(f"[REMOVE] {g_event.get('summary')}")

if __name__ == "__main__":
    cards = fetch_auriga_schedule()
    print(f"[INFO] Retrieved {len(cards)} card elements.")
    if cards:
        today = date.today()
        formatted = []
        for c in cards:
            if c.get("title") and c.get("time"):
                formatted.append(parse_card_to_event(c, today))
        
        print(f"[INFO] Parsed {len(formatted)} valid course sessions.")
        sync_to_google(formatted)
        print("[SUCCESS] Calendar sync complete.")