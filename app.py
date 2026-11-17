import os
import re
import json
import asyncio
import difflib
import random
import unicodedata
from flask import Flask, request, jsonify, send_from_directory, Response
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

    seen = load_seen()
    used = seen.get(category, [])
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

    seen[category] = (used + items)[-300:]
    save_seen(seen)
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
