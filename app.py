import os
import dotenv
import google.generativeai as genai
from google.generativeai import types
import google.api_core
import re
from flask import Flask, jsonify, request, redirect, url_for, abort
from flask_cors import CORS
from flask_talisman import Talisman
from flask_mongoengine import MongoEngine
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import datetime
import stripe
import base64
import binascii

# --- Configuration & Setup ---
dotenv.load_dotenv()

# Initialize the Gemini API
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise ValueError("API key not found in your .env file. Please set GEMINI_API_KEY.")
genai.configure(api_key=api_key)

# Initialize Flask app
app = Flask(__name__)

# --- Load Prompts ---
prompts_folder = 'prompts'
prompts = {}
if os.path.exists(prompts_folder):
    for filename in os.listdir(prompts_folder):
        prompt_name = os.path.splitext(filename)[0]
        with open(os.path.join(prompts_folder, filename), 'r') as f:
            prompts[prompt_name] = f.read()
else:
    print(f"Warning: Prompts folder '{prompts_folder}' not found. Creating it.")
    os.makedirs(prompts_folder, exist_ok=True)
    # Create a default prompt for demonstration
    default_prompt_path = os.path.join(prompts_folder, "default_prompt.md")
    with open(default_prompt_path, 'w') as f:
        f.write("Generate a detailed description based on the following features: [FEATURES_PLACEHOLDER]")
    prompts['default_prompt'] = "Generate a detailed description based on the following features: [FEATURES_PLACEHOLDER]"
    print(f"Created a default prompt at '{default_prompt_path}'")


app.config['MONGODB_SETTINGS'] = {
    'host': os.getenv("MONGO_URI", "mongodb://localhost:27017/your_default_db")
}
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", "a_very_secret_key_that_should_be_in_env_for_production")
stripe.api_key = os.getenv("STRIPE_API_KEY")
webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

