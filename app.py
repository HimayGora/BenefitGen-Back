from flask import Flask, jsonify, request, redirect, url_for
from flask_cors import CORS
from flask_talisman import Talisman
import os
import google.generativeai as genai
from flask_mongoengine import MongoEngine
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

# --- Configuration & Setup ---
# Load environment variables from .env file
from dotenv import load_dotenv
load_dotenv()

# Configure Gemini API with the API key from environment variables
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# Initialize the Generative Model
model = genai.GenerativeModel('gemini-1.5-flash')

# 1. App Initialization
app = Flask(__name__)

# --- NEW: MongoDB and Flask-Login Configuration ---
# Configure MongoDB connection settings
# MONGO_URI should be set in your environment variables (e.g., in a .env file)
# Example: MONGO_URI="mongodb://localhost:27017/your_database_name"
# For MongoDB Atlas: MONGO_URI="mongodb+srv://user:password@cluster.mongodb.net/your_database_name?retryWrites=true&w=majority"
app.config['MONGODB_SETTINGS'] = {
    'host': os.getenv("MONGO_URI", "mongodb://localhost:27017/your_default_db")
}
# SECRET_KEY is crucial for Flask-Login session management.
# It should be a long, random string and stored securely in environment variables.
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", "a_very_secret_key_that_should_be_in_env_for_production")

# Initialize Flask-MongoEngine
db = MongoEngine(app)

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
# Define the view that users are redirected to if they need to log in
login_manager.login_view = 'login'

