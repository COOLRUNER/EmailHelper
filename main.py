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
    company: str
    role: str
    status: str

# 1. Reconstruct Gmail Session
def get_gmail_service():
    token_json = os.environ.get("GMAIL_TOKEN")
    if not token_json:
        raise ValueError("GMAIL_TOKEN environment variable not set")
    
    creds_dict = json.loads(token_json)
    creds = Credentials.from_authorized_user_info(creds_dict)
    
    return build('gmail', 'v1', credentials=creds)

# 2. Get unread job-related emails
def get_job_emails(service):
    query = "is:unread (application OR careers OR interview)"
    results = service.users().messages().list(userId='me', q=query).execute()
    messages = results.get('messages', [])
    
    email_data = []
    for msg in messages:
        msg_id = msg['id']
        thread_id = msg['threadId']
        
        # Get full message to read body
        full_msg = service.users().messages().get(userId='me', id=msg_id, format='full').execute()
        
        # Extract subject and body
        headers = full_msg['payload']['headers']
        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), "No Subject")
        
        # Super simple body extraction (can be improved for multipart)
        body = ""
        if 'parts' in full_msg['payload']:
            # Look for plain text parts
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

# 3. LLM Extraction
def analyze_email_with_llm(subject: str, body: str) -> Optional[ExtractedApplication]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not set")
        return None
        
    client = OpenAI(api_key=api_key)
    prompt = f"Subject: {subject}\n\nBody: {body}"
    
    try:
        completion = client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Extract the company name, position (role), and status (Applied/Interview/Rejection/Offer) from this email. Return strictly valid JSON matching the schema."},
                {"role": "user", "content": prompt}
            ],
            response_format=ExtractedApplication,
        )
        return completion.choices[0].message.parsed
    except Exception as e:
        print(f"Failed to analyze email: {e}")
        return None

# 4. Supabase Upsert
def upsert_to_supabase(data: dict):
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise ValueError("Supabase credentials not set")
    
    supabase: Client = create_client(url, key)
    try:
        response = supabase.table('applications').upsert(data, on_conflict='thread_id').execute()
        print(f"Upserted to Supabase! thread_id: {data.get('thread_id')}")
    except Exception as e:
        print(f"Failed to upsert to Supabase: {e}")

def main():
    print("Starting job application tracker ETL pipeline...")
    
    try:
        service = get_gmail_service()
    except Exception as e:
        print(f"Failed to initialize Gmail service: {e}")
        return

    emails = get_job_emails(service)
    print(f"Found {len(emails)} unread job-related emails.")
    
    for email in emails:
        print(f"Analyzing: {email['subject']}")
        extracted = analyze_email_with_llm(email['subject'], email['body'])
        if extracted:
            print(f"Extracted -> Company: {extracted.company}, Role: {extracted.role}, Status: {extracted.status}")
            
            # Prepare data
            data = {
                "thread_id": email['thread_id'],
                "status": extracted.status,
                "company": extracted.company,
                "role": extracted.role
            }
            upsert_to_supabase(data)
            
            # Mark as read so we don't process it again next run
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
