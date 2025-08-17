import os, json, sqlite3, time
import threading
try:
    from openai import OpenAI
    _oa = OpenAI()  # reads OPENAI_API_KEY from env
except Exception:
    _oa = None
from datetime import datetime
from flask import Flask, request, jsonify, make_response
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv
import re

MAX_LENS_WORDS = int(os.getenv("MAX_LENS_WORDS", "160"))

def _word_cap(md: str, limit: int = MAX_LENS_WORDS) -> str:
    words = md.split()
    if len(words) <= limit:
        return md
    return " ".join(words[:limit]) + " …"

def _normalize_headers(md: str) -> str:
    # Ensure expected headings are bold and in order
    sections = {
        "Verdict": r"(?:^|\n)\*?Verdict\*?\s*[:—-]",
        "Payback": r"(?:^|\n)\*?Payback\*?\s*[:—-]",
        "Key Points": r"(?:^|\n)\*?Key Points\*?\s*[:—-]",
        "Risks & Mitigations": r"(?:^|\n)\*?Risks\s*&\s*Mitigations\*?\s*[:—-]"
    }
    out = md
    for label, pat in sections.items():
        out = re.sub(pat, f"\n*{label}* —", out, flags=re.IGNORECASE)
    return out.strip()


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

    # Parse payload
    payload = json.loads(request.form.get("payload", "{}"))
    # === TEMP: push a test modal for ANY button click (remove after we see it) ===
    if payload.get("type") == "block_actions":
        return jsonify({
            "response_action": "push",
            "view": {
                "type": "modal",
                "title": {"type": "plain_text", "text": "Test Modal"},
                "close": {"type": "plain_text", "text": "Close"},
                "blocks": [
                    {"type":"section","text":{"type":"mrkdwn","text":"If you see this, push works ✅"}}
                ],
                "callback_id": "noop"
            }
        })
    # === end TEMP block ===

    print("[interactivity] type =", payload.get("type"))

    user_id = (payload.get("user") or {}).get("id", "")
    channel_id = (
        (payload.get("channel") or {}).get("id")
        or (payload.get("container") or {}).get("channel_id", "")
    )

    # Log the interaction
    log_corpus(
        "interaction",
        text=(payload.get("message") or {}).get("text", ""),
        user=user_id, channel=channel_id, payload=payload
    )

    # ---------- 1) MESSAGE SHORTCUT ----------
    if payload.get("type") == "message_action" and payload.get("callback_id") == "apply_lens_action":
        try:
            client.chat_postEphemeral(
                channel=(payload.get("channel") or {}).get("id", channel_id),
                user=user_id,
                text="Which lens do you want to apply?",
                blocks=lens_picker_blocks(),
            )
            print("[shortcut] posted lens picker OK")
        except Exception as e:
            try:
                from slack_sdk.errors import SlackApiError
                if isinstance(e, SlackApiError):
                    print("[shortcut] Slack error:", e.response.get("error"))
                else:
                    print("[shortcut] other error:", repr(e))
            except Exception:
                print("[shortcut] error:", repr(e))
        return make_response("", 200)

    # ---------- 2) BUTTON CLICKS ----------
    if payload.get("type") == "block_actions":
        action = (payload.get("actions") or [{}])[0]
        aid = action.get("action_id", "")
        selected = action.get("value", "")

        # publish final analysis to channel
        if aid == "post_lens":
            try:
                original_blocks = (payload.get("message") or {}).get("blocks", [])
                client.chat_postMessage(channel=channel_id, text="Lens Analysis", blocks=original_blocks)
                client.chat_postEphemeral(channel=channel_id, user=user_id, text="Posted to channel ✅")
                log_corpus("lens_posted", "lens analysis posted", user_id, channel_id, payload)
            except Exception as e:
                print("[interactivity/post_lens] error:", repr(e))
            return make_response("", 200)

        # lens selected
        if aid.startswith("lens_") or selected in {"cfo_skeptic","builder_ceo","scaler","challenger","operator"}:
            lens = selected
            label = LENS_NAMES.get(lens, lens)

            # Determine if we have real context (message shortcut path)
            orig_text = (payload.get("message") or {}).get("text", "") or ""
            picker_text = "which lens do you want to apply"
            has_context = bool(orig_text.strip()) and picker_text not in orig_text.lower()
            print("[lens] aid=", aid, "selected=", selected, "has_context=", has_context)

            # Quick ack so user sees activity
            try:
                client.chat_postEphemeral(channel=channel_id, user=user_id, text=f"⏳ {label} analysis…")
            except Exception as e:
                print("[lens ack] error:", repr(e))

            if has_context:
                # Run synchronously so you see the result now
                try:
                    md = run_lens(lens, orig_text)
                    client.chat_postEphemeral(
                        channel=channel_id, user=user_id, text=label,
                        blocks=[{"type":"section","text":{"type":"mrkdwn","text": md[:2900]}}],
                    )
                    print("[lens sync] posted result OK")
                except Exception as e:
                    print("[lens sync] post error:", repr(e))
                return make_response("", 200)

            # No usable context → PUSH a modal in the HTTP response (most reliable)
            view = {
                "type": "modal",
                "callback_id": "lens_modal",
                "private_metadata": json.dumps({
                    "lens": lens, "channel_id": channel_id, "user_id": user_id
                }),
                "title": {"type": "plain_text", "text": f"{label} Lens"},
                "submit": {"type": "plain_text", "text": "Analyze"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": [
                    {"type":"input","block_id":"ctx",
                     "element":{"type":"plain_text_input","action_id":"v","multiline":True,
                                "placeholder":{"type":"plain_text","text":"Paste the proposal or context to analyze…"}},
                     "label":{"type":"plain_text","text":"Context"}}
                ]
            }
            return jsonify({"response_action": "push", "view": view})

        # Unknown action
        return make_response("", 200)

    # ---------- 3) MODAL SUBMISSIONS ----------
    if payload.get("type") == "view_submission":
        view = payload.get("view") or {}
        cb = view.get("callback_id", "")
        if cb == "lens_modal":
            try:
                pm = json.loads(view.get("private_metadata") or "{}")
                state = (view.get("state") or {}).get("values", {})
                ctx = (((state.get("ctx") or {}).get("v") or {}).get("value") or "").strip()
                lens = pm.get("lens")
                chan = pm.get("channel_id")
                usr = pm.get("user_id")

                try:
                    client.chat_postEphemeral(channel=chan, user=usr, text=f"⏳ {LENS_NAMES.get(lens,lens)} analysis…")
                except Exception as e:
                    print("[lens modal ack] error:", repr(e))

                # background is fine here; we already responded with a modal
                threading.Thread(
                    target=_post_lens_result_async,
                    args=(lens, ctx, chan, usr),
                    daemon=True
                ).start()
            except Exception as e:
                print("[lens modal submit] error:", repr(e))
            return jsonify({"response_action": "clear"})

    # default ack
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
    trigger_id = form.get("trigger_id")

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
        # Open the decision modal via views_open (slash-commands path)
        pm = json.dumps({"channel_id": channel_id, "user_id": user_id})
        view = {
            "type": "modal",
            "callback_id": "decide_modal",
            "private_metadata": pm,
            "title": {"type": "plain_text", "text": "Decision Brief"},
            "submit": {"type": "plain_text", "text": "Create"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {"type":"input","block_id":"title",
                 "element":{"type":"plain_text_input","action_id":"v","placeholder":{"type":"plain_text","text":"One-line decision title"}},
                 "label":{"type":"plain_text","text":"Title"}},
                {"type":"input","block_id":"context",
                 "element":{"type":"plain_text_input","action_id":"v","multiline":True,"placeholder":{"type":"plain_text","text":"Context, constraints, what matters"}},
                 "label":{"type":"plain_text","text":"Context"}},
                {"type":"input","block_id":"options",
                 "element":{"type":"plain_text_input","action_id":"v","multiline":True,"placeholder":{"type":"plain_text","text":"Option A\nOption B\nOption C"}},
                 "label":{"type":"plain_text","text":"Options (one per line)"}},
                {"type":"input","block_id":"recommendation",
                 "element":{"type":"plain_text_input","action_id":"v","placeholder":{"type":"plain_text","text":"Your recommended option"}},
                 "label":{"type":"plain_text","text":"Recommendation"}},
                {"type":"input","block_id":"risks","optional":True,
                 "element":{"type":"plain_text_input","action_id":"v","multiline":True,"placeholder":{"type":"plain_text","text":"Risk → Mitigation"}},
                 "label":{"type":"plain_text","text":"Risks & Mitigations"}}
            ]
        }
        try:
            client.views_open(trigger_id=trigger_id, view=view)
        except Exception as e:
            print("[/decide] views_open error:", repr(e))
        return jsonify({"response_type": "ephemeral", "text": "Opening decision modal…"})
    
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

