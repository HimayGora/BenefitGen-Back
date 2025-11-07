import google.generativeai as genai
import os
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

for model in genai.list_models():
    print(f"{model.name} - {model.display_name}")