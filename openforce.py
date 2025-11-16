# app.py
"""
OpenForce - Fixed stable backend (replacement)
- Robust parsing for text/html/json/pdf
- Schema fingerprinting & history stored in MongoDB
- /upload, /schema, /schema/history, /schemas, /records endpoints
- /records returns JSON-serializable documents (ObjectId/datetime safe)
"""

import os
import re
import json
import uuid
import hashlib
import datetime
from flask import Flask, request, jsonify
from werkzeug.utils import secure_filename
from flask_cors import CORS
from bs4 import BeautifulSoup
import xmltodict
import pymongo
import bleach
from PyPDF2 import PdfReader
from bson import ObjectId

# ---------- CONFIG ----------
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME = os.environ.get("DB_NAME", "openforce_etl")
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "./uploads")
ALLOWED = set([
    'html','htm','xml','json','txt','md','pdf','csv','yaml','yml'
])
MAX_CONTENT_BYTES = 10 * 1024 * 1024  # 10 MB

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ---------- APP & DB ----------
app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_BYTES

client = pymongo.MongoClient(MONGO_URI)
db = client[DB_NAME]
raw_col = db['raw_data']
proc_col = db['processed_data']
schema_col = db['schemas']
records_col = db['records']

# ---------- UTILITIES ----------
def get_ext(filename):
    return filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''

def detect_type_ext(ext, mimetype):
    if ext in ('html','htm'): return 'html'
    if ext == 'xml': return 'xml'
    if ext == 'json': return 'json'
    if ext in ('txt','md','csv','yaml','yml'): return 'text'
    if ext == 'pdf': return 'pdf'
    if mimetype:
        if 'html' in mimetype: return 'html'
        if 'xml' in mimetype: return 'xml'
        if 'json' in mimetype: return 'json'
        if 'pdf' in mimetype: return 'pdf'
    return 'unknown'

def _safe_decode(bytes_data):
    try:
        s = bytes_data.decode('utf-8', errors='ignore')
    except Exception:
        s = bytes_data.decode('utf-8', errors='ignore')
    if '\x00' in s:
        try:
            s2 = bytes_data.decode('utf-16', errors='ignore')
            s = s2
        except Exception:
            pass
    if '\x00' in s:
        s = s.replace('\x00', '')
    return s

def try_cast_value(v):
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    if not isinstance(v, str):
        return v
    v = v.strip()
    if v == "":
        return v
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        if "." not in v and v.replace(',', '').lstrip('-').isdigit():
            return int(v.replace(',', ''))
    except:
        pass
    try:
        return float(v.replace(',', ''))
    except:
        pass
    return v

def sanitize_html(s):
    allowed = list(bleach.sanitizer.ALLOWED_TAGS) + ['img','p','div','span','table','thead','tbody','tr','th','td']
    return bleach.clean(s, tags=allowed, strip=True)

# ---------- Parsing helpers ----------
JSON_LD_RE = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE)
HTML_TABLE_RE = re.compile(r'<table[\s\S]*?>[\s\S]*?</table>', re.IGNORECASE)
_SIMPLE_JSON_RE = re.compile(r'(\{[\s\S]*?\})', re.DOTALL)

def _extract_json_blocks(text):
    out = []
    for m in JSON_LD_RE.finditer(text):
        raw = m.group(1).strip()
        if raw:
            out.append({'kind': 'jsonld', 'text': raw})
    for m in _SIMPLE_JSON_RE.finditer(text):
        candidate = m.group(1).strip()
        if len(candidate) < 8:
            continue
        if ':' in candidate:
            out.append({'kind': 'json', 'text': candidate})
    return out

# ---------- Conservative KV extraction (avoid parsing JSON lines as KV) ----------
def extract_kv_pairs(text):
    kvs = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith('{') or line.startswith('}') or line.startswith('['):
            continue
        if line.startswith('"') or line.startswith("'"):
            continue
        if line.startswith('<') and line.endswith('>'):
            continue
        if ':' not in line:
            continue
        left, right = line.split(':', 1)
        key = left.strip()
        val = right.strip()
        if ' ' in key:
            continue
        if key.startswith('"') or key.endswith('"') or key.startswith("'") or key.endswith("'"):
            continue
        if '<' in key or '>' in key:
            continue
        if len(key) == 0 or len(key) > 120:
            continue
        kvs[key] = try_cast_value(val)
    return kvs

