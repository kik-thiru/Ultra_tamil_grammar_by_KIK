import os
import re
import json
import asyncio
import difflib
import random
import unicodedata
import hmac
import time
from werkzeug.security import generate_password_hash, check_password_hash
from collections import Counter
from datetime import timedelta
from flask import Flask, request, jsonify, send_from_directory, Response, session, render_template_string
import urllib.request
import edge_tts

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)


# ---------- API key ----------
# முதலில் GEMINI_API_KEY என்ற environment variable பார்க்கும்.
# இல்லையென்றால் app.py இருக்கிற ஃபோல்டரில் உள்ள key.txt ஃபைலை படிக்கும்.
def load_api_key():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    key_path = os.path.join(BASE_DIR, "key.txt")
    if os.path.exists(key_path):
        with open(key_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


GEMINI_API_KEY = load_api_key()
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

if not GEMINI_API_KEY:
    print("எச்சரிக்கை: Gemini API key கிடைக்கவில்லை. key.txt ஃபைலில் key-ஐ வைக்கவும்.")


def ask_gemini(prompt):
    if not GEMINI_API_KEY:
        raise RuntimeError("Gemini API key இல்லை")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    })
    with urllib.request.urlopen(req, timeout=40) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


# ---------- Pages ----------
@app.route('/')
def index():
    # index.html, app.py இருக்கும் ஃபோல்டரிலோ அல்லது அதன் templates ஃபோல்டரிலோ இருக்கலாம்
    for folder in (BASE_DIR, os.path.join(BASE_DIR, 'templates')):
        if os.path.exists(os.path.join(folder, 'index.html')):
            return send_from_directory(folder, 'index.html')
    files = ", ".join(sorted(os.listdir(BASE_DIR)))
    return ("index.html கிடைக்கவில்லை. ஃபோல்டர்: " + BASE_DIR +
            " | இங்கே உள்ள ஃபைல்கள்: " + files), 404


# ---------- Database (Render PostgreSQL) ----------
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None

# ரகசியங்களை கோடுக்குள் எழுதாமல், Render Environment Variables-ல் வைக்கவும்
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Tg7-kQ2m-Rv9x")   # இதை Render-ல் மாற்றவும்
app.secret_key = os.environ.get("SECRET_KEY") or (ADMIN_PASSWORD + "-kik-session")


def db():
    if not (psycopg2 and DATABASE_URL):
        return None
    return psycopg2.connect(DATABASE_URL)


def db_problem():
    if psycopg2 is None:
        return "psycopg2 நிறுவப்படவில்லை. requirements.txt-ல் psycopg2-binary என்ற வரியை சேர்த்து மறுபடி டெப்ளாய் செய்யவும்."
    if not DATABASE_URL:
        return "DATABASE_URL கிடைக்கவில்லை. ரெண்டர் Environment-ல் பெயர் சரியாக DATABASE_URL என்று உள்ளதா, மதிப்பு உள்ளதா என சரிபார்க்கவும்."
    return "டேட்டாபேஸ் இணைக்கப்படவில்லை."


