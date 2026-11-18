import os
import re
import json
import asyncio
import difflib
import random
import unicodedata
import hmac
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


def init_db():
    conn = None
    try:
        conn = db()
        if not conn:
            print("DATABASE_URL இல்லை: முடிவுகள் சேமிக்கப்படாது.")
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
    mistakes = d.get('mistakes') or []
    conn = None
    try:
        conn = db()
        if not conn:
            return jsonify({"saved": False})
        with conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO results (name, role, level, detail, mode, category, method, item_count,
                   seconds, mistakes_count, mistakes, details, sid) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (str(st.get('name', ''))[:200], str(st.get('role', ''))[:30], str(st.get('level', ''))[:30],
                 str(st.get('detail', ''))[:200], str(d.get('mode', ''))[:30], str(d.get('category', ''))[:30],
                 str(d.get('method', ''))[:30], d.get('count'), d.get('seconds'), len(mistakes),
                 json.dumps(mistakes, ensure_ascii=False)[:20000], str(d.get('details', ''))[:20000], str(st.get('sid', ''))[:60]))
        return jsonify({"saved": True})
    except Exception as e:
        print("save_result பிழை:", e)
        return jsonify({"saved": False}), 500
    finally:
        if conn:
            conn.close()


# ---------- Admin Results Page ----------
MODE_L = {'learning': 'Learning Mode', 'testing': 'Testing Mode', 'manual_writing': 'Manual Writing'}
METH_L = {'hearing': 'Hearing and Writing', 'manual': 'Self writing (Manual)', 'editor': 'Writing box (Manual Writing mode)'}


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
                         'level': (r['level'] or '').lower(), 'detail': (r['detail'] or '').strip(),
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
        p['words'] += words_in(r['details'], r['method'])
        p['mistakes'] += r['mistakes_count'] or 0
        if r['method'] != 'editor':
            p['items'] += r['item_count'] or 0
        if r['seconds'] is not None:
            p['seconds'] += r['seconds']
        line = (f"{ist(r['created_at']).strftime('%H:%M')} - {MODE_L.get(r['mode'], r['mode'])}, {r['category']}, "
                f"{METH_L.get(r['method'], r['method'])}, {r['item_count']} items, {r['mistakes_count']} mistakes")
        if r['seconds'] is not None:
            line += f", {r['seconds']} seconds"
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
    return (f"Name: {p['name']} | {p['role_label']} | {p['level_label']} | {p['detail_label']}: {p['detail']}\n"
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
        if action in ('delete_one', 'delete_all', 'delete_time'):
            conn = db()
            if not conn:
                return "டேட்டாபேஸ் இணைக்கப்படவில்லை. DATABASE_URL-ஐ சரிபார்க்கவும்.", 500
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
            return "டேட்டாபேஸ் இணைக்கப்படவில்லை. DATABASE_URL-ஐ சரிபார்க்கவும்.", 500
        search = request.form.get('search', '').strip().lower()
        if search:
            people = [p for p in people if search in (p['name'] + ' ' + p['detail'] + ' ' + p['role_label'] + ' ' + p['level_label']).lower()]
        return render_template_string(html, logged_in=True, error="", people=people, search=search)
    except Exception as e:
        print("Admin Page Error:", e)
        return f"Database Error: {e}", 500


@app.route('/api/log_login', methods=['POST'])
def log_login():
    st = (request.get_json(silent=True) or {}).get('student') or {}
    conn = None
    try:
        conn = db()
        if not conn:
            return jsonify({"saved": False})
        with conn, conn.cursor() as cur:
            cur.execute("INSERT INTO logins (name, role, level, detail, sid) VALUES (%s,%s,%s,%s,%s)",
                        (str(st.get('name', ''))[:200], str(st.get('role', ''))[:30], str(st.get('level', ''))[:30],
                         str(st.get('detail', ''))[:200], str(st.get('sid', ''))[:60]))
        return jsonify({"saved": True})
    except Exception as e:
        print("log_login பிழை:", e)
        return jsonify({"saved": False}), 500
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


def norm(s):
    s = unicodedata.normalize('NFC', s or '')
    s = re.sub(r'[\u200b-\u200d\ufeff\s]', '', s)
    return re.sub(r'[.,!?;:\'"“”‘’]+$', '', s)


def letter_name(s):
    return SUZHI.get(s, s) if len(s) == 1 else s


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
            parts.append(f"{wrong[a1:a2]} கூடுதலாக உள்ளது")
        elif tag == 'insert':
            parts.append(f"{correct[b1:b2]} விடுபட்டுள்ளது{braille_text(correct[b1:b2])}")
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

    used = get_used(category)
    used_norm = {norm(x) for x in used}

    rules = {
        'Words': "single meaningful Tamil words (one word each, no spaces)",
        'Line': "single meaningful Tamil sentences, each ending with a dot (.)",
        'Stanza': "short Tamil poetic stanzas of exactly 3 lines each (separate lines with a newline)",
        'Paragraph': "short Tamil paragraphs of 3 or 4 sentences, each sentence ending with a dot (.)",
    }[category]
    avoid = "; ".join(used[-30:]).replace("\n", " / ")
    prompt = f"""Generate exactly {count + 8} different, new {rules} for a Tamil spelling practice app.
Theme hint: {random.choice(TOPICS)}. Mix items that contain easily confused letters (ண ன ந, ல ள ழ, ர ற).
Do NOT use any of these already used items: {avoid or 'none'}.
Return ONLY the Tamil items separated by '|||'. No numbering, no markdown, no English."""

    items, chosen = [], set()
    try:
        for it in ask_gemini(prompt).split('|||'):
            it = re.sub(r'^\s*(\d+[.)]|[-*•])\s*', '', it.replace('*', '').replace('`', '')).strip()
            k = norm(it)
            if it and k not in used_norm and k not in chosen:
                items.append(it)
                chosen.add(k)
    except Exception as e:
        print("generate_prompts பிழை:", e)

    items = items[:count]
    chosen = {norm(x) for x in items}
    pool = FALLBACK[category][:]
    random.shuffle(pool)
    for it in pool:
        if len(items) >= count:
            break
        if norm(it) not in used_norm and norm(it) not in chosen:
            items.append(it)
            chosen.add(norm(it))
    k = 0
    while len(items) < count:
        items.append(pool[k % len(pool)])
        k += 1

    add_used(category, items)
    return jsonify({"status": "success", "items": items})


# ---------- Check one typed entry (self-writing) ----------
@app.route('/api/check_word', methods=['POST'])
def check_word():
    text = (request.get_json(silent=True) or {}).get('text', '').strip()
    prompt = f"""Check the Tamil spelling of this text: "{text}"
If it is completely correct, reply with exactly: சரி
Otherwise reply with ONLY the corrected text, nothing else."""
    reply = (ask_gemini(prompt).strip().splitlines() or [''])[0].replace('*', '').replace('`', '').strip().strip('"“”')
    if not reply or reply == 'சரி' or norm(reply) == norm(text):
        return jsonify({"ok": True, "correct": text})
    return jsonify({"ok": False, "correct": reply})


# ---------- Check a long text (Manual Writing editor) ----------
@app.route('/api/check_text', methods=['POST'])
def check_text():
    text = (request.get_json(silent=True) or {}).get('text', '')
    prompt = f"""You are an expert Tamil grammar and spelling checker.
Find every misspelt word in this Tamil text, especially மயங்கொலி mistakes (ண/ன/ந, ல/ள/ழ, ர/ற) and grammar slips:
\"\"\"{text}\"\"\"
Return ONLY a JSON array like [{{"wrong":"வனக்கம்","correct":"வணக்கம்"}}]. Use [] if there are no mistakes.
Each item must be a single word. No markdown."""
    raw = ask_gemini(prompt)
    raw = re.sub(r"```(?:json)?", "", raw).strip()
    mistakes = json.loads(raw)
    clean = [{"wrong": m["wrong"], "correct": m["correct"]} for m in mistakes
             if isinstance(m, dict) and m.get("wrong") and m.get("correct") and m["wrong"] != m["correct"]]
    return jsonify({"mistakes": clean})


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
