from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_talisman import Talisman
import os
import google.generativeai as genai

# --- Configuration & Setup ---
from dotenv import load_dotenv
load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

model = genai.GenerativeModel('gemini-1.5-flash')

# --- Helper Functions ---
def load_keys():
    """
    Loads allowed keys (UUIDs) from a comma-separated string in an environment variable.
    Keys should be provided as a comma-separated string in the ALLOWED_KEYS_CSV environment variable.
    """
    print("Attempting to load keys from environment variable...")

    # The delimiter is a comma, as UUIDs don't contain commas
    KEY_DELIMITER = "," 

    keys_delimited_string = os.getenv("ALLOWED_KEYS_CSV")

    if keys_delimited_string:
        # Split the string by the comma and create a set of keys
        keys = {key.strip() for key in keys_delimited_string.split(KEY_DELIMITER) if key.strip()}
        print(f"Successfully loaded {len(keys)} UUID keys from environment variable.")
        return keys
    else:
        print("WARNING: ALLOWED_KEYS_CSV environment variable not found or is empty. No UUID keys loaded.")
        return set() # Return an empty set if no keys are provided

def read_prompt_template(filename="landing_prompt.md"):
    """Reads a prompt template from the /prompts directory."""
    current_dir = os.path.dirname(__file__)
    prompts_dir = os.path.join(current_dir, 'prompts')
    prompt_path = os.path.join(prompts_dir, filename)

    print(f"DEBUG: Attempting to read prompt from: {prompt_path}")
    print(f"DEBUG: Current module directory: {current_dir}")
    print(f"DEBUG: Prompts directory should be: {prompts_dir}")

    # Add this for deep debugging: list contents of the prompts directory
    try:
        print(f"DEBUG: Contents of prompts directory: {os.listdir(prompts_dir)}")
    except FileNotFoundError:
        print(f"DEBUG: Prompts directory not found at: {prompts_dir}")
    except Exception as e:
        print(f"DEBUG: Error listing prompts directory: {e}")


    try:
        with open(prompt_path, 'r') as f:
            content = f.read()
            print(f"DEBUG: Successfully read prompt: {filename}")
            return content
    except FileNotFoundError:
        print(f"ERROR: Prompt file not found at {prompt_path}. Gunicorn import may fail.")
        return None
    except Exception as e:
        print(f"ERROR: An unexpected error occurred while reading prompt file {prompt_path}: {e}")
        return None

def generate_text_with_gemini(prompt_text):
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
        return response.candidates[0].content.parts[0].text.strip()
    except Exception as e:
        print(f"An error occurred during text generation: {e}")
        return f"An error occurred during text generation."

# 1. App Initialization
app = Flask(__name__)
valid_keys = load_keys()

# 2. Configuration
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://generator.hsgportfolio.com")
BACKEND_API_URL = os.getenv("RENDER_EXTERNAL_URL", "https://mvp-flask-api.onrender.com")
ALLOWED_CORS_ORIGINS = [
    FRONTEND_BASE_URL,
    f"{FRONTEND_BASE_URL}/" # Allow with and without trailing slash
]

csp = {
    'default-src': [
        '\'self\'', # Allow resources from the same origin as the Flask app itself
        'https://fonts.googleapis.com', # If your Flask app serves templates that use Google Fonts
        'https://fonts.gstatic.com'    # If your Flask app serves templates that use Google Fonts
    ],
    
    'connect-src': [
        '\'self\'', # Allow API calls to the Flask app's own domain
        BACKEND_API_URL, # Explicitly allow your own Flask backend URL (redundant if self is backend, but good to be explicit)
        FRONTEND_BASE_URL # Allow your frontend's domain to be used in connect-src context, though less common for *backend* CSP
    ],
    
}

# 3. Extensions Initialization
CORS(app, resources={r"/api/*": {"origins": ALLOWED_CORS_ORIGINS}})
Talisman(app, content_security_policy=csp)


# 4. API Routes
@app.route('/api/verify', methods=['POST'])
def verify_key():
    data = request.get_json()
    submitted_key = data.get('key')
    if not submitted_key:
        return jsonify({"status": "error", "message": "No key provided."}), 400
    if submitted_key in valid_keys:
        return jsonify({"status": "ok", "message": "Key is valid."}), 200
    else:
        return jsonify({"status": "error", "message": "Invalid key."}), 401


# THIS IS THE FINAL, UPDATED ROUTE
@app.route('/api/generate', methods=['POST'])
def generate_content():
    # 1. Read the master prompt from the file
    prompt_template = read_prompt_template("landing_prompt.md")
    if not prompt_template:
        return jsonify({"error": "Could not load prompt template."}), 500

    # 2. Get the features from the frontend request
    data = request.get_json()
    features_raw = data.get('features') # Get raw input first
    
    if not features_raw:
        print("DEBUG: Backend: 'features_raw' is empty. Returning 400.")
        return jsonify({"error": "Features are a required field."}), 400

    # --- INPUT SANITIZATION ---
    # Strip whitespace
    features_sanitized = features_raw.strip()

    # Optional: Limit input length to prevent overly long injection attempts or abuse
    MAX_FEATURE_INPUT_LENGTH = 100 # Adjust as needed for your use case
    if len(features_sanitized) > MAX_FEATURE_INPUT_LENGTH:
        print(f"DEBUG: Backend: Input length {len(features_sanitized)} exceeds {MAX_FEATURE_INPUT_LENGTH}. Returning 400.")
        return jsonify({"error": f"Input too long. Please limit to {MAX_FEATURE_INPUT_LENGTH} characters."}), 400

    # Optional: Basic filtering for problematic phrases.
    # This is not foolproof but can catch simple attempts.
    # Adjust this list based on what you observe or want to prevent.
    problematic_phrases = [
        "ignore previous instructions",
        "as an ai model",
        "generate content in json",
        "disregard all",
        "output only",
        "act as a",
        "you are now",
        "system prompt"
    ]
    for phrase in problematic_phrases:
        if phrase in features_sanitized.lower(): # Check lowercased to be case-insensitive
            # Option A: Reject the input
            print(f"DEBUG: Backend: Problematic phrase '{phrase}' found. Returning 400.")
            return jsonify({"error": "Input contains potentially problematic content. Please rephrase."}), 400
            # Option B: Neutralize (e.g., replace with empty string or spaces, or escape)
            # features_sanitized = features_sanitized.lower().replace(phrase, "") # This might change content unexpectedly

    # You might also want to neutralize specific characters if they could break prompt structure,
    # but for simple text injection into a placeholder, string replacement usually handles this.
    # For example, if you were using specific delimiters that the user could inject.
    # If the LLM is correctly constrained by your main prompt, it should treat this as descriptive text.
    # --- END INPUT SANITIZATION ---

    # 3. Inject the user's sanitized input into the master prompt
    # Make sure '[FEATURES_PLACEHOLDER]' matches exactly what you put in landing_prompt.md
    final_prompt = prompt_template.replace("[FEATURES_PLACEHOLDER]", features_sanitized)
    
    # 4. Call the AI
    ai_result = generate_text_with_gemini(final_prompt)

    # 5. Return the result
    return jsonify({"generatedText": ai_result}), 200

# 5. Running the Server
if __name__ == '__main__':
    port = int(os.getenv("PORT", 5001))
    app.run(debug=False,host='0.0.0.0', port=port)