def init_db():
    conn = None
    try:
        conn = db()
        if not conn:
            print(db_problem(), "முடிவுகள் சேமிக்கப்படாது.")
            return
        with conn, conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS results (
                id SERIAL PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                name TEXT, role TEXT, level TEXT, detail TEXT, mode TEXT, category TEXT, method TEXT,
                item_count INT, seconds INT, mistakes_count INT, mistakes TEXT, details TEXT)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS seen_items (
                id SERIAL PRIMARY KEY, category TEXT, item TEXT)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS logins (
                id SERIAL PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                name TEXT, role TEXT, level TEXT, detail TEXT, sid TEXT)""")
            cur.execute("ALTER TABLE results ADD COLUMN IF NOT EXISTS sid TEXT")
            cur.execute("ALTER TABLE results ADD COLUMN IF NOT EXISTS region TEXT")
            cur.execute("ALTER TABLE results ADD COLUMN IF NOT EXISTS words INT")
            cur.execute("ALTER TABLE logins ADD COLUMN IF NOT EXISTS region TEXT")
            cur.execute("""CREATE TABLE IF NOT EXISTS teachers (
                id SERIAL PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, first_name TEXT, last_name TEXT,
                subject TEXT, region TEXT, username TEXT, email TEXT, password_hash TEXT)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS teacher_logins (
                id SERIAL PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, teacher_id INT,
                name TEXT, username TEXT, region TEXT)""")
            cur.execute("CREATE TABLE IF NOT EXISTS examples (word TEXT PRIMARY KEY, sentence TEXT)")
            cur.execute("CREATE TABLE IF NOT EXISTS rejected_words (id SERIAL PRIMARY KEY, rejected TEXT UNIQUE, correct TEXT, meaning TEXT)")
            cur.execute("INSERT INTO rejected_words (rejected, correct, meaning) VALUES ('நாழை','நாளை','நாளை என்றால் tomorrow') ON CONFLICT (rejected) DO NOTHING")
    except Exception as e:
        print("Database Initialization Error:", e)
    finally:
        if conn:
            conn.close()


init_db()


def get_used(category):
    conn = None
    try:
        conn = db()
        if conn:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT item FROM seen_items WHERE category=%s ORDER BY id DESC LIMIT 300", (category,))
                return [r[0] for r in cur.fetchall()][::-1]
    except Exception as e:
        print("get_used பிழை:", e)
    finally:
        if conn:
            conn.close()
    return load_seen().get(category, [])


def add_used(category, items):
    conn = None
    try:
        conn = db()
        if conn:
            with conn, conn.cursor() as cur:
                cur.executemany("INSERT INTO seen_items (category, item) VALUES (%s, %s)", [(category, i) for i in items])
            return
    except Exception as e:
        print("add_used பிழை:", e)
    finally:
        if conn:
            conn.close()
    seen = load_seen()
    seen[category] = (seen.get(category, []) + items)[-300:]
    save_seen(seen)


@app.route('/api/save_result', methods=['POST'])
def save_result():
    d = request.get_json(silent=True) or {}
    st = d.get('student') or {}
    if st.get('role') == 'teacher':
        return jsonify({"saved": False})
    mistakes = d.get('mistakes') or []
    conn = None
    try:
        conn = db()
        if not conn:
            return jsonify({"saved": False})
        with conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO results (name, role, level, detail, mode, category, method, item_count,
                   seconds, mistakes_count, mistakes, details, sid, region, words) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (str(st.get('name', ''))[:200], str(st.get('role', ''))[:30], str(st.get('level', ''))[:30],
                 str(st.get('detail', ''))[:200], str(d.get('mode', ''))[:30], str(d.get('category', ''))[:30],
                 str(d.get('method', ''))[:30], d.get('count'), d.get('seconds'), len(mistakes),
                 '[]', str(d.get('note', ''))[:500], str(st.get('sid', ''))[:60], str(st.get('region', ''))[:80],
                 d.get('words') if isinstance(d.get('words'), int) else None))
        return jsonify({"saved": True})
    except Exception as e:
        print("save_result பிழை:", e)
        return jsonify({"saved": False}), 500
    finally:
        if conn:
            conn.close()


# ---------- Admin Results Page ----------
MODE_L = {'learning': 'Learning Mode', 'testing': 'Testing Mode', 'manual_writing': 'Manual Writing',
          'dictation_learning': 'Dictation (Learning)', 'dictation_testing': 'Dictation (Testing)'}
METH_L = {'hearing': 'Hearing and Writing', 'manual': 'Self writing (Manual)', 'editor': 'Writing box (Manual Writing mode)', 'dictation': 'Dictation'}


def ist(dt):
    return dt + timedelta(hours=5, minutes=30)


def pkey(name, role, level, detail):
    return "||".join((x or '').strip().lower() for x in (name, role, level, detail))


def words_in(details, method):
    try:
        if method == 'editor':
            return len(str(details).split())
        return sum(len(str(e.get('typed', '')).split()) for e in json.loads(details))
    except Exception:
        return 0


def load_people():
    conn = db()
    if not conn:
        return None
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM logins ORDER BY created_at")
            logins = cur.fetchall()
            cur.execute("SELECT * FROM results ORDER BY created_at")
            results = cur.fetchall()
    finally:
        conn.close()

    people = {}

    def get(r):
        k = pkey(r['name'], r['role'], r['level'], r['detail'])
        if k not in people:
            people[k] = {'key': k, 'name': (r['name'] or '').strip(), 'role': (r['role'] or '').lower(),
                         'level': (r['level'] or '').lower(), 'detail': (r['detail'] or '').strip(), 'region': (r['region'] or '').strip(),
                         'sids': set(), 'modes': Counter(), 'methods': Counter(), 'items': 0, 'words': 0,
                         'mistakes': 0, 'seconds': 0, 'sessions': 0, 'first': None, 'last': None, 'days': {}}
        return people[k]

    def touch(p, dt, sid):
        p['first'] = dt if not p['first'] or dt < p['first'] else p['first']
        p['last'] = dt if not p['last'] or dt > p['last'] else p['last']
        p['sids'].add(sid)
        return p['days'].setdefault(ist(dt).strftime('%Y-%m-%d'), {'sids': set(), 'lines': []})

    for r in logins:
        p = get(r)
        touch(p, r['created_at'], r['sid'] or 'l%s' % r['id'])['sids'].add(r['sid'] or 'l%s' % r['id'])
    for r in results:
        p = get(r)
        sid = r['sid'] or 'r%s' % r['id']
        day = touch(p, r['created_at'], sid)
        day['sids'].add(sid)
        p['sessions'] += 1
        p['modes'][r['mode']] += 1
        p['methods'][r['method']] += 1
        p['words'] += r['words'] if r['words'] is not None else words_in(r['details'], r['method'])
        p['mistakes'] += r['mistakes_count'] or 0
        if r['method'] != 'editor':
            p['items'] += r['item_count'] or 0
        if r['seconds'] is not None:
            p['seconds'] += r['seconds']
        line = (f"{ist(r['created_at']).strftime('%H:%M')} - {MODE_L.get(r['mode'], r['mode'])}, {r['category']}, "
                f"{METH_L.get(r['method'], r['method'])}, {r['item_count']} items, {r['mistakes_count']} mistakes")
        if r['seconds'] is not None:
            line += f", {r['seconds']} seconds"
        if r['details']:
            line += f" ({r['details']})"
        day['lines'].append(line)

    out = []
    for p in people.values():
        p['role_label'] = 'Teacher' if p['role'] == 'teacher' else 'Student'
        p['level_label'] = 'College' if p['level'] == 'college' else 'School'
        p['detail_label'] = ('Degree' if p['level'] == 'college' else 'Class') + (' taught' if p['role'] == 'teacher' else '')
        p['visits'] = len(p['sids'])
        p['mode_lines'] = [f"{MODE_L.get(k, k)}: used {v} time(s)" for k, v in p['modes'].items()]
        p['method_lines'] = [f"{METH_L.get(k, k)}: {v} time(s)" for k, v in p['methods'].items()]
        p['days'] = [{'date': d, 'logins': len(v['sids']), 'lines': v['lines']}
                     for d, v in sorted(p['days'].items(), reverse=True)]
        p['first'] = ist(p['first']).strftime('%Y-%m-%d %H:%M') if p['first'] else '-'
        p['last'] = ist(p['last']).strftime('%Y-%m-%d %H:%M') if p['last'] else '-'
        out.append(p)
    out.sort(key=lambda x: x['last'], reverse=True)
    return out


def person_text(p):
    days = "\n".join(f"  {d['date']} ({d['logins']} login): " + "; ".join(d['lines']) for d in p['days'])
    return (f"Name: {p['name']} | {p['role_label']} | {p['level_label']} | {p['detail_label']}: {p['detail']} | Region: {p['region']}\n"
            f"Logged in {p['visits']} time(s); first {p['first']}, last {p['last']}; practice sessions {p['sessions']}\n"
            f"Modes: {', '.join(p['mode_lines']) or 'none'}\nWriting methods: {', '.join(p['method_lines']) or 'none'}\n"
            f"Total words written {p['words']}; items {p['items']}; spelling mistakes (words) {p['mistakes']}; "
            f"testing time {p['seconds']} seconds\nDaily log:\n{days}")


@app.route('/view_results/ai', methods=['POST'])
def view_results_ai():
    if not session.get('admin'):
        return jsonify({"summary": "Please login again."}), 401
    key = (request.get_json(silent=True) or {}).get('key', '')
    people = load_people() or []
    chosen = [p for p in people if p['key'] == key] if key else people
    if not chosen:
        return jsonify({"summary": "No data to summarize."})
    text = "\n\n".join(person_text(p) for p in chosen)[:14000]
    prompt = ("Below is usage data from a Tamil spelling and grammar practice app for blind students and teachers. "
              "Write a clear, simple English summary for the supervising teacher: how often they used the app, which modes "
              "and writing methods they preferred, how much they wrote, their spelling accuracy, and any pattern or advice. "
              "Use short plain sentences, no markdown symbols.\n\n" + text)
    try:
        return jsonify({"summary": ask_gemini(prompt)})
    except Exception as e:
        print("AI summary பிழை:", e)
        return jsonify({"summary": "AI summary is not available right now."}), 500


def load_admin_html():
    for folder in (BASE_DIR, os.path.join(BASE_DIR, 'templates')):
        path = os.path.join(folder, 'view_results.html')
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                return f.read()
    return "<p>view_results.html கிடைக்கவில்லை. app.py இருக்கும் ஃபோல்டரில் வைக்கவும்.</p>"


@app.route('/view_results', methods=['GET', 'POST'])
def view_results():
    error = ""
    if request.method == 'POST':
        if request.form.get('action') == 'logout':
            session.pop('admin', None)
        elif request.form.get('password') is not None:
            if hmac.compare_digest(request.form['password'], ADMIN_PASSWORD):
                session['admin'] = True
            else:
                error = "Wrong Password!"
    html = load_admin_html()
    if not session.get('admin'):
        return render_template_string(html, logged_in=False, error=error, people=[], search="")
    try:
        action = request.form.get('action')
        if action in ('rej_add', 'rej_del'):
            change_rejected(action, request.form)
        if action in ('delete_one', 'delete_all', 'delete_time'):
            conn = db()
            if not conn:
                return db_problem(), 500
            try:
                with conn, conn.cursor() as cur:
                    for table in ('results', 'logins'):
                        if action == 'delete_all':
                            cur.execute(f"DELETE FROM {table}")
                        elif action == 'delete_time':
                            spans = {"30_minutes": "30 minutes", "1_hour": "1 hour", "24_hours": "24 hours",
                                     "7_days": "7 days", "30_days": "30 days", "60_days": "60 days"}
                            span = spans.get(request.form.get('time_filter'))
                            if span:
                                cur.execute(f"DELETE FROM {table} WHERE created_at >= NOW() - %s::interval", (span,))
                        else:
                            parts = request.form.get('key', '').split('||')
                            if len(parts) == 4:
                                cur.execute(f"""DELETE FROM {table} WHERE lower(trim(coalesce(name,'')))=%s
                                    AND lower(trim(coalesce(role,'')))=%s AND lower(trim(coalesce(level,'')))=%s
                                    AND lower(trim(coalesce(detail,'')))=%s""", parts)
            finally:
                conn.close()
        people = load_people()
        if people is None:
            return db_problem(), 500
        search = request.form.get('search', '').strip().lower()
        if search:
            people = [p for p in people if search in (p['name'] + ' ' + p['detail'] + ' ' + p['role_label'] + ' ' + p['level_label']).lower()]
        return render_template_string(html, logged_in=True, error="", people=people, search=search, teacher_logins=load_teacher_logins(), rejected=load_rejected())
    except Exception as e:
        print("Admin Page Error:", e)
        return f"Database Error: {e}", 500


@app.route('/api/log_login', methods=['POST'])
def log_login():
    st = (request.get_json(silent=True) or {}).get('student') or {}
    if st.get('role') == 'teacher':
        return jsonify({"saved": False})
    conn = None
    try:
        conn = db()
        if not conn:
            return jsonify({"saved": False})
        with conn, conn.cursor() as cur:
            cur.execute("INSERT INTO logins (name, role, level, detail, sid, region) VALUES (%s,%s,%s,%s,%s,%s)",
                        (str(st.get('name', ''))[:200], str(st.get('role', ''))[:30], str(st.get('level', ''))[:30],
                         str(st.get('detail', ''))[:200], str(st.get('sid', ''))[:60], str(st.get('region', ''))[:80]))
        return jsonify({"saved": True})
    except Exception as e:
        print("log_login பிழை:", e)
        return jsonify({"saved": False}), 500
    finally:
        if conn:
            conn.close()


# ---------- Dictation data, rejected dictionary ----------
_rej_cache = {"t": 0, "m": {}}


def rejected_map():
    if time.time() - _rej_cache["t"] < 30:
        return _rej_cache["m"]
    m, conn = {}, None
    try:
        conn = db()
        if conn:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT rejected, correct, meaning FROM rejected_words")
                for r in cur.fetchall():
                    m[unicodedata.normalize('NFC', r[0])] = {"correct": r[1], "meaning": r[2] or ''}
    except Exception as e:
        print("rejected_map பிழை:", e)
    finally:
        if conn:
            conn.close()
    _rej_cache.update(t=time.time(), m=m)
    return m


def apply_rejected_text(text):
    m = rejected_map()
    if not m:
        return text
    return re.sub(r'[\u0B80-\u0BFF]+', lambda mo: m[mo.group(0)]['correct'] if mo.group(0) in m else mo.group(0),
                  unicodedata.normalize('NFC', text))


def load_rejected():
    conn = None
    try:
        conn = db()
        if not conn:
            return []
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM rejected_words ORDER BY id DESC")
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print("load_rejected பிழை:", e)
        return []
    finally:
        if conn:
            conn.close()


def change_rejected(action, form):
    conn = None
    try:
        conn = db()
        if not conn:
            return
        with conn, conn.cursor() as cur:
            if action == 'rej_add':
                a = unicodedata.normalize('NFC', form.get('rejected', '').strip())
                b = form.get('correct', '').strip()
                if a and b and a != b:
                    cur.execute("""INSERT INTO rejected_words (rejected, correct, meaning) VALUES (%s,%s,%s)
                                   ON CONFLICT (rejected) DO UPDATE SET correct=EXCLUDED.correct, meaning=EXCLUDED.meaning""",
                                (a, b, form.get('meaning', '').strip()[:200]))
            elif action == 'rej_del':
                cur.execute("DELETE FROM rejected_words WHERE id=%s", (int(form.get('rid', 0)),))
    except Exception as e:
        print("change_rejected பிழை:", e)
    finally:
        if conn:
            conn.close()
        _rej_cache["t"] = 0


@app.route('/api/rejected')
def api_rejected():
    return jsonify([{"rejected": k, "correct": v["correct"], "meaning": v["meaning"]} for k, v in rejected_map().items()])


@app.route('/api/dictation')
def dictation_data():
    with open(os.path.join(BASE_DIR, 'exercises.json'), encoding='utf-8') as f:
        return Response(f.read(), mimetype='application/json')


# ---------- Teachers, examples ----------
TEACHER_CODE = os.environ.get("TEACHER_PASSWORD", "")
USER_RE = re.compile(r'^[A-Za-z0-9_-]{3,30}$')
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def bad(msg):
    return jsonify({"ok": False, "message": msg})


@app.route('/api/teacher/register', methods=['POST'])
def teacher_register():
    d = request.get_json(silent=True) or {}
    first, last, subject, region = (str(d.get(k, '')).strip()[:80] for k in ('first', 'last', 'subject', 'region'))
    username = str(d.get('username', '')).strip()
    email = str(d.get('email', '')).strip().lower()[:120]
    password = str(d.get('password', ''))
    code = str(d.get('code', ''))
    if not TEACHER_CODE:
        return bad("ஆசிரியர் குறியீடு சர்வரில் இன்னும் அமைக்கப்படவில்லை.")
    if not hmac.compare_digest(code, TEACHER_CODE):
        return bad("ஆசிரியர் குறியீடு தவறு.")
    if not (first and last and subject and region):
        return bad("எல்லா பெட்டிகளையும் நிரப்புங்கள்.")
    if not USER_RE.match(username):
        return bad("பயனர் பெயரில் பெரிய எழுத்து, சின்ன எழுத்து, எண், கோடு, அடிக்கோடு மட்டும் இருக்கலாம். இடைவெளி கூடாது. மூன்று முதல் முப்பது எழுத்துகள்.")
    if not EMAIL_RE.match(email):
        return bad("சரியான மின்னஞ்சல் முகவரியை எழுதுங்கள்.")
    if len(password) < 4:
        return bad("பாஸ்வேர்டு குறைந்தது நான்கு எழுத்துகள் இருக்க வேண்டும்.")
    conn = None
    try:
        conn = db()
        if not conn:
            return bad(db_problem())
        with conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM teachers WHERE lower(username)=%s", (username.lower(),))
            if cur.fetchone():
                return bad("இந்த பயனர் பெயர் ஏற்கனவே எடுக்கப்பட்டுள்ளது. வேறு ஒன்றை தேர்ந்தெடுங்கள்.")
            cur.execute("SELECT 1 FROM teachers WHERE email=%s", (email,))
            if cur.fetchone():
                return bad("இந்த மின்னஞ்சலுக்கு ஏற்கனவே கணக்கு உள்ளது.")
            cur.execute("""INSERT INTO teachers (first_name, last_name, subject, region, username, email, password_hash)
                           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (first, last, subject, region, username, email, generate_password_hash(password)))
        return jsonify({"ok": True})
    except Exception as e:
        print("teacher_register பிழை:", e)
        return bad("கணக்கு உருவாக்க முடியவில்லை.")
    finally:
        if conn:
            conn.close()


