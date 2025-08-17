import os, json, sqlite3, time
from datetime import datetime
from flask import Flask, request, jsonify, make_response
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv

# ---------- Environment ----------
load_dotenv()
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
PORT = int(os.getenv("PORT", "3000"))
if not SLACK_BOT_TOKEN or not SLACK_SIGNING_SECRET:
    raise RuntimeError("Missing SLACK_BOT_TOKEN or SLACK_SIGNING_SECRET in .env")

# ---------- App / Slack clients ----------
app = Flask(__name__)
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

# ---------- Health / routes ----------
@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/routes")
def routes():
    return {"routes": sorted(r.rule for r in app.url_map.iter_rules())}

# ---------- Events API ----------
@app.post("/slack/events")
def slack_events():
    if not verify_request(request):
        return make_response("invalid signature", 401)

    body = request.get_json(silent=True) or {}

    if body.get("type") == "url_verification":
        return jsonify({"challenge": body.get("challenge")})

    if body.get("type") == "event_callback":
        event = body.get("event", {})

        # Log human messages (ignore bot messages)
        if event.get("type") == "message" and not event.get("bot_id"):
            log_corpus("event", event.get("text",""), event.get("user",""), event.get("channel",""), event)

        # Optional helper if someone @mentions the app
        if event.get("type") == "app_mention":
            try:
                client.chat_postMessage(
                    channel=event.get("channel"),
                    thread_ts=event.get("ts"),
                    text="Try `/lens` to pick a lens, or `/decide <prompt>` for a decision brief."
                )
            except Exception as e:
                print("[events] post error:", repr(e))

    return make_response("", 200)

