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
import datetime
from dotenv import load_dotenv
import json

import firebase_admin
from firebase_admin import credentials, firestore, auth

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')

MONGO_URI = os.environ.get('MONGO_URI')
DB_NAME = os.environ.get('MONGO_DB_NAME')

if not MONGO_URI:
    print("WARNING: MONGO_URI environment variable not set. Using default placeholder. "
          "Please set MONGO_URI with your MongoDB Atlas connection string.")
    MONGO_URI = 'mongodb+srv://ai-generation:<F3G8zKs5BPNX53qI>@cluster0.18rtbk6.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0'

if not DB_NAME:
    print("WARNING: MONGO_DB_NAME environment variable not set. Using default 'ai_assistant_db'.")
    DB_NAME = 'ai_assistant_db'


client = MongoClient(MONGO_URI)
db = client[DB_NAME]
users_collection = db.users

try:
    client.admin.command('ping')
    print("MongoDB connected successfully!")
except Exception as e:
    print(f"MongoDB connection failed: {e}")
    # You might want to raise an exception or handle this more gracefully in production
    # For now, it will print the error and continue, but subsequent DB operations will fail.

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

FIREBASE_CONFIG = os.environ.get('FIREBASE_CONFIG')
db_firestore = None

if FIREBASE_CONFIG:
    try:
        firebase_credentials = json.loads(FIREBASE_CONFIG)
        cred = credentials.Certificate(firebase_credentials)
        firebase_admin.initialize_app(cred)
        db_firestore = firestore.client()
        print("Firestore initialized successfully!")
    except Exception as e:
        print(f"Error initializing Firebase Admin SDK: {e}. Check FIREBASE_CONFIG format.")
else:
    print("FIREBASE_CONFIG environment variable not found. Firestore will not be available for history.")

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel("gemini-1.5-flash")
    print("Gemini API configured successfully!")
else:
    print("GEMINI_API_KEY environment variable not found. Gemini API will not be available.")
    gemini_model = None

HF_API = "https://api-inference.huggingface.co/models/mrm8488/t5-base-finetuned-emojify"
HF_API_TOKEN = os.getenv('HF_API_TOKEN')
HF_HEADERS = {"Authorization": f"Bearer {HF_API_TOKEN}"} if HF_API_TOKEN else {}

def get_gemini_answer(content_parts):
    if not gemini_model:
        current_app.logger.error("Gemini model not initialized.")
        return None
    try:
        response = gemini_model.generate_content(content_parts)
        return getattr(response, "text", "").strip()
    except Exception as e:
        print(f"Gemini error: {e}")
        current_app.logger.error(f"Gemini generation failed: {e}", exc_info=True)
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
        app_id = os.environ.get('__app_id', 'default_app')
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
        app_id = os.environ.get('__app_id', 'default_app')
        history_ref = db_firestore.collection(f"artifacts/{app_id}/users/{user_id}/search_history")
        history_ref.add({
            'prompt': prompt,
            'answer': answer,
            'timestamp': firestore.SERVER_TIMESTAMP
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

    user_id = current_user.get_id()

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
            query_type = classify_query_type(user_text_prompt)

            raw_answer = get_gemini_answer(content_parts)

            if raw_answer:
                add_to_user_history(user_id, user_text_prompt, raw_answer)
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
                           search_history=search_history)

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('home'))

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        confirm_password = request.form.get("confirm_password")

        # Check if users_collection is properly initialized and connected
        if users_collection is None:
            flash("Database not connected. Please try again later.", "danger")
            return render_template("signup.html", title="Sign Up")

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

        # Check if users_collection is properly initialized and connected
        if users_collection is None:
            flash("Database not connected. Please try again later.", "danger")
            return render_template("login.html", title="Login")

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

if __name__ == "__main__":
    app.run(debug=True)