@app.route('/api/teacher/login', methods=['POST'])
def teacher_login():
    d = request.get_json(silent=True) or {}
    ident = str(d.get('id', '')).strip().lower()
    password = str(d.get('password', ''))
    conn = None
    try:
        conn = db()
        if not conn:
            return bad(db_problem())
        with conn, conn.cursor() as cur:
            cur.execute("""SELECT id, first_name, last_name, region, username, password_hash FROM teachers
                           WHERE lower(username)=%s OR email=%s""", (ident, ident))
            row = cur.fetchone()
            if not row or not check_password_hash(row[5], password):
                return bad("பயனர் பெயர் அல்லது பாஸ்வேர்டு தவறு.")
            name = f"{row[1]} {row[2]}"
            session['teacher'] = {'id': row[0], 'region': row[3], 'name': name}
            cur.execute("INSERT INTO teacher_logins (teacher_id, name, username, region) VALUES (%s,%s,%s,%s)",
                        (row[0], name, row[4], row[3]))
            cur.execute("DELETE FROM teacher_logins WHERE created_at < NOW() - INTERVAL '30 days'")
        return jsonify({"ok": True, "name": name, "region": row[3]})
    except Exception as e:
        print("teacher_login பிழை:", e)
        return bad("லாகின் செய்ய முடியவில்லை.")
    finally:
        if conn:
            conn.close()


