# -*- coding: utf-8 -*-
import os
import uuid
import json
import smtplib
import datetime
from typing import Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

from flask import Flask, request, send_from_directory, render_template, abort, jsonify, url_for

VERSION = "proofok-clean-v1"

# Email config (we'll start with EMAIL_MODE=off in Render so it's instant)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.example.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "no-reply@example.com")
TO_EMAIL   = os.getenv("TO_EMAIL", "orders@example.com")
SMTP_SSL   = os.getenv("SMTP_SSL", "false").lower() == "true"
SMTP_TIMEOUT = int(os.getenv("SMTP_TIMEOUT", "10"))
EMAIL_MODE = os.getenv("EMAIL_MODE", "async").lower()  # async, off, sync

# Optional override for building links; if empty we use the request host
BASE_URL_OVERRIDE = os.getenv("BASE_URL", "").rstrip("/")

app = Flask(__name__)
BASE_DIR = os.path.dirname(__file__)
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DATA_DIR   = os.path.join(BASE_DIR, "data")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DATA_DIR,   exist_ok=True)

executor = ThreadPoolExecutor(max_workers=2)

def base_url() -> str:
    if BASE_URL_OVERRIDE:
        return BASE_URL_OVERRIDE
    return (request.host_url or "http://127.0.0.1:5000/").rstrip("/")

def record_path(token: str) -> str:
    return os.path.join(DATA_DIR, f"{token}.json")

def save_record(token: str, record: dict) -> None:
    with open(record_path(token), "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)

def load_record(token: str) -> Optional[dict]:
    path = record_path(token)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def send_email(subject: str, html: str, text: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = FROM_EMAIL
    msg["To"]      = TO_EMAIL
    msg["Date"]    = formatdate(localtime=True)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html,  "html",  "utf-8"))

    if SMTP_SSL:
        import ssl
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT, context=ctx) as s:
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as s:
            s.ehlo()
            try:
                s.starttls()
                s.ehlo()
            except Exception:
                pass
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)

@app.get("/")
def index():
    return (
        "ProofOK is running ({}). See <a href='/healthz'>/healthz</a>, "
        "<a href='/routes'>/routes</a>, or <a href='/upload'>/upload</a> to test."
    ).format(VERSION), 200

@app.get("/healthz")
def healthz():
    return {"ok": True, "version": VERSION, "time": datetime.datetime.utcnow().isoformat() + "Z"}

@app.get("/routes")
def routes():
    return {"routes": [str(r) for r in app.url_map.iter_rules()]}

# Simple manual upload page (no watcher needed to test)
@app.get("/upload")
def upload_form():
    return render_template("upload.html", version=VERSION)

# Manual upload handler (renders a link)
@app.post("/upload")
def upload_post():
    file = request.files.get("file")
    if not file or not file.filename.lower().endswith(".pdf"):
        return render_template("uploaded.html", ok=False, message="Please choose a .pdf file.", version=VERSION)

    original_name = file.filename
    token = uuid.uuid4().hex[:12]
    token_dir = os.path.join(UPLOAD_DIR, token)
    os.makedirs(token_dir, exist_ok=True)
    safe_name = original_name.replace("/", "_").replace("\\", "_")
    pdf_path = os.path.join(token_dir, safe_name)
    file.save(pdf_path)

    now = datetime.datetime.utcnow().isoformat() + "Z"
    rec = {"token": token, "original_name": original_name, "stored_name": safe_name,
           "created_utc": now, "status": "pending", "responses": []}
    save_record(token, rec)

    proof_link = "{}/proof/{}".format(base_url(), token)
    return render_template("uploaded.html", ok=True, url=proof_link, token=token,
                           original_name=original_name, version=VERSION)

# API upload (for watcher/scripts)
@app.post("/api/upload")
def api_upload():
    file = request.files.get("file")
    if not file or not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please upload a .pdf file"}), 400

    original_name = request.form.get("original_name", file.filename)
    token = uuid.uuid4().hex[:12]
    token_dir = os.path.join(UPLOAD_DIR, token)
    os.makedirs(token_dir, exist_ok=True)

    safe_name = original_name.replace("/", "_").replace("\\", "_")
    pdf_path = os.path.join(token_dir, safe_name)
    file.save(pdf_path)

    now = datetime.datetime.utcnow().isoformat() + "Z"
    rec = {"token": token, "original_name": original_name, "stored_name": safe_name,
           "created_utc": now, "status": "pending", "responses": []}
    save_record(token, rec)

    url = "{}/proof/{}".format(base_url(), token)
    return jsonify({"ok": True, "token": token, "url": url})

@app.get("/proof/<token>")
def proof_page(token):
    rec = load_record(token)
    if not rec:
        abort(404)
    return render_template(
        "proof.html",
        token=token,
        original_name=rec["original_name"],
        pdf_url=url_for("serve_pdf", token=token, filename=rec["stored_name"]),
        base_url=base_url(),
        version=VERSION
    )

@app.get("/p/<token>/<path:filename>")
def serve_pdf(token, filename):
    folder = os.path.join(UPLOAD_DIR, token)
    if not os.path.isdir(folder):
        abort(404)
    return send_from_directory(folder, filename, mimetype="application/pdf", as_attachment=False)

def email_body(rec: dict, decision: str, event: dict) -> tuple[str, str, str]:
    proof_url = "{}/proof/{}".format(base_url(), rec["token"])
    subject = "[Proof] {} -- {}".format(rec["original_name"], decision.upper())
    text = (
        "Proof decision received.\n\n"
        "File: {}\nLink: {}\nDecision: {}\nName: {}\nEmail: {}\nComment:\n{}\n\n"
        "Time (UTC): {}\nIP: {}\n"
    ).format(rec["original_name"], proof_url, decision, event.get("viewer_name",""),
             event.get("viewer_email",""), event.get("comment",""),
             event["ts_utc"], event.get("ip",""))
    html = (
        "<h2>Proof decision received</h2>"
        "<p><b>File:</b> {}</p>"
        "<p><b>Link:</b> <a href='{}'>{}</a></p>"
        "<p><b>Decision:</b> {}</p>"
        "<p><b>Name:</b> {} &lt;{}&gt;</p>"
        "<p><b>Comment:</b><br>{}</p>"
        "<p><small>Time (UTC): {} | IP: {}</small></p>"
    ).format(rec["original_name"], proof_url, proof_url, decision,
             event.get("viewer_name",""), event.get("viewer_email",""),
             (event.get("comment","") or "").replace("\n","<br>"),
             event["ts_utc"], event.get("ip",""))
    return subject, html, text

@app.post("/respond/<token>")
def respond_form(token):
    rec = load_record(token)
    if not rec:
        return render_template("result.html", ok=False, message="This proof link was not found.", version=VERSION,
                               token=token, original_name="")

    decision = (request.form.get("decision") or "").lower()
    comment  = (request.form.get("comment")  or "").strip()
    viewer_name  = (request.form.get("viewer_name")  or "").strip()
    viewer_email = (request.form.get("viewer_email") or "").strip()

    if decision not in ("approved", "rejected"):
        return render_template("result.html", ok=False, message="Invalid decision.", version=VERSION,
                               token=token, original_name=rec["original_name"])
    if decision == "rejected" and not comment:
        return render_template("result.html", ok=False, message="Please add a comment when rejecting.", version=VERSION,
                               token=token, original_name=rec["original_name"])

    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    event = {
        "ts_utc": datetime.datetime.utcnow().isoformat() + "Z",
        "decision": decision,
        "comment": comment,
        "viewer_name": viewer_name,
        "viewer_email": viewer_email,
        "ip": ip,
    }
    rec["status"] = decision
    rec["responses"].append(event)
    save_record(token, rec)

    warning = ""
    if EMAIL_MODE == "off":
        pass
    else:
        subj, html, text = email_body(rec, decision, event)
        if EMAIL_MODE == "sync":
            try:
                send_email(subj, html, text)
            except Exception as e:
                warning = "Email send failed ({}:{}): {}".format(SMTP_HOST, SMTP_PORT, e)
        else:
            try:
                fut = executor.submit(send_email, subj, html, text)
                fut.result(timeout=SMTP_TIMEOUT)
            except FuturesTimeout:
                warning = "Email is sending in background (timeout {}s).".format(SMTP_TIMEOUT)
            except Exception as e:
                warning = "Email send failed ({}:{}): {}".format(SMTP_HOST, SMTP_PORT, e)

    return render_template("result.html", ok=True, message="Thank you. Your decision was recorded.",
                           warning=warning, token=token, original_name=rec["original_name"],
                           version=VERSION)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
