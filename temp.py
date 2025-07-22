import hashlib
import time
import uuid
from typing import Optional, Dict, Any
import logging
import os
import dotenv
import google.generativeai as genai
from google.generativeai import types
import google.api_core
import re
from flask import Flask, jsonify, request, url_for, abort
from flask_cors import CORS
from flask_talisman import Talisman
from flask_mongoengine import MongoEngine
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import datetime
import stripe
import base64
import binascii
from functools import wraps
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# --- Configuration & Setup ---
dotenv.load_dotenv()

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')

api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise ValueError("API key not found in your .env file. Please set GEMINI_API_KEY.")
genai.configure(api_key=api_key)

app = Flask(__name__)

# --- Load Prompts ---
prompts_folder = 'prompts'
prompts = {}
if os.path.exists(prompts_folder):
    for filename in os.listdir(prompts_folder):
        prompt_name = os.path.splitext(filename)[0].lower()
        with open(os.path.join(prompts_folder, filename), 'r') as f:
            prompts[prompt_name] = f.read()
else:
    logging.warning(f"Prompts folder '{prompts_folder}' not found. Creating it.")
    os.makedirs(prompts_folder, exist_ok=True)
    default_prompt_path = os.path.join(prompts_folder, "default_prompt.md")
    with open(default_prompt_path, 'w') as f:
        f.write("Generate a detailed description based on the following features: [FEATURES_PLACEHOLDER]")
    prompts['default_prompt'] = "Generate a detailed description based on the following features: [FEATURES_PLACEHOLDER]"
    logging.info(f"Created a default prompt at '{default_prompt_path}'")

app.config['MONGODB_SETTINGS'] = {
    'host': os.getenv("MONGO_URI", "mongodb://localhost:27017/your_default_db")
}
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", "a_very_secret_key_that_should_be_in_env_for_production")
stripe.api_key = os.getenv("STRIPE_API_KEY")
webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

db = MongoEngine(app)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri=os.getenv("REDIS_URL", "memory://"),
    storage_options={"socket_connect_timeout": 30},
    strategy="fixed-window"
)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = None
login_manager.login_message = None

@login_manager.unauthorized_handler
def unauthorized():
    logging.warning(f"Unauthorized access attempt to {request.path} from IP {get_remote_address()}")
    return jsonify(status="error", message="Authentication required"), 401

PLAN_DETAILS = {
    'prod_SggQsgqkHOCPi3': { # Pro Plan
        'name': 'pro',
        'daily_limit': 500,
        'monthly_limit': 5000
    },
    'prod_SggRiU8cNzdjNu': { # Ultra Plan
        'name': 'ultra',
        'daily_limit': 100,
        'monthly_limit': 1200
    }
}