# ---------- Content Parsers ----------
def parse_html(content_bytes):
    html = _safe_decode(content_bytes)
    html = sanitize_html(html)
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.title.string if soup.title else None
    text = soup.get_text(separator=' ', strip=True)
    links = [a.get('href') for a in soup.find_all('a') if a.get('href')]
    summary = {
        'title': title,
        'text_snippet': text[:200],
        'links_count': len(links),
        'links': links[:10]
    }
    return {'full_text': text, 'meta': summary, 'links': links}

def parse_xml(content_bytes):
    try:
        d = xmltodict.parse(_safe_decode(content_bytes))
        return {'xml': d}
    except Exception:
        return {'xml_raw': _safe_decode(content_bytes)}

def parse_json(content_bytes):
    try:
        return json.loads(_safe_decode(content_bytes))
    except Exception:
        return {'json_raw': _safe_decode(content_bytes)}

def parse_pdf(path):
    try:
        reader = PdfReader(path)
        text_parts = []
        for page in reader.pages:
            try:
                text_parts.append(page.extract_text() or '')
            except Exception:
                text_parts.append('')
        full_text = "\n".join(text_parts)
        return {'pdf_text': full_text, 'length_chars': len(full_text)}
    except Exception as e:
        return {'error': 'pdf_parse_failed', 'message': str(e)}

# ---------- Text parser (robust) ----------
def parse_text(content_bytes):
    text = _safe_decode(content_bytes)
    kv = extract_kv_pairs(text)

    extracted_json = []
    extracted_json_ld = []
    for jb in _extract_json_blocks(text):
        raw = jb['text']
        try:
            parsed = json.loads(raw)
            if jb['kind'] == 'jsonld':
                extracted_json_ld.append(parsed)
            else:
                extracted_json.append(parsed)
        except Exception:
            if jb['kind'] == 'jsonld':
                extracted_json_ld.append(raw)
            else:
                extracted_json.append(raw)

    html_tables = []
    html_tables_count = 0
    for tmatch in HTML_TABLE_RE.finditer(text):
        tbl_html = tmatch.group(0)
        try:
            soup = BeautifulSoup(tbl_html, 'html.parser')
            table = soup.find('table')
            if not table:
                continue
            headers = []
            thead = table.find('thead')
            if thead:
                headers = [th.get_text(strip=True) for th in thead.find_all('th')]
            if not headers:
                first_row = table.find('tr')
                if first_row:
                    headers = [td.get_text(strip=True) for td in first_row.find_all(['th','td'])]
            rows = []
            for tr in table.find_all('tr'):
                cells = [td.get_text(strip=True) for td in tr.find_all(['td','th'])]
                if not cells:
                    continue
                if headers and len(headers) == len(cells):
                    rows.append({headers[i]: cells[i] for i in range(len(cells))})
                else:
                    rows.append({f'c{i}': cells[i] for i in range(len(cells))})
            if rows:
                html_tables.append(rows)
                html_tables_count += 1
        except Exception:
            continue

    csv_tables = []
    csv_tables_count = 0
    lines_clean = [ln for ln in text.splitlines() if ln.strip() != '']
    i = 0
    while i < len(lines_clean):
        if ',' in lines_clean[i]:
            block = []
            j = i
            while j < len(lines_clean) and ',' in lines_clean[j]:
                block.append(lines_clean[j])
                j += 1
            if len(block) >= 2:
                header = [h.strip() for h in block[0].split(',')]
                rows = []
                for row_line in block[1:]:
                    parts = [p.strip() for p in row_line.split(',')]
                    if len(parts) == len(header):
                        rows.append({header[k]: parts[k] for k in range(len(header))})
                    else:
                        rows.append({'raw': row_line})
                csv_tables.append(rows)
                csv_tables_count += 1
                i = j
                continue
        i += 1

    parsed_summary = {
        'json_like_fragments_est': max(0, len(extracted_json) + len(extracted_json_ld)),
        'html_tables_est': html_tables_count,
        'kv_pairs_est': len(kv),
        'csv_tables_est': csv_tables_count
    }

    out = {
        'parsed_fragments_summary': parsed_summary,
        'text': text
    }
    if kv:
        out['parsed'] = kv
    if extracted_json:
        out['extracted_json'] = extracted_json
    if extracted_json_ld:
        out['extracted_json_ld'] = extracted_json_ld
    if html_tables:
        out['html_tables'] = html_tables
    if csv_tables:
        out['csv_tables'] = csv_tables

    return out

