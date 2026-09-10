# ISAE-SUPAERO Auriga to Google Calendar Sync

Automatically synchronizes your ISAE-SUPAERO Auriga timetable with Google Calendar via GitHub Actions. Features true API-level datetime accuracy, dynamic course color-coding, exam red highlighting, and automated updates through May 2027.

---

## Setup Instructions

### 1. Create a Dedicated Google Calendar
1. Open [Google Calendar](https://calendar.google.com).
2. Click the **`+`** icon next to **Other calendars** -> **Create new calendar** (e.g., name it `Auriga Classes`).
3. Open the calendar's settings, scroll down to **Integrate calendar**, and copy the **Calendar ID** (format: `abcdef12345@group.calendar.google.com`).

### 2. Create a Google Cloud Service Account
1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (e.g., `Auriga Sync`).
3. Search for **Google Calendar API** in the top bar and click **Enable**.
4. Go to **APIs & Services** -> **Credentials** -> **Create Credentials** -> **Service Account**.
5. Give it a name (e.g., `auriga-sync`), click **Create and Continue**, then **Done**.
6. Click on your newly created service account email -> go to the **Keys** tab -> **Add Key** -> **Create new key** -> **JSON**. Save the downloaded `.json` key file.
7. Copy the service account email address.
8. Go back to Google Calendar settings for your new calendar, scroll to **Share with specific people**, click **Add people**, paste the service account email, and set permissions to **Make changes to events**.

### 3. Use as Template & Configure Secrets
1. Go to the main page of this repository and click the green **Use this template** -> **Create a new repository** button. Keep your new repository **Private**.
2. In your new repository, go to **Settings** -> **Secrets and variables** -> **Actions**.
3. Click **New repository secret** and add the following 4 secrets:
   - `SCHOOL_USERNAME`: Your ISAE Eliot username (e.g., `j.doe`)
   - `SCHOOL_PASSWORD`: Your ISAE Eliot password
   - `CALENDAR_ID`: Your Google Calendar ID from Step 1
   - `GOOGLE_SERVICE_ACCOUNT_JSON`: The entire raw text contents of the `.json` key file you downloaded in Step 2.

### 4. Enable Workflows
1. Go to the **Actions** tab in your repository.
2. If GitHub prompts you that workflows are disabled, click **I understand my workflows, go ahead and enable them**.
3. Select **Auriga Timetable Sync** in the left sidebar and click **Run workflow** to test it out!
