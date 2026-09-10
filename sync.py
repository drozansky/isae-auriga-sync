import os
import json
import re
from datetime import datetime, timedelta
import pytz
from icalendar import Calendar
from auriga import extract_calendar
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

def fetch_and_parse_events():
    username = os.environ["SCHOOL_USERNAME"]
    password = os.environ["SCHOOL_PASSWORD"]

    ics_filename = "schedule.ics"
    print("[INFO] Extracting timetable via auriga-extract...")
    
    # Run the extraction library
    extract_calendar(username, password, ics_filename)

    if not os.path.exists(ics_filename):
        raise FileNotFoundError("Calendar file schedule.ics was not created.")

    with open(ics_filename, "rb") as f:
        cal = Calendar.from_ical(f.read())

    parsed_events = []

    for component in cal.walk():
        if component.name == "VEVENT":
            uid = str(component.get("uid", ""))
            summary = str(component.get("summary", "Cours"))
            description = str(component.get("description", ""))
            location = str(component.get("location", ""))

            # Parse start and end datetimes
            dtstart = component.get("dtstart").dt
            dtend = component.get("dtend").dt

            # Ensure localized to Paris timezone
            if not hasattr(dtstart, "tzinfo") or dtstart.tzinfo is None:
                dtstart = PARIS_TZ.localize(dtstart)
            else:
                dtstart = dtstart.astimezone(PARIS_TZ)

            if not hasattr(dtend, "tzinfo") or dtend.tzinfo is None:
                dtend = PARIS_TZ.localize(dtend)
            else:
                dtend = dtend.astimezone(PARIS_TZ)

            event_id = uid if uid else f"{summary}_{dtstart.isoformat()}"

            parsed_events.append({
                "id": event_id,
                "summary": summary,
                "description": description,
                "location": location,
                "start": {"dateTime": dtstart.isoformat()},
                "end": {"dateTime": dtend.isoformat()},
            })

    return parsed_events

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
            # Detect changes in schedule or room
            if (curr.get("summary") != body["summary"] or
                curr.get("start", {}).get("dateTime") != body["start"]["dateTime"] or
                curr.get("end", {}).get("dateTime") != body["end"]["dateTime"] or
                curr.get("location") != body["location"]):
                service.events().patch(calendarId=calendar_id, eventId=curr["id"], body=body).execute()
                print(f"Updated: {body['summary']} ({item['start']['dateTime']})")
        else:
            service.events().insert(calendarId=calendar_id, body=body).execute()
            print(f"Added: {body['summary']} ({item['start']['dateTime']})")

    # Handle canceled classes
    for auriga_id, g_event in existing_events.items():
        if auriga_id and auriga_id not in seen_ids:
            start_iso = g_event.get("start", {}).get("dateTime")
            if start_iso and datetime.fromisoformat(start_iso) > now:
                service.events().delete(calendarId=calendar_id, eventId=g_event["id"]).execute()
                print(f"Removed canceled class: {g_event.get('summary')}")

if __name__ == "__main__":
    events = fetch_and_parse_events()
    print(f"[INFO] Successfully parsed {len(events)} events.")
    if events:
        sync_to_google(events)
        print("[SUCCESS] Calendar sync complete.")