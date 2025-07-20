from flask import Flask, render_template, request, jsonify, redirect, Response, send_file
import requests
import json
import os
from datetime import datetime
import random
import hashlib
import base64
from premailer import transform
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import uuid
import csv
import io
import re
import psycopg2
from psycopg2 import sql
from urllib.parse import urlparse
import glob

# Add dotenv support for local development
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("SUCCESS: .env file loaded for local development.")
except ImportError:
    print('INFO: python-dotenv not installed; .env file will not be loaded. This is normal for production.')

app = Flask(__name__)

# --- Configuration for Render Deployment ---
PORT = int(os.environ.get("PORT", 5000))
# The BASE_URL is taken from your Render web service's public URL.
# Set this as an environment variable in the Render dashboard.
BASE_URL = os.environ.get("BASE_URL", f"http://localhost:{PORT}")
print(f"INFO: Application will use BASE_URL: {BASE_URL}")

# --- Supabase Database Configuration (Explicit & Robust) ---
# Set these as individual environment variables in your Render dashboard
DB_HOST = os.environ.get("DB_HOST")
DB_NAME = os.environ.get("DB_NAME")
DB_USER = os.environ.get("DB_USER")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
DB_PORT = os.environ.get("DB_PORT")
GROQ_API_KEY = os.getenv('GROQ_API_KEY')

# This will handle the SSL certificate for Supabase.
# We will create this file on Render from an environment variable.
SSL_CERT_PATH = "supabase_ca.crt"


# --- Environment Variable File Creation ---
# This section creates the necessary JSON and certificate files from environment variables
# when the application starts up on Render.

# Google Credentials
if os.environ.get('GOOGLE_CREDENTIALS_JSON_B64'):
    try:
        decoded_credentials = base64.b64decode(os.environ['GOOGLE_CREDENTIALS_JSON_B64']).decode('utf-8')
        with open('credentials.json', 'w') as f:
            f.write(decoded_credentials)
        print("SUCCESS: credentials.json created from environment variable.")
    except Exception as e:
        print(f"ERROR: Could not decode or write GOOGLE_CREDENTIALS_JSON_B64: {e}")

# Google Token
if os.environ.get('GOOGLE_TOKEN_JSON_B64'):
    try:
        decoded_token = base64.b64decode(os.environ['GOOGLE_TOKEN_JSON_B64']).decode('utf-8')
        with open('token.json', 'w') as f:
            f.write(decoded_token)
        print("SUCCESS: token.json created from environment variable.")
    except Exception as e:
        print(f"ERROR: Could not decode or write GOOGLE_TOKEN_JSON_B64: {e}")

# Supabase SSL Certificate
if os.environ.get('SUPABASE_SSL_CERT'):
    try:
        # The certificate is a plain string, no decoding needed.
        with open(SSL_CERT_PATH, 'w') as f:
            f.write(os.environ['SUPABASE_SSL_CERT'])
        print(f"SUCCESS: {SSL_CERT_PATH} created from environment variable.")
    except Exception as e:
        print(f"ERROR: Could not create {SSL_CERT_PATH} from environment variable: {e}")


def get_db_connection():
    """
    Establishes a robust connection to the Supabase PostgreSQL database
    using individual connection parameters and a required SSL certificate.
    """
    if not all([DB_HOST, DB_NAME, DB_USER, DB_PASSWORD, DB_PORT]):
        raise ValueError("One or more database environment variables (DB_HOST, DB_NAME, DB_USER, DB_PASSWORD, DB_PORT) are not set.")

    if not os.path.exists(SSL_CERT_PATH):
        raise FileNotFoundError(f"SSL certificate not found at {SSL_CERT_PATH}. Ensure the SUPABASE_SSL_CERT environment variable is set and correct.")

    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            port=DB_PORT,
            # This is crucial for connecting to Supabase securely
            sslmode='require',
            sslrootcert=SSL_CERT_PATH
        )
        return conn
    except psycopg2.OperationalError as e:
        print(f"ERROR: Could not connect to the Supabase database. Please check your connection details and firewall settings.")
        print(f"Details: {e}")
        raise