# ---------- Schema fingerprinting ----------
def record_schema(obj):
    schema = {}
    def walk(prefix, v):
        if v is None:
            schema[prefix] = 'null'
            return
        if isinstance(v, dict):
            for k, val in v.items():
                walk(f"{prefix}.{k}" if prefix else k, val)
        elif isinstance(v, list):
            schema[prefix] = 'list'
            first = None
            for e in v:
                if e is not None:
                    first = e
                    break
            if first is not None:
                walk(prefix + "[]", first)
        else:
            schema[prefix] = type(v).__name__
    if not isinstance(obj, dict):
        walk('value', obj)
    else:
        walk('', obj)
    return dict(sorted(schema.items()))

def schema_id_from(source_id, schema_obj):
    s = source_id + '|' + json.dumps(schema_obj, sort_keys=True)
    h = hashlib.sha1(s.encode('utf-8')).hexdigest()[:12]
    return f"{source_id}_{h}"

def compare_and_save_schema(source_id, new_schema):
    last = schema_col.find_one({'source_id': source_id}, sort=[('created_at', pymongo.DESCENDING)])
    now = datetime.datetime.utcnow()
    if not last or last.get('schema') != new_schema:
        before = last['schema'] if last else {}
        diff = {
            'added': {k: v for k, v in new_schema.items() if k not in before},
            'removed': {k: v for k, v in before.items() if k not in new_schema},
            'changed': {k: {'from': before[k], 'to': new_schema[k]} for k in new_schema if k in before and before[k] != new_schema[k]}
        }
        sid = schema_id_from(source_id, new_schema)
        entry = {
            'source_id': source_id,
            'schema_id': sid,
            'created_at': now,
            'schema': new_schema,
            'diff': diff,
            'compatible_dbs': ['postgresql', 'mongodb']
        }
        schema_col.insert_one(entry)
        entry_copy = entry.copy()
        entry_copy['created_at'] = entry_copy['created_at'].isoformat()
        return entry_copy
    return None

# ---------- JSON-serializable helper for Mongo types ----------
def make_json_serializable(x):
    if x is None:
        return None
    if isinstance(x, ObjectId):
        return str(x)
    if isinstance(x, datetime.datetime):
        try:
            return x.isoformat()
        except:
            return str(x)
    if isinstance(x, bytes):
        try:
            return x.decode('utf-8', errors='ignore')
        except:
            return str(x)
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            out[k] = make_json_serializable(v)
        return out
    if isinstance(x, list):
        return [make_json_serializable(v) for v in x]
    return x

# ---------- ROUTES ----------
@app.route('/')
def ui_index():
    return app.send_static_file('index.html')