db = MongoEngine(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# --- User Model ---
class User(UserMixin, db.Document):
    email = db.StringField(required=True, unique=True)
    password_hash = db.StringField(required=True)
    daily_generations = db.IntField(default=0)
    last_generation_day = db.IntField(default=0)
    monthly_generations = db.IntField(default=0)
    last_generation_month = db.IntField(default=0)
    daily_generation_limit = db.IntField(default=20)
    monthly_generation_limit = db.IntField(default=200)
    plan= db.StringField(default='free')  # 'free', 'pro', etc.
    date_created = db.DateTimeField(default=datetime.datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

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

# --- Helper Functions ---

def is_valid_input(user_input):
    MAX_INPUT_LENGTH = 4000
    if len(user_input) > MAX_INPUT_LENGTH:
        return False
    PROMPT_INJECTION_PATTERNS = [
        r"\bignore all previous instructions\b", r"\bignore the above\b",
        r"\bignore your instructions\b", r"\bdisregard the previous statement\b",
        r"\bforget the preceding text\b", r"\bpretend to be\b", r"\bdev mode\b",
        r"\bsystem prompt\b", r"\byour initial instructions\b",
        r"\brepeat the text above\b", r"\bwhat were your exact instructions\b",
        r"\btranslate this sentence as\b", r"\bdo anything now\b", r"\bDAN prompt\b"
    ]
    def run_checks(text_to_check):
        for pattern in PROMPT_INJECTION_PATTERNS:
            if re.search(pattern, text_to_check, re.IGNORECASE):
                return False
        return True

    if not run_checks(user_input):
        return False

    try:
        decoded_input = base64.b64decode(user_input).decode('utf-8')
        if not run_checks(decoded_input):
            return False
    except (binascii.Error, UnicodeDecodeError):
        pass

    return True

def generate_text_with_gemini(temp, max_output_tokens, system_instruction, contents):
    ## Error Code Meaning: 1(Invalid Temperature), 2(Invalid Max Output Tokens), 3(Invalid System Instruction), 4(Invalid Contents), 5(Invalid Input Detected)
    if not isinstance(temp, (int, float)) or not 0 <= temp <= 1: return "Error: Code 1."
    if not isinstance(max_output_tokens, int) or max_output_tokens <= 0: return "Error: Code 2."
    if not system_instruction or not isinstance(system_instruction, str): return "Error: Code 3."
    if not contents or not isinstance(contents, str): return "Error: Code 4."
    if not is_valid_input(contents): return "Error: Code 5."

    try:
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash",
            system_instruction=system_instruction
        )
        generation_config = types.GenerationConfig(
            temperature=temp,
            max_output_tokens=max_output_tokens
        )
        response = model.generate_content(
            contents=contents,
            generation_config=generation_config
        )
        return response.text
    except google.api_core.exceptions.InvalidArgument as e:
        return "Error: Invalid argument provided to the API. Please check the 'contents' you sent."
    except google.api_core.exceptions.PermissionDenied as e:
        return "Error: API key is invalid or missing. Please check your configuration."
    except google.api_core.exceptions.ResourceExhausted as e:
        return "Error: The API quota has been exceeded. Please try again later."
    except genai.types.generation_types.BlockedPromptException as e:
        return "Error: The prompt was blocked for safety reasons. Please modify the input"
    except Exception as e:
        return f"An unexpected API error occurred: {e}"


def handle_checkout_session(session):
    client_reference_id = session.get('client_reference_id')
    if not client_reference_id:
        print("ERROR: client_reference_id not found in session")
        return
    user = User.objects(id=client_reference_id).first()
    if not user:
        print(f"ERROR: User with id {client_reference_id} not found.")
        return
    user.daily_generation_limit = 100
    user.monthly_generation_limit = 1000
    user.save()
    print(f"Successfully updated limits for user {user.email}")


def handle_payment_succeeded(invoice):
    customer_email = invoice.get('customer_email')
    if not customer_email:
        print("ERROR: customer_email not found in invoice")
        return
    user = User.objects(email=customer_email).first()
    if not user:
        print(f"ERROR: User with email {customer_email} not found.")
        return
    print(f"Invoice payment successful for user {user.email}")

# --- Config for CORS and CSP ---
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://generator.hsgportfolio.com")
BACKEND_API_URL = os.getenv("RENDER_EXTERNAL_URL", "https://mvp-flask-api.onrender.com")
ALLOWED_CORS_ORIGINS = [FRONTEND_BASE_URL, f"{FRONTEND_BASE_URL}/"]
csp = { 'default-src': ['\'self\'', 'https://fonts.googleapis.com', 'https://fonts.gstatic.com'], 'connect-src': ['\'self\'', BACKEND_API_URL, FRONTEND_BASE_URL], }
CORS(app, resources={r"/api/*": {"origins": ALLOWED_CORS_ORIGINS}}, supports_credentials=True)
Talisman(
    app,
    content_security_policy=csp,
    session_cookie_secure=True,
    session_cookie_samesite='None'
)

# --- Authentication Routes ---
@app.route('/api/register', methods=['POST'])
def register():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')
    if not email or not password:
        return jsonify({"status": "error", "message": "Email and password are required."}), 400
    if User.objects(email=email).first():
        return jsonify({"status": "error", "message": "Email address already in use."}), 409
    new_user = User(email=email)
    new_user.set_password(password)
    new_user.save()
    return jsonify({"status": "ok", "message": "User registered successfully."}), 201

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')
    if not email or not password:
        return jsonify({"status": "error", "message": "Email and password are required."}), 400
    user = User.objects(email=email).first()
    if user and user.check_password(password):
        login_user(user)
        return jsonify({"status": "ok", "message": "Logged in successfully."}), 200
    else:
        return jsonify({"status": "error", "message": "Invalid email or password."}), 401

@app.route('/api/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return jsonify({"status": "ok", "message": "Logged out successfully."}), 200

@app.route('/api/status', methods=['GET'])
def get_login_status():
    if current_user.is_authenticated:
        # Check if the user is logged in
        return jsonify({
            "status": "ok", 
            "message": "User is logged in.", 
            "email": current_user.email,
            "plan": current_user.plan  # Include the user's plan in the response
        }), 200
    else:
        return jsonify({"status": "error", "message": "User is not logged in."}), 401

# --- Main Application Route ---
@app.route('/api/generate/<prompt_name>', methods=['POST'])
@login_required
def generate_content(prompt_name):
    # --- 1. GET AND VALIDATE INPUT ---
    data = request.get_json()
    contents = data.get('contents')

    if not contents:
        return jsonify({"error": "Field 'contents' is required."}), 400

    if prompt_name not in prompts:
        return jsonify({"error": f"Prompt '{prompt_name}' not found."}), 404

    # --- 2. USAGE LIMIT CHECK ---
    now = datetime.datetime.utcnow()
    today_day_of_year = now.timetuple().tm_yday
    current_month = now.month

    if current_user.last_generation_day != today_day_of_year:
        current_user.daily_generations = 0
        current_user.last_generation_day = today_day_of_year
    
    if current_user.last_generation_month != current_month:
        current_user.monthly_generations = 0
        current_user.last_generation_month = current_month

    if current_user.daily_generations >= current_user.daily_generation_limit:
        return jsonify({"error": "Daily generation limit reached."}), 429
    
    if current_user.monthly_generations >= current_user.monthly_generation_limit:
        return jsonify({"error": "Monthly generation limit reached."}), 429

    # --- 3. GENERATE CONTENT ---
    temp = float(os.getenv('TB_temp', '0.5'))
    max_tokens = 600
    system_instruction = prompts[prompt_name]
    
    # Replace placeholder in the prompt if it exists
    final_contents = system_instruction.replace("[FEATURES_PLACEHOLDER]", contents)

    result = generate_text_with_gemini(
        temp=temp,
        max_output_tokens=max_tokens,
        system_instruction=system_instruction, # The full prompt template acts as the system instruction
        contents=contents # The user's specific input
    )

    if result.startswith("Error:"):
        return jsonify({"error": result}), 400

    # --- 4. INCREMENT COUNTERS ---
    current_user.daily_generations += 1
    current_user.monthly_generations += 1
    current_user.save()

    # --- 5. RETURN RESULT ---
    return jsonify({"generatedText": result}), 200

# --- Billing Webhook Route ---
@app.route('/api/billing', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    event = None

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except ValueError:
        return 'Invalid payload', 400
    except stripe.error.SignatureVerificationError:
        return 'Invalid signature', 400

    if event['type'] == 'checkout.session.completed':
        handle_checkout_session(event['data']['object'])
    elif event['type'] == 'invoice.payment_succeeded':
        handle_payment_succeeded(event['data']['object'])
    else:
        print('Unhandled event type {}'.format(event['type']))

    return jsonify(success=True)

# --- Server Start ---
if __name__ == '__main__':
    port = int(os.getenv("PORT", 5001))
    app.run(debug=False, host='0.0.0.0', port=port)