# --- NEW: User Model for MongoDB using MongoEngine ---
class User(UserMixin, db.Document):
    """
    User model for storing user credentials in MongoDB.
    Inherits from UserMixin for Flask-Login integration.
    """
    username = db.StringField(required=True, unique=True) # Username must be unique
    password_hash = db.StringField(required=True)         # Stores hashed password

    def set_password(self, password):
        """Hashes the provided password and stores it."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """Checks if the provided password matches the stored hash."""
        return check_password_hash(self.password_hash, password)

    # Required by Flask-Login: Returns a unique identifier for the user.
    # We use the document's primary key (ObjectId) converted to a string.
    def get_id(self):
        return str(self.pk)

# --- NEW: Flask-Login User Loader ---
@login_manager.user_loader
def load_user(user_id):
    """
    This callback is used to reload the user object from the user ID stored in the session.
    It's required by Flask-Login.
    """
    try:
        return User.objects.get(pk=user_id) # Retrieve user by primary key (ObjectId)
    except User.DoesNotExist:
        return None # User not found

# --- Helper Functions ---
def read_prompt_template(filename="landing_prompt.md"):
    """Reads a prompt template from the /prompts directory."""
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

def generate_text_with_gemini(prompt_text):
    """
    Generates text using the Google Gemini API.
    Includes basic error handling and response validation.
    """
    if not prompt_text:
        return "Error: Prompt cannot be empty."
    try:
        response = model.generate_content(
            [prompt_text],
            generation_config=genai.types.GenerationConfig(
                temperature=0.3, # Controls randomness; lower values make responses more deterministic
                max_output_tokens=800 # Maximum number of tokens in the generated response
            )
        )
        # Check if candidates list is not empty and content exists before accessing
        if response.candidates and response.candidates[0].content.parts:
            return response.candidates[0].content.parts[0].text.strip()
        else:
            print("WARNING: Gemini API returned no content in the expected format.")
            return "An error occurred during text generation: No content returned."
    except Exception as e:
        print(f"An error occurred during text generation: {e}")
        return f"An error occurred during text generation. Details: {e}"

# 2. Configuration for CORS and CSP
# Define the frontend base URL for CORS and CSP policies
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://generator.hsgportfolio.com")
# Define the backend API URL, useful for CSP connect-src
BACKEND_API_URL = os.getenv("RENDER_EXTERNAL_URL", "https://mvp-flask-api.onrender.com")

# List of allowed origins for CORS (e.g., your frontend's domain)
ALLOWED_CORS_ORIGINS = [
    FRONTEND_BASE_URL,
    f"{FRONTEND_BASE_URL}/" # Allow with and without trailing slash
]

# Content Security Policy (CSP) for security headers
csp = {
    'default-src': [
        '\'self\'', # Allow resources from the same origin as the Flask app
        'https://fonts.googleapis.com', # Allow Google Fonts CSS
        'https://fonts.gstatic.com'    # Allow Google Fonts assets
    ],
    'connect-src': [
        '\'self\'', # Allow API calls to the Flask app's own domain
        BACKEND_API_URL, # Explicitly allow your own Flask backend URL
        FRONTEND_BASE_URL # Allow your frontend's domain for connections if needed
    ],
}

# 3. Extensions Initialization
# Enable Cross-Origin Resource Sharing (CORS) for API routes
CORS(app, resources={r"/api/*": {"origins": ALLOWED_CORS_ORIGINS}}, supports_credentials=True)
# Apply Talisman for security headers, including CSP
Talisman(app, content_security_policy=csp)

# --- NEW: Authentication Routes ---
@app.route('/api/register', methods=['POST'])
def register():
    """
    Handles user registration. Requires 'username' and 'password' in the request body.
    """
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify({"status": "error", "message": "Username and password are required."}), 400

    # Check if a user with this username already exists
    existing_user = User.objects(username=username).first()
    if existing_user:
        return jsonify({"status": "error", "message": "Username already exists."}), 409 # 409 Conflict

    # Create a new user and set the hashed password
    new_user = User(username=username)
    new_user.set_password(password)
    new_user.save() # Save the new user to MongoDB

    return jsonify({"status": "ok", "message": "User registered successfully."}), 201 # 201 Created

@app.route('/api/login', methods=['POST'])
def login():
    """
    Handles user login. Requires 'username' and 'password' in the request body.
    Authenticates the user and logs them in using Flask-Login.
    """
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify({"status": "error", "message": "Username and password are required."}), 400

    user = User.objects(username=username).first()
    # Check if user exists and if the provided password is correct
    if user and user.check_password(password):
        login_user(user) # Log the user in via Flask-Login
        return jsonify({"status": "ok", "message": "Logged in successfully."}), 200
    else:
        return jsonify({"status": "error", "message": "Invalid username or password."}), 401 # 401 Unauthorized

@app.route('/api/logout', methods=['POST'])
@login_required # This route requires the user to be logged in to access
def logout():
    """
    Handles user logout. Logs the current user out using Flask-Login.
    """
    logout_user() # Log the current user out
    return jsonify({"status": "ok", "message": "Logged out successfully."}), 200

# --- MODIFIED: Login Status Check ---
# This route is repurposed to check if a user is currently logged in.
# It replaces the old `/api/verify` route which checked static keys.
@app.route('/api/status', methods=['GET'])
def get_login_status():
    """
    Checks the current login status of the user.
    """
    if current_user.is_authenticated:
        # If logged in, return success status and username
        return jsonify({"status": "ok", "message": "User is logged in.", "username": current_user.username}), 200
    else:
        # If not logged in, return error status
        return jsonify({"status": "error", "message": "User is not logged in."}), 401

# THIS IS THE FINAL, UPDATED ROUTE (NOW PROTECTED BY LOGIN_REQUIRED)
@app.route('/api/generate', methods=['POST'])
@login_required # This decorator ensures only authenticated users can access this route
def generate_content():
    """
    Generates content using the Gemini API based on user features.
    This route now requires user authentication.
    """
    # 1. Read the master prompt from the file
    prompt_template = read_prompt_template("landing_prompt.md")
    if not prompt_template:
        return jsonify({"error": "Could not load prompt template."}), 500

    # 2. Get the features from the frontend request
    data = request.get_json()
    features_raw = data.get('features')

    if not features_raw:
        return jsonify({"error": "Features are a required field."}), 400

    # --- INPUT SANITIZATION ---
    features_sanitized = features_raw.strip()
    MAX_FEATURE_INPUT_LENGTH = 1000
    if len(features_sanitized) > MAX_FEATURE_INPUT_LENGTH:
        return jsonify({"error": f"Input too long. Please limit to {MAX_FEATURE_INPUT_LENGTH} characters."}), 400

    # Basic filtering for problematic phrases to prevent prompt injection
    problematic_phrases = [
        "ignore previous instructions", "as an ai model", "generate content in json",
        "disregard all", "output only", "act as a", "you are now", "system prompt"
    ]
    for phrase in problematic_phrases:
        if phrase in features_sanitized.lower():
            return jsonify({"error": "Input contains potentially problematic content. Please rephrase."}), 400
    # --- END INPUT SANITIZATION ---

    # 3. Inject the user's sanitized input into the master prompt
    # The [FEATURES_PLACEHOLDER] in landing_prompt.md will be replaced by user input
    final_prompt = prompt_template.replace("[FEATURES_PLACEHOLDER]", features_sanitized)

    # 4. Call the AI to generate content
    ai_result = generate_text_with_gemini(final_prompt)

    # 5. Return the result
    return jsonify({"generatedText": ai_result}), 200

# 5. Running the Server
if __name__ == '__main__':
    # Ensure the 'prompts' directory exists
    prompts_dir = os.path.join(os.path.dirname(__file__), 'prompts')
    os.makedirs(prompts_dir, exist_ok=True)

    # Create a default 'landing_prompt.md' if it doesn't exist
    # This helps in running the app without manual file creation
    landing_prompt_path = os.path.join(prompts_dir, "landing_prompt.md")
    if not os.path.exists(landing_prompt_path):
        with open(landing_prompt_path, 'w') as f:
            f.write("Generate a detailed description based on the following features: [FEATURES_PLACEHOLDER]")
        print(f"Created a default {landing_prompt_path} in the 'prompts' directory.")

    # Get the port from environment variables or default to 5001
    port = int(os.getenv("PORT", 5001))
    # Run the Flask app
    app.run(debug=True,host='0.0.0.0', port=port)