@app.route('/api/teacher/logout', methods=['POST'])
def teacher_logout():
    session.pop('teacher', None)
    return jsonify({"ok": True})


@app.route('/api/teacher/results', methods=['POST'])
def teacher_results():
    t = session.get('teacher')
    if not t:
        return jsonify({"error": "login"}), 401
    cls = str((request.get_json(silent=True) or {}).get('cls', '')).strip().lower()
    people = load_people() or []
    rows = [p for p in people if p['role'] == 'student' and p['detail'].lower() == cls
            and p['region'].lower() == (t['region'] or '').lower()]
    rows.sort(key=lambda p: p['name'].lower())
    return jsonify({"region": t['region'], "cls": cls,
                    "students": [{"name": p['name'], "text": person_text(p)} for p in rows]})


@app.route('/api/example', methods=['POST'])
def example():
    word = str((request.get_json(silent=True) or {}).get('word', '')).strip()[:60]
    if not word:
        return jsonify({"sentence": ""})
    conn = None
    try:
        conn = db()
        if conn:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT sentence FROM examples WHERE word=%s", (word,))
                row = cur.fetchone()
                if row:
                    return jsonify({"sentence": row[0]})
    except Exception as e:
        print("example படிக்க பிழை:", e)
    finally:
        if conn:
            conn.close()
    try:
        prompt = (f'Write ONE short, simple Tamil sentence (6 to 10 words) that clearly shows the meaning of the Tamil word "{word}", '
                  "suitable for a school student. Return only the sentence, nothing else.")
        sentence = (ask_gemini(prompt).splitlines() or [''])[0].replace('*', '').strip()
    except Exception as e:
        print("example Gemini பிழை:", e)
        return jsonify({"sentence": ""}), 500
    conn = None
    try:
        conn = db()
        if conn and sentence:
            with conn, conn.cursor() as cur:
                cur.execute("INSERT INTO examples (word, sentence) VALUES (%s,%s) ON CONFLICT (word) DO NOTHING", (word, sentence))
    except Exception as e:
        print("example சேமிக்க பிழை:", e)
    finally:
        if conn:
            conn.close()
    return jsonify({"sentence": sentence})


