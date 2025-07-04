from flask import Flask, jsonify, request, redirect, url_for, abort
from flask_cors import CORS
from flask_talisman import Talisman
import os
import google.generativeai as genai
from flask_mongoengine import MongoEngine
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import datetime
import stripe

# --- Configuration & Setup ---
from dotenv import load_dotenv
load_dotenv()


genai.configure(api_key=os.getenv("GEMINI_API_KEY")) 
model = genai.GenerativeModel('gemini-1.5-flash')

app = Flask(__name__)

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
    """
    User model with email and state-based generation limits.
    """
    email = db.StringField(required=True, unique=True)
    password_hash = db.StringField(required=True)
    
    ## FIXED: Added fields for state-based limit tracking
    daily_generations = db.IntField(default=0)
    last_generation_day = db.IntField(default=0) # Will store the day of the year (1-366)
    monthly_generations = db.IntField(default=0)
    last_generation_month = db.IntField(default=0) # Will store the month (1-12)

    ## FIXED: Default limits are still here
    daily_generation_limit = db.IntField(default=20)
    monthly_generation_limit = db.IntField(default=200)
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

# --- Helper Functions (UPDATED) ---

# ADDED: List of prompt injection keywords
PROMPT_INJECTION_KEYWORDS = [
    "ignore all previous instructions",
    "ignore the above",
    "ignore your instructions",
    "disregard the previous statement",
    "forget the preceding text",
    "act as",
    "pretend to be",
    "you are a",
    "developer mode",
    "dev mode",
    "system prompt",
    "your initial instructions",
    "repeat the text above",
    "what were your exact instructions",
    "translate this sentence as",
    "haha pwned",
    "render markdown",
    "execute code",
    "run python",
    "do anything now",
    "DAN prompt"
]

def check_for_prompt_injection(input_text):
    """
    Checks for prompt injection keywords in the input text.
    Returns True if a keyword is found, False otherwise.
    """
    # Convert input to lowercase for case-insensitive matching
    lower_input = input_text.lower()
    # Check if any of the keywords are present in the input
    if any(keyword in lower_input for keyword in PROMPT_INJECTION_KEYWORDS):
        return True
    return False

def read_prompt_template(filename="landing_prompt.md"):
    # ... (function code is correct and remains unchanged)
    current_dir = os.path.dirname(__file__)
    prompts_dir = os.path.join(current_dir, 'prompts')
    prompt_path = os.path.join(prompts_dir, filename)

    try:
        with open(prompt_path, 'r') as f:
            content = f.read()
            return content
    except FileNotFoundError:
        print(f"ERROR: Prompt file not found at {prompt_path}. Ensure it exists in the 'prompts' directory.")
        return None
    except Exception as e:
        print(f"ERROR: An unexpected error occurred while reading prompt file {prompt_path}: {e}")
        return None
def handle_checkout_session(session):
    """
    Handles the 'checkout.session.completed' event.
    """
    client_reference_id = session.get('client_reference_id')
    if not client_reference_id:
        print("ERROR: client_reference_id not found in session")
        return

    user = User.objects(id=client_reference_id).first()
    if not user:
        print(f"ERROR: User with id {client_reference_id} not found.")
        return

    # Example: Update user's subscription status or grant access to a feature
    # In this example, let's assume we are updating the user's generation limits
    # based on the product they purchased.

    # You would typically retrieve the line items from the session to determine
    # what was purchased. For simplicity, we will just update the limits here.
    user.daily_generation_limit = 100  # New daily limit
    user.monthly_generation_limit = 1000 # New monthly limit
    user.save()

    print(f"Successfully updated limits for user {user.email}")


def handle_payment_succeeded(invoice):
    """
    Handles the 'invoice.payment_succeeded' event.
    """
    customer_id = invoice.get('customer')
    if not customer_id:
        print("ERROR: customer_id not found in invoice")
        return

    # You can retrieve the user by their Stripe customer ID if you have stored it
    # in your User model. This is a robust way to link your users to Stripe customers.
    # For now, we will assume you can look them up by email.
    customer_email = invoice.get('customer_email')
    if not customer_email:
        print("ERROR: customer_email not found in invoice")
        return

    user = User.objects(email=customer_email).first()
    if not user:
        print(f"ERROR: User with email {customer_email} not found.")
        return

    # Logic to handle a successful recurring payment, for example,
    # extending their subscription period.
    print(f"Invoice payment successful for user {user.email}")
    
def generate_text_with_gemini(prompt_text):
    # ... (function code is correct and remains unchanged)
    if not prompt_text:
        return "Error: Prompt cannot be empty."
    try:
        response = model.generate_content(
            [prompt_text],
            generation_config=genai.types.GenerationConfig(
                temperature=0.3,
                max_output_tokens=800
            )
        )
        if response.candidates and response.candidates[0].content.parts:
            return response.candidates[0].content.parts[0].text.strip()
        else:
            print("WARNING: Gemini API returned no content in the expected format.")
            return "An error occurred during text generation: No content returned."
    except Exception as e:
        print(f"An error occurred during text generation: {e}")
        return f"An error occurred during text generation. Details: {e}"


