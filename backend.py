"""
DailyDo backend
---------------

Flow:
    Anonymous visitor
        -> POST /organize
        -> receives a structured task tree

    User chooses to save
        -> Google Sign-In
        -> server creates session
        -> frontend PUT /tree
        -> tree is stored with that user in Postgres

Important:
- /organize does NOT require authentication.
- /tree DOES require authentication.
- The backend never trusts a user_id/email supplied by the browser for tree storage.
- The authenticated user is determined from the HttpOnly session cookie.
"""

import os
import re
import json
import secrets
import hmac
import hashlib

import requests as http_requests
from dotenv import load_dotenv

from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

from flask import Flask, jsonify, request
from flask_cors import CORS

import db

load_dotenv()

app = Flask(__name__)

FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "http://127.0.0.1:8000")

CORS(
    app,
    origins=[FRONTEND_ORIGIN],
    supports_credentials=True
)

ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")
COOKIE_NAME = "__Host-session"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)


db.init_pool()
db.init_db()


PROMPT_TEMPLATE = """
Convert this messy brain dump into a well-organized hierarchical task list.

Rules:
- Group related tasks under short category headings when it genuinely helps.
- Category headings should be 2-6 words.
- Break vague or large tasks into 2-4 concrete sub-steps only when genuinely useful.
- Keep task text short, specific, and action-oriented.
- Preserve the user's actual intent.
- Do not invent unrelated tasks.
- Do not turn ordinary details into separate tasks unless useful.
- If an item is already concrete, keep it concrete.
- Return at most 12 top-level items.
- Do not include explanations.

Respond with ONLY a raw JSON array, no markdown fences, no commentary.

Exactly this shape:
[{{"text": "Category or task", "children": [{{"text": "sub task", "children": []}}]}}]

If a top-level item has no natural sub-items, use an empty children array.

Brain dump:
\"\"\"
{dump}
\"\"\"
"""


def strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def normalize_nodes(nodes, depth=0):
    """
    Validate the Gemini output and cap recursion so a bad model response
    cannot create an absurdly deep tree.
    """
    clean = []

    if not isinstance(nodes, list):
        return clean

    if depth > 6:
        return clean

    for item in nodes:
        if not isinstance(item, dict):
            continue

        text = str(item.get("text", "")).strip()

        if not text:
            continue

        children = normalize_nodes(
            item.get("children", []),
            depth + 1
        )

        clean.append({
            "text": text[:300],
            "children": children
        })

    return clean


def get_current_user_id():
    """
    Resolve the logged-in user from the HttpOnly session cookie.

    Never accept user_id/google_id/email from the frontend as the authority
    for account-owned data.
    """
    session_id = request.cookies.get("__Host-session")

    if not session_id:
        return None

    return db.get_session(session_id)


def require_user():
    user_id = get_current_user_id()

    if not user_id:
        return None, (
            jsonify({
                "error": "authentication_required",
                "message": "Please sign in to access your saved plan."
            }),
            401
        )

    return user_id, None


@app.route("/auth/google", methods=["POST"])
def google_login():
    if not GOOGLE_CLIENT_ID:
        return jsonify({
            "error": "GOOGLE_CLIENT_ID is not configured"
        }), 500

    payload = request.get_json(silent=True) or {}
    credential = payload.get("credential")

    if not credential:
        return jsonify({
            "error": "No Google credential provided"
        }), 400

    try:
        google_user = id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            GOOGLE_CLIENT_ID
        )
    except ValueError as e:
        print("Token verification error:", str(e))
        return jsonify({
            "error": "Invalid Google credential"
        }), 401

    google_id = google_user.get("sub")
    email = (google_user.get("email") or "").strip().lower()
    name = google_user.get("name") or ""
    picture = google_user.get("picture") or ""

    if not google_id:
        return jsonify({"error": "Google account has no ID"}), 400

    if not email:
        return jsonify({
            "error": "Google account has no email"
        }), 400

    try:
        db.create_user(
            google_id,
            email,
            name,
            picture
        )

        user_id = db.get_user_id(google_id)

        if not user_id:
            return jsonify({
                "error": "Could not create/find the DailyDo user"
            }), 500

        # One active session is enough for this MVP.
        session_id = secrets.token_urlsafe(32)
        db.create_session(session_id, user_id)

        response = jsonify({
            "success": True,
            "user": {
                "google_id": google_id,
                "email": email,
                "name": name,
                "picture": picture
            }
        })

        # Local development:
        # secure=False is necessary over http://127.0.0.1.
        #
        # When deployed behind HTTPS, set COOKIE_SECURE=true.
        secure_cookie = (
            os.environ.get("COOKIE_SECURE", "false").lower() == "true"
        )

        response.set_cookie(
            # key="__Host-session",
            key=COOKIE_NAME,
            value=session_id,
            httponly=True,
            secure=secure_cookie,
            samesite=None,
            path="/",
            max_age=60 * 60 * 24 * 30
        )

        return response

    except Exception as e:
        print("Google login/database error:", repr(e))
        return jsonify({
            "error": "Could not create the DailyDo session"
        }), 500