def load_teacher_logins():
    conn = None
    try:
        conn = db()
        if not conn:
            return []
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM teacher_logins ORDER BY created_at DESC LIMIT 300")
            rows = cur.fetchall()
        return [{'name': r['name'], 'username': r['username'], 'region': r['region'],
                 'when': ist(r['created_at']).strftime('%Y-%m-%d %H:%M')} for r in rows]
    except Exception as e:
        print("load_teacher_logins பிழை:", e)
        return []
    finally:
        if conn:
            conn.close()


# ---------- Braille (Tamil) ----------
BRAILLE = {
 'அ':'1','ஆ':'3 4 5','இ':'2 4','ஈ':'3 5','உ':'1 3 6','ஊ':'1 2 5 6','எ':'1 5','ஏ':'2 6','ஐ':'3 4',
 'ஒ':'1 3 5','ஓ':'1 3 4 6','ஔ':'2 4 6','க':'1 3','ங':'3 4 6','ச':'1 4','ஞ':'2 5','ட':'2 3 4 5 6',
 'ண':'3 4 5 6','த':'1 4 5 6','ந':'1 3 4 5','ப':'1 2 3 4','ம':'1 3 4','ய':'1 3 4 5 6','ர':'1 2 3 5',
 'ல':'1 2 3','வ':'1 2 3 6','ழ':'1 2 3 5 6','ள':'4 5 6','ற':'1 2 4 5 6','ன':'5 6',
}
SUZHI = {'ண': 'மூன்று சுழி ண', 'ன': 'இரண்டு சுழி ன'}
PUNCT = {'.': 'புள்ளி', ',': 'காற்புள்ளி', '?': 'வினாக்குறி', '!': 'வியப்புக்குறி', ';': 'அரைப்புள்ளி',
         ':': 'முக்காற்புள்ளி', '"': 'மேற்கோள் குறி', "'": 'ஒற்றை மேற்கோள் குறி', '-': 'இடைக்கோடு', ' ': 'இடைவெளி'}


def norm(s):
    s = unicodedata.normalize('NFC', s or '')
    s = re.sub(r'[\u200b-\u200d\ufeff\s]', '', s)
    return re.sub(r'[.,!?;:\'"“”‘’]+$', '', s)


def letter_name(s):
    return PUNCT.get(s, SUZHI.get(s, s)) if len(s) == 1 else s


def braille_text(s):
    parts = [f"{ch} பிரெயில் புள்ளிகள் {BRAILLE[ch]}" for ch in s if ch in BRAILLE]
    return (", " + ", ".join(parts)) if parts else ""


def explain_pair(wrong, correct):
    sm = difflib.SequenceMatcher(None, wrong, correct)
    parts = []
    for tag, a1, a2, b1, b2 in sm.get_opcodes():
        if tag == 'replace':
            w, c = wrong[a1:a2], correct[b1:b2]
            parts.append(f"{letter_name(w)} போட்டுள்ளீர்கள், {letter_name(c)} போட வேண்டும்{braille_text(c)}")
        elif tag == 'delete':
            parts.append(f"{letter_name(wrong[a1:a2])} கூடுதலாக உள்ளது")
        elif tag == 'insert':
            parts.append(f"{letter_name(correct[b1:b2])} விடுபட்டுள்ளது{braille_text(correct[b1:b2])}")
    detail = "; ".join(parts) if parts else "எழுத்துப் பிழை"
    return f"{wrong}: {detail}. சரியான வார்த்தை: {correct}"


# ---------- Fallback items (Gemini இல்லாதபோது) ----------
FALLBACK = {
    'Words': ['வணக்கம்', 'பழம்', 'தமிழ்', 'மலர்', 'நிலா', 'கடல்', 'மழை', 'பள்ளி', 'புத்தகம்', 'நண்பன்', 'அன்பு', 'வானம்'],
    'Line': ['தமிழ் எங்கள் உயிர் மொழி.', 'மழை பெய்து நிலம் குளிர்ந்தது.', 'நான் தினமும் பள்ளிக்குச் செல்கிறேன்.',
             'அம்மா சுவையான உணவு சமைத்தார்.', 'நல்ல நண்பர்கள் வாழ்க்கையின் பெருமை.', 'காலையில் பறவைகள் பாடுகின்றன.'],
    'Stanza': ['அன்பே சிவம் என்பர் அறிந்தோர்\nஅறிவே ஒளி என்பர் தெளிந்தோர்\nபண்பே பெருமை என்பர் பெரியோர்.',
               'கல்வி கரையில கற்பவர் நாள்சில\nமெல்ல நினைக்கின் பிணிபல\nதெள்ளிதின் ஆராய்ந்து கற்க.'],
    'Paragraph': ['தமிழ் மொழி மிகவும் தொன்மையான மொழி. இது இனிமையும் செழுமையும் கொண்டது. தமிழில் எண்ணற்ற இலக்கியங்கள் உள்ளன.',
                  'கல்வி மனிதனுக்கு அறிவைத் தருகிறது. ஒவ்வொருவரும் தினமும் படிக்க வேண்டும். படிப்பு வாழ்க்கையை உயர்த்தும்.'],
}