def init_db():
    """Initialize PostgreSQL database tables."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Campaigns table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, company_name TEXT NOT NULL,
                product_name TEXT NOT NULL, offer_details TEXT NOT NULL, campaign_type TEXT NOT NULL,
                target_audience TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'draft', total_recipients INTEGER DEFAULT 0
            )
        ''')
        # Email variations table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS email_variations (
                id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, variation_name TEXT NOT NULL,
                subject_line TEXT NOT NULL, email_body TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE
            )
        ''')
        # Recipients table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS recipients (
                id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, email_address TEXT NOT NULL,
                first_name TEXT, last_name TEXT, variation_assigned TEXT NOT NULL,
                sent_at TIMESTAMP, opened_at TIMESTAMP, clicked_at TIMESTAMP, converted_at TIMESTAMP,
                status TEXT DEFAULT 'pending', tracking_id TEXT UNIQUE,
                FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE
            )
        ''')
        # A/B test results table (PostgreSQL uses SERIAL for auto-increment)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ab_results (
                id SERIAL PRIMARY KEY, campaign_id TEXT NOT NULL, variation_name TEXT NOT NULL,
                metric_name TEXT NOT NULL, metric_value REAL NOT NULL,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE
            )
        ''')
        conn.commit()
        cursor.close()
        print("SUCCESS: PostgreSQL database tables checked/created successfully!")
    except Exception as e:
        print(f"FATAL ERROR: Could not initialize the database. The application cannot start.")
        print(f"Details: {e}")
        raise
    finally:
        if conn:
            conn.close()

# Initialize the database when the app starts.
# This is critical for the application to function.
try:
    init_db()
except Exception:
    # Error messages are printed within init_db(), so we just prevent the app from continuing.
    # On Render, this will cause the deployment to fail, which is the desired behavior.
    exit(1)


# --- API & Model Configurations ---
SCOPES = ['https://www.googleapis.com/auth/gmail.send', 'https://www.googleapis.com/auth/gmail.readonly']
LLAMA_MODEL = "meta-llama/Meta-Llama-3-8B-Instruct"
HF_API_URL = f"https://api-inference.huggingface.co/models/{LLAMA_MODEL}"
HF_TOKEN = os.getenv('HUGGINGFACE_API_TOKEN')

# Gmail API functions
def authenticate_gmail():
    """Authenticate and return Gmail service object"""
    creds = None

    # Load existing credentials
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

    # If no valid credentials, get new ones
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # You need to download credentials.json from Google Cloud Console
            if os.path.exists('credentials.json'):
                flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
                creds = flow.run_local_server(port=0)
            else:
                raise Exception("credentials.json file not found. Download it from Google Cloud Console or set GOOGLE_CREDENTIALS_JSON_B64.")

        # Save credentials for next run
        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    return build('gmail', 'v1', credentials=creds)

def create_email_message(to_email, subject, body, tracking_id):
    """Create email message with tracking pixel"""
    message = MIMEMultipart('alternative')
    message['to'] = to_email
    message['subject'] = subject

    # Add tracking pixel to HTML version
    tracking_pixel = f'<img src="{BASE_URL}/pixel/{tracking_id}" width="1" height="1" style="display:none;">'

    # Convert plain text body to HTML and add tracking
    html_body = body.replace('\n', '<br>') + tracking_pixel

    # Add click tracking to links
    html_body = add_click_tracking(html_body, tracking_id)

    # Create both plain text and HTML versions
    text_part = MIMEText(body, 'plain')
    html_part = MIMEText(html_body, 'html')

    message.attach(text_part)
    message.attach(html_part)

    return {'raw': base64.urlsafe_b64encode(message.as_bytes()).decode()}

def add_click_tracking(html_body, tracking_id):
    """Add click tracking to links in email body"""
    # Find all links and replace with tracking links
    def replace_link(match):
        original_url = match.group(1)
        tracking_url = f"{BASE_URL}/click/{tracking_id}?url={original_url}"
        return f'href="{tracking_url}"'

    # Replace href attributes
    html_body = re.sub(r'href="([^"]*)"', replace_link, html_body)

    return html_body

def send_email_via_gmail(service, email_message):
    """Send email using Gmail API"""
    try:
        message = service.users().messages().send(userId="me", body=email_message).execute()
        return {'success': True, 'message_id': message['id']}
    except HttpError as error:
        return {'success': False, 'error': str(error)}

def calculate_ab_metrics(campaign_id):
    """Calculate A/B testing metrics for a campaign"""
    conn = get_db_connection()
    cursor = conn.cursor()

    # Get all variations for this campaign
    cursor.execute(sql.SQL('''
        SELECT DISTINCT variation_assigned FROM recipients
        WHERE campaign_id = %s
    '''), [campaign_id])
    variations = [row[0] for row in cursor.fetchall()]

    metrics = {}

    for variation in variations:
        # Calculate metrics for each variation
        cursor.execute(sql.SQL('''
            SELECT
                COUNT(*) as total_sent,
                COUNT(CASE WHEN opened_at IS NOT NULL THEN 1 END) as opened,
                COUNT(CASE WHEN clicked_at IS NOT NULL THEN 1 END) as clicked,
                COUNT(CASE WHEN converted_at IS NOT NULL THEN 1 END) as converted
            FROM recipients
            WHERE campaign_id = %s AND variation_assigned = %s AND status = 'sent'
        '''), [campaign_id, variation])

        result = cursor.fetchone()
        total_sent, opened, clicked, converted = result

        metrics[variation] = {
            'total_sent': total_sent,
            'opened': opened,
            'clicked': clicked,
            'converted': converted,
            'open_rate': (opened / total_sent * 100) if total_sent > 0 else 0,
            'click_rate': (clicked / total_sent * 100) if total_sent > 0 else 0,
            'conversion_rate': (converted / total_sent * 100) if total_sent > 0 else 0,
            'click_through_rate': (clicked / opened * 100) if opened > 0 else 0
        }

    cursor.close()
    conn.close()
    return metrics

# Original email generation functions (keeping existing code)
def query_huggingface(payload):
    """Query the Hugging Face API using Llama 3 8B"""
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}

    try:
        response = requests.post(HF_API_URL, headers=headers, json=payload, timeout=60)

        if response.status_code == 200:
            return response.json()
        elif response.status_code == 503:
            return {"error": "Model is loading. Please wait and try again."}
        elif response.status_code == 404:
            return {"error": "Model not accessible. Check API token permissions."}
        elif response.status_code == 429:
            return {"error": "Rate limit exceeded. Please wait before trying again."}
        else:
            return {"error": f"API request failed with status {response.status_code}"}

    except requests.exceptions.Timeout:
        return {"error": "Request timed out. Please try again."}
    except requests.exceptions.RequestException as e:
        return {"error": f"Request failed: {str(e)}"}

def generate_email_variations(company_name, product_name, offer_details, campaign_type, target_audience=""):
    """Generate email variations using AI"""
    prompt = f"""<|begin_of_text|><|start_header_id|>system<|end_header_id|>

