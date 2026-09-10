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
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12
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
    Extracts each planning card along with its explicit day cell date or day number.
    """
    return page.evaluate("""
        () => {
            const results = [];
            
            // Get view title (e.g. 'September 2026') to determine current month and year
            const titleEl = document.querySelector('.fc-toolbar-title, [class*="calendar-title"], [class*="header-title"], h2');
            const viewTitle = titleEl ? titleEl.innerText.trim() : '';

            const headers = document.querySelectorAll('pl-planning-card-header, .pl-planning-card--header');
            
            headers.forEach((headerEl) => {
                const card = headerEl.closest('div[class*="planning-card"], [class*="card"], li, div') || headerEl.parentElement;
                
                // Title
                const titleEl = headerEl.querySelector('.pl-planning-card--header--title--text, p, div');
                const title = titleEl ? titleEl.innerText.trim() : 'Course';
                
                // Time
                const timeEl = card.querySelector('.pl-planning-card--content--time');
                const timeText = timeEl ? timeEl.innerText.trim() : '';
                
                // Room
                const roomEl = card.querySelector('.pl-planning-card--footer--left, [pl-planning-card-footer-left]');
                const room = roomEl ? roomEl.innerText.trim() : '';
                
                // Teacher
                const teacherEl = card.querySelector('.pl-planning-card--footer--right, [pl-planning-card-footer-right]');
                const teacher = teacherEl ? teacherEl.innerText.trim() : '';
                
                // Track / Details
                const contentEl = card.querySelector('.pl-planning-card--content');
                const contentText = contentEl ? contentEl.innerText.replace(timeText, '').trim() : '';

                // Find date from the enclosing cell
                let dayNum = '';
                let cellDateAttr = '';
                
                // Look for common calendar day cell wrappers
                const cell = card.closest('td, [class*="day-cell"], [class*="daygrid-day"], [class*="cell"]');
                if (cell) {
                    cellDateAttr = cell.getAttribute('data-date') || '';
                    
                    // Look for day number element inside this cell
                    const dayEl = cell.querySelector('[class*="day-top"], [class*="day-number"], [class*="date"], a, span');
                    if (dayEl) {
                        const m = dayEl.innerText.match(/\\b(\\d{1,2})\\b/);
                        if (m) dayNum = m[1];
                    }
                }

                results.push({
                    title: title,
                    time: timeText,
                    room: room,
                    teacher: teacher,
                    content: contentText,
                    fullText: card.innerText,
                    dayNum: dayNum,
                    cellDateAttr: cellDateAttr,
                    viewTitle: viewTitle
                });
            });
            
            return results;
        }
    """)

def switch_language_to_english(page):
    print("[INFO] Setting language to English...")
    try:
        lang_btn = page.locator("text=/Français|Francais/i").first
        if lang_btn.is_visible():
            lang_btn.click()
            page.wait_for_timeout(1000)
            opt = page.locator("text=/Anglais|English/i").first
            if opt.is_visible():
                print("[INFO] Selected English.")
                opt.click()
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(2000)
    except Exception as e:
        print(f"[WARN] Language toggle: {e}")

def fetch_auriga_schedule():
    username = os.environ["SCHOOL_USERNAME"]
    password = os.environ["SCHOOL_PASSWORD"]
    
    all_cards = []
    
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

        # 2. Login
        if "eliot.isae.fr" in page.url or page.locator("input[type='password']").count() > 0:
            print("[INFO] Logging into Eliot IDP...")
            page.locator("input[type='text'], input[name*='username'], input[id*='username'], input[name='j_username']").first.fill(username)
            page.locator("input[type='password'], input[name='j_password']").first.fill(password)
            submit_btn = page.locator("button[type='submit'], input[type='submit'], button[name='_eventId_proceed']").first
            submit_btn.click()
            page.wait_for_load_state("networkidle")

        page.wait_for_timeout(2500)

        # 3. Switch Language
        switch_language_to_english(page)

        # 4. Open Planning view
        print(f"[INFO] Navigating to {PLANNING_URL}...")
        page.goto(PLANNING_URL, wait_until="networkidle")
        page.wait_for_timeout(4000)

        # 5. Month View
        try:
            month_btn = page.locator("button:has-text('Month'), button:has-text('Mois'), [aria-label*='Month'], [aria-label*='Mois']").first
            if month_btn.is_visible():
                print("[INFO] Switching to Month view...")
                month_btn.click()
                page.wait_for_timeout(3000)
        except Exception:
            pass

        # 6. Scrape 5 consecutive months forward (covers full semester)
        months_to_scrape = 5
        for i in range(months_to_scrape):
            print(f"[INFO] Scraping calendar view #{i+1}...")
            cards = extract_cards_from_page(page)
            all_cards.extend(cards)
            print(f"[INFO] Collected {len(cards)} cards in view #{i+1}.")

            if i < months_to_scrape - 1:
                next_btn = page.locator("button:has-text('>'), [aria-label*='next'], [aria-label*='suivant'], .fc-next-button").first
                if next_btn.is_visible():
                    next_btn.click()
                    page.wait_for_timeout(3000)
                else:
                    break

        page.screenshot(path="debug_screen.png")
        browser.close()

    return all_cards

def parse_card_to_event(card, reference_date):
    title = card.get("title") or "Course"
    time_str = card.get("time", "")
    room = card.get("room", "")
    teacher = card.get("teacher", "")
    content = card.get("content", "")
    full_text = card.get("fullText", "")
    day_num = card.get("dayNum", "")
    cell_date_attr = card.get("cellDateAttr", "")
    view_title = card.get("viewTitle", "").lower()

    # 1. Determine Date
    event_date = reference_date

    # Case A: Cell has explicit 'data-date="2026-09-14"'
    if cell_date_attr and re.match(r'^\d{4}-\d{2}-\d{2}$', cell_date_attr):
        try:
            event_date = datetime.strptime(cell_date_attr, "%Y-%m-%d").date()
        except Exception:
            pass
    # Case B: Combine day_num with viewTitle (e.g. 'September 2026')
    elif day_num:
        try:
            day = int(day_num)
            month = reference_date.month
            year = reference_date.year
            
            # Extract month from viewTitle
            for name, m_val in MONTH_NAMES.items():
                if name in view_title:
                    month = m_val
                    break
            
            # Extract year from viewTitle
            yr_match = re.search(r'\b(202\d)\b', view_title)
            if yr_match:
                year = int(yr_match.group(1))

            event_date = date(year, month, day)
        except Exception:
            event_date = reference_date

    # 2. Determine Start & End Times (e.g., '09:00 - 12:15')
    time_search = f"{time_str} {full_text}"
    times = re.findall(r'(\d{1,2})[:h](\d{2})', time_search)

    if len(times) >= 2:
        sh, sm = int(times[0][0]), int(times[0][1])
        eh, em = int(times[1][0]), int(times[1][1])
        start_dt = PARIS_TZ.localize(datetime.combine(event_date, time(sh, sm)))
        end_dt = PARIS_TZ.localize(datetime.combine(event_date, time(eh, em)))
    else:
        start_dt = PARIS_TZ.localize(datetime.combine(event_date, time(9, 0)))
        end_dt = start_dt + timedelta(hours=2)

    # 3. Description
    desc_lines = []
    if content:
        desc_lines.append(content)
    if teacher:
        desc_lines.append(f"Instructor: {teacher}")
    if room:
        desc_lines.append(f"Room: {room}")

    # Unique UID based on Title, Start Time, and Room
    raw_uid = f"{title}_{start_dt.strftime('%Y%m%d%H%M')}_{room}"
    event_id = re.sub(r'[^a-zA-Z0-9]', '', raw_uid)[:64]

    return {
        "id": event_id,
        "summary": title,
        "description": "\n".join(desc_lines),
        "location": room,
        "start": {"dateTime": start_dt.isoformat()},
        "end": {"dateTime": end_dt.isoformat()}
    }

def sync_to_google(parsed_events):
    calendar_id = os.environ["CALENDAR_ID"]
    service = get_google_service()

    now = datetime.now(PARIS_TZ)
    time_min = (now - timedelta(days=7)).isoformat()
    time_max = (now + timedelta(days=180)).isoformat()

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

    # Clean up obsolete placeholder events from previous buggy runs
    for auriga_id, g_event in existing_events.items():
        if auriga_id and auriga_id not in seen_ids:
            start_iso = g_event.get("start", {}).get("dateTime")
            if start_iso and datetime.fromisoformat(start_iso) > now:
                service.events().delete(calendarId=calendar_id, eventId=g_event["id"]).execute()
                print(f"[REMOVE] {g_event.get('summary')}")

if __name__ == "__main__":
    cards = fetch_auriga_schedule()
    print(f"[INFO] Retrieved {len(cards)} total cards across views.")

    today = date.today()
    parsed_events = []
    
    # Process cards and prevent duplicates across overlapping views
    seen_events = set()
    for c in cards:
        if c.get("title") and len(c.get("title")) > 2:
            evt = parse_card_to_event(c, today)
            key = (evt["summary"], evt["start"]["dateTime"], evt["location"])
            if key not in seen_events:
                seen_events.add(key)
                parsed_events.append(evt)

    print(f"[INFO] Successfully parsed {len(parsed_events)} distinct class sessions.")
    if parsed_events:
        sync_to_google(parsed_events)
        print("[SUCCESS] Full semester calendar sync complete.")