@app.route("/auth/me", methods=["GET"])
def auth_me():
    user_id = get_current_user_id()

    if not user_id:
        return jsonify({
            "authenticated": False
        }), 401

    row = db.get_user(user_id)

    if not row:
        return jsonify({
            "authenticated": False
        }), 401

    return jsonify({
        "authenticated": True,
        "user": {
            "google_id": row[0],
            "email": row[1],
            "name": row[2],
            "picture": row[3]
        }
    })


@app.route("/auth/logout", methods=["POST"])
def logout():
    # session_id = request.cookies.get("__Host-session")
    session_id = request.cookies.get(COOKIE_NAME)

    if session_id:
        db.delete_session(session_id)

    response = jsonify({
        "success": True
    })

    response.delete_cookie(
        key=COOKIE_NAME,
        path="/"
    )

    return response


@app.route("/tree", methods=["GET"])
def get_tree():
    user_id, error = require_user()

    if error:
        return error

    try:
        tree = db.get_user_tree(user_id)

        return jsonify({
            "tree": tree
        })

    except Exception as e:
        print("GET /tree error:", repr(e))
        return jsonify({
            "error": "Could not load your saved plan"
        }), 500


@app.route("/tree", methods=["PUT"])
def save_tree():
    user_id, error = require_user()

    if error:
        return error

    payload = request.get_json(silent=True) or {}
    tree = payload.get("tree")

    if not isinstance(tree, list):
        return jsonify({
            "error": "tree must be an array"
        }), 400

    # Basic server-side protection against accidentally huge payloads.
    try:
        serialized = json.dumps(tree, separators=(",", ":"))

        if len(serialized.encode("utf-8")) > 1_000_000:
            return jsonify({
                "error": "tree is too large"
            }), 413

        db.save_user_tree(user_id, tree)

        return jsonify({
            "success": True
        })

    except (TypeError, ValueError):
        return jsonify({
            "error": "tree contains invalid data"
        }), 400

    except Exception as e:
        print("PUT /tree error:", repr(e))
        return jsonify({
            "error": "Could not save your plan"
        }), 500


@app.route("/status", methods=["GET"])
def status():
    user_id, error = require_user()

    if error:
        return error

    try:
        status_data = db.get_user_status(user_id)

        if status_data is None:
            return jsonify({
                "error": "User not found"
            }), 404

        return jsonify(status_data)

    except Exception as e:
        return jsonify({
            "error": str(e)
        }), 500


@app.route("/organize", methods=["POST"])
def organize():
    """
    Public MVP endpoint.

    A visitor does NOT need an account to experience DailyDo's core value.
    Account authentication is only needed to persist the resulting tree.
    """
    if not GEMINI_API_KEY:
        return jsonify({
            "error": "GEMINI_API_KEY is not set on the server"
        }), 500

    payload = request.get_json(silent=True) or {}
    dump_text = (payload.get("text") or "").strip()

    if not dump_text:
        return jsonify({
            "error": "No text provided"
        }), 400

    # Prevent accidental/abusive giant requests.
    if len(dump_text) > 12000:
        return jsonify({
            "error": "Brain dump is too long. Please keep it under 12,000 characters."
        }), 413

    prompt = PROMPT_TEMPLATE.format(dump=dump_text)

    request_body = {
        "contents": [{"role": "user","parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "responseMimeType": "application/json"
        }
    }

    try:
        response = http_requests.post(
            GEMINI_URL,
            headers={
                "x-goog-api-key": GEMINI_API_KEY,
                "Content-Type": "application/json"
            },
            json=request_body,
            timeout=60
        )

        response.raise_for_status()
        data = response.json()

        candidates = data.get("candidates", [])
        if not candidates:
            return jsonify({
                "error": "Gemini returned no candidates"
            }), 502

        parts = candidates[0].get("content", {}).get("parts", [])
        raw_text = "".join(
            p.get("text", "")
            for p in parts
        )

        raw_text = strip_code_fences(raw_text)
        parsed = json.loads(raw_text)
        nodes = normalize_nodes(parsed)

        if not nodes:
            return jsonify({
                "error": "DailyDo couldn't find actionable items in that brain dump."
            }), 422

        # Usage accounting can be added here later using the authenticated
        # user or a proper anonymous rate-limit mechanism.
        return jsonify({
            "nodes": nodes
        })

    except http_requests.exceptions.HTTPError as e:
        return jsonify({
            "error": f"Gemini API error: {e}",
            "details": response.text
        }), 502

    except json.JSONDecodeError as e:
        return jsonify({
            "error": f"Could not parse Gemini's response as JSON: {e}",
            "raw": raw_text
        }), 502

    except http_requests.exceptions.RequestException as e:
        return jsonify({
            "error": f"Request to Gemini failed: {e}"
        }), 502

    except Exception as e:
        print("POST /organize error:", repr(e))
        return jsonify({
            "error": "Unexpected server error while organizing the brain dump"
        }), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "model": GEMINI_MODEL,
        "key_configured": bool(GEMINI_API_KEY),
        "db_configured": bool(db.DATABASE_URL)
    })


# Keep your existing Razorpay/admin code here when you are ready to
# reconnect payments. It should identify accounts from verified payment
# data, not from frontend claims.


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )
