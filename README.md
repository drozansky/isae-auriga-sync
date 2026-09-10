\# ISAE-SUPAERO Auriga to Google Calendar Sync



Automatically synchronizes your ISAE-SUPAERO Auriga timetable with Google Calendar via GitHub Actions.



\## Setup Instructions



\### 1. Create a Dedicated Google Calendar

1\. Open \[Google Calendar](https://calendar.google.com).

2\. Click \*\*`+` (Other calendars)\*\* -> \*\*Create new calendar\*\* (e.g., name it `Auriga Classes`).

3\. Open the calendar's settings, scroll to \*\*Integrate calendar\*\*, and copy the \*\*Calendar ID\*\* (format: `...@group.calendar.google.com`).



\### 2. Create a Google Cloud Service Account

1\. Go to the \[Google Cloud Console](https://console.cloud.google.com/).

2\. Create a new project (e.g., `Auriga Sync`).

3\. Search for \*\*Google Calendar API\*\* and click \*\*Enable\*\*.

4\. Go to \*\*APIs \& Services\*\* -> \*\*Credentials\*\* -> \*\*Create Credentials\*\* -> \*\*Service Account\*\*.

5\. Give it a name, click \*\*Create and Continue\*\*, then \*\*Done\*\*.

6\. Click on the new service account email -> go to \*\*Keys\*\* tab -> \*\*Add Key\*\* -> \*\*Create new key\*\* -> \*\*JSON\*\*. Save the downloaded `.json` file.

7\. Copy the service account email address.

8\. Go back to Google Calendar settings for your new calendar, scroll to \*\*Share with specific people\*\*, click \*\*Add people\*\*, paste the service account email, and set permissions to \*\*Make changes to events\*\*.



\### 3. Use as Template \& Configure Secrets

1\. Click \*\*Use this template\*\* at the top of this GitHub repository to create your own \*\*Private\*\* copy.

2\. In your new repository, go to \*\*Settings\*\* -> \*\*Secrets and variables\*\* -> \*\*Actions\*\*.

3\. Add the following 4 Repository Secrets:

&#x20;  - `SCHOOL\_USERNAME`: Your ISAE Eliot username (e.g., `j.doe`)

&#x20;  - `SCHOOL\_PASSWORD`: Your ISAE Eliot password

&#x20;  - `CALENDAR\_ID`: Your Google Calendar ID from Step 1

&#x20;  - `GOOGLE\_SERVICE\_ACCOUNT\_JSON`: The entire raw text contents of the `.json` key file you downloaded in Step 2.



\### 4. Enable Workflows

1\. Go to the \*\*Actions\*\* tab in your repository.

2\. Click \*\*I understand my workflows, go ahead and enable them\*\* if prompted.

3\. Select \*\*Auriga Timetable Sync\*\* and click \*\*Run workflow\*\* to test it!