LENS_NAMES = {
    "cfo_skeptic": "CFO Skeptic",
    "builder_ceo": "Builder CEO",
    "scaler": "Scaler",
    "challenger": "Challenger",
    "operator": "Operator",
}

def _fallback_lens_text(lens: str, text: str) -> str:
    # Your existing checklists as a safe fallback
    responses = {
        "cfo_skeptic": "CFO Skeptic checklist:\n• Payback < 12 months?\n• Cash vs EBITDA?\n• Sensitivity to accuracy deltas?\n• Hidden costs (services/data/change mgmt)?",
        "builder_ceo": "Builder CEO lens:\n• Ship a thin slice this week.\n• What becomes faster?\n• Delete work, don’t add it.\n• 90-day compounding?",
        "scaler":      "Scaler lens:\n• Repeatable playbook?\n• Unit econ at 10× volume?\n• Runbooks + guardrails?",
        "challenger":  "Challenger lens:\n• Which sacred cow to challenge?\n• If starting fresh, is this the path?",
        "operator":    "Operator lens:\n• Who owns the KPI?\n• SOP + SLA?\n• Rollback plan if metrics slip?"
    }
    return responses.get(lens, f"Lens applied: {lens}")

def run_lens(lens: str, text: str) -> str:
    """
    Returns Slack-friendly markdown. Uses OpenAI if OPENAI_API_KEY is set; otherwise falls back.
    Always normalizes headings and caps length.
    """
    if not _oa:  # no API key -> use checklist
        md = _fallback_lens_text(lens, text)
        md = _normalize_headers(md)
        return _word_cap(md, MAX_LENS_WORDS)

    focus = {
        "cfo_skeptic":   "payback period, cash impact vs EBITDA, sensitivity to forecast deltas, hidden costs",
        "builder_ceo":   "shipping thin slices this week, deleting work (not adding), 90-day compounding",
        "scaler":        "repeatability, unit economics at 10× volume, runbooks and guardrails",
        "challenger":    "sacred cows to challenge, blank-sheet alternative",
        "operator":      "KPI ownership, SOP/SLA strength, rollback plans",
    }.get(lens, "key decision criteria")

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    prompt = f"""
You are the {LENS_NAMES.get(lens,lens)} evaluating the proposal below.

Return Slack-friendly Markdown under {MAX_LENS_WORDS} words with these headings, in this exact order:

*Verdict* — **Go**/**Gate**/**Don't** with 1-sentence why.
*Payback* — Best estimate in months (or “n/a”).
*Key Points* — 4–6 bullets focused on {focus}.
*Risks & Mitigations* — 2–3 bullets.

No code fences. No intro/outro. Keep it crisp.

Proposal:
{text.strip()[:4000]}
""".strip()

    try:
        resp = _oa.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=450,
        )
        md = resp.choices[0].message.content.strip()
    except Exception as e:
        print("[run_lens] LLM error:", repr(e))
        md = _fallback_lens_text(lens, text)

    # Always apply formatting & cap length
    md = _normalize_headers(md)
    md = _word_cap(md, MAX_LENS_WORDS)
    return md