You are an expert email marketing copywriter. Create two completely different marketing email variations for A/B testing. Each should have a unique approach and tone while maintaining high conversion potential.

<|eot_id|><|start_header_id|>user<|end_header_id|>

Create TWO different marketing email variations for A/B testing:

Company: {company_name}
Product/Service: {product_name}
Campaign Focus: {offer_details}
Type: {campaign_type}
Audience: {target_audience if target_audience else "General customers"}

Requirements:
- Subject line: Under 50 characters, A/B test friendly
- Email body: Professional, persuasive, conversion-focused
- Minimal emoji use (2-3 maximum per email)
- Different psychological triggers for each variation
- Clear call-to-action with trackable links
- Optimized for mobile reading

Format response as:

VARIATION A:
SUBJECT: [subject line]
BODY: [email content]

VARIATION B:
SUBJECT: [subject line]
BODY: [email content]

<|eot_id|><|start_header_id|>assistant<|end_header_id|>"""

    payload = {
        "inputs": prompt,
        "parameters": {
            "max_new_tokens": 800,
            "temperature": 0.7,
            "top_p": 0.9,
            "do_sample": True,
            "return_full_text": False,
            "stop": ["<|eot_id|>"]
        }
    }

    result = query_huggingface(payload)

    if 'error' in result:
        print(f"Hugging Face API Error: {result['error']}. Generating fallback variations.")
        return create_fallback_variations(company_name, product_name, offer_details, campaign_type)

    return result

def create_fallback_variations(company_name, product_name, offer_details, campaign_type):
    """Create fallback variations optimized for A/B testing"""

    variation_a = {
        'subject': f'🚀 {product_name} - Limited Time',
        'body': f'''Hi there,

Big news! We've just launched {product_name} and it's already creating a buzz.

{offer_details}

Here's what makes this special:
✓ Designed specifically for people like you
✓ Proven results from our beta testing
✓ Limited-time exclusive access

Ready to be among the first to experience this?

[Claim Your Spot Now]

Best,
{company_name} Team

P.S. This offer expires soon - don't miss out!'''
    }

    variation_b = {
        'subject': f'You\'re invited: {product_name}',
        'body': f'''Hello!

We have something exciting to share with you.

After months of development, {product_name} is finally here. The early feedback has been incredible, and we think you'll love what we've created.

{offer_details}

What our customers are saying:
"This exceeded all my expectations" - Sarah M.
"Finally, a solution that actually works" - David L.

Want to see what all the excitement is about?

[Discover More]

Warmly,
The {company_name} Team

P.S. Join hundreds of satisfied customers who've already made the switch. 🌟'''
    }

    return [{"generated_text": f"VARIATION A:\nSUBJECT: {variation_a['subject']}\nBODY: {variation_a['body']}\n\nVARIATION B:\nSUBJECT: {variation_b['subject']}\nBODY: {variation_b['body']}"}]

# API Routes
@app.route('/')
def index():
    """Redirects to the A/B Dashboard"""
    return redirect('/ab-dashboard')

@app.route('/ab-dashboard')
def ab_dashboard():
    """A/B testing dashboard"""
    return render_template('ab_dashboard.html', base_url=BASE_URL)

@app.route('/create-campaign', methods=['POST'])
def create_campaign():
    """Create a new A/B testing campaign"""
    try:
        data = request.get_json()

        # Validate required fields
        required_fields = ['company_name', 'product_name', 'offer_details', 'campaign_type']
        if not all(field in data and data[field].strip() for field in required_fields):
            return jsonify({'success': False, 'error': 'Missing required fields'})

        # Generate email variations
        result = generate_email_variations(
            data['company_name'], data['product_name'],
            data['offer_details'], data['campaign_type'],
            data.get('target_audience', '')
        )

        if 'error' in result:
            return jsonify({'success': False, 'error': result['error']})

        # Parse variations
        variations = parse_email_variations(result[0]['generated_text'])

        # Create campaign in database
        conn = get_db_connection()
        cursor = conn.cursor()

        campaign_id = str(uuid.uuid4())
        cursor.execute(sql.SQL('''
            INSERT INTO campaigns (id, name, company_name, product_name, offer_details, campaign_type, target_audience)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        '''), (
            campaign_id,
            f"{data['company_name']} - {data['campaign_type'].title()}",
            data['company_name'],
            data['product_name'],
            data['offer_details'],
            data['campaign_type'],
            data.get('target_audience', '')
        ))

        # Save variations
        for i, variation in enumerate(variations):
            variation_id = str(uuid.uuid4())
            cursor.execute(sql.SQL('''
                INSERT INTO email_variations (id, campaign_id, variation_name, subject_line, email_body)
                VALUES (%s, %s, %s, %s, %s)
            '''), (
                variation_id, campaign_id, f"Variation_{chr(65+i)}",
                variation['subject'], variation['body']
            ))

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({
            'success': True,
            'campaign_id': campaign_id,
            'variations': variations
        })

    except Exception as e:
        print(f"Error in create_campaign: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/upload-recipients', methods=['POST'])
def upload_recipients():
    """Upload recipient list and assign variations using a round-robin method for even distribution."""
    try:
        campaign_id = request.form.get('campaign_id')
        if not campaign_id:
            return jsonify({'success': False, 'error': 'Campaign ID required'})

        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file uploaded'})

        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'No file selected'})

        # Read CSV file
        stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
        csv_input = list(csv.DictReader(stream)) # Read all recipients into a list first

        # Get campaign variations
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(sql.SQL('SELECT variation_name FROM email_variations WHERE campaign_id = %s'), [campaign_id])
        variations = [{'variation_name': row[0]} for row in cursor.fetchall()]

        if not variations:
            return jsonify({'success': False, 'error': 'No variations found for this campaign. Cannot assign recipients.'})

        recipients_added = 0
        
        # --- MODIFIED LOGIC: Round-Robin Assignment ---
        for i, row in enumerate(csv_input):
            email = row.get('email', '').strip()
            if not email:
                continue

            # Assign variation by cycling through the variations list
            variation_index = i % len(variations)
            assigned_variation = variations[variation_index]['variation_name']
            
            tracking_id = str(uuid.uuid4())

            cursor.execute(sql.SQL('''
                INSERT INTO recipients (id, campaign_id, email_address, first_name, last_name, variation_assigned, tracking_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            '''), (
                str(uuid.uuid4()), campaign_id, email,
                row.get('first_name', ''), row.get('last_name', ''),
                assigned_variation, tracking_id
            ))
            recipients_added += 1

        # Update campaign total recipients
        cursor.execute(sql.SQL('UPDATE campaigns SET total_recipients = %s WHERE id = %s'), (recipients_added, campaign_id))

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({
            'success': True,
            'recipients_added': recipients_added,
            'message': f'Successfully uploaded and assigned {recipients_added} recipients evenly.'
        })

    except Exception as e:
        print(f"Error in upload_recipients: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/send-campaign', methods=['POST'])
def send_campaign():
    """Send A/B testing campaign"""
    try:
        data = request.get_json()
        campaign_id = data.get('campaign_id')
        if not campaign_id:
            return jsonify({'success': False, 'error': 'Campaign ID required'})

        # Authenticate Gmail
        try:
            gmail_service = authenticate_gmail()
        except Exception as e:
            return jsonify({'success': False, 'error': f'Gmail authentication failed: {str(e)}'})

        # Get campaign and variations
        conn = get_db_connection()
        cursor = conn.cursor()

        # Get email variations
        cursor.execute(sql.SQL('''
            SELECT variation_name, subject_line, email_body FROM email_variations WHERE campaign_id = %s
        '''), [campaign_id])
        variations = {row[0]: {'subject': row[1], 'body': row[2]} for row in cursor.fetchall()}

        # Get recipients
        cursor.execute(sql.SQL('''
            SELECT id, email_address, first_name, variation_assigned, tracking_id FROM recipients WHERE campaign_id = %s AND status = 'pending'
        '''), [campaign_id])
        recipients = cursor.fetchall()

        sent_count = 0
        errors = []
        print(f"--- Starting to send campaign {campaign_id} to {len(recipients)} recipients ---")

        for recipient_id, email, first_name, variation, tracking_id in recipients:
            print(f"\nProcessing recipient: {email} for variation: {variation}")
            try:
                # Get variation content
                variation_content = variations[variation]

                # Personalize content
                subject = variation_content['subject']
                body = variation_content['body']
                if first_name:
                    body = body.replace('Hi there', f'Hi {first_name}')
                    body = body.replace('Hello!', f'Hello {first_name}!')

                # Create and send email
                print(f" > Creating email message for {email}...")
                email_message = create_email_message(email, subject, body, tracking_id)

                print(f" > Attempting to send via Gmail API...")
                result = send_email_via_gmail(gmail_service, email_message)

                if result['success']:
                    print(f" > SUCCESS: Email sent. Updating status to 'sent'.")
                    # Update recipient status and explicitly set tracking timestamps to NULL for new tracking
                    cursor.execute(sql.SQL('''
                        UPDATE recipients
                        SET status = 'sent', sent_at = CURRENT_TIMESTAMP, opened_at = NULL, clicked_at = NULL, converted_at = NULL
                        WHERE id = %s
                    '''), [recipient_id])
                    sent_count += 1
                else:
                    print(f" > FAILED: Gmail API returned an error: {result['error']}")
                    errors.append(f'{email}: {result["error"]}')
                    cursor.execute(sql.SQL('''
                        UPDATE recipients SET status = 'failed' WHERE id = %s
                    '''), [recipient_id])
            except Exception as e:
                print(f" > FAILED: An exception occurred: {str(e)}")
                errors.append(f'{email}: {str(e)}')

        # Commit all the database changes at the end of the loop
        conn.commit()
        print(f"--- Campaign sending finished. Committing changes to database. ---")

        # Update campaign status and total recipients (if not already handled by upload)
        cursor.execute(sql.SQL("UPDATE campaigns SET status = %s WHERE id = %s"), ('sent', campaign_id))
        conn.commit()

        cursor.close()
        conn.close()

        if errors:
            return jsonify({
                'success': False,
                'message': f'Campaign sent with {sent_count} successes and {len(errors)} errors.',
                'errors': errors
            })
        else:
            return jsonify({
                'success': True,
                'message': f'Campaign sent successfully to {sent_count} recipients.'
            })

    except Exception as e:
        print(f"Error in send_campaign: {e}")
        return jsonify({'success': False, 'error': str(e)})
@app.route('/campaign-results/<campaign_id>')
def campaign_results(campaign_id):
    """Get A/B testing results for a campaign"""
    conn = None # Initialize conn to None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(sql.SQL('SELECT name, status, total_recipients FROM campaigns WHERE id = %s'), [campaign_id])
        campaign = cursor.fetchone()

        cursor.close()
        # conn.close() # Close conn in finally block

        if not campaign:
            return jsonify({'success': False, 'error': 'Campaign not found'})

        metrics = calculate_ab_metrics(campaign_id) # This function gets its own connection

        return jsonify({
            'success': True,
            'campaign': {
                'name': campaign[0],
                'status': campaign[1],
                'total_recipients': campaign[2]
            },
            'metrics': metrics
        })

    except Exception as e:
        print(f"Error in campaign_results: {e}")
        return jsonify({'success': False, 'error': str(e)})
    finally:
        if conn:
            conn.close()

@app.route('/campaigns')
def list_campaigns():
    """List all campaigns"""
    conn = None # Initialize conn to None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(sql.SQL('SELECT id, name, status, total_recipients, created_at FROM campaigns ORDER BY created_at DESC'))
        campaigns = [
            {
                'id': row[0],
                'name': row[1],
                'status': row[2],
                'total_recipients': row[3],
                'created_at': row[4]
            }
            for row in cursor.fetchall()
        ]

        cursor.close()
        # conn.close() # Close conn in finally block

        return jsonify({'success': True, 'campaigns': campaigns})

    except Exception as e:
        print(f"Error in list_campaigns: {e}")
        return jsonify({'success': False, 'error': str(e)})
    finally:
        if conn:
            conn.close()


# Tracking routes
@app.route('/pixel/<tracking_id>')
def tracking_pixel(tracking_id):
    """Track email opens"""
    conn = None # Initialize conn to None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(sql.SQL('''
            UPDATE recipients
            SET opened_at = CURRENT_TIMESTAMP
            WHERE tracking_id = %s AND opened_at IS NULL
        '''), [tracking_id])

        conn.commit()
        cursor.close()
        # conn.close() # Close conn in finally block

        # Return 1x1 transparent pixel
        pixel = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
        return Response(pixel, mimetype='image/gif')

    except Exception as e:
        print(f"Error tracking pixel for {tracking_id}: {e}")
        # Return pixel even if tracking fails, to not break email client display
        pixel = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
        return Response(pixel, mimetype='image/gif')
    finally:
        if conn:
            conn.close()

@app.route('/click/<tracking_id>')
def track_click(tracking_id):
    """Track email clicks and redirect"""
    conn = None # Initialize conn to None
    try:
        original_url = request.args.get('url', BASE_URL) # Fallback to BASE_URL

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(sql.SQL('''
            UPDATE recipients
            SET clicked_at = CURRENT_TIMESTAMP
            WHERE tracking_id = %s AND clicked_at IS NULL
        '''), [tracking_id])

        conn.commit()
        cursor.close()
        # conn.close() # Close conn in finally block

        return redirect(original_url)

    except Exception as e:
        print(f"Error tracking click for {tracking_id}: {e}")
        return redirect(BASE_URL) # Redirect to BASE_URL on error
    finally:
        if conn:
            conn.close()

def parse_email_variations(generated_text):
    """Parse generated text into variation objects"""
    variations = []

    # Split by VARIATION markers
    parts = generated_text.split('VARIATION')

    for i, part in enumerate(parts[1:], 1):
        if i > 2:
            break # Only take first two variations

        lines = part.strip().split('\n')
        subject = ""
        body_lines = []
        body_started = False

        for line in lines:
            line = line.strip()
            if line.upper().startswith('SUBJECT:'):
                subject = line[8:].strip()
            elif line.upper().startswith('BODY:'):
                body_started = True
            elif body_started: # Any non-empty line after BODY: is part of the body
                body_lines.append(line)

        body = '\n'.join(body_lines).strip() # .strip() removes leading/trailing whitespace

        if subject and body:
            variations.append({
                'subject': subject,
                'body': body
            })

    # Fallback if parsing fails or less than 2 variations are generated
    if len(variations) < 2:
        print("Warning: Less than two variations parsed. Using fallback variations.")
        variations = [
            {
                'subject': 'Exclusive Offer Inside 🎯',
                'body': 'We have something special for you...\n\n[Learn More]'
            },
            {
                'subject': 'You\'re Going to Love This',
                'body': 'This is exactly what you\'ve been waiting for...\n\n[Discover More]'
            }
        ]

    return variations

# --- Finalize Mails Endpoints ---
import glob

@app.route('/list-template-categories')
def list_template_categories():
    base_dir = os.path.join(os.getcwd(), 'html_templates')
    categories = [d for d in os.listdir(base_dir) if os.path.isdir(os.path.join(base_dir, d))]
    return jsonify({'success': True, 'categories': categories})

@app.route('/list-template-files/<category>')
def list_template_files(category):
    base_dir = os.path.join(os.getcwd(), 'html_templates', category)
    if not os.path.exists(base_dir):
        return jsonify({'success': False, 'error': 'Category not found'})
    files = [f for f in os.listdir(base_dir) if f.endswith('.html')]
    return jsonify({'success': True, 'files': files})

@app.route('/get-template-content/<category>/<filename>')
def get_template_content(category, filename):
    base_dir = os.path.join(os.getcwd(), 'html_templates', category)
    file_path = os.path.join(base_dir, filename)
    if not os.path.exists(file_path):
        return jsonify({'success': False, 'error': 'Template not found'})
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    return jsonify({'success': True, 'content': content})

@app.route('/get-campaign-variants/<campaign_id>')
def get_campaign_variants(campaign_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(sql.SQL('SELECT variation_name, subject_line, email_body FROM email_variations WHERE campaign_id = %s'), [campaign_id])
    variants = [{'name': row[0], 'subject': row[1], 'body': row[2]} for row in cursor.fetchall()]
    cursor.close()
    conn.close()
    return jsonify({'success': True, 'variants': variants})


@app.route('/integrate-content-template', methods=['POST'])
def integrate_content_template():
    data = request.get_json()
    content = data.get('content')
    template_html = data.get('template_html')

    if not content or not template_html:
        return jsonify({'success': False, 'error': 'Missing content or template_html'})

    groq_api_key = os.getenv('GROQ_API_KEY')
    if not groq_api_key:
        return jsonify({'success': False, 'error': 'GROQ_API_KEY not set in environment'})

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {groq_api_key}",
        "Content-Type": "application/json"
    }

    prompt = f"""You are an expert email formatter.