# --- User Model ---
class User(UserMixin, db.Document):
    email = db.StringField(required=True, unique=True)
    password_hash = db.StringField(required=True)
    daily_generations = db.IntField(default=0)
    last_generation_day = db.IntField(default=0)
    monthly_generations = db.IntField(default=0)
    last_generation_month = db.IntField(default=0)
    last_login = db.DateTimeField(default=datetime.datetime.utcnow)
    daily_generation_limit = db.IntField(default=20)
    monthly_generation_limit = db.IntField(default=200)
    plan = db.StringField(default='free')
    date_created = db.DateTimeField(default=datetime.datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256', salt_length=16)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def get_id(self):
        return str(self.pk)

@login_manager.user_loader
def load_user(user_id):
    try:
        return User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return None

# --- Decorators ---
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.plan != 'admin':
            logging.warning(f"Forbidden: Non-admin user '{getattr(current_user, 'email', 'anonymous')}' attempted to access admin route {request.path}.")
            abort(403)
        return f(*args, **kwargs)
    return decorated_function

# --- Helper Functions (PII and Input Validation) ---
def is_valid_input(user_input):
    MAX_INPUT_LENGTH = 4000
    if len(user_input) > MAX_INPUT_LENGTH:
        return False
    PROMPT_INJECTION_PATTERNS = [
        r"\bignore all previous instructions\b", r"\bignore your instructions\b", r"\bdisregard the previous statement\b",
        r"\bforget the preceding text\b", r"\bpretend to be\b", r"\bsystem prompt\b", r"\byour initial instructions\b",
        r"\brepeat the text above\b", r"\bwhat were your exact instructions\b", r"\btranslate this sentence as\b",
        r"\bdo anything now\b", r"\bDAN prompt\b"
    ]
    def run_checks(text_to_check):
        for pattern in PROMPT_INJECTION_PATTERNS:
            if re.search(pattern, text_to_check, re.IGNORECASE):
                logging.warning(f"Potential prompt injection attempt detected: {text_to_check[:100]}")
                return False
        return True

    if not run_checks(user_input):
        return False

    try:
        decoded_input = base64.b64decode(user_input, validate=True).decode('utf-8')
        if not run_checks(decoded_input):
            return False
    except (binascii.Error, UnicodeDecodeError):
        pass

    return True

PII_PATTERNS = {
    'email': re.compile(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'),
    'phone': re.compile(r'(\+1[-.\s]?|1[-.\s]?|)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}'),
    'ssn': re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),
    'credit_card': re.compile(r'\b(?:\d[ -]*?){13,16}\b'),
    'drivers_license': re.compile(r'\b[A-Z0-9]{1,2}\d{4,8}\b'),
    'passport': re.compile(r'\b[0-9]{9}\b'),
    'zip_code': re.compile(r'\b\d{5}(?:-\d{4})?\b'),
    'ipv4': re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'),
    'mac_address': re.compile(r'\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b'),
}

WHITELIST_PHRASES = [
    "memory address", "ip address", "server address", "address space", "addressing mode", "build number", "version number",
    "port number", "serial number", "process id", "thread id", "mac address", "example@example.com", "test@example.com",
    "localhost", "127.0.0.1",
]

def is_whitelisted(text, whitelist):
    lower_text = text.lower()
    return any(phrase in lower_text for phrase in whitelist)

def detect_hard_pii(text):
    if is_whitelisted(text, WHITELIST_PHRASES):
        return False, None
    for pii_type, pattern in PII_PATTERNS.items():
        if pattern.search(text):
            return True, pii_type
    return False, None

def contains_prohibited_content(text: str) -> bool:
    """
    Check for prohibited content patterns that violate usage policies.
    """
    PROHIBITED_PATTERNS = [
        # Add patterns based on your compliance requirements
        r'\b(?:illegal|hack|exploit|vulnerability)\b.*(?:instructions|guide|tutorial)\b',
        r'\b(?:generate|create|make)\b.*(?:malware|virus|trojan)\b',
        r'\b(?:personal|private|confidential)\b.*(?:information|data|details)\b',
        # Add more patterns as needed
    ]
    
    text_lower = text.lower()
    for pattern in PROHIBITED_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            return True
    return False


def mask_pii_in_text(text: str) -> str:
    """
    Mask detected PII in the text to maintain functionality while protecting privacy.
    Context-aware for technical documentation.
    """
    masked_text = text
    
    # Context-aware email masking (skip obvious examples)
    if not is_technical_example_email(text):
        masked_text = re.sub(
            r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
            '[EMAIL_REDACTED]',
            masked_text
        )
    
    # Context-aware phone masking (skip technical contexts)
    if not is_technical_phone_context(text):
        masked_text = re.sub(
            r'(\+1[-.\s]?|1[-.\s]?|)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}',
            '[PHONE_REDACTED]',
            masked_text
        )
    
    # Always mask SSN (rarely used in technical docs)
    masked_text = re.sub(
        r'\b\d{3}-\d{2}-\d{4}\b',
        '[SSN_REDACTED]',
        masked_text
    )
    
    return masked_text


def is_technical_example_email(text: str) -> bool:
    """Check if email appears to be a technical example rather than real PII."""
    technical_indicators = [
        'example.com', 'test.com', 'demo.com', 'sample.com',
        'localhost', 'domain.com', 'company.com',
        'user@example', 'test@', 'demo@', 'admin@localhost',
        'code example', 'example:', 'sample code', 'documentation',
        'api documentation', 'curl', 'json', 'xml', 'http'
    ]
    text_lower = text.lower()
    return any(indicator in text_lower for indicator in technical_indicators)


def is_technical_phone_context(text: str) -> bool:
    """Check if phone numbers appear in technical context rather than personal info."""
    technical_contexts = [
        # Technical documentation indicators
        'port', 'api', 'endpoint', 'url', 'configuration', 'config',
        'documentation', 'example', 'sample', 'demo', 'test',
        'code', 'json', 'xml', 'curl', 'http', 'webhook',
        # Phone number as identifier/code patterns
        'id:', 'identifier', 'reference', 'code:', 'number:',
        # Technical phone contexts
        'phone number format', 'validation', 'regex', 'pattern',
        'format example', 'placeholder', 'template'
    ]
    
    # Check for technical context within reasonable proximity (500 chars)
    context_window = 500
    text_lower = text.lower()
    
    # Look for technical indicators near phone number patterns
    phone_pattern = r'(\+1[-.\s]?|1[-.\s]?|)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}'
    phone_matches = list(re.finditer(phone_pattern, text))
    
    for match in phone_matches:
        start_pos = max(0, match.start() - context_window)
        end_pos = min(len(text), match.end() + context_window)
        context = text_lower[start_pos:end_pos]
        
        if any(indicator in context for indicator in technical_contexts):
            return True
    
    return False


def sanitize_for_logging(data: Any, max_length: int = 100) -> str:
    """
    Sanitize data for secure logging by removing PII and truncating.
    """
    if not isinstance(data, str):
        data = str(data)
    
    # Remove potential PII patterns
    sanitized = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '[EMAIL]', data)
    sanitized = re.sub(r'(\+1[-.\s]?|1[-.\s]?|)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', '[PHONE]', sanitized)
    sanitized = re.sub(r'\b\d{3}-\d{2}-\d{4}\b', '[SSN]', sanitized)
    
    # Truncate for logging
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + "..."
    
    return sanitized