def _post_lens_result_async(lens: str, text: str, channel_id: str, user_id: str):
    md = run_lens(lens, text)
    try:
        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text=LENS_NAMES.get(lens, lens),
            blocks=[{"type":"section","text":{"type":"mrkdwn","text": md[:2900]}}],
        )
    except Exception as e:
        print("[post_lens] Slack error:", repr(e))

# ---------- Entrypoint ----------

# --- JSON APIs for direct testing (no Slack) ---
@app.post("/api/lens")
def api_lens():
    data = request.get_json(silent=True) or {}
    lens = data.get("lens", "cfo_skeptic")
    text = data.get("text", "") or ""
    # use the real runner if present; otherwise fallback checklist
    try:
        md = run_lens(lens, text)
    except Exception:
        md = _fallback_lens_text(lens, text)
    log_corpus("api_lens", text, "", "", data)
    return {"ok": True, "lens": lens, "result": md}, 200

@app.post("/api/decide")
def api_decide():
    d = request.get_json(silent=True) or {}
    title = d.get("title", "Decision")
    context = d.get("context", "")
    options = d.get("options", "")
    rec = d.get("recommendation", "")
    risks = d.get("risks", "")
    brief_md = (
        f"# {title}\n\n"
        f"**Context**\n{context or '—'}\n\n"
        f"**Options**\n{options or '—'}\n\n"
        f"**Recommendation**\n{rec or '—'}\n\n"
        f"**Risks & Mitigations**\n{risks or '—'}\n"
    )
    log_corpus("api_decide", title, "", "", d)
    return {"ok": True, "brief_md": brief_md}, 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=True)
