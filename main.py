import os
import json
import base64
from typing import Optional
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from openai import OpenAI
from pydantic import BaseModel
from supabase import create_client, Client

class ExtractedApplication(BaseModel):
    is_valid_application_update: bool
    company: str
    role: str
    status: str
    reasoning: str

# 1. Reconstruct Gmail Session with Base64 Fallback
def get_gmail_service():
    token_str = os.environ.get("GMAIL_TOKEN")
    if not token_str:
        raise ValueError("GMAIL_TOKEN environment variable not set")
    
    try:
        # Check if it is valid base64
        decoded_bytes = base64.b64decode(token_str).decode('utf-8')
        # If it decoded successfully, it might be JSON, try loading it
        creds_dict = json.loads(decoded_bytes)
    except Exception:
        # If any of the above fails, assume it's just raw JSON
        creds_dict = json.loads(token_str)

    creds = Credentials.from_authorized_user_info(creds_dict)
    
    return build('gmail', 'v1', credentials=creds)

# 2. Get unread job-related emails using targeted ATS domains
def get_job_emails(service):
    query = 'is:unread (from:greenhouse.io OR from:lever.co OR from:workday.com OR from:icims.com OR "application received" OR "interview")'
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
            "body": body[:2000] # truncate
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
        "Your first task is to determine if this email is a personal update regarding a specific job application. "
        "If it is a generic job alert, marketing email, or newsletter, set is_valid_application_update to false and stop. "
        "Otherwise, set it to true and extract the company name, position (role), and status (Applied/Interview/Rejection/Offer). "
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

# 4. Stateful Tracking Layer (Lookup then Act)
def get_supabase_client() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise ValueError("Supabase credentials not set")
    return create_client(url, key)

def find_existing_application(company: str, role: str, thread_id: str, supabase: Client):
    # 1. Try exact thread match
    res = supabase.table('applications').select("*").eq("thread_id", thread_id).execute()
    if res.data:
        return res.data[0]

    # 2. Fallback: Try Company + Role match (Fuzzy equivalent logic)
    res = supabase.table('applications').select("*").ilike("company", f"%{company}%").execute()
    for row in res.data:
        # Ensure role is not empty and loosely matches
        if role and row.get('role') and role.lower() in row['role'].lower():
            return row
            
    return None

def process_application(thread_id: str, extracted: ExtractedApplication):
    supabase = get_supabase_client()
    
    existing = find_existing_application(extracted.company, extracted.role, thread_id, supabase)
    
    if existing:
        print(f"Match found! Updating existing record (ID: {existing['id']})")
        # Update the status, and ensure the thread_id gets updated to the latest thread
        update_data = {
            "status": extracted.status,
            "thread_id": thread_id # Keep thread_id current
        }
        supabase.table('applications').update(update_data).eq("id", existing['id']).execute()
    else:
        print("No existing application found. Creating new record.")
        insert_data = {
            "thread_id": thread_id,
            "status": extracted.status,
            "company": extracted.company,
            "role": extracted.role
        }
        supabase.table('applications').insert(insert_data).execute()

def main():
    print("Starting Stateful tracking engine...")
    
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
                    process_application(email['thread_id'], extracted)
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