Integrate the following content into the provided HTML template.
Make sure to preserve styles and formatting.

CONTENT:
{content}

TEMPLATE_HTML:
{template_html}
"""

    payload = {
        "model": "llama3-70b-8192",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7
    }

    try:
        print("Payload sent to Groq:")
        print(json.dumps(payload, indent=2))

        response = requests.post(url, headers=headers, json=payload)
        print("Raw Groq response:")
        print(response.text)

        result = response.json()
        raw_html = result['choices'][0]['message']['content']



# Step 1: Remove markdown-style HTML block markers
        if "```html" in raw_html:
            raw_html = raw_html.split("```html", 1)[-1]
        if "```" in raw_html:
            raw_html = raw_html.split("```", 1)[0]

# Step 2: Strip typical AI wrap-up lines
        wrapup_phrases = [
        "Let me know if you need any further assistance",
        "Let me know if you need anything else",
        "Hope this helps",
        "Have a great day",
        "Happy to help"
        ]
        for phrase in wrapup_phrases:
            if phrase.lower() in raw_html.lower():
                raw_html = raw_html[:raw_html.lower().find(phrase.lower())].strip()

# Step 3: Remove "Here is..." intro text
        raw_html = raw_html.strip()
        if raw_html.lower().startswith("here is"):
            raw_html = raw_html[raw_html.find("<"):]

# Inline CSS
        finalized_html = transform(raw_html)


        return jsonify({'success': True, 'finalized_html': finalized_html})

    except Exception as e:
        print(f"Error parsing Groq response: {e}")
        return jsonify({'success': False, 'error': f'Parsing error: {str(e)}'})


"""@app.route('/send-finalized-mail', methods=['POST'])
def send_finalized_mail():
    # Expects: subject, html_body, sender_csv (file upload)
    subject = request.form.get('subject')
    html_body = request.form.get('html_body')
    if 'sender_csv' not in request.files:
        return jsonify({'success': False, 'error': 'No CSV file uploaded'})
    file = request.files['sender_csv']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'})
    stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
    csv_input = csv.DictReader(stream)
    # Use Gmail API to send emails (reuse authenticate_gmail and create_email_message)
    service = authenticate_gmail()
    sent_count = 0
    for row in csv_input:
        to_email = row.get('email', '').strip()
        if not to_email:
            continue
        msg = create_email_message(to_email, subject, html_body, tracking_id=str(uuid.uuid4()))
        result = send_email_via_gmail(service, msg)
        if result.get('success'):
            sent_count += 1
    return jsonify({'success': True, 'sent_count': sent_count})"""

@app.route('/send-optimized-schedule', methods=['POST'])
def send_optimized_schedule():
    """Send finalized emails to customers based on open-time batches"""
    
    print("📩 Starting optimized send route...")
    
    try:
        # Validate file upload
        if 'customer_csv' not in request.files:
            print("❌ No CSV file uploaded")
            return jsonify({'success': False, 'error': 'CSV file not uploaded'})

        file = request.files['customer_csv']
        if file.filename == '':
            print("❌ Empty filename")
            return jsonify({'success': False, 'error': 'No file selected'})

        subject = request.form.get('subject', '').strip()
        html_body = request.form.get('html_body', '').strip()

        print(f"✅ Subject: {subject[:50] if subject else 'None'}...")
        print(f"✅ HTML body length: {len(html_body) if html_body else 0}")

        if not subject:
            return jsonify({'success': False, 'error': 'Subject is required'})
        if not html_body:
            return jsonify({'success': False, 'error': 'HTML body is required'})

        # Import required libraries
        import pandas as pd
        import datetime
        import uuid
        import io

        # Read CSV with better error handling
        try:
            # Read file content
            file_content = file.read()
            file.seek(0)  # Reset file pointer
            
            # Try different encodings
            encodings = ['utf-8', 'utf-8-sig', 'latin1', 'cp1252']
            df = None
            
            for encoding in encodings:
                try:
                    file_string = file_content.decode(encoding)
                    df = pd.read_csv(io.StringIO(file_string))
                    print(f"✅ CSV loaded successfully with encoding: {encoding}")
                    break
                except (UnicodeDecodeError, pd.errors.ParserError):
                    continue
            
            if df is None:
                return jsonify({'success': False, 'error': 'Could not read CSV file. Please check file encoding.'})
                
        except Exception as csv_error:
            print(f"❌ CSV Error: {csv_error}")
            return jsonify({'success': False, 'error': f'CSV reading error: {str(csv_error)}'})

        # Validate CSV structure
        print(f"📊 CSV Info: {len(df)} rows, columns: {list(df.columns)}")
        
        # Check for required columns (case-insensitive)
        df_columns_lower = [col.lower().strip() for col in df.columns]
        email_col = None
        opentime_col = None
        
        for i, col in enumerate(df_columns_lower):
            if 'email' in col:
                email_col = df.columns[i]
            elif 'open' in col and 'time' in col:
                opentime_col = df.columns[i]
        
        if not email_col or not opentime_col:
            return jsonify({
                'success': False, 
                'error': f'Required columns not found. Found: {list(df.columns)}. Need: email, opentime'
            })

        print(f"✅ Using columns: email='{email_col}', opentime='{opentime_col}'")

        # Clean the data
        df = df.dropna(subset=[email_col, opentime_col])
        df[email_col] = df[email_col].astype(str).str.strip()
        df[opentime_col] = df[opentime_col].astype(str).str.strip()
        
        # Remove empty rows
        df = df[(df[email_col] != '') & (df[opentime_col] != '')]
        
        print(f"📊 After cleaning: {len(df)} valid rows")

        # Define batch times (simplified)
        BATCH_SEND_TIMES = {
            "Morning Batch 1": (8, 0),
            "Morning Batch 2": (11, 0), 
            "Evening Batch 1": (14, 0),
            "Evening Batch 2": (19, 0),
            "Night Batch": (23, 0)  # Simplified to match your data
        }

        # Test Gmail authentication early
        try:
            service = authenticate_gmail()
            if not service:
                print("❌ Gmail authentication failed")
                return jsonify({'success': False, 'error': 'Gmail authentication failed'})
            print("✅ Gmail authenticated successfully")
        except Exception as auth_error:
            print(f"❌ Gmail auth error: {auth_error}")
            return jsonify({'success': False, 'error': f'Gmail authentication error: {str(auth_error)}'})

        # Process emails into batches
        batches = {batch: [] for batch in BATCH_SEND_TIMES}
        processed_count = 0
        error_count = 0
        
        for index, row in df.iterrows():
            try:
                email = str(row[email_col]).strip()
                opentime_str = str(row[opentime_col]).strip()
                
                # Basic email validation
                if '@' not in email or '.' not in email.split('@')[-1]:
                    print(f"⚠️  Invalid email format: {email}")
                    error_count += 1
                    continue
                
                # Parse time - handle multiple formats
                try:
                    # Try HH:MM format first
                    if ':' in opentime_str:
                        open_time = datetime.datetime.strptime(opentime_str, "%H:%M").time()
                    # Try H:MM format
                    elif '.' in opentime_str:
                        open_time = datetime.datetime.strptime(opentime_str, "%H.%M").time()
                    else:
                        raise ValueError("Unknown time format")
                        
                except ValueError as time_error:
                    print(f"⚠️  Invalid time format '{opentime_str}' for {email}: {time_error}")
                    error_count += 1
                    continue
                
                # Classify into batches (simplified logic)
                hour = open_time.hour
                if 6 <= hour < 10:
                    batch = "Morning Batch 1"
                elif 10 <= hour < 12:
                    batch = "Morning Batch 2" 
                elif 12 <= hour < 17:
                    batch = "Evening Batch 1"
                elif 17 <= hour < 21:
                    batch = "Evening Batch 2"
                else:  # 21-6 (night hours)
                    batch = "Night Batch"
                
                batches[batch].append({
                    'email': email,
                    'opentime': opentime_str,
                    'hour': hour
                })
                processed_count += 1
                
            except Exception as row_error:
                print(f"⚠️  Error processing row {index}: {row_error}")
                error_count += 1
                continue

        print(f"📊 Processing complete: {processed_count} valid, {error_count} errors")

        # Create batch summary
        batch_summary = []
        total_emails = 0
        
        for batch_name, emails in batches.items():
            if emails:
                count = len(emails)
                send_hour, send_minute = BATCH_SEND_TIMES[batch_name]
                batch_summary.append({
                    'batch': batch_name,
                    'count': count,
                    'send_time': f"{send_hour:02d}:{send_minute:02d}",
                    'emails': [e['email'] for e in emails[:3]]  # Show first 3 emails
                })
                total_emails += count

        if total_emails == 0:
            return jsonify({
                'success': False,
                'error': 'No valid emails found to process',
                'processed_count': processed_count,
                'error_count': error_count
            })

        print(f"✅ Batch summary: {len(batch_summary)} batches, {total_emails} total emails")

        # Instead of threading, send emails immediately for testing
        # You can modify this later to use background processing
        
        all_results = []
        
        for batch_name, emails in batches.items():
            if not emails:
                continue
                
            print(f"\n📤 Processing batch '{batch_name}' with {len(emails)} emails")
            batch_results = {
                'batch': batch_name,
                'total': len(emails),
                'sent': 0,
                'failed': 0,
                'errors': []
            }
            
            # Send emails in this batch
            for email_data in emails:
                try:
                    email = email_data['email']
                    
                    # Create email message
                    msg = create_email_message(
                        to_email=email,
                        subject=subject,
                        html_body=html_body,
                        message_id=str(uuid.uuid4())
                    )
                    
                    # Send email
                    result = send_email_via_gmail(service, msg)
                    
                    if result and result.get('success'):
                        print(f"✅ Sent to {email}")
                        batch_results['sent'] += 1
                    else:
                        error_msg = result.get('error', 'Unknown error') if result else 'No response'
                        print(f"❌ Failed to send to {email}: {error_msg}")
                        batch_results['failed'] += 1
                        batch_results['errors'].append(f"{email}: {error_msg}")
                        
                except Exception as send_error:
                    print(f"❌ Exception sending to {email_data['email']}: {send_error}")
                    batch_results['failed'] += 1
                    batch_results['errors'].append(f"{email_data['email']}: {str(send_error)}")
            
            all_results.append(batch_results)
            
            # Small delay between batches
            if len(emails) > 10:  # Only delay for larger batches
                import time
                time.sleep(1)

        # Prepare final response
        success_response = {
            'success': True,
            'message': f'Email campaign processed successfully',
            'summary': {
                'total_processed': processed_count,
                'total_errors': error_count,
                'total_sent': sum(r['sent'] for r in all_results),
                'total_failed': sum(r['failed'] for r in all_results),
                'batches_processed': len([r for r in all_results if r['total'] > 0])
            },
            'batch_results': all_results,
            'batch_summary': batch_summary
        }
        
        print("✅ Send optimized schedule completed successfully")
        return jsonify(success_response)
        
    except Exception as e:
        error_msg = f"Unexpected error in send_optimized_schedule: {str(e)}"
        print(f"❌ {error_msg}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': error_msg})



if __name__ == '__main__':
    # This block will now only run when you execute 'python final.py' directly.
    # The init_db() call for Gunicorn is moved above.
    print("🧪 A/B Testing Email Marketing App")
    print("✉️  Gmail API Integration Ready")
    print("📊 Campaign Tracking Enabled")
    print("🎯 Endpoints:")
    print(f"   - Main: {BASE_URL}")
    print(f"   - Dashboard: {BASE_URL}/ab-dashboard")
    print(f"   - Campaigns: {BASE_URL}/campaigns")

    app.run(debug=True, host='0.0.0.0', port=PORT)
