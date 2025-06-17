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
    try:
        with open('keys.txt', 'r') as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()

def read_prompt_template(filename="landing_prompt.md"):
    """Reads a prompt template from the /prompts directory."""
    prompt_path = os.path.join(os.path.dirname(__file__), 'prompts', filename)
    try:
        with open(prompt_path, 'r') as f:
            return f.read()
    except FileNotFoundError:
        print(f"ERROR: Prompt file not found at {prompt_path}")
        return None

def generate_text_with_gemini(prompt_text):
    if not prompt_text:
        return "Error: Prompt cannot be empty."
    try:
        response = model.generate_content(
            [prompt_text],
            generation_config=genai.types.GenerationConfig(
                temperature=0.3, # Using the temp from your refined prompt
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
FRONTEND_URL_DEV = "http://localhost:5173"
csp = {'default-src': '\'self\''}

# 3. Extensions Initialization
CORS(app, resources={r"/api/*": {"origins": FRONTEND_URL_DEV}})
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
    features = data.get('features')
    if not features:
        return jsonify({"error": "Features are a required field."}), 400

    # 3. Inject the user's input into the master prompt
    # This replaces the placeholder text at the end of the prompt file
    final_prompt = f"{prompt_template}\n\nSample input:\n[{features}]"
    
    # 4. Call the AI
    ai_result = generate_text_with_gemini(final_prompt)

    # 5. Return the result
    return jsonify({"generatedText": ai_result}), 200


# 5. Running the Server
if __name__ == '__main__':
    app.run(debug=False, port=5001)