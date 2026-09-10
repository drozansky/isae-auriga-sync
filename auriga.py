import re
import requests
from bs4 import BeautifulSoup

CAS_LOGIN_URL = "https://cas.isae-supaero.fr/cas/login"
AURIGA_BASE_URL = "https://auriga.isae-supaero.fr"
AURIGA_PLANNING_URL = "https://auriga.isae-supaero.fr/faces/Planning.xhtml"

def extract_calendar(username, password, output_filename="schedule.ics"):
    """
    Authenticates via ISAE-SUPAERO CAS, navigates to Aurion Planning,
    and downloads the exported .ics timetable file.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })

    # 1. Fetch CAS Login Form to obtain execution flow token
    login_page = session.get(CAS_LOGIN_URL, params={"service": AURIGA_PLANNING_URL})
    soup = BeautifulSoup(login_page.text, "html.parser")

    execution_input = soup.find("input", {"name": "execution"})
    execution = execution_input["value"] if execution_input else None

    # 2. Authenticate against CAS
    payload = {
        "username": username,
        "password": password,
        "execution": execution,
        "_eventId": "submit",
        "submit": "SE CONNECTER"
    }

    auth_response = session.post(CAS_LOGIN_URL, data=payload, params={"service": AURIGA_PLANNING_URL})
    if "Identifiants invalides" in auth_response.text or "Authentication failed" in auth_response.text:
        raise ValueError("CAS Authentication failed: Please verify SCHOOL_USERNAME and SCHOOL_PASSWORD.")

    # 3. Access Planning Page to grab PrimeFaces ViewState
    planning_page = session.get(AURIGA_PLANNING_URL)
    soup_planning = BeautifulSoup(planning_page.text, "html.parser")

    view_state_input = soup_planning.find("input", {"name": "javax.faces.ViewState"})
    if not view_state_input:
        # If redirected through a landing portal, follow the menu redirection
        planning_page = session.get(AURIGA_PLANNING_URL)
        soup_planning = BeautifulSoup(planning_page.text, "html.parser")
        view_state_input = soup_planning.find("input", {"name": "javax.faces.ViewState"})

    view_state = view_state_input["value"] if view_state_input else None

    # 4. Trigger ICS Export
    # Find form elements or execute the export button POST action
    form = soup_planning.find("form")
    form_id = form["id"] if form else "form"

    # Search for an export button / menuitem identifier in page source
    export_id_match = re.search(r'(form:[a-zA-Z0-9_]+export[a-zA-Z0-9_]*)', planning_page.text, re.IGNORECASE)
    source_id = export_id_match.group(1) if export_id_match else f"{form_id}:export"

    export_payload = {
        form_id: form_id,
        "javax.faces.ViewState": view_state,
        source_id: source_id,
        "javax.faces.partial.ajax": "false"
    }

    headers = {
        "Faces-Request": "partial/ajax",
        "Referer": AURIGA_PLANNING_URL
    }

    ics_response = session.post(AURIGA_PLANNING_URL, data=export_payload, headers=headers)

    # 5. Save the calendar file
    with open(output_filename, "wb") as f:
        f.write(ics_response.content)

    print(f"[SUCCESS] Saved timetable to {output_filename} ({len(ics_response.content)} bytes)")