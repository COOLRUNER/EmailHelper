import os
import json
import base64
import datetime
from typing import Optional, Literal
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from openai import OpenAI
from pydantic import BaseModel
from supabase import create_client, Client
from rapidfuzz import fuzz
import re

class ExtractedApplication(BaseModel):
    is_valid_application_update: bool
    company: str
    role: str
    status: Literal["Applied", "Screening", "Interview", "Rejected", "Offer"]
    reasoning: str

# 1. Reconstruct Gmail Session with Base64 Fallback
def get_gmail_service():
    token_str = os.environ.get("GMAIL_TOKEN")
    if not token_str:
        raise ValueError("GMAIL_TOKEN environment variable not set")
    
    try:
        decoded_bytes = base64.b64decode(token_str).decode('utf-8')
        creds_dict = json.loads(decoded_bytes)
    except Exception:
        creds_dict = json.loads(token_str)

    creds = Credentials.from_authorized_user_info(creds_dict)
    
    return build('gmail', 'v1', credentials=creds)

# 2. Get unread job-related emails safely without overly aggressive filtering
def get_job_emails(service):
    query = 'is:unread (application OR apply OR careers OR interview OR offer OR rejection OR "thank you")'
    results = service.users().messages().list(userId='me', q=query).execute()
    messages = results.get('messages', [])
    
    email_data = []
    for msg in messages:
        msg_id = msg['id']
        thread_id = msg['threadId']
        
        full_msg = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
        
        headers = full_msg['payload']['headers']
        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), "No Subject")
        
        body = ""
        if 'parts' in full_msg['payload']:
            for part in full_msg['payload']['parts']:
                if part.get('mimeType') == 'text/plain' and 'data' in part.get('body', {}):
                    body_data = part['body']['data']
                    body += base64.urlsafe_b64decode(body_data).decode('utf-8')
        elif 'body' in full_msg['payload'] and 'data' in full_msg['payload']['body']:
            body_data = full_msg['payload']['body']['data']
            body = base64.urlsafe_b64decode(body_data).decode('utf-8')
            
        email_data.append({
            "id": msg_id,
            "thread_id": thread_id,
            "subject": subject,
            "body": body[:2000] # truncate to save tokens
        })
        
    return email_data

# 3. LLM Extraction with Adversarial Prompting
def analyze_email_with_llm(subject: str, body: str) -> Optional[ExtractedApplication]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not set")
        return None
        
    client = OpenAI(api_key=api_key)
    prompt = f"Subject: {subject}\n\nBody: {body}"
    
    system_prompt = (
        "You are an expert recruitment data analyst. Your task is to extract data regarding job applications.\n"
        "Your first task is to determine if this email is a direct personal communication regarding a specific application.\n\n"
        "Red Flags (set is_valid_application_update to false):\n"
        "- The email lists multiple jobs from different companies.\n"
        "- The email contains phrases like 'Apply Now' or 'Jobs for you'.\n"
        "- The email is a newsletter or generic marketing from LinkedIn, Indeed, or Glassdoor.\n\n"
        "Green Flags:\n"
        "- The email is a confirmation of a specific submission.\n"
        "- The email is a rejection, interview invite, or offer addressed specifically to the user.\n\n"
        "If Green Flags apply, set is_valid_application_update to true and extract company name and position (role). "
        "Also map the status strictly to 'Applied', 'Screening', 'Interview', 'Rejected', or 'Offer'. "
        "Remove suffixes like LLC, Inc, or LTD from the company name. Provide brief reasoning for your choice."
    )

    try:
        completion = client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            response_format=ExtractedApplication,
        )
        return completion.choices[0].message.parsed
    except Exception as e:
        print(f"Failed to analyze email: {e}")
        return None

# 4. Stateful Tracking Layer (Entity Resolution)
def get_supabase_client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise ValueError("Supabase credentials not set")
    return create_client(url, key)

def normalize_company(company: str) -> str:
    # Normalize heavily for fuzzy matching
    # Strip common legal suffixes
    company = re.sub(r'(?i)\b(LLC|Inc|Corp|Ltd|Corporation|Limited)\b\.?', '', company)
    return company.strip()

def find_existing_application(company: str, role: str, thread_id: str, supabase: Client):
    # 1. Exact Thread Match
    res = supabase.table('applications').select("*").eq("thread_id", thread_id).execute()
    if res.data:
        return res.data[0]

    # 2. Fuzzy Entity Match
    normalized_company = normalize_company(company)
    # Query potential candidates from the same company
    candidates = supabase.table('applications').select("*").ilike("company", f"%{normalized_company}%").execute()
    
    for entry in candidates.data:
        if role and entry.get('role'):
            # Compare roles using fuzzy ratio
            score = fuzz.ratio(role.lower(), entry['role'].lower())
            if score > 85: # Threshold for high confidence match
                return entry
            
    return None

def process_application(email_info: dict, extracted: ExtractedApplication):
    supabase = get_supabase_client()
    thread_id = email_info['thread_id']
    subject = email_info['subject']
    
    # Check for existing record
    existing = find_existing_application(extracted.company, extracted.role, thread_id, supabase)
    
    # Prepare the event log payload
    # Timestamp ideally in ISO format UTC
    now_iso = datetime.datetime.utcnow().isoformat() + "Z"
    new_event = {"date": now_iso, "subject": subject, "status": extracted.status}
    
    if existing:
        print(f"Match found! Updating existing record (ID: {existing['id']})")
        # Extend the existed event log, defaulting to empty list if None    
        event_log = existing.get('event_log') or []
        event_log.append(new_event)
        
        update_data = {
            "status": extracted.status,
            "thread_id": thread_id, # Link new thread
            "event_log": event_log
        }
        supabase.table('applications').update(update_data).eq("id", existing['id']).execute()
    else:
        print("No existing application found. Creating new record.")
        insert_data = {
            "thread_id": thread_id,
            "status": extracted.status,
            "company": extracted.company,
            "role": extracted.role,
            "event_log": [new_event]
        }
        supabase.table('applications').insert(insert_data).execute()

def main():
    print("Starting Entity Resolution Tracking Pipeline...")
    
    try:
        service = get_gmail_service()
    except Exception as e:
        print(f"Failed to initialize Gmail service: {e}")
        return

    emails = get_job_emails(service)
    print(f"Found {len(emails)} unread job-related emails.")
    
    for email in emails:
        print(f"\nAnalyzing: {email['subject']}")
        extracted = analyze_email_with_llm(email['subject'], email['body'])
        
        if extracted:
            print(f"Valid Update: {extracted.is_valid_application_update}")
            print(f"Reasoning: {extracted.reasoning}")
            
            if extracted.is_valid_application_update:
                print(f"Extracted -> Company: {extracted.company}, Role: {extracted.role}, Status: {extracted.status}")
                
                try:
                    process_application(email, extracted)
                except Exception as e:
                    print(f"Failed database operations for thread {email['thread_id']}: {e}")
            else:
                print("Skipping - Not a valid application update based on AI reasoning.")
                
            # Always mark as read to prevent infinite loop reprocessing
            try:
                service.users().messages().modify(
                    userId='me', 
                    id=email['id'], 
                    body={'removeLabelIds': ['UNREAD']}
                ).execute()
                print(f"Marked email {email['id']} as read.")
            except Exception as e:
                print(f"Error marking email as read: {e}")

if __name__ == '__main__':
    main()