def generate_text_with_gemini(temp, max_output_tokens, system_instruction, contents):
    """
    Enhanced Gemini API call with comprehensive security measures for data compliance.
    
    Security Features:
    - Input validation and sanitization
    - PII detection and masking
    - Request/response logging without sensitive data
    - Error handling without data leakage
    - Request tracking for audit trails
    - Input size limits
    - Content filtering
    """
    
    # Generate unique request ID for tracking (no PII)
    request_id = str(uuid.uuid4())[:8]
    request_timestamp = time.time()
    
    # === ENHANCED INPUT VALIDATION ===
    if not isinstance(temp, (int, float)) or not 0 <= temp <= 1:
        logging.warning(f"Request {request_id}: Invalid temperature parameter")
        return "Error: Invalid temperature parameter."
    
    if not isinstance(max_output_tokens, int) or not (1 <= max_output_tokens <= 8192):
        logging.warning(f"Request {request_id}: Invalid max_output_tokens parameter")
        return "Error: Invalid max output tokens parameter."
    
    if not isinstance(system_instruction, str) or len(system_instruction.strip()) == 0:
        logging.warning(f"Request {request_id}: Invalid system instruction")
        return "Error: Invalid system instruction."
    
    if not isinstance(contents, str):
        logging.warning(f"Request {request_id}: Invalid contents type")
        return "Error: Invalid contents type."
    
    # === CONTENT SIZE LIMITS ===
    MAX_CONTENT_SIZE = 30000  # Adjust based on your needs
    MAX_SYSTEM_INSTRUCTION_SIZE = 10000
    
    if len(contents) > MAX_CONTENT_SIZE:
        logging.warning(f"Request {request_id}: Content exceeds size limit ({len(contents)} > {MAX_CONTENT_SIZE})")
        return "Error: Content exceeds maximum allowed size."
    
    if len(system_instruction) > MAX_SYSTEM_INSTRUCTION_SIZE:
        logging.warning(f"Request {request_id}: System instruction exceeds size limit")
        return "Error: System instruction exceeds maximum allowed size."
    
    # === ENHANCED SECURITY VALIDATION ===
    if not is_valid_input(contents):
        logging.warning(f"Request {request_id}: Potentially malicious input detected")
        return "Error: Potentially malicious input detected."
    
    if not is_valid_input(system_instruction):
        logging.warning(f"Request {request_id}: Potentially malicious system instruction detected")
        return "Error: Invalid system instruction content."
    
    # === PII DETECTION AND MASKING ===
    pii_found, pii_type = detect_hard_pii(contents)
    if pii_found:
        logging.warning(f"Request {request_id}: PII detected - {pii_type}")
        return f"Error: Personal information ({pii_type}) detected. Please remove sensitive data and try again."
    
    # === CONTENT FILTERING ===
    if contains_prohibited_content(contents):
        logging.warning(f"Request {request_id}: Prohibited content detected")
        return "Error: Content violates usage policies."
    
    # === SECURE LOGGING (NO SENSITIVE DATA) ===
    content_hash = hashlib.sha256(contents.encode()).hexdigest()[:12]
    system_hash = hashlib.sha256(system_instruction.encode()).hexdigest()[:12]
    
    logging.info(f"Request {request_id}: Starting Gemini API call - "
                f"content_hash={content_hash}, system_hash={system_hash}, "
                f"temp={temp}, max_tokens={max_output_tokens}, "
                f"user={getattr(current_user, 'email', 'unknown')}")
    
    try:
        # === SECURE API CONFIGURATION ===
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash", 
            system_instruction=system_instruction
        )
        
        generation_config = types.GenerationConfig(
            temperature=temp, 
            max_output_tokens=max_output_tokens,
            # Additional safety settings
            candidate_count=1,  # Only generate one candidate
        )
        
        # === SAFETY SETTINGS FOR COMPLIANCE ===
        safety_settings = [
            {
                "category": "HARM_CATEGORY_HARASSMENT",
                "threshold": "BLOCK_MEDIUM_AND_ABOVE"
            },
            {
                "category": "HARM_CATEGORY_HATE_SPEECH", 
                "threshold": "BLOCK_MEDIUM_AND_ABOVE"
            },
            {
                "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "threshold": "BLOCK_MEDIUM_AND_ABOVE"
            },
            {
                "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                "threshold": "BLOCK_MEDIUM_AND_ABOVE"
            }
        ]
        
        # === SECURE API CALL ===
        response = model.generate_content(
            contents=contents,
            generation_config=generation_config,
            safety_settings=safety_settings
        )
        
        # === RESPONSE VALIDATION ===
        if not response or not response.text:
            logging.warning(f"Request {request_id}: Empty response from Gemini API")
            return "Error: No content generated. Please try with different input."
        
        response_text = response.text.strip()
        
        # === OUTPUT CONTENT FILTERING ===
        if contains_prohibited_content(response_text):
            logging.warning(f"Request {request_id}: Prohibited content in API response")
            return "Error: Generated content violates usage policies."
        
        # === PII CHECK ON OUTPUT ===
        output_pii_found, output_pii_type = detect_hard_pii(response_text)
        if output_pii_found:
            logging.warning(f"Request {request_id}: PII detected in output - {output_pii_type}")
            # Mask the PII instead of failing completely
            response_text = mask_pii_in_text(response_text)
        
        # === SUCCESS LOGGING ===
        response_hash = hashlib.sha256(response_text.encode()).hexdigest()[:12]
        processing_time = time.time() - request_timestamp
        
        logging.info(f"Request {request_id}: Successfully generated content - "
                    f"response_hash={response_hash}, "
                    f"processing_time={processing_time:.2f}s, "
                    f"response_length={len(response_text)}")
        
        return response_text
        
    except google.api_core.exceptions.PermissionDenied as e:
        logging.critical(f"Request {request_id}: Gemini API PermissionDenied - {str(e)[:100]}")
        return "Error: API access denied. Please contact support."
        
    except genai.types.generation_types.BlockedPromptException as e:
        logging.warning(f"Request {request_id}: Content blocked by safety filters")
        return "Error: Content was blocked for safety reasons. Please modify your input."
        
    except google.api_core.exceptions.ResourceExhausted as e:
        logging.warning(f"Request {request_id}: API quota exceeded")
        return "Error: Service temporarily unavailable due to high demand. Please try again later."
        
    except google.api_core.exceptions.InvalidArgument as e:
        logging.warning(f"Request {request_id}: Invalid API argument - {str(e)[:100]}")
        return "Error: Invalid request parameters. Please check your input."
        
    except Exception as e:
        # Log error without exposing sensitive details to user
        error_id = str(uuid.uuid4())[:8]
        logging.error(f"Request {request_id}: Unexpected error {error_id} - {type(e).__name__}: {str(e)[:100]}")
        return f"Error: An unexpected error occurred. Reference ID: {error_id}. Please contact support if this persists."