@app.route('/upload', methods=['POST'])
def upload():
    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'no file provided'}), 400

    source_id = request.form.get('source_id') or f"src_{uuid.uuid4().hex[:8]}"
    filename = secure_filename(file.filename or 'uploaded')
    ext = get_ext(filename)
    if ext and ext not in ALLOWED:
        return jsonify({'error': f'extension {ext} not allowed'}), 400

    save_name = f"{uuid.uuid4().hex[:12]}.{ext}" if ext else uuid.uuid4().hex
    saved_path = os.path.join(app.config['UPLOAD_FOLDER'], save_name)
    file.save(saved_path)

    mimetype = file.mimetype
    ftype = detect_type_ext(ext, mimetype)

    processed = {}
    parsed_fragments_summary = {}
    try:
        if ftype == 'html':
            with open(saved_path, 'rb') as f:
                processed = parse_html(f.read())
                parsed_fragments_summary = processed.get('meta', {})
        elif ftype == 'xml':
            with open(saved_path, 'rb') as f:
                processed = parse_xml(f.read())
        elif ftype == 'json':
            with open(saved_path, 'rb') as f:
                processed = parse_json(f.read())
        elif ftype == 'text':
            with open(saved_path, 'rb') as f:
                processed = parse_text(f.read())
                parsed_fragments_summary = processed.get('parsed_fragments_summary', {})
        elif ftype == 'pdf':
            processed = parse_pdf(saved_path)
            parsed_fragments_summary = {'pdf_text_chars': processed.get('length_chars', 0)}
        else:
            with open(saved_path, 'rb') as f:
                raw = f.read()
            processed = {'raw': _safe_decode(raw)[:2000]}
    except Exception as e:
        processed = {'error': str(e)}

    raw_doc = {
        'source_id': source_id,
        'original_filename': filename,
        'stored_filename': save_name,
        'path': saved_path,
        'mimetype': mimetype,
        'detected_type': ftype,
        'uploaded_at': datetime.datetime.utcnow()
    }
    raw_id = raw_col.insert_one(raw_doc).inserted_id

    proc_doc = {
        'source_id': source_id,
        'raw_id': raw_id,
        'processed': processed,
        'parsed_fragments_summary': parsed_fragments_summary,
        'created_at': datetime.datetime.utcnow()
    }
    proc_id = proc_col.insert_one(proc_doc).inserted_id

    # Build schema source by preferring parsed KV and first JSON fragment(s)
    schema_source = {}
    if isinstance(processed, dict) and 'parsed' in processed:
        schema_source.update(processed.get('parsed', {}))
    if isinstance(processed, dict) and 'extracted_json' in processed and processed['extracted_json']:
        first = processed['extracted_json'][0]
        if isinstance(first, dict):
            schema_source.update(first)
    if isinstance(processed, dict) and 'extracted_json_ld' in processed and processed['extracted_json_ld']:
        first = processed['extracted_json_ld'][0]
        if isinstance(first, dict):
            schema_source.update(first)
    if not schema_source:
        schema_source = processed if isinstance(processed, dict) else {'value': processed}

    schema = record_schema(schema_source)
    schema_change = compare_and_save_schema(source_id, schema)

    try:
        if isinstance(schema_source, dict):
            records_col.insert_one({
                'source_id': source_id,
                'raw_id': raw_id,
                'record': schema_source,
                'ingested_at': datetime.datetime.utcnow()
            })
    except Exception:
        pass

    out = {
        'status': 'ok',
        'source_id': source_id,
        'file_id': str(raw_id),
        'processed_id': str(proc_id),
        'schema': schema,
        'detected_type': ftype,
        'parsed_fragments_summary': parsed_fragments_summary
    }
    if schema_change:
        out['schema_id'] = schema_change['schema_id']
        out['schema_generated_at'] = schema_change['created_at']
        out['compatible_dbs'] = schema_change.get('compatible_dbs', [])
        out['schema_diff'] = schema_change['diff']
    else:
        last = schema_col.find_one({'source_id': source_id}, sort=[('created_at', pymongo.DESCENDING)])
        if last:
            out['schema_id'] = last['schema_id']
            out['schema_generated_at'] = last['created_at'].isoformat()
            out['compatible_dbs'] = last.get('compatible_dbs', [])

    return jsonify(out), 201

@app.route('/schema', methods=['GET'])
def get_schema_latest():
    source_id = request.args.get('source_id')
    if not source_id:
        return jsonify({'error': 'source_id parameter required'}), 400
    last = schema_col.find_one({'source_id': source_id}, sort=[('created_at', pymongo.DESCENDING)])
    if not last:
        return jsonify({'error': 'no schema for source_id'}), 404
    last_copy = {
        'schema_id': last['schema_id'],
        'generated_at': last['created_at'].isoformat(),
        'compatible_dbs': last.get('compatible_dbs', []),
        'fields': [{'path': k, 'type': v} for k, v in last['schema'].items()],
        'diff': last.get('diff', {})
    }
    return jsonify(last_copy)

@app.route('/schema/history', methods=['GET'])
def get_schema_history():
    source_id = request.args.get('source_id')
    if not source_id:
        return jsonify({'error': 'source_id parameter required'}), 400
    docs = list(schema_col.find({'source_id': source_id}).sort('created_at', -1).limit(50))
    out = []
    for d in docs:
        out.append({
            'schema_id': d['schema_id'],
            'created_at': d['created_at'].isoformat(),
            'schema': d['schema'],
            'diff': d.get('diff', {}),
            'compatible_dbs': d.get('compatible_dbs', [])
        })
    return jsonify(out)

@app.route('/schemas', methods=['GET'])
def schemas_index():
    docs = list(schema_col.find().sort('created_at', -1).limit(20))
    for d in docs:
        d['_id'] = str(d['_id'])
        d['created_at'] = d['created_at'].isoformat()
    return jsonify(docs)

@app.route('/records', methods=['GET'])
def get_records():
    source_id = request.args.get('source_id')
    if not source_id:
        return jsonify({'error': 'source_id parameter required'}), 400

    try:
        limit = int(request.args.get('limit') or 100)
    except:
        limit = 100

    try:
        docs_cursor = raw_col.find({'source_id': source_id}).sort('uploaded_at', -1).limit(limit)
    except Exception:
        docs_cursor = raw_col.find().limit(limit)

    docs = []
    for d in docs_cursor:
        docs.append(make_json_serializable(d))

    return jsonify(docs)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'ok': True, 'db': DB_NAME})

if __name__ == '__main__':
    app.run(debug=True, port=int(os.environ.get('PORT', 5000)))