# --- Config for CORS and CSP (Unchanged) ---
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
    """
    Handles user registration. Requires 'email' and 'password'.
    """
    data = request.get_json()
    ## FIXED: Use 'email' instead of 'username'
    email = data.get('email')
    password = data.get('password')

    ## FIXED: Check for 'email'
    if not email or not password:
        return jsonify({"status": "error", "message": "Email and password are required."}), 400

    ## FIXED: Check if email already exists
    if User.objects(email=email).first():
        return jsonify({"status": "error", "message": "Email address already in use."}), 409 # 409 Conflict

    ## FIXED: Create user with email
    new_user = User(email=email)
    new_user.set_password(password)
    new_user.save()

    return jsonify({"status": "ok", "message": "User registered successfully."}), 201

@app.route('/api/login', methods=['POST'])
def login():
    """
    Handles user login. Requires 'email' and 'password'.
    """
    data = request.get_json()
    ## FIXED: Use 'email' instead of 'username'
    email = data.get('email')
    password = data.get('password')

    ## FIXED: Check for 'email'
    if not email or not password:
        return jsonify({"status": "error", "message": "Email and password are required."}), 400

    ## FIXED: Look up user by email
    user = User.objects(email=email).first()

    if user and user.check_password(password):
        login_user(user)
        return jsonify({"status": "ok", "message": "Logged in successfully."}), 200
    else:
        ## FIXED: More accurate error message
        return jsonify({"status": "error", "message": "Invalid email or password."}), 401

@app.route('/api/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return jsonify({"status": "ok", "message": "Logged out successfully."}), 200

@app.route('/api/status', methods=['GET'])
def get_login_status():
    if current_user.is_authenticated:
        ## FIXED: Return 'email' instead of 'username'
        return jsonify({"status": "ok", "message": "User is logged in.", "email": current_user.email}), 200
    else:
        return jsonify({"status": "error", "message": "User is not logged in."}), 401


# --- Main Application Route (UPDATED) ---
@app.route('/api/generate', methods=['POST'])
@login_required
def generate_content():
    """
    Generates content, enforcing state-based usage limits and checking for prompt injection.
    """
    # --- 1. GET AND VALIDATE INPUT ---
    data = request.get_json()
    features_raw = data.get('features')

    if not features_raw:
        return jsonify({"error": "Features are a required field."}), 400
    
    # --- 2. PROMPT INJECTION CHECK ---
    if check_for_prompt_injection(features_raw):
        # Using abort() is a clean way to stop the request and return an error
        abort(400, description="Invalid input provided.")

    ## FIXED: Completely new logic for state-based limit checking
    # --- 3. USAGE LIMIT CHECK (State-based) ---
    now = datetime.datetime.utcnow()
    today_day_of_year = now.timetuple().tm_yday
    current_month = now.month

    # Reset daily counter if the last generation was on a different day
    if current_user.last_generation_day != today_day_of_year:
        current_user.daily_generations = 0
        current_user.last_generation_day = today_day_of_year
    
    # Reset monthly counter if the last generation was in a different month
    if current_user.last_generation_month != current_month:
        current_user.monthly_generations = 0
        current_user.last_generation_month = current_month

    # Check limits
    if current_user.daily_generations >= current_user.daily_generation_limit:
        return jsonify({"error": "Daily generation limit reached."}), 429
    
    if current_user.monthly_generations >= current_user.monthly_generation_limit:
        return jsonify({"error": "Monthly generation limit reached."}), 429

    # --- 4. READ PROMPT ---
    prompt_template = read_prompt_template("landing_prompt.md")
    if not prompt_template:
        return jsonify({"error": "Could not load prompt template."}), 500

    # --- 5. SANITIZE AND PREPARE FINAL PROMPT ---
    features_sanitized = features_raw.strip()
    final_prompt = prompt_template.replace("[FEATURES_PLACEHOLDER]", features_sanitized)

    # --- 6. GENERATE CONTENT ---
    ai_result = generate_text_with_gemini(final_prompt)

    # --- 7. INCREMENT COUNTERS on successful generation ---
    if "An error occurred" not in ai_result:
        current_user.daily_generations += 1
        current_user.monthly_generations += 1
        current_user.save()

    # --- 8. RETURN RESULT ---
    return jsonify({"generatedText": ai_result}), 200
# --- Billing Webhook Route ---
@app.route('/api/billing', methods=['POST'])
def stripe_webhook():
    """
    Handles incoming Stripe webhooks to update user billing information.
    """
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    event = None

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret
        )
    except ValueError as e:
        # Invalid payload
        return 'Invalid payload', 400
    except stripe.error.SignatureVerificationError as e:
        # Invalid signature
        return 'Invalid signature', 400

    # Handle the event
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        # Fulfill the purchase...
        handle_checkout_session(session)
    elif event['type'] == 'invoice.payment_succeeded':
        invoice = event['data']['object']
        # Handle successful payment...
        handle_payment_succeeded(invoice)
    # ... handle other event types
    else:
        print('Unhandled event type {}'.format(event['type']))

    return jsonify(success=True)

# --- Server Start ---
if __name__ == '__main__':
    # ... (this section is correct and remains unchanged)
    prompts_dir = os.path.join(os.path.dirname(__file__), 'prompts')
    os.makedirs(prompts_dir, exist_ok=True)
    landing_prompt_path = os.path.join(prompts_dir, "landing_prompt.md")
    if not os.path.exists(landing_prompt_path):
        with open(landing_prompt_path, 'w') as f:
            f.write("Generate a detailed description based on the following features: [FEATURES_PLACEHOLDER]")
        print(f"Created a default {landing_prompt_path} in the 'prompts' directory.")
    port = int(os.getenv("PORT", 5001))
    app.run(debug=False, host='0.0.0.0', port=port)