def handle_checkout_session(session):
    client_reference_id = session.get('client_reference_id')
    if not client_reference_id:
        logging.error("Stripe Webhook: client_reference_id not found in session.")
        return
    user = User.objects(id=client_reference_id).first()
    if not user:
        logging.error(f"Stripe Webhook: User with id {client_reference_id} not found.")
        return
    try:
        session_with_line_items = stripe.checkout.Session.retrieve(session.id, expand=['line_items'])
        price_id = session_with_line_items.line_items.data[0].price.id
        if price_id in PLAN_DETAILS:
            plan = PLAN_DETAILS[price_id]
            user.plan = plan['name']
            user.daily_generation_limit = plan['daily_limit']
            user.monthly_generation_limit = plan['monthly_limit']
            user.save()
            logging.info(f"Stripe Webhook: Successfully upgraded user {user.email} to the {user.plan} plan.")
        else:
            logging.error(f"Stripe Webhook: Price ID {price_id} not found in PLAN_DETAILS map.")
    except Exception as e:
        logging.error(f"Stripe Webhook: Error processing checkout session: {e}")

def handle_payment_succeeded(invoice):
    customer_email = invoice.get('customer_email')
    if not customer_email:
        logging.error("Stripe Webhook: customer_email not found in invoice.")
        return
    user = User.objects(email=customer_email).first()
    if not user:
        logging.error(f"Stripe Webhook: User with email {customer_email} not found.")
        return
    logging.info(f"Stripe Webhook: Invoice payment successful for user {user.email}")

