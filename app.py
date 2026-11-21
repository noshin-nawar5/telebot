from flask import Flask, request, jsonify, send_file, render_template, session, redirect, url_for
from flask_cors import CORS
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
import hashlib, io, os, time, mimetypes, sqlite3
from dotenv import load_dotenv

load_dotenv()
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # allow http for localhost

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key")
CORS(app)

CHUNK_SIZE = 512 * 1024  # 512KB
CLIENT_SECRET_FILE = "client_secret.json"
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
REDIRECT_URI = "http://localhost:5000/callback"

# Simulate 3 "accounts" using 3 folders — all under the same user's Drive
FOLDER_IDS = [
    os.getenv("FOLDER_ID_1", ""),
    os.getenv("FOLDER_ID_2", ""),
    os.getenv("FOLDER_ID_3", ""),
]
ACCOUNT_LABELS = ["Drive Account A", "Drive Account B", "Drive Account C"]

# --- Database ---
def get_db():
    db = sqlite3.connect("storage.db")
    db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_hash TEXT UNIQUE NOT NULL,
            filename TEXT NOT NULL,
            total_chunks INTEGER NOT NULL,
            file_size INTEGER NOT NULL,
            uploaded_at REAL NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_hash TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            account_index INTEGER NOT NULL,
            drive_file_id TEXT NOT NULL,
            FOREIGN KEY (file_hash) REFERENCES files(file_hash)
        )
    """)
    db.commit()
    db.close()

# --- OAuth helpers ---
def get_credentials():
    if "credentials" not in session:
        return None
    creds = Credentials(**session["credentials"])
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        session["credentials"] = credentials_to_dict(creds)
    return creds

def credentials_to_dict(creds):
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }

def get_drive_service():
    creds = get_credentials()
    if not creds:
        return None
    return build("drive", "v3", credentials=creds)

# --- Drive helpers ---
def upload_chunk(service, chunk_data, chunk_name, account_index):
    folder_id = FOLDER_IDS[account_index]
    metadata = {"name": chunk_name, "parents": [folder_id]}
    media = MediaIoBaseUpload(io.BytesIO(chunk_data), mimetype="application/octet-stream")
    file = service.files().create(body=metadata, media_body=media, fields="id").execute()
    return file["id"]

def download_chunk(service, drive_file_id):
    req = service.files().get_media(fileId=drive_file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf.read()

def delete_chunk(service, drive_file_id):
    service.files().delete(fileId=drive_file_id).execute()

# --- Auth routes ---
@app.route("/login")
def login():
    flow = Flow.from_client_secrets_file(CLIENT_SECRET_FILE, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")
    session["state"] = state
    return redirect(auth_url)

@app.route("/callback")
def callback():
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRET_FILE, scopes=SCOPES,
        redirect_uri=REDIRECT_URI, state=session["state"]
    )
    flow.fetch_token(authorization_response=request.url)
    session["credentials"] = credentials_to_dict(flow.credentials)
    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/auth-status")
def auth_status():
    return jsonify({"logged_in": get_credentials() is not None})

# --- App routes ---
@app.route("/")
def index():
    return render_template("index.html", account_labels=ACCOUNT_LABELS)

@app.route("/upload", methods=["POST"])
def upload_file():
    service = get_drive_service()
    if not service:
        return jsonify({"error": "Not logged in"}), 401

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    file_data = file.read()
    file_hash = hashlib.sha256(file_data).hexdigest()
    filename = file.filename
    file_size = len(file_data)

    db = get_db()
    existing = db.execute("SELECT * FROM files WHERE file_hash = ?", (file_hash,)).fetchone()
    if existing:
        db.close()
        return jsonify({"error": "File already uploaded"}), 409

    chunks = [file_data[i:i + CHUNK_SIZE] for i in range(0, len(file_data), CHUNK_SIZE)]
    total_chunks = len(chunks)

    chunk_records = []
    try:
        for idx, chunk_data in enumerate(chunks):
            account_index = idx % len(FOLDER_IDS)
            chunk_name = f"{file_hash}_part{idx}"
            drive_file_id = upload_chunk(service, chunk_data, chunk_name, account_index)
            chunk_records.append({
                "chunk_index": idx,
                "account_index": account_index,
                "drive_file_id": drive_file_id,
            })
    except Exception as e:
        db.close()
        return jsonify({"error": f"Upload failed: {str(e)}"}), 500

    db.execute(
        "INSERT INTO files (file_hash, filename, total_chunks, file_size, uploaded_at) VALUES (?, ?, ?, ?, ?)",
        (file_hash, filename, total_chunks, file_size, time.time())
    )
    for c in chunk_records:
        db.execute(
            "INSERT INTO chunks (file_hash, chunk_index, account_index, drive_file_id) VALUES (?, ?, ?, ?)",
            (file_hash, c["chunk_index"], c["account_index"], c["drive_file_id"])
        )
    db.commit()
    db.close()

    return jsonify({
        "message": "Uploaded successfully",
        "file_hash": file_hash,
        "filename": filename,
        "total_chunks": total_chunks,
        "file_size": file_size,
        "chunks": chunk_records
    }), 200

@app.route("/files", methods=["GET"])
def list_files():
    db = get_db()
    files = db.execute("SELECT * FROM files ORDER BY uploaded_at DESC").fetchall()
    result = []
    for f in files:
        chunks = db.execute(
            "SELECT chunk_index, account_index, drive_file_id FROM chunks WHERE file_hash = ? ORDER BY chunk_index",
            (f["file_hash"],)
        ).fetchall()
        result.append({
            "file_hash": f["file_hash"],
            "filename": f["filename"],
            "total_chunks": f["total_chunks"],
            "file_size": f["file_size"],
            "uploaded_at": f["uploaded_at"],
            "chunks": [dict(c) for c in chunks]
        })
    db.close()
    return jsonify({"files": result})

@app.route("/download/<file_hash>", methods=["GET"])
def download_file(file_hash):
    service = get_drive_service()
    if not service:
        return jsonify({"error": "Not logged in"}), 401

    db = get_db()
    file_row = db.execute("SELECT * FROM files WHERE file_hash = ?", (file_hash,)).fetchone()
    if not file_row:
        db.close()
        return jsonify({"error": "File not found"}), 404

    chunks = db.execute(
        "SELECT * FROM chunks WHERE file_hash = ? ORDER BY chunk_index", (file_hash,)
    ).fetchall()
    db.close()

    merged = io.BytesIO()
    try:
        for chunk in chunks:
            data = download_chunk(service, chunk["drive_file_id"])
            merged.write(data)
    except Exception as e:
        return jsonify({"error": f"Download failed: {str(e)}"}), 500

    merged.seek(0)
    mime, _ = mimetypes.guess_type(file_row["filename"])
    return send_file(merged, as_attachment=True, download_name=file_row["filename"],
                     mimetype=mime or "application/octet-stream")

@app.route("/delete/<file_hash>", methods=["DELETE"])
def delete_file(file_hash):
    service = get_drive_service()
    if not service:
        return jsonify({"error": "Not logged in"}), 401

    db = get_db()
    file_row = db.execute("SELECT * FROM files WHERE file_hash = ?", (file_hash,)).fetchone()
    if not file_row:
        db.close()
        return jsonify({"error": "File not found"}), 404

    chunks = db.execute("SELECT * FROM chunks WHERE file_hash = ?", (file_hash,)).fetchall()
    errors = []
    for chunk in chunks:
        try:
            delete_chunk(service, chunk["drive_file_id"])
        except Exception as e:
            errors.append(str(e))

    db.execute("DELETE FROM chunks WHERE file_hash = ?", (file_hash,))
    db.execute("DELETE FROM files WHERE file_hash = ?", (file_hash,))
    db.commit()
    db.close()

    if errors:
        return jsonify({"message": "Deleted with some errors", "errors": errors}), 207
    return jsonify({"message": "Deleted successfully"}), 200

@app.route("/stats", methods=["GET"])
def stats():
    db = get_db()
    total_files = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    total_chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    total_size = db.execute("SELECT SUM(file_size) FROM files").fetchone()[0] or 0
    per_account = []
    for i in range(len(FOLDER_IDS)):
        count = db.execute(
            "SELECT COUNT(*) FROM chunks WHERE account_index = ?", (i,)
        ).fetchone()[0]
        per_account.append({"label": ACCOUNT_LABELS[i], "chunks": count})
    db.close()
    return jsonify({
        "total_files": total_files,
        "total_chunks": total_chunks,
        "total_size": total_size,
        "per_account": per_account
    })

if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
