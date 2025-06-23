from flask import Flask, render_template, request, redirect, url_for, flash, current_app
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from pymongo import MongoClient
from bson.objectid import ObjectId
import google.generativeai as genai
import requests
import re
import os
import base64
import datetime # For timestamps
from dotenv import load_dotenv

# --- Firebase Admin SDK for Firestore ---
import firebase_admin
from firebase_admin import credentials, firestore, auth # Import auth for custom token sign-in

# --- Load .env ---
load_dotenv()

# --- Flask Setup ---
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')

# --- MongoDB Setup ---
MONGO_URI = os.environ.get('MONGO_URI') or 'mongodb://localhost:27017/'
DB_NAME = os.environ.get('MONGO_DB_NAME') or 'ai_assistant_db'

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
users_collection = db.users

try:
    client.admin.command('ping')
    print("MongoDB connected successfully!")
except Exception as e:
    print(f"MongoDB connection failed: {e}")

# --- Flask-Login Setup ---
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'

# --- User Model ---
class User(UserMixin):
    def __init__(self, id, username, password_hash):
        self.id = str(id)
        self.username = username
        self.password_hash = password_hash

    @staticmethod
    def get_by_username(username):
        data = users_collection.find_one({'username': username})
        if data:
            return User(data['_id'], data['username'], data['password_hash'])
        return None

    @staticmethod
    def get_by_id(user_id):
        try:
            data = users_collection.find_one({'_id': ObjectId(user_id)})
            if data:
                return User(data['_id'], data['username'], data['password_hash'])
        except Exception as e:
            print(f"User ID fetch error: {e}")
        return None

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

@login_manager.user_loader
def load_user(user_id):
    return User.get_by_id(user_id)

# --- Firebase Admin SDK Initialization for Firestore ---
# This part is crucial for Firestore integration.
# We'll use environment variables for Firebase configuration.
FIREBASE_CONFIG = os.environ.get('FIREBASE_CONFIG')
if FIREBASE_CONFIG:
    try:
        # Firebase Admin SDK expects a dictionary or path to a service account key file
        # For Canvas environment, __firebase_config is a JSON string.
        # Ensure 'project_id' is present in your Firebase config for the default credential.
        firebase_credentials = json.loads(FIREBASE_CONFIG)
        cred = credentials.Certificate(firebase_credentials)
        firebase_admin.initialize_app(cred)
        db_firestore = firestore.client()
        print("Firestore initialized successfully!")
    except Exception as e:
        print(f"Error initializing Firebase Admin SDK: {e}")
        db_firestore = None # Set to None if initialization fails
else:
    print("FIREBASE_CONFIG environment variable not found. Firestore will not be available.")
    db_firestore = None

# Gemini Setup
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-1.5-flash") # Model updated

# Hugging Face Setup (Remains the same)
HF_API = "https://api-inference.huggingface.co/models/mrm8488/t5-base-finetuned-emojify"
HF_API_TOKEN =  os.getenv('HF_API_TOKEN')
HF_HEADERS = {"Authorization": f"Bearer {HF_API_TOKEN}"} if HF_API_TOKEN else {}

# AI Answer Function (Handles multimodal input)
def get_gemini_answer(content_parts):
    try:
        response = gemini_model.generate_content(content_parts)
        return getattr(response, "text", "").strip()
    except Exception as e:
        print(f"Gemini error: {e}")
        current_app.logger.error(f"Gemini generation failed: {e}", exc_info=True)
        return None

# Prompt Type Detection
def classify_query_type(prompt):
    coding_keywords = ["code", "python", "javascript", "java", "c++", "html", "css",
                       "react", "sql", "write a program", "function", "syntax"]
    if any(keyword in prompt.lower() for keyword in coding_keywords):
        return "code"
    return "text"

# --- Firestore History Functions ---
def get_user_history(user_id, limit=10):
    if not db_firestore:
        print("Firestore is not initialized. Cannot retrieve history.")
        return []
    try:
        # Public data: artifacts/{appId}/public/data/{your_collection_name}
        # Private data: artifacts/{appId}/users/{userId}/{your_collection_name}
        # Assuming __app_id is globally available in the Canvas environment
        app_id = os.environ.get('__app_id', 'default_app')
        history_ref = db_firestore.collection(f"artifacts/{app_id}/users/{user_id}/search_history")
        
        # Firestore does not directly support 'orderBy' on real-time data easily without indexing
        # and it's best to fetch all, then sort in Python for simplicity here.
        # Alternatively, if order is critical and you expect many items,
        # you'd need to create a Firestore index on 'timestamp' in descending order.
        docs = history_ref.order_by('timestamp', direction=firestore.Query.DESCENDING).limit(limit).stream()
        history_items = []
        for doc in docs:
            item = doc.to_dict()
            # Convert Firestore Timestamp to Python datetime object
            if 'timestamp' in item and isinstance(item['timestamp'], firestore.Timestamp):
                item['timestamp'] = item['timestamp']._datetime
            history_items.append(item)
        return history_items
    except Exception as e:
        print(f"Error fetching user history from Firestore: {e}")
        current_app.logger.error(f"Firestore history fetch failed: {e}", exc_info=True)
        return []