# --- Config for CORS and Security Headers ---
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://generator.hsgportfolio.com")
BACKEND_API_URL = os.getenv("RENDER_EXTERNAL_URL", "https://mvp-flask-api.onrender.com")

CORS(app, origins=[FRONTEND_BASE_URL], supports_credentials=True, methods=["GET", "POST", "PUT"])

if os.getenv('FLASK_DEBUG') != '1':
    logging.info("--- RUNNING IN PRODUCTION MODE: Applying Talisman with strict security headers ---")
    # NEW: Stricter CSP and security headers
    csp = {
        'default-src': '\'self\'',
        'connect-src': ['\'self\'', BACKEND_API_URL, FRONTEND_BASE_URL],
        # Add other policies as needed, e.g., 'script-src', 'style-src'
    }
    Talisman(
        app,
        content_security_policy=csp,
        force_https=True,  # Redirect all HTTP requests to HTTPS
        strict_transport_security=True, # Enable HSTS
        strict_transport_security_max_age=31536000, # 1 year
        strict_transport_security_include_subdomains=True,
        # strict_transport_security_preload=True, # Uncomment after confirming your site and all subdomains work on HTTPS
        frame_options='DENY', # Prevent clickjacking
        content_type_nosniff=True, # Prevent MIME type sniffing
        session_cookie_secure=True,
        session_cookie_samesite='None' # Required for cross-origin credentials
    )

# --- Authentication Routes with Rate Limiting ---
@app.route('/api/register', methods=['POST'])
@limiter.limit("5 per hour") # Stricter limit for registration
def register():
    data = request.get_json()
    if not data or 'email' not in data or 'password' not in data:
        return jsonify({"status": "error", "message": "Email and password are required."}), 400
    email = data.get('email')
    password = data.get('password')
    # Basic validation for email format and password length
    if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        return jsonify({"status": "error", "message": "Invalid email format."}), 400
    if len(password) < 8:
        return jsonify({"status": "error", "message": "Password must be at least 8 characters long."}), 400
    if User.objects(email=email).first():
        return jsonify({"status": "error", "message": "Email address already in use."}), 409
    new_user = User(email=email)
    new_user.set_password(password)
    new_user.save()
    logging.info(f"New user registered: {email}")
    return jsonify({"status": "ok", "message": "User registered successfully."}), 201

@app.route('/api/login', methods=['POST'])
@limiter.limit("10 per minute") # Protect against credential stuffing
def login():
    data = request.get_json()
    if not data or 'email' not in data or 'password' not in data:
        return jsonify({"status": "error", "message": "Email and password are required."}), 400
    email = data.get('email')
    password = data.get('password')
    user = User.objects(email=email).first()
    if user and user.check_password(password):
        login_user(user)
        user.last_login = datetime.datetime.utcnow()
        user.save()
        logging.info(f"Successful login for user: {email}")
        return jsonify({"status": "ok", "message": "Logged in successfully."}), 200
    else:
        logging.warning(f"Failed login attempt for email: {email} from IP: {get_remote_address()}")
        return jsonify({"status": "error", "message": "Invalid email or password."}), 401

@app.route('/api/logout', methods=['POST'])
@login_required
def logout():
    logging.info(f"User logged out: {current_user.email}")
    logout_user()
    return jsonify({"status": "ok", "message": "Logged out successfully."}), 200

@app.route('/api/status', methods=['GET'])
@login_required # Protect this endpoint
def get_login_status():
    return jsonify({
        "status": "ok", 
        "message": "User is logged in.", 
        "email": current_user.email,
        "plan": current_user.plan
    }), 200

# --- Main Application Route ---
@app.route('/api/generate/<prompt_name>', methods=['POST'])
@login_required
@limiter.limit("60 per minute", key_func=lambda: current_user.get_id()) # User-specific rate limit
def generate_content(prompt_name):
    data = request.get_json()
    if not data or 'contents' not in data:
        return jsonify({"error": "Field 'contents' is required."}), 400
    contents = data.get('contents')
    
    pii_found, pii_type = detect_hard_pii(contents)
    if pii_found:
        logging.warning(f"User {current_user.email} triggered PII check for type '{pii_type}'.")
        return jsonify({
            "error": "pii_detected",
            "message": f"Potential personal information ({pii_type}) detected. Please remove it and try again.",
        }), 422
        
    if prompt_name not in prompts:
        return jsonify({"error": f"Prompt '{prompt_name}' not found."}), 404

    now = datetime.datetime.utcnow()
    if current_user.last_generation_day != now.timetuple().tm_yday:
        current_user.daily_generations = 0
        current_user.last_generation_day = now.timetuple().tm_yday
    if current_user.last_generation_month != now.month:
        current_user.monthly_generations = 0
        current_user.last_generation_month = now.month

    if current_user.daily_generations >= current_user.daily_generation_limit or \
       current_user.monthly_generations >= current_user.monthly_generation_limit:
        logging.warning(f"User {current_user.email} reached generation limit.")
        return jsonify({"error": "Generation limit reached."}), 429

    temp = float(os.getenv('TB_temp', '0.5'))
    max_tokens = 1200
    system_instruction = prompts[prompt_name]
    
    result = generate_text_with_gemini(
        temp=temp,
        max_output_tokens=max_tokens,
        system_instruction=system_instruction,
        contents=contents
    )

    if result.startswith("Error:"):
        return jsonify({"error": result}), 400

    current_user.daily_generations += 1
    current_user.monthly_generations += 1
    current_user.save()
    
    logging.info(f"Generation success for {current_user.email} | Prompt: {prompt_name}")
    return jsonify({"generatedText": result}), 200

