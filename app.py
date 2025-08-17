import threading
import os
import sqlite3
import json, time
from datetime import datetime
import re

from flask import Flask, request, jsonify, make_response
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv

try:
    from openai import OpenAI
    _oa = OpenAI()  # reads OPENAI_API_KEY from env
except Exception:
    _oa = None

# ---------- Basic sanity routes ----------
app = Flask(__name__)

@app.get("/")
def index():
    return "OK"

@app.get("/health")
def health():
    return "healthy", 200

# ---------- Environment ----------
load_dotenv()
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
PORT = int(os.getenv("PORT", "3000"))
if not SLACK_BOT_TOKEN or not SLACK_SIGNING_SECRET:
    raise RuntimeError("Missing SLACK_BOT_TOKEN or SLACK_SIGNING_SECRET in .env")

client = WebClient(token=SLACK_BOT_TOKEN)
verifier = SignatureVerifier(SLACK_SIGNING_SECRET)

# ---------- SQLite corpus ----------
DB_PATH = os.getenv("DB_PATH", "corpus.db")
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS corpus (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            user TEXT,
            channel TEXT,
            kind TEXT,
            text TEXT,
            payload_json TEXT,
            created_at TEXT
        )
        """)
init_db()

def log_corpus(kind, text="", user="", channel="", payload=None):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO corpus (ts,user,channel,kind,text,payload_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (str(time.time()), user or "", channel or "", kind, text or "",
                 json.dumps(payload or {}), datetime.utcnow().isoformat())
            )
    except Exception as e:
        print(f"[corpus] log error: {e}")

def verify_request(req) -> bool:
    try:
        return verifier.is_valid_request(req.get_data(), req.headers)
    except Exception as e:
        print(f"[signing] verification error: {e}")
        return False

# ---------- Interactivity ----------
@app.post("/slack/interactivity")
def interactivity():
    if not verify_request(request):
        return make_response("invalid signature", 401)

    payload = json.loads(request.form.get("payload", "{}"))
    print("[interactivity] type =", payload.get("type")); import sys; sys.stdout.flush()

    user_id = (payload.get("user") or {}).get("id", "")
    channel_id = (
        (payload.get("channel") or {}).get("id")
        or (payload.get("container") or {}).get("channel_id", "")
    )

    # Log interaction
    log_corpus("interaction", text=(payload.get("message") or {}).get("text", ""),
               user=user_id, channel=channel_id, payload=payload)

    # --- TEMP: always push a test modal on block_actions ---
    if payload.get("type") == "block_actions":
        return jsonify({
            "response_action": "push",
            "view": {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Breaker Brain"},
                "close": {"type": "plain_text", "text": "Close"},
                "blocks": [
                    {"type": "section",
                     "text": {"type": "mrkdwn",
                              "text": "✅ Modal push is working!"}}
                ]
            }
        })

    return make_response("", 200)

# ---------- Slash commands ----------
@app.post("/slack/commands")
def commands():
    if not verify_request(request):
        return make_response("invalid signature", 401)

    form = request.form
    cmd = form.get("command")
    user_id = form.get("user_id")
    channel_id = form.get("channel_id")
    text = form.get("text", "")

    log_corpus("command", text, user_id, channel_id, dict(form))

    if cmd == "/lens":
        try:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text="Which lens do you want to apply?",
                blocks=lens_picker_blocks(),
            )
        except SlackApiError as e:
            print("[/lens] Slack error:", e.response.get("error"))
        return jsonify({"response_type": "ephemeral", "text": "Pick a lens below ⬇️"})

    return jsonify({"response_type": "ephemeral", "text": f"Unsupported command `{cmd}`."})

# ---------- Block Kit builders ----------
def lens_picker_blocks():
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Which lens do you want to apply?*"}},
        {"type": "actions", "block_id": "lens_actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "CFO Skeptic"}, "action_id": "lens_cfo", "value": "cfo_skeptic"},
            {"type": "button", "text": {"type": "plain_text", "text": "Builder CEO"}, "action_id": "lens_builder", "value": "builder_ceo"},
            {"type": "button", "text": {"type": "plain_text", "text": "Scaler"}, "action_id": "lens_scaler", "value": "scaler"},
            {"type": "button", "text": {"type": "plain_text", "text": "Challenger"}, "action_id": "lens_challenger", "value": "challenger"},
            {"type": "button", "text": {"type": "plain_text", "text": "Operator"}, "action_id": "lens_operator", "value": "operator"}
        ]}
    ]

# ---------- Entrypoint ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=True)
