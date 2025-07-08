from flask import Flask, render_template, request, redirect, url_for, flash, current_app, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure
from bson.objectid import ObjectId
import google.generativeai as genai
import requests
import re
import os
import base64
import datetime
from dotenv import load_dotenv
import json

import firebase_admin
from firebase_admin import credentials, firestore

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# --- Flask Configuration ---
# Set a strong, permanent SECRET_KEY in production.
# os.urandom(24) is for local development if SECRET_KEY isn't set in .env
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(24))

# --- MongoDB Configuration ---
# Load MongoDB URI and DB name from environment variables
MONGO_URI = os.environ.get('MONGO_URI')
DB_NAME = os.environ.get('MONGO_DB_NAME')

# Provide helpful warnings if environment variables are not set
if not MONGO_URI:
    print("WARNING: MONGO_URI environment variable not set.")
    print("Please set MONGO_URI with your MongoDB Atlas connection string.")
    print("Example: mongodb+srv://<username>:<password>@<cluster-url>/<your-database-name>?retryWrites=true&w=majority")
    # This placeholder will still cause "bad auth" if used directly.
    # It's here just to prevent a NameError if env var is missing during testing.
    MONGO_URI = 'mongodb+srv://ai-generation:YOUR_ACTUAL_MONGODB_PASSWORD@cluster0.18rtbk6.mongodb.net/ai_assistant_db?retryWrites=true&w=majority&appName=Cluster0'

if not DB_NAME:
    print("WARNING: MONGO_DB_NAME environment variable not set. Using default 'ai_assistant_db'.")
    DB_NAME = 'ai_assistant_db'

# Initialize MongoDB client, database, and collection to None initially
client = None
db = None
users_collection = None

# Attempt to connect to MongoDB Atlas
try:
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    users_collection = db.users # Assign collection after successful db connection

    # Ping the database to verify the connection and authentication
    client.admin.command('ping')
    print("MongoDB Atlas connection successful!")
except ConnectionFailure as e:
    print(f"MongoDB Atlas connection failed (network/server not reachable): {e}")
    flash("Database connection error. The server could not be reached. Please try again later.", "danger")
except OperationFailure as e:
    print(f"MongoDB Atlas authentication failed: {e}")
    print("Please check your MongoDB Atlas username, password, and IP whitelist settings.")
    flash("Database authentication failed. Please check credentials or IP whitelist on MongoDB Atlas.", "danger")
except Exception as e:
    print(f"An unexpected error occurred during MongoDB Atlas connection: {e}")
    flash("An unexpected database error occurred during connection. Please contact support.", "danger")
finally:
    # If any error occurred, ensure client, db, users_collection are explicitly None
    if client is None or db is None:
        client = None
        db = None
        users_collection = None # Ensure collection is None if connection failed


# --- Flask-Login Configuration ---
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'

class User(UserMixin):
    def __init__(self, id, username, password_hash):
        self.id = str(id)
        self.username = username
        self.password_hash = password_hash

    @staticmethod
    def get_by_username(username):
        # Ensure users_collection is available before querying
        if users_collection is None:
            current_app.logger.error("MongoDB users_collection not available for get_by_username.")
            return None
        data = users_collection.find_one({'username': username})
        if data:
            return User(data['_id'], data['username'], data['password_hash'])
        return None

    @staticmethod
    def get_by_id(user_id):
        # Ensure users_collection is available before querying
        if users_collection is None:
            current_app.logger.error("MongoDB users_collection not available for get_by_id.")
            return None
        try:
            data = users_collection.find_one({'_id': ObjectId(user_id)})
            if data:
                return User(data['_id'], data['username'], data['password_hash'])
        except Exception as e:
            current_app.logger.error(f"User ID fetch error: {e}", exc_info=True)
        return None

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

@login_manager.user_loader
def load_user(user_id):
    return User.get_by_id(user_id)

# --- Firebase Firestore Configuration ---
FIREBASE_CONFIG = os.environ.get('FIREBASE_CONFIG')
db_firestore = None

if FIREBASE_CONFIG:
    try:
        # Load Firebase credentials from the JSON string in the environment variable
        firebase_credentials = json.loads(FIREBASE_CONFIG)
        cred = credentials.Certificate(firebase_credentials)
        firebase_admin.initialize_app(cred)
        db_firestore = firestore.client()
        print("Firestore initialized successfully!")
    except json.JSONDecodeError as e:
        print(f"Error decoding FIREBASE_CONFIG JSON: {e}. Ensure it's valid JSON in the environment variable.")
        db_firestore = None
    except Exception as e:
        print(f"Error initializing Firebase Admin SDK: {e}. Check FIREBASE_CONFIG format or content.")
        db_firestore = None
else:
    print("FIREBASE_CONFIG environment variable not found. Firestore will not be available for history.")


# --- Gemini API Configuration ---
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
gemini_model = None
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel("gemini-1.5-flash")
        print("Gemini API configured successfully!")
    except Exception as e:
        print(f"Error configuring Gemini API: {e}. Check GEMINI_API_KEY.")
        gemini_model = None
else:
    print("GEMINI_API_KEY environment variable not found. Gemini API will not be available.")


# --- Hugging Face API Configuration (for emojify, if still used) ---
HF_API = "https://api-inference.huggingface.co/models/mrm8488/t5-base-finetuned-emojify"
HF_API_TOKEN = os.getenv('HF_API_TOKEN')
HF_HEADERS = {"Authorization": f"Bearer {HF_API_TOKEN}"} if HF_API_TOKEN else {}

# --- Helper Functions ---

def get_gemini_answer(content_parts):
    if not gemini_model:
        current_app.logger.error("Gemini model not initialized. Cannot get answer.")
        flash("AI assistant is currently unavailable. Please try again later.", "danger")
        return None
    try:
        response = gemini_model.generate_content(content_parts)
        return getattr(response, "text", "").strip()
    except Exception as e:
        print(f"Gemini error during content generation: {e}")
        current_app.logger.error(f"Gemini generation failed: {e}", exc_info=True)
        flash("Error fetching AI answer. Please try again.", "danger")
        return None

def classify_query_type(prompt):
    coding_keywords = ["code", "python", "javascript", "java", "c++", "html", "css",
                       "react", "sql", "write a program", "function", "syntax",
                       "algorithm", "script", "json", "xml", "api", "backend", "frontend"]
    if any(keyword in prompt.lower() for keyword in coding_keywords):
        return "code"
    return "text"

def get_user_history(user_id, limit=10):
    if not db_firestore:
        print("Firestore is not initialized. Cannot retrieve history.")
        return []
    try:
        # Use a consistent app ID for Firestore paths
        app_id = os.environ.get('FLASK_APP_ID', 'default_ai_assistant_app')
        history_ref = db_firestore.collection(f"artifacts/{app_id}/users/{user_id}/search_history")
        docs = history_ref.order_by('timestamp', direction=firestore.Query.DESCENDING).limit(limit).stream()
        history_items = []
        for doc in docs:
            item = doc.to_dict()
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
        app_id = os.environ.get('FLASK_APP_ID', 'default_ai_assistant_app')
        history_ref = db_firestore.collection(f"artifacts/{app_id}/users/{user_id}/search_history")
        history_ref.add({
            'prompt': prompt,
            'answer': answer,
            'timestamp': firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        print(f"Error saving user history to Firestore: {e}")
        current_app.logger.error(f"Firestore history save failed: {e}", exc_info=True)

# --- Flask Routes ---

@app.route("/", methods=["GET", "POST"])
@login_required
def home():
    user_text_prompt = ""
    rendered_content = None
    is_code_response = False
    search_history = []

    user_id = current_user.get_id()

    # Always attempt to get history, it will return empty if Firestore is not available
    search_history = get_user_history(user_id)

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
                        mime_type = file.mimetype
                        content_parts.append({
                            "mime_type": mime_type,
                            "data": base64.b64encode(image_bytes).decode('utf-8')
                        })
                except Exception as e:
                    flash(f"Error processing file: {e}", "danger")
                    current_app.logger.error(f"File processing failed: {e}", exc_info=True)
            elif file.filename != '':
                flash("File type not allowed. Only .txt, .png, .jpg, .jpeg, .gif, .bmp, .webp are supported.", "danger")

        if content_parts:
            # Check if Gemini model is available before trying to query
            if not gemini_model:
                flash("AI assistant is not configured. Please check API key.", "danger")
                rendered_content = None
            else:
                query_type = classify_query_type(user_text_prompt)
                raw_answer = get_gemini_answer(content_parts)

                if raw_answer:
                    add_to_user_history(user_id, user_text_prompt, raw_answer)
                    search_history = get_user_history(user_id) # Refresh history after adding

                    if query_type == "code":
                        rendered_content = raw_answer
                        is_code_response = True
                    else:
                        lines = raw_answer.splitlines()
                        cleaned = [re.sub(r"^\s*[-–•]?\s*", "", line.strip()) for line in lines if line.strip()]
                        if cleaned:
                            cleaned[-1] += " 👋" # Add emoji to the last line of non-code answers
                        rendered_content = cleaned
                # else: Flash message handled by get_gemini_answer already, no need to duplicate.
        else:
            flash("Please enter a question or upload a file.", "warning")

    return render_template("index.html",
                           prompt=user_text_prompt,
                           rendered_content=rendered_content,
                           is_code_response=is_code_response,
                           search_history=search_history)

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('home'))

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        confirm_password = request.form.get("confirm_password")

        # Crucial check: Ensure users_collection is valid before use
        if users_collection is None:
            flash("Database connection not established. Please try again later.", "danger")
            return render_template("signup.html", title="Sign Up")

        # Input validation
        if not username or not password or not confirm_password:
            flash("All fields are required.", "danger")
        elif password != confirm_password:
            flash("Passwords do not match.", "danger")
        elif len(password) < 6:
            flash("Password must be at least 6 characters.", "danger")
        else:
            try:
                # Check for existing username only if inputs are valid
                if users_collection.find_one({'username': username}):
                    flash("Username already exists. Please choose a different one.", "danger")
                else:
                    hashed = generate_password_hash(password)
                    users_collection.insert_one({'username': username, 'password_hash': hashed})
                    flash("Account created successfully. You can now log in.", "success")
                    return redirect(url_for("login"))
            except Exception as e:
                current_app.logger.error(f"Error during signup database operation: {e}", exc_info=True)
                flash("An error occurred during registration. Please try again.", "danger")

    return render_template("signup.html", title="Sign Up")

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('home'))

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        # Crucial check: Ensure users_collection is valid before use
        if users_collection is None:
            flash("Database connection not established. Please try again later.", "danger")
            return render_template("login.html", title="Login")

        try:
            data = users_collection.find_one({'username': username})
            if data:
                user = User(data['_id'], data['username'], data['password_hash'])
                if user.check_password(password):
                    login_user(user)
                    flash(f"Welcome back, {user.username}!", "success")
                    return redirect(url_for('home'))
            flash("Invalid username or password.", "danger")
        except Exception as e:
            current_app.logger.error(f"Error during login database operation: {e}", exc_info=True)
            flash("An error occurred during login. Please try again.", "danger")

    return render_template("login.html", title="Login")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("home"))
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