# --- Stripe Checkout and Webhook Routes ---
@app.route('/api/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    data = request.get_json()
    price_id = data.get('priceId')
    if not price_id:
        return jsonify({"error": "priceId is required"}), 400
    try:
        checkout_session = stripe.checkout.Session.create(
            client_reference_id=current_user.get_id(),
            customer_email=current_user.email,
            success_url=f"{FRONTEND_BASE_URL}/success",
            cancel_url=f"{FRONTEND_BASE_URL}/cancel",
            payment_method_types=['card'],
            mode='subscription',
            line_items=[{'price': price_id, 'quantity': 1}]
        )
        return jsonify({'url': checkout_session.url})
    except Exception as e:
        logging.error(f"Stripe create_checkout_session failed for user {current_user.email}: {e}")
        return jsonify(error=str(e)), 500

@app.route('/api/billing', methods=['POST'])
@limiter.exempt # Webhooks should not be rate-limited
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    if not sig_header:
        return 'Missing Stripe-Signature header', 400
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except ValueError:
        logging.error("Stripe webhook error: Invalid payload.")
        return 'Invalid payload', 400
    except stripe.error.SignatureVerificationError:
        logging.error("Stripe webhook error: Invalid signature.")
        return 'Invalid signature', 400

    if event['type'] == 'checkout.session.completed':
        handle_checkout_session(event['data']['object'])
    elif event['type'] == 'invoice.payment_succeeded':
        handle_payment_succeeded(event['data']['object'])
    else:
        logging.info(f"Received unhandled Stripe event type: {event['type']}")
    return jsonify(success=True)

# --- Admin Routes ---
@app.route('/api/admin/users', methods=['GET'])
@login_required
@admin_required
def admin_get_all_users():
    users = User.objects.all()
    user_list = [{'id': str(u.id), 'email': u.email, 'plan': u.plan, 'daily_generations': u.daily_generations, 'monthly_generations': u.monthly_generations} for u in users]
    return jsonify({'users': user_list})

@app.route('/api/admin/users/<user_id>', methods=['PUT'])
@login_required
@admin_required
def admin_update_user(user_id):
    data = request.get_json()
    user = User.objects(id=user_id).first()
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    logging.info(f"Admin '{current_user.email}' is updating user '{user.email}'. Data: {data}")

    # NEW: Validate input data types before updating
    if 'plan' in data:
        if not isinstance(data['plan'], str):
            return jsonify({'error': 'Invalid data type for plan.'}), 400
        user.plan = data['plan']
    if 'daily_generation_limit' in data:
        if not isinstance(data['daily_generation_limit'], int):
            return jsonify({'error': 'Invalid data type for daily_generation_limit.'}), 400
        user.daily_generation_limit = data['daily_generation_limit']
    if 'monthly_generation_limit' in data:
        if not isinstance(data['monthly_generation_limit'], int):
            return jsonify({'error': 'Invalid data type for monthly_generation_limit.'}), 400
        user.monthly_generation_limit = data['monthly_generation_limit']
    
    user.save()
    return jsonify({'message': 'User updated successfully'})

@app.route('/api/admin/stats', methods=['GET'])
@login_required
@admin_required
def admin_get_site_stats():
    total_users = User.objects.count()
    pro_users = User.objects(plan='pro').count()
    thirty_days_ago = datetime.datetime.utcnow() - datetime.timedelta(days=30)
    monthly_active_users = User.objects(last_login__gte=thirty_days_ago).count()
    return jsonify({'total_users': total_users, 'pro_users': pro_users, 'monthly_active_users': monthly_active_users})

# --- Server Start ---
if __name__ == '__main__':
    port = int(os.getenv("PORT", 5001))
    # For production, use a production-grade WSGI server like Gunicorn or uWSGI
    # Example: gunicorn --workers 4 --bind 0.0.0.0:5001 app:app
    app.run(debug=False, host='0.0.0.0', port=port)