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
    "jan": 1, "fév": 2, "fev": 2, "feb": 2, "mar": 3, "avr": 4, "apr": 4, 
    "mai": 5, "may": 5, "juin": 6, "jun": 6, "juil": 7, "jul": 7, "aoû": 8, 
    "aou": 8, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "déc": 12, "dec": 12
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
    return page.evaluate("""
        () => {
            const results = [];
            const headers = document.querySelectorAll('pl-planning-card-header, .pl-planning-card--header');
            
            headers.forEach((headerEl) => {
                const parent = headerEl.closest('div[class*="planning-card"], [class*="card"], li, td, div') || headerEl.parentElement;
                
                // 1. Title
                const titleEl = headerEl.querySelector('.pl-planning-card--header--title--text, p, div');
                const title = titleEl ? titleEl.innerText.trim() : 'Course';
                
                // 2. Time string (e.g., '09:00 - 12:15')
                const timeEl = parent.querySelector('.pl-planning-card--content--time');
                let timeText = timeEl ? timeEl.innerText.trim() : '';
                
                // 3. Room
                const roomEl = parent.querySelector('.pl-planning-card--footer--left, [pl-planning-card-footer-left]');
                const room = roomEl ? roomEl.innerText.trim() : '';
                
                // 4. Teacher
                const teacherEl = parent.querySelector('.pl-planning-card--footer--right, [pl-planning-card-footer-right]');
                const teacher = teacherEl ? teacherEl.innerText.trim() : '';
                
                // 5. Course Cohort/Group
                const contentEl = parent.querySelector('.pl-planning-card--content');
                let contentText = contentEl ? contentEl.innerText.replace(timeText, '').trim() : '';
                
                // 6. Find date context from parent column or day cell
                let dateStr = '';
                let col = parent.closest('[class*="col"], [class*="day"], [class*="column"], td, [class*="cell"]');
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
                    dateContext: dateStr,
                    fullText: parent.innerText
                });
            });
            
            return results;
        }
    """)

def switch_language_to_english(page):
    print("[INFO] Attempting to set language to English...")
    try:
        # Match element containing 'Français' or 'Francais'
        lang_btn = page.locator("text=/Français|Francais/i").first
        if lang_btn.is_visible():
            lang_btn.click()
            page.wait_for_timeout(1000)
            
            # Select Anglais or English from the opened dropdown
            opt = page.locator("text=/Anglais|English/i").first
            if opt.is_visible():
                print("[INFO] Selected 'Anglais' / 'English'.")
                opt.click()
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(2000)
    except Exception as e:
        print(f"[WARN] Language toggle skipped: {e}")

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

        # 1. SSO Click
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

        # 3. Switch Language to English
        switch_language_to_english(page)

        # 4. Open Planning view
        print(f"[INFO] Navigating to {PLANNING_URL}...")
        page.goto(PLANNING_URL, wait_until="networkidle")
        page.wait_for_timeout(4000)

        # 5. Switch to Month view
        try:
            month_btn = page.locator("button:has-text('Month'), button:has-text('Mois'), [aria-label*='Month'], [aria-label*='Mois']").first
            if month_btn.is_visible():
                print("[INFO] Switching to Month view...")
                month_btn.click()
                page.wait_for_timeout(3000)
        except Exception:
            pass

        # 6. Scrape across the current month and upcoming months
        # We step forward month-by-month up to 4 times to pull the upcoming semester
        months_to_scrape = 4
        for i in range(months_to_scrape):
            print(f"[INFO] Scraping calendar view #{i+1}...")
            cards = extract_cards_from_page(page)
            all_cards.extend(cards)
            print(f"[INFO] Collected {len(cards)} cards in view #{i+1}.")

            if i < months_to_scrape - 1:
                # Find and click Next button ('>')
                next_btn = page.locator("button:has-text('>'), [aria-label*='next'], [aria-label*='suivant'], .fc-next-button").first
                if next_btn.is_visible():
                    next_btn.click()
                    page.wait_for_timeout(2500)
                else:
                    break

        page.screenshot(path="debug_screen.png")
        browser.close()

    return all_cards

def parse_card_to_event(card, fallback_date):
    title = card.get("title") or "Course"
    full_text = card.get("fullText", "")
    time_str = card.get("time", "")
    room = card.get("room", "")
    teacher = card.get("teacher", "")
    content = card.get("content", "")
    date_ctx = card.get("dateContext", "")

    # Resolve date
    event_date = fallback_date
    date_search_text = f"{date_ctx} {full_text}"
    date_match = re.search(r'(\d{1,2})[\s/\.-]+([a-zA-Zà-üÀ-Ü]{3,}|\d{1,2})', date_search_text)
    if date_match:
        try:
            day = int(date_match.group(1))
            m_str = date_match.group(2).lower()[:3]
            month = MONTH_NAMES.get(m_str, int(m_str) if m_str.isdigit() else fallback_date.month)
            year = fallback_date.year
            # Adjust year if rolling past December
            if month < fallback_date.month and fallback_date.month >= 10:
                year += 1
            event_date = date(year, month, day)
        except Exception:
            event_date = fallback_date

    # Resolve start and end times
    time_search_text = f"{time_str} {full_text}"
    times = re.findall(r'(\d{1,2})[:h](\d{2})', time_search_text)
    
    if len(times) >= 2:
        sh, sm = int(times[0][0]), int(times[0][1])
        eh, em = int(times[1][0]), int(times[1][1])
        start_dt = PARIS_TZ.localize(datetime.combine(event_date, time(sh, sm)))
        end_dt = PARIS_TZ.localize(datetime.combine(event_date, time(eh, em)))
    else:
        start_dt = PARIS_TZ.localize(datetime.combine(event_date, time(9, 0)))
        end_dt = start_dt + timedelta(hours=2)

    desc_lines = []
    if content:
        desc_lines.append(content)
    if teacher:
        desc_lines.append(f"Instructor: {teacher}")
    if room:
        desc_lines.append(f"Room: {room}")

    # Unique deterministic ID prevents duplicates when scraping overlapping months
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
    time_max = (now + timedelta(days=150)).isoformat()

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
    print(f"[INFO] Retrieved {len(cards)} total raw cards across views.")
    
    # Deduplicate raw cards by text and time
    unique_cards = []
    seen = set()
    for c in cards:
        sig = (c.get("title"), c.get("time"), c.get("room"), c.get("dateContext"))
        if sig not in seen:
            seen.add(sig)
            unique_cards.append(c)

    today = date.today()
    formatted = [parse_card_to_event(c, today) for c in unique_cards if c.get("title")]
    
    print(f"[INFO] Parsed {len(formatted)} unique course sessions.")
    if formatted:
        sync_to_google(formatted)
        print("[SUCCESS] Full calendar sync complete.")