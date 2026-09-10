def fetch_auriga_schedule():
    username = os.environ["SCHOOL_USERNAME"]
    password = os.environ["SCHOOL_PASSWORD"]
    
    events = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="fr-FR", 
            timezone_id="Europe/Paris",
            viewport={"width": 1440, "height": 900}
        )
        page = context.new_page()

        # Listen for any JSON or PrimeFaces schedule responses
        def handle_response(response):
            try:
                # Capture any calendar, planning, or schedule endpoints
                url = response.url.lower()
                if any(k in url for k in ["planning", "schedule", "agenda", "evenement", "event"]):
                    text = response.text()
                    if '"events"' in text or 'events:' in text or '"start"' in text:
                        match = re.search(r'\{"events"\s*:\s*(\[.*?\])\}', text)
                        if match:
                            parsed = json.loads(match.group(1))
                            events.extend(parsed)
                            print(f"[INFO] Intercepted {len(parsed)} events via regex.")
                        else:
                            # Try parsing pure JSON array responses
                            try:
                                data = response.json()
                                if isinstance(data, list) and len(data) > 0 and "start" in data[0]:
                                    events.extend(data)
                                    print(f"[INFO] Intercepted {len(data)} raw JSON events.")
                                elif isinstance(data, dict) and "events" in data:
                                    events.extend(data["events"])
                                    print(f"[INFO] Intercepted {len(data['events'])} dict JSON events.")
                            except Exception:
                                pass
            except Exception:
                pass

        page.on("response", handle_response)

        print("[INFO] Navigating to Auriga landing page...")
        page.goto("https://auriga.isae-supaero.fr", wait_until="networkidle")

        # 1. Click SSO button if present
        sso_btn = page.locator("text=/Connexion SSO|SSO|Authentification/i").first
        if sso_btn.is_visible():
            print("[INFO] Clicking Auriga SSO button...")
            sso_btn.click()
            page.wait_for_load_state("networkidle")

        # 2. Complete Eliot login
        if "eliot.isae.fr" in page.url or page.locator("input[type='password']").count() > 0:
            print("[INFO] Submitting Eliot credentials...")
            page.locator("input[type='text'], input[name*='username'], input[id*='username'], input[name='j_username']").first.fill(username)
            page.locator("input[type='password'], input[name='j_password']").first.fill(password)
            submit_btn = page.locator("button[type='submit'], input[type='submit'], button[name='_eventId_proceed']").first
            submit_btn.click()
            page.wait_for_load_state("networkidle")
            print(f"[INFO] Post-login URL: {page.url}")

        # 3. Wait for the SPA dashboard to fully load
        page.wait_for_timeout(3000)

        # Look for the Planning/Agenda button in the UI
        # Common selectors in modern Auriga/Aurion: sidebar menu items, cards, or nav links
        print("[INFO] Searching for Planning navigation link...")
        planning_target = page.locator(
            "a:has-text('Planning'), "
            "button:has-text('Planning'), "
            "div[role='button']:has-text('Planning'), "
            "span:has-text('Planning'), "
            "a[href*='planning'], "
            "a[href*='Planning'], "
            "a:has-text('Emploi du temps'), "
            "a:has-text('Mon planning')"
        ).first

        if planning_target.is_visible():
            print(f"[INFO] Found planning link ({planning_target.inner_text().strip()}). Clicking...")
            planning_target.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(4000)
        else:
            print("[WARN] Planning link not immediately visible. Checking hamburger menu or sub-routes...")
            # If there is a sidebar toggle button
            menu_btn = page.locator("button[aria-label*='menu'], .menu-button, .pi-bars, i.fa-bars").first
            if menu_btn.is_visible():
                menu_btn.click()
                page.wait_for_timeout(1000)
                planning_target = page.locator("text=/Planning|Emploi du temps|Mon planning/i").first
                if planning_target.is_visible():
                    planning_target.click()
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(4000)

        print(f"[INFO] Current URL after navigation: {page.url}")

        # Capture a debug screenshot so we can see the exact dashboard / calendar UI
        page.screenshot(path="debug_screen.png")

        # Let any remaining AJAX calendar requests complete
        page.wait_for_timeout(5000)
        browser.close()

    return events