# ---------- Prompts (எப்போதும் புதிய உள்ளீடுகள்) ----------
SEEN_FILE = os.path.join(BASE_DIR, 'seen_items.json')
TOPICS = ['nature', 'family', 'school', 'food', 'festival', 'village', 'animals', 'river', 'farming',
          'friendship', 'health', 'travel', 'weather', 'music', 'market', 'temple', 'sea', 'garden']


def load_seen():
    try:
        with open(SEEN_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen(d):
    try:
        with open(SEEN_FILE, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception as e:
        print("seen சேமிக்க முடியவில்லை:", e)


@app.route('/api/generate_prompts', methods=['POST'])
def generate_prompts():
    data = request.get_json(silent=True) or {}
    category = data.get('category', 'Words')
    if category not in FALLBACK:
        category = 'Words'
    try:
        count = max(1, int(data.get('count', 1)))
    except (TypeError, ValueError):
        count = 1

    rules = {
        'Words': "single meaningful Tamil words (one word each, no spaces)",
        'Line': "single meaningful Tamil sentences, each ending with a dot (.)",
        'Stanza': "short Tamil poetic stanzas of exactly 3 lines each (separate lines with a newline)",
        'Paragraph': "short Tamil paragraphs of 3 or 4 sentences, each sentence ending with a dot (.)",
    }[category]
    mix = {
        'Words': "Mix the lengths: some words of 2 to 3 letters, some of 4, 5, 6 and 7 letters, and some long words of 8 or more letters.",
        'Line': "Mix short sentences of about 4 words and longer sentences of 10 or more words.",
        'Stanza': "Vary the topics and the rhythm.",
        'Paragraph': "Vary the topics and the length.",
    }[category]

    recent = get_used(category)[-40:]      # only a soft hint, repeats across sessions are allowed
    taken = set()                          # nothing repeats inside this one session
    items = []
    attempts = 0
    while len(items) < count and attempts < 6:
        attempts += 1
        need = min(count - len(items) + 6, 40)
        avoid = "; ".join(recent + items[-40:]).replace("\n", " / ")
        prompt = f"""Generate exactly {need} different, new {rules} for a Tamil spelling practice app.
Theme hint: {random.choice(TOPICS)}. {mix}
Include items with easily confused letters (ண ன ந, ல ள ழ, ர ற).
Do NOT use any of these already used items: {avoid or 'none'}.
Return ONLY the Tamil items separated by '|||'. No numbering, no markdown, no English."""
        try:
            raw = ask_gemini(prompt)
        except Exception as e:
            print("generate_prompts பிழை:", e)
            break
        for it in raw.split('|||'):
            it = re.sub(r'^\s*(\d+[.)]|[-*•])\s*', '', it.replace('*', '').replace('`', '')).strip()
            k = norm(it)
            if it and k not in taken and len(items) < count:
                items.append(it)
                taken.add(k)

    if len(items) < count:                 # Gemini gave too few: fill only with unused built-in items
        pool = FALLBACK[category][:]
        random.shuffle(pool)
        for it in pool:
            if len(items) >= count:
                break
            if norm(it) not in taken:
                items.append(it)
                taken.add(norm(it))

    random.shuffle(items)
    add_used(category, items)
    return jsonify({"status": "success", "items": items})


# ---------- Check one typed entry (self-writing) ----------
@app.route('/api/check_word', methods=['POST'])
def check_word():
    body = request.get_json(silent=True) or {}
    text = body.get('text', '').strip()
    fixed = apply_rejected_text(text)
    if fixed != unicodedata.normalize('NFC', text):
        return jsonify({"ok": False, "correct": fixed})
    what = "spelling" if body.get('cat', 'Words') == 'Words' else "spelling and punctuation (full stops, commas, question marks, spacing)"
    prompt = f"""Check the Tamil {what} of this text: "{text}"
If it is completely correct, reply with exactly: சரி
Otherwise reply with ONLY the corrected text, nothing else."""
    reply = (ask_gemini(prompt).strip().splitlines() or [''])[0].replace('*', '').replace('`', '').strip().strip('"“”')
    if not reply or reply == 'சரி' or norm(reply) == norm(text):
        return jsonify({"ok": True, "correct": text})
    return jsonify({"ok": False, "correct": reply})


# ---------- Check a long text (Manual Writing editor) ----------
LETTER_GROUPS = {'ண': 1, 'ன': 1, 'ந': 1, 'ல': 2, 'ள': 2, 'ழ': 2, 'ர': 3, 'ற': 3}
GROUP_NAMES = {1: 'ண ன ந', 2: 'ல ள ழ', 3: 'ர ற'}


def split_sentences(text):
    parts = re.split(r'(?<=[.!?])\s+|\n+', text)
    return [p.strip() for p in parts if p.strip()]


@app.route('/api/check_text', methods=['POST'])
def check_text():
    text = str((request.get_json(silent=True) or {}).get('text', ''))[:20000]
    sents = split_sentences(text)
    total_words = len(text.split())
    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(sents))
    prompt = f"""You are an expert Tamil spelling and grammar checker.
Below are numbered Tamil sentences. Find every misspelt word, especially மயங்கொலி mistakes (ண/ன/ந, ல/ள/ழ, ர/ற).
Return ONLY a JSON array like [{{"i":1,"wrong":"வனக்கம்","correct":"வணக்கம்"}}] where i is the sentence number.
Also report punctuation mistakes (missing or wrong full stop, comma, question mark, extra space before punctuation): for those, put the word with its punctuation as written in "wrong" and the corrected form in "correct".
Each item must be a single word. Use [] if there are no mistakes. No markdown.

{numbered}"""
    raw = re.sub(r"```(?:json)?", "", ask_gemini(prompt)).strip()
    found = json.loads(raw)

    per = [[] for _ in sents]
    pairs = []
    for m in found:
        try:
            i = int(m.get('i')) - 1
            w, c = str(m['wrong']).strip(), str(m['correct']).strip()
        except Exception:
            continue
        if not w or not c or w == c or not (0 <= i < len(sents)):
            continue
        per[i].append({"wrong": w, "correct": c, "explain": explain_pair(w, c)})
        pairs.append((w, c))

    rm = rejected_map()
    for i, s in enumerate(sents):
        for tok in re.findall(r'[\u0B80-\u0BFF]+', unicodedata.normalize('NFC', s)):
            if tok in rm and not any(x['wrong'] == tok for x in per[i]):
                c = rm[tok]['correct']
                note = f". {rm[tok]['meaning']}" if rm[tok]['meaning'] else ""
                per[i].append({"wrong": tok, "correct": c,
                               "explain": f"{tok}: இங்கு இந்த வார்த்தை பொருந்தாது. சரியான வார்த்தை: {c}{note}"})
                pairs.append((tok, c))

    by_group, by_pair = Counter(), Counter()
    for w, c in pairs:
        for tag, a1, a2, b1, b2 in difflib.SequenceMatcher(None, w, c).get_opcodes():
            if tag == 'replace' and a2 - a1 == 1 and b2 - b1 == 1:
                x, y = w[a1], c[b1]
                if LETTER_GROUPS.get(x) and LETTER_GROUPS.get(x) == LETTER_GROUPS.get(y):
                    by_group[LETTER_GROUPS[x]] += 1
                    by_pair[f"{letter_name(x)} போட்டுள்ளீர்கள், {letter_name(y)} போட வேண்டும்"] += 1

    wrong_words = len(pairs)
    return jsonify({
        "total_words": total_words, "wrong_words": wrong_words,
        "correct_words": max(total_words - wrong_words, 0),
        "groups": [f"{GROUP_NAMES[g]} எழுத்துகளில் {n} தவறு" for g, n in by_group.most_common()],
        "top_pairs": [f"{k}: {n} முறை" for k, n in by_pair.most_common(5)],
        "sentences": [{"text": s, "mistakes": per[i]} for i, s in enumerate(sents)],
        "mistakes": [[w, c] for w, c in pairs],
    })


# ---------- Explain mistakes (no Gemini, instant) ----------
@app.route('/api/explain', methods=['POST'])
def explain():
    body = request.get_json(silent=True) or {}
    pairs = body.get('pairs', [])
    if body.get('header', True):
        lines = ["வணக்கம், இந்த அல்ட்ரா கிராமர் தமிழ் என்ஜின் பை கேஐகே செயலியின் படி நீங்கள் எழுதிய தவறுகள்:"]
        for i, (wrong, correct) in enumerate(pairs, 1):
            lines.append(f"{i}. {explain_pair(wrong, correct)}")
    else:
        lines = [explain_pair(w, c) for w, c in pairs]
    return jsonify({"text": "\n".join(lines)})


# ---------- Translate ----------
@app.route('/api/translate', methods=['POST'])
def translate_text():
    text = (request.get_json(silent=True) or {}).get('text', '')
    prompt = f"Translate the following Tamil text accurately into simple English. Return only the translation: '{text}'"
    try:
        translation = ask_gemini(prompt) or text
    except Exception as e:
        print("translate பிழை:", e)
        translation = text
    return jsonify({"status": "success", "translation": translation})


# ---------- Text to speech ----------
async def make_audio(text, voice):
    communicate = edge_tts.Communicate(text, voice)
    audio = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
    return audio


@app.route('/api/generate_tts', methods=['POST'])
def generate_tts():
    data = request.get_json(silent=True) or {}
    text = data.get('text', '').strip()
    lang = data.get('lang', 'ta')

    if not text:
        return jsonify({"status": "error", "message": "text இல்லை"}), 400

    voice = "ta-IN-PallaviNeural" if lang == 'ta' else "en-US-GuyNeural"

    try:
        audio = asyncio.run(make_audio(text, voice))
    except Exception as e:
        print("tts பிழை:", e)
        return jsonify({"status": "error", "message": str(e)}), 500

    if not audio:
        return jsonify({"status": "error", "message": "ஆடியோ உருவாகவில்லை"}), 500

    return Response(audio, mimetype="audio/mpeg")


if __name__ == '__main__':
    app.run(debug=True, port=5000)
