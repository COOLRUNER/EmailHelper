# Autonomous AI Job Application Tracker

An automated, intelligent ETL pipeline that utilizes the Gmail API and OpenAI's `gpt-4o-mini` to silently monitor, extract, and track job applications right into a Supabase PostgreSQL database.

## Features

- **Headless Automation**: Bound to a GitHub Actions CI/CD workflow, automatically checking your inbox on an hourly cron schedule framework.
- **Entity Resolution Engine**: Incorporates `RapidFuzz` string-matching algorithms to handle recruiter emails establishing new chains, accurately linking disjointed email threads back to their parent application without creating duplicates.
- **Adversarial AI Filtering**: Leverages a dual-pass classification prompt to drastically minimize API hallucination. Hardened to explicitly reject marketing job digests, One-Time Passwords (OTPs), and verification prompts.
- **State Machine Priority Logic**: Implements a strict status-hierarchy to prevent edge-case race conditions where a newer automated "Thank you for applying" email mistakenly regresses an older "Interview Invitation" state. 
- **Historical Event Tracing**: All application progress iterations are natively linked and appended into a Supabase `JSONB` array timeline.

## Setup

### 1. Database Configuration
Create your tracking table by running the following inside your Supabase SQL Editor:
```sql
create table applications (
  id bigint primary key generated always as identity,
  company text not null,
  role text,
  status text default 'Applied',
  thread_id text unique, 
  event_log JSONB DEFAULT '[]'::jsonb,
  created_at timestamp with time zone default now(),
  last_updated timestamp with time zone default now()
);
```

### 2. Google API Credentials
1. Set up a free Google Cloud Console project and enable the **Gmail API**.
2. Setup an externally available OAuth Consent screen and add your email.
3. Generate an OAuth Client ID *(Desktop Application)* and download the resulting `credentials.json` into the root of this folder.

### 3. Local Authorization (Initial Requirement)
Before you can run the application headlessly safely, you need to generate a localized token with `gmail.modify` abilities:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run the auth generation
python auth.py
```
*A browser window will appear to verify access. Accept the permissions.* 

### 4. GitHub Actions Deployment
Because fragile raw multi-line JSON breaks Bash environments in automated runners, you must Base64 encode your newly generated `token.json` before passing it to GitHub Secrets:
```bash
cat token.json | base64 | pbcopy
```

Go to your repository **Settings** -> **Secrets and variables** -> **Actions** and paste it as a Repository Secret under `GMAIL_TOKEN`.

Ensure you also inject the remaining environment variables exactly:
- `OPENAI_API_KEY`: Your OpenAI key (`sk-proj...`)
- `SUPABASE_URL`: e.g., `https://your-proj.supabase.co`
- `SUPABASE_KEY`: Your `anon` public database api string.

Once completed, the pipeline fully runs autonomously upon `git push`!