# ---------- Interactivity (buttons) ----------
@app.post("/slack/interactivity")
def interactivity():
    if not verify_request(request):
        return make_response("invalid signature", 401)

    payload_raw = request.form.get("payload", "{}")
    try:
        payload = json.loads(payload_raw)
    except Exception:
        payload = {}

    user_id = (payload.get("user") or {}).get("id", "")
    channel_id = (
        (payload.get("channel") or {}).get("id")
        or (payload.get("container") or {}).get("channel_id", "")
    )

    log_corpus("interaction", (payload.get("message") or {}).get("text",""),
               user_id, channel_id, payload)

    # Button clicks
    if payload.get("type") == "block_actions":
        action = (payload.get("actions") or [{}])[0]
        aid = action.get("action_id", "")
        selected = action.get("value", "")

        # A) Publish Decision Template to channel
        if aid == "post_brief":
            try:
                original_blocks = (payload.get("message") or {}).get("blocks", [])
                client.chat_postMessage(channel=channel_id, text="Decision Template", blocks=original_blocks)
                client.chat_postEphemeral(channel=channel_id, user=user_id, text="Posted to channel ✅")
                log_corpus("decision_posted", "decision template posted", user_id, channel_id, payload)
            except Exception as e:
                print("[interactivity/post_brief] error:", repr(e))
            return make_response("", 200)

        # B) Lens buttons → send a helpful checklist
        if aid.startswith("lens_") or selected in {"cfo_skeptic","builder_ceo","scaler","challenger","operator"}:
            responses = {
                "cfo_skeptic": "CFO Skeptic checklist:\n• Payback < 12 months?\n• Cash vs EBITDA?\n• Sensitivity to accuracy deltas?\n• Hidden costs (services/data/change mgmt)?",
                "builder_ceo": "Builder CEO lens:\n• Ship a thin slice this week.\n• What becomes faster?\n• Delete work, don’t add it.\n• 90-day compounding?",
                "scaler":      "Scaler lens:\n• Repeatable playbook?\n• Unit econ at 10× volume?\n• Runbooks + guardrails?",
                "challenger":  "Challenger lens:\n• Which sacred cow to challenge?\n• If starting fresh, is this the path?",
                "operator":    "Operator lens:\n• Who owns the KPI?\n• SOP + SLA?\n• Rollback plan if metrics slip?"
            }
            try:
                client.chat_postEphemeral(
                    channel=channel_id,
                    user=user_id,
                    text=responses.get(selected, f"Lens applied: {selected}")
                )
            except Exception as e:
                print("[interactivity/lens] error:", repr(e))
            return make_response("", 200)

        # Unknown action — still ack
        return make_response("", 200)

    # Modal submissions (only used if you add the /decide modal later)
    # ---------- Modal submissions (Decision Brief) ----------
    if payload.get("type") == "view_submission" and (payload.get("view") or {}).get("callback_id") == "decide_modal":
        try:
            view = payload.get("view", {})
            state = (view.get("state") or {}).get("values", {})

            def get(bid):
                return (((state.get(bid) or {}).get("v") or {}).get("value") or "").strip()

            title = get("title") or "Decision"
            context = get("context")
            options_text = get("options")
            recommendation = get("recommendation")
            risks = get("risks")

            # Recover channel/user to reply ephemerally
            pm = json.loads(view.get("private_metadata") or "{}")
            chan = pm.get("channel_id") or channel_id
            usr = pm.get("user_id") or user_id

            blocks = [
                {"type":"header","text":{"type":"plain_text","text": title[:150]}},
                {"type":"section","text":{"type":"mrkdwn","text": f"*Context*\n{context or '—'}"}},
                {"type":"section","text":{"type":"mrkdwn","text": f"*Options*\n{options_text or '—'}"}},
                {"type":"section","text":{"type":"mrkdwn","text": f"*Recommendation*\n{recommendation or '—'}"}}
            ]
            if risks:
                blocks.append({"type":"section","text":{"type":"mrkdwn","text": f"*Risks & Mitigations*\n{risks}"}})

            # 🔽 Add the publish button
            blocks.append({
                "type":"actions",
                "block_id":"brief_actions",
                "elements":[
                    {"type":"button",
                     "text":{"type":"plain_text","text":"Post to channel"},
                     "action_id":"post_brief",
                     "value":"post"}
                ]
            })

            client.chat_postEphemeral(channel=chan, user=usr, text="Decision Brief", blocks=blocks)
            log_corpus("decision", f"{title} | {recommendation}", usr, chan, payload)
        except Exception as e:
            print("[interactivity/decide_modal] error:", repr(e))
        return jsonify({"response_action":"clear"})

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
        # Minimal ack so Slack accepts the command
        ack = jsonify({"response_type": "ephemeral", "text": "Pick a lens below ⬇️"})
        try:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text="Which lens do you want to apply?",
                blocks=lens_picker_blocks(),
            )
        except SlackApiError as e:
            print("[/lens] Slack error:", e.response.get("error"))
        except Exception as e:
            print("[/lens] Other error:", repr(e))
        return ack

    if cmd == "/decide":
        trigger_id = form.get("trigger_id")
        pm = json.dumps({"channel_id": channel_id, "user_id": user_id})

        view = {
            "type": "modal",
            "callback_id": "decide_modal",
            "private_metadata": pm,
            "title": {"type": "plain_text", "text": "Decision Brief"},
            "submit": {"type": "plain_text", "text": "Create"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {"type":"input","block_id":"title","element":{"type":"plain_text_input","action_id":"v","placeholder":{"type":"plain_text","text":"One-line decision title"}},"label":{"type":"plain_text","text":"Title"}},
                {"type":"input","block_id":"context","element":{"type":"plain_text_input","action_id":"v","multiline":True,"placeholder":{"type":"plain_text","text":"Context, constraints, what matters"}},"label":{"type":"plain_text","text":"Context"}},
                {"type":"input","block_id":"options","element":{"type":"plain_text_input","action_id":"v","multiline":True,"placeholder":{"type":"plain_text","text":"Option A\\nOption B\\nOption C"}},"label":{"type":"plain_text","text":"Options (one per line)"}},
                {"type":"input","block_id":"recommendation","element":{"type":"plain_text_input","action_id":"v","placeholder":{"type":"plain_text","text":"Your recommended option"}},"label":{"type":"plain_text","text":"Recommendation"}},
                {"type":"input","block_id":"risks","optional":True,"element":{"type":"plain_text_input","action_id":"v","multiline":True,"placeholder":{"type":"plain_text","text":"Risk → Mitigation"}},"label":{"type":"plain_text","text":"Risks & Mitigations"}}
            ]
        }

        try:
            client.views_open(trigger_id=trigger_id, view=view)
        except Exception as e:
            print("[/decide] views_open error:", repr(e))
            # Fallback: if modal fails for any reason, send the template
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text="Decision Template",
                blocks=decision_template_blocks(form.get("text","") or "No prompt provided.")
            )

        # Short ack so Slack is happy
        return jsonify({"response_type": "ephemeral", "text": "Opening decision modal…"})


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

def decision_template_blocks(prompt: str):
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "Decision Template"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Prompt:* {prompt or 'No prompt provided.'}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Options*\n1) Option A\n2) Option B\n3) Option C"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Recommendation*\n<fill in>"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Risks & Mitigations*\n- Risk 1 → Mitigation\n- Risk 2 → Mitigation"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "Use `/decide {prompt}` to regenerate."}]},
        {"type": "actions", "block_id": "brief_actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Post to channel"}, "action_id": "post_brief", "value": "post"}
        ]}
    ]

# ---------- Corpus export (optional) ----------
@app.get("/corpus/export")
def export_corpus():
    import os, csv, sqlite3

    # Require admin token header
    token = request.headers.get("X-Admin-Token", "")
    if token != os.getenv("ADMIN_TOKEN", ""):
        return {"ok": False, "error": "unauthorized"}, 401

    db_path = os.getenv("DB_PATH", "corpus.db")          # respects your env setting
    out_path = "/var/data/corpus_export.csv"             # write onto the mounted disk

    with sqlite3.connect(db_path) as conn, open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id","ts","user","channel","kind","text","payload_json","created_at"])
        for row in conn.execute(
            "SELECT id,ts,user,channel,kind,text,payload_json,created_at "
            "FROM corpus ORDER BY id"
        ):
            w.writerow(row)

    return {"ok": True, "path": out_path}, 200


# ---------- Entrypoint ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=True)