def add_to_user_history(user_id, prompt, answer):
    if not db_firestore:
        print("Firestore is not initialized. Cannot save history.")
        return
    try:
        app_id = os.environ.get('__app_id', 'default_app')
        history_ref = db_firestore.collection(f"artifacts/{app_id}/users/{user_id}/search_history")
        history_ref.add({
            'prompt': prompt,
            'answer': answer, # Storing the raw answer
            'timestamp': firestore.SERVER_TIMESTAMP # Let Firestore set the timestamp
        })
    except Exception as e:
        print(f"Error saving user history to Firestore: {e}")
        current_app.logger.error(f"Firestore history save failed: {e}", exc_info=True)


@app.route("/", methods=["GET", "POST"])
@login_required
def home():
    user_text_prompt = ""
    rendered_content = None
    is_code_response = False
    search_history = []

    # Get user ID for Firestore operations
    user_id = current_user.get_id() # Use Flask-Login's current_user ID

    # Always fetch history when rendering the page
    search_history = get_user_history(user_id)

    # Allowed extensions for both text and image files
    ALLOWED_EXTENSIONS = {'txt', 'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'}

    def allowed_file(filename):
        return '.' in filename and \
               filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

    if request.method == "POST":
        user_text_prompt = request.form.get("text", "").strip()
        
        content_parts = []
        if user_text_prompt:
            content_parts.append(user_text_prompt)

        file_uploaded = False
        if 'file' in request.files:
            file = request.files['file']
            if file and allowed_file(file.filename):
                file_uploaded = True
                try:
                    file_extension = file.filename.rsplit('.', 1)[1].lower()
                    if file_extension in {'txt'}:
                        text_content = file.read().decode('utf-8')
                        content_parts.append(f"\n\n--- Content from uploaded file ---\n{text_content}")
                    elif file_extension in {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'}:
                        image_bytes = file.read()
                        encoded_image = base64.b64encode(image_bytes).decode('utf-8')
                        mime_type = file.mimetype
                        content_parts.append({
                            "mime_type": mime_type,
                            "data": encoded_image
                        })
                except Exception as e:
                    flash(f"Error processing file: {e}", "danger")
                    current_app.logger.error(f"File processing failed: {e}", exc_info=True)
            elif file.filename != '':
                flash("File type not allowed. Only .txt, .png, .jpg, .jpeg, .gif, .bmp, .webp are supported.", "danger")


        if content_parts:
            # Determine if the query is for code generation based on the text prompt
            query_type = classify_query_type(user_text_prompt)

            raw_answer = get_gemini_answer(content_parts)

            if raw_answer:
                # Save to history only if a successful answer is received
                add_to_user_history(user_id, user_text_prompt, raw_answer)
                # Re-fetch history to include the new entry
                search_history = get_user_history(user_id)

                if query_type == "code":
                    rendered_content = raw_answer
                    is_code_response = True
                else:
                    lines = raw_answer.splitlines()
                    cleaned = [re.sub(r"^\s*[-–•]?\s*", "", line.strip()) for line in lines if line.strip()]
                    if cleaned:
                        cleaned[-1] += " 👋"
                    rendered_content = cleaned
            else:
                flash("Sorry, the assistant couldn’t process your request. Try again!", "danger")
        else:
            flash("Please enter a question or upload a file.", "warning")

    return render_template("index.html",
                           prompt=user_text_prompt,
                           rendered_content=rendered_content,
                           is_code_response=is_code_response,
                           search_history=search_history) # Pass history to template

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('home'))

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        confirm_password = request.form.get("confirm_password")

        if users_collection.find_one({'username': username}):
            flash("Username already exists.", "danger")
        elif not username or not password or not confirm_password:
            flash("All fields are required.", "danger")
        elif password != confirm_password:
            flash("Passwords do not match.", "danger")
        elif len(password) < 6:
            flash("Password must be at least 6 characters.", "danger")
        else:
            hashed = generate_password_hash(password)
            users_collection.insert_one({'username': username, 'password_hash': hashed})
            flash("Account created successfully. You can now log in.", "success")
            return redirect(url_for("login"))

    return render_template("signup.html", title="Sign Up")

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('home'))

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        data = users_collection.find_one({'username': username})
        if data:
            user = User(data['_id'], data['username'], data['password_hash'])
            if user.check_password(password):
                login_user(user)
                flash(f"Welcome back, {user.username}!", "success")
                return redirect(url_for('home'))
        flash("Invalid username or password.", "danger")

    return render_template("login.html", title="Login")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("home"))

app.run(host='0.0.0.0', port=5000)

# --- Run App ---
if __name__ == "__main__":
    import json # Import json for parsing FIREBASE_CONFIG
    app.run(debug=True)
