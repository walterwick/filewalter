#!/usr/bin/env python3
"""
S3 / Cloudflare R2 Manager - Tek dosyalık Flask uygulaması
------------------------------------------------------------
- 2 bucket kod içinde gömülü (daha sonra .env'e taşınabilir)
- Arayüzden bucket ekleme / değiştirme (buckets_extra.json'a kaydedilir)
- Sürükle-bırak, Ctrl+V ile yapıştırma, çoklu yükleme + ilerleme çubuğu
- Linkten (URL) yükleme: sunucu indirir, direkt bucket'a stream eder
- Klasör mantığı (prefix tabanlı), yeniden adlandırma, taşıma (aynı/farklı bucket)
- Public link ve signed (presigned) link oluşturma
- Önizleme: resim/video/pdf için presigned URL ile direkt tarayıcıda (server proxy yok -> hızlı)

Çalıştırma:
    pip install flask boto3
    python s3manager.py
    -> http://127.0.0.1:5000
"""

import os
import io
import re
import json
import socket
import ipaddress
import mimetypes
import posixpath
import threading
import urllib.parse
import urllib.request
from datetime import datetime

from flask import Flask, request, jsonify, render_template_string, Response
import boto3
from boto3.s3.transfer import TransferConfig
from botocore.client import Config
from botocore.exceptions import ClientError

# ------------------------------------------------------------------
# BUCKET KONFİGÜRASYONU (daha sonra .env'e taşıyabilirsin)
# ------------------------------------------------------------------
BUCKETS = {
    'bucket1': {
        'name': 'Bucket 1',
        'access_key_id': '0a7f0017d897d5b8b982ad26e5711a21',
        'secret_access_key': '9125af83e42fbf5237304c13ea46df52bb1aa5557227fa5f15e8df2fe45f9f31',
        'bucket_name': 'walter',
        'endpoint_url': 'https://a3855f747521f7ef4cea32514f2279e2.r2.cloudflarestorage.com',
        'region': 'auto',
        'public_url': 'https://pub-4dfa55f23513493caa0be91a20704b4c.r2.dev/',
    },
    'bucket2': {
        'name': 'Bucket 2',
        'access_key_id': '112b183cdd116461df8ee2d8a647a58c',
        'secret_access_key': 'dd104010783bf0278926182bb9a1d0496c6f62907241d5f196918d7089fe005d',
        'bucket_name': 'walter',
        'endpoint_url': 'https://238a54d3f39bc03c25b5550bbd2683ed.r2.cloudflarestorage.com',
        'region': 'auto',
        'public_url': 'https://s3.walterwick.de/',
    },
}

DEFAULT_BUCKET = 'bucket1'
EXTRA_BUCKETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'buckets_extra.json')
LIST_PAGE_SIZE = 300

# Buyuk dosyalarda otomatik parcali (multipart) yukleme + paralel parca gonderimi
TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=8 * 1024 * 1024,
    multipart_chunksize=8 * 1024 * 1024,
    max_concurrency=4,
    use_threads=True,
)

_lock = threading.Lock()
_clients = {}  # bucket_key -> boto3 client cache


def load_extra_buckets():
    if os.path.exists(EXTRA_BUCKETS_FILE):
        try:
            with open(EXTRA_BUCKETS_FILE, 'r', encoding='utf-8') as f:
                extra = json.load(f)
            BUCKETS.update(extra)
        except Exception as e:
            print(f"[uyari] buckets_extra.json okunamadi: {e}")


def save_extra_buckets():
    # Kod içindeki ilk 2 bucket'i tekrar dosyaya yazmıyoruz, sadece arayüzden eklenenleri.
    base_keys = {'bucket1', 'bucket2'}
    extra = {k: v for k, v in BUCKETS.items() if k not in base_keys}
    with open(EXTRA_BUCKETS_FILE, 'w', encoding='utf-8') as f:
        json.dump(extra, f, ensure_ascii=False, indent=2)


def get_client(bucket_key):
    with _lock:
        if bucket_key in _clients:
            return _clients[bucket_key]
        cfg = BUCKETS.get(bucket_key)
        if not cfg:
            raise KeyError(f"Bilinmeyen bucket: {bucket_key}")
        client = boto3.client(
            's3',
            aws_access_key_id=cfg['access_key_id'],
            aws_secret_access_key=cfg['secret_access_key'],
            endpoint_url=cfg['endpoint_url'],
            region_name=cfg.get('region', 'auto'),
            config=Config(signature_version='s3v4', s3={'addressing_style': 'path'}),
        )
        _clients[bucket_key] = client
        return client


load_extra_buckets()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = None  # büyük dosyalara izin ver


def bucket_cfg(bucket_key):
    cfg = BUCKETS.get(bucket_key)
    if not cfg:
        raise KeyError('bucket bulunamadi')
    return cfg


def public_url_for(bucket_key, key):
    cfg = bucket_cfg(bucket_key)
    base = cfg.get('public_url') or ''
    if not base:
        return None
    if not base.endswith('/'):
        base += '/'
    return base + key


# ------------------------------------------------------------------
# API: bucket listesi / ekleme
# ------------------------------------------------------------------
@app.route('/api/buckets', methods=['GET'])
def api_list_buckets():
    out = []
    for k, v in BUCKETS.items():
        out.append({'key': k, 'name': v.get('name', k), 'has_public_url': bool(v.get('public_url'))})
    return jsonify({'buckets': out, 'default': DEFAULT_BUCKET})


@app.route('/api/buckets', methods=['POST'])
def api_add_bucket():
    data = request.get_json(force=True)
    key = (data.get('key') or '').strip()
    if not key or not key.replace('_', '').isalnum():
        return jsonify({'error': 'Gecerli bir anahtar (key) girin (harf/rakam/_)'}), 400
    if key in BUCKETS:
        return jsonify({'error': 'Bu key zaten mevcut'}), 400
    required = ['name', 'access_key_id', 'secret_access_key', 'bucket_name', 'endpoint_url']
    for r in required:
        if not data.get(r):
            return jsonify({'error': f'"{r}" alani zorunlu'}), 400
    BUCKETS[key] = {
        'name': data['name'],
        'access_key_id': data['access_key_id'],
        'secret_access_key': data['secret_access_key'],
        'bucket_name': data['bucket_name'],
        'endpoint_url': data['endpoint_url'],
        'region': data.get('region') or 'auto',
        'public_url': data.get('public_url') or '',
    }
    save_extra_buckets()
    return jsonify({'ok': True})


@app.route('/api/buckets/<bucket_key>', methods=['DELETE'])
def api_delete_bucket(bucket_key):
    if bucket_key in ('bucket1', 'bucket2'):
        return jsonify({'error': 'Kod icindeki bucketlar silinemez'}), 400
    if bucket_key in BUCKETS:
        del BUCKETS[bucket_key]
        _clients.pop(bucket_key, None)
        save_extra_buckets()
        return jsonify({'ok': True})
    return jsonify({'error': 'bulunamadi'}), 404


# ------------------------------------------------------------------
# API: dosya listeleme (klasör mantığı = prefix + delimiter)
# ------------------------------------------------------------------
@app.route('/api/list', methods=['GET'])
def api_list():
    bucket_key = request.args.get('bucket', DEFAULT_BUCKET)
    prefix = request.args.get('prefix', '')
    token = request.args.get('token') or None
    if prefix and not prefix.endswith('/'):
        prefix += '/'
    cfg = bucket_cfg(bucket_key)
    client = get_client(bucket_key)
    try:
        kwargs = {'Bucket': cfg['bucket_name'], 'Prefix': prefix, 'Delimiter': '/', 'MaxKeys': LIST_PAGE_SIZE}
        if token:
            kwargs['ContinuationToken'] = token
        resp = client.list_objects_v2(**kwargs)
    except ClientError as e:
        return jsonify({'error': str(e)}), 500

    folders = []
    for cp in resp.get('CommonPrefixes', []):
        p = cp['Prefix']
        folders.append({'name': p[len(prefix):].rstrip('/'), 'prefix': p})

    files = []
    for obj in resp.get('Contents', []):
        key = obj['Key']
        if key == prefix:
            continue
        name = key[len(prefix):]
        if not name or '/' in name:
            continue
        files.append({
            'name': name,
            'key': key,
            'size': obj['Size'],
            'last_modified': obj['LastModified'].strftime('%Y-%m-%d %H:%M'),
            'public_url': public_url_for(bucket_key, key),
        })

    next_token = resp.get('NextContinuationToken') if resp.get('IsTruncated') else None
    return jsonify({'prefix': prefix, 'folders': folders, 'files': files, 'next_token': next_token})


# ------------------------------------------------------------------
# API: bucket boyutu / dosya sayisi (tum bucket icin, sayfali tarama)
# ------------------------------------------------------------------
@app.route('/api/bucket-stats', methods=['GET'])
def api_bucket_stats():
    bucket_key = request.args.get('bucket', DEFAULT_BUCKET)
    cfg = bucket_cfg(bucket_key)
    client = get_client(bucket_key)
    count = 0
    total_size = 0
    token = None
    truncated_safety = False
    max_pages = 200  # guvenlik siniri (~200k obje), asirilikta hesap yarim kalir ama uygulama askida kalmaz
    pages = 0
    try:
        while True:
            kwargs = {'Bucket': cfg['bucket_name'], 'MaxKeys': 1000}
            if token:
                kwargs['ContinuationToken'] = token
            resp = client.list_objects_v2(**kwargs)
            for obj in resp.get('Contents', []):
                count += 1
                total_size += obj['Size']
            pages += 1
            if resp.get('IsTruncated'):
                token = resp.get('NextContinuationToken')
                if pages >= max_pages:
                    truncated_safety = True
                    break
            else:
                break
    except ClientError as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'count': count, 'total_size': total_size, 'partial': truncated_safety})


# ------------------------------------------------------------------
# API: yükleme (drag&drop, ctrl+v, çoklu dosya) - streaming upload
# ------------------------------------------------------------------
@app.route('/api/upload', methods=['POST'])
def api_upload():
    bucket_key = request.form.get('bucket', DEFAULT_BUCKET)
    prefix = request.form.get('prefix', '')
    if prefix and not prefix.endswith('/'):
        prefix += '/'
    filename = request.form.get('filename')
    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'dosya yok'}), 400

    name = filename or file.filename or f"paste_{int(datetime.utcnow().timestamp())}"
    key = prefix + name
    cfg = bucket_cfg(bucket_key)
    client = get_client(bucket_key)
    content_type = file.mimetype or mimetypes.guess_type(name)[0] or 'application/octet-stream'
    try:
        client.upload_fileobj(
            file.stream, cfg['bucket_name'], key,
            ExtraArgs={'ContentType': content_type},
            Config=TRANSFER_CONFIG,
        )
    except ClientError as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'ok': True, 'key': key, 'public_url': public_url_for(bucket_key, key)})


# ------------------------------------------------------------------
# API: linkten yükleme (sunucu indirir, direkt bucket'a stream eder)
# ------------------------------------------------------------------
def _is_safe_url(url):
    p = urllib.parse.urlparse(url)
    if p.scheme not in ('http', 'https') or not p.hostname:
        return False
    try:
        for info in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
    except Exception:
        return False
    return True


def _filename_from_response(resp, url):
    cd = resp.headers.get('Content-Disposition', '')
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
    if m:
        name = urllib.parse.unquote(m.group(1)).strip()
        if name:
            return posixpath.basename(name)
    name = posixpath.basename(urllib.parse.unquote(urllib.parse.urlparse(url).path))
    return name or f"indirilen_{int(datetime.utcnow().timestamp())}"


@app.route('/api/upload-url', methods=['POST'])
def api_upload_url():
    data = request.get_json(force=True)
    bucket_key = data.get('bucket', DEFAULT_BUCKET)
    prefix = data.get('prefix', '')
    url = (data.get('url') or '').strip()
    custom_name = (data.get('filename') or '').strip()
    if prefix and not prefix.endswith('/'):
        prefix += '/'
    if not _is_safe_url(url):
        return jsonify({'error': 'Gecersiz veya izin verilmeyen link'}), 400

    cfg = bucket_cfg(bucket_key)
    client = get_client(bucket_key)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if not _is_safe_url(resp.geturl()):
                return jsonify({'error': 'Yonlendirme izin verilmeyen bir adrese gidiyor'}), 400
            name = custom_name or _filename_from_response(resp, url)
            key = prefix + name
            content_type = (resp.headers.get('Content-Type', '').split(';')[0].strip()
                            or mimetypes.guess_type(name)[0] or 'application/octet-stream')
            client.upload_fileobj(
                resp, cfg['bucket_name'], key,
                ExtraArgs={'ContentType': content_type},
                Config=TRANSFER_CONFIG,
            )
    except ClientError as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        return jsonify({'error': f'Indirilemedi: {e}'}), 500

    return jsonify({'ok': True, 'key': key, 'public_url': public_url_for(bucket_key, key)})


# ------------------------------------------------------------------
# API: yeni klasör (sıfır byte'lık marker obje)
# ------------------------------------------------------------------
@app.route('/api/mkdir', methods=['POST'])
def api_mkdir():
    data = request.get_json(force=True)
    bucket_key = data.get('bucket', DEFAULT_BUCKET)
    prefix = data.get('prefix', '')
    name = (data.get('name') or '').strip().strip('/')
    if not name:
        return jsonify({'error': 'klasor adi bos olamaz'}), 400
    if prefix and not prefix.endswith('/'):
        prefix += '/'
    key = f"{prefix}{name}/"
    cfg = bucket_cfg(bucket_key)
    client = get_client(bucket_key)
    try:
        client.put_object(Bucket=cfg['bucket_name'], Key=key, Body=b'')
    except ClientError as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})


# ------------------------------------------------------------------
# API: silme (tekli / çoklu, klasör dahil recursive)
# ------------------------------------------------------------------
@app.route('/api/delete', methods=['POST'])
def api_delete():
    data = request.get_json(force=True)
    bucket_key = data.get('bucket', DEFAULT_BUCKET)
    keys = data.get('keys', [])
    if not keys:
        return jsonify({'error': 'silinecek dosya yok'}), 400
    cfg = bucket_cfg(bucket_key)
    client = get_client(bucket_key)
    all_keys = set()
    for key in keys:
        if key.endswith('/'):
            all_keys.update(_list_all_keys(client, cfg['bucket_name'], key))
        else:
            all_keys.add(key)
    try:
        objs = [{'Key': k} for k in all_keys]
        for i in range(0, len(objs), 1000):
            client.delete_objects(Bucket=cfg['bucket_name'], Delete={'Objects': objs[i:i + 1000]})
    except ClientError as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True, 'deleted': len(all_keys)})


def _list_all_keys(client, bucket_name, prefix):
    keys = []
    token = None
    while True:
        kwargs = {'Bucket': bucket_name, 'Prefix': prefix}
        if token:
            kwargs['ContinuationToken'] = token
        resp = client.list_objects_v2(**kwargs)
        for obj in resp.get('Contents', []):
            keys.append(obj['Key'])
        if resp.get('IsTruncated'):
            token = resp.get('NextContinuationToken')
        else:
            break
    return keys


# ------------------------------------------------------------------
# API: yeniden adlandırma (rename) - aynı bucket icinde copy + delete
# ------------------------------------------------------------------
@app.route('/api/rename', methods=['POST'])
def api_rename():
    data = request.get_json(force=True)
    bucket_key = data.get('bucket', DEFAULT_BUCKET)
    old_key = data['key']
    new_name = data['new_name'].strip()
    if not new_name:
        return jsonify({'error': 'yeni isim bos olamaz'}), 400
    cfg = bucket_cfg(bucket_key)
    client = get_client(bucket_key)

    is_folder = old_key.endswith('/')
    parent = posixpath.dirname(old_key.rstrip('/')) + '/' if '/' in old_key.rstrip('/') else ''
    if parent == '/':
        parent = ''

    try:
        if is_folder:
            new_prefix = f"{parent}{new_name}/"
            old_prefix = old_key
            all_keys = _list_all_keys(client, cfg['bucket_name'], old_prefix)
            for k in all_keys:
                new_k = new_prefix + k[len(old_prefix):]
                client.copy_object(Bucket=cfg['bucket_name'], CopySource={'Bucket': cfg['bucket_name'], 'Key': k}, Key=new_k)
            objs = [{'Key': k} for k in all_keys]
            for i in range(0, len(objs), 1000):
                client.delete_objects(Bucket=cfg['bucket_name'], Delete={'Objects': objs[i:i + 1000]})
        else:
            new_key = parent + new_name
            client.copy_object(Bucket=cfg['bucket_name'], CopySource={'Bucket': cfg['bucket_name'], 'Key': old_key}, Key=new_key)
            client.delete_object(Bucket=cfg['bucket_name'], Key=old_key)
    except ClientError as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'ok': True})


# ------------------------------------------------------------------
# API: taşıma (move) - aynı bucket (copy hizli) veya farkli bucket (stream)
# ------------------------------------------------------------------
@app.route('/api/move', methods=['POST'])
def api_move():
    data = request.get_json(force=True)
    src_bucket = data['src_bucket']
    dst_bucket = data['dst_bucket']
    src_key = data['src_key']
    dst_prefix = data.get('dst_prefix', '')
    if dst_prefix and not dst_prefix.endswith('/'):
        dst_prefix += '/'

    src_cfg = bucket_cfg(src_bucket)
    dst_cfg = bucket_cfg(dst_bucket)
    src_client = get_client(src_bucket)
    dst_client = get_client(dst_bucket)

    is_folder = src_key.endswith('/')
    keys = _list_all_keys(src_client, src_cfg['bucket_name'], src_key) if is_folder else [src_key]

    try:
        for k in keys:
            name = k[len(src_key):] if is_folder else posixpath.basename(k)
            dst_key = dst_prefix + (posixpath.basename(src_key.rstrip('/')) + '/' + name if is_folder else name)
            if src_bucket == dst_bucket:
                dst_client.copy_object(Bucket=dst_cfg['bucket_name'], CopySource={'Bucket': src_cfg['bucket_name'], 'Key': k}, Key=dst_key)
            else:
                obj = src_client.get_object(Bucket=src_cfg['bucket_name'], Key=k)
                dst_client.upload_fileobj(obj['Body'], dst_cfg['bucket_name'], dst_key)
            src_client.delete_object(Bucket=src_cfg['bucket_name'], Key=k)
    except ClientError as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'ok': True})


# ------------------------------------------------------------------
# API: kopyalama (copy) - aynı/farkli bucket, orijinali silmez
# ------------------------------------------------------------------
@app.route('/api/copy', methods=['POST'])
def api_copy():
    data = request.get_json(force=True)
    src_bucket = data['src_bucket']
    dst_bucket = data['dst_bucket']
    src_key = data['src_key']
    dst_prefix = data.get('dst_prefix', '')
    if dst_prefix and not dst_prefix.endswith('/'):
        dst_prefix += '/'

    src_cfg = bucket_cfg(src_bucket)
    dst_cfg = bucket_cfg(dst_bucket)
    src_client = get_client(src_bucket)
    dst_client = get_client(dst_bucket)

    name = posixpath.basename(src_key.rstrip('/'))
    dst_key = dst_prefix + name
    try:
        if src_bucket == dst_bucket:
            dst_client.copy_object(Bucket=dst_cfg['bucket_name'], CopySource={'Bucket': src_cfg['bucket_name'], 'Key': src_key}, Key=dst_key)
        else:
            obj = src_client.get_object(Bucket=src_cfg['bucket_name'], Key=src_key)
            dst_client.upload_fileobj(obj['Body'], dst_cfg['bucket_name'], dst_key)
    except ClientError as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})


# ------------------------------------------------------------------
# API: signed URL (presigned) ve public URL
# ------------------------------------------------------------------
@app.route('/api/presign', methods=['GET'])
def api_presign():
    bucket_key = request.args.get('bucket', DEFAULT_BUCKET)
    key = request.args.get('key')
    expires = int(request.args.get('expires', 3600))
    cfg = bucket_cfg(bucket_key)
    client = get_client(bucket_key)
    try:
        url = client.generate_presigned_url(
            'get_object', Params={'Bucket': cfg['bucket_name'], 'Key': key}, ExpiresIn=expires
        )
    except ClientError as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'url': url, 'expires_in': expires})


@app.route('/api/public-url', methods=['GET'])
def api_public_url():
    bucket_key = request.args.get('bucket', DEFAULT_BUCKET)
    key = request.args.get('key')
    url = public_url_for(bucket_key, key)
    if not url:
        return jsonify({'error': 'bu bucket icin public_url tanimli degil'}), 400
    return jsonify({'url': url})


# ------------------------------------------------------------------
# HTML arayüz
# ------------------------------------------------------------------
INDEX_HTML = r"""
<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>S3 / R2 Manager</title>
<style>
  :root{
    --bg:#111318; --panel:#16181f; --panel2:#1c1f28; --border:#282c36; --border-soft:#20232c;
    --text:#e4e6eb; --muted:#8a8f9c; --accent:#5b7fdb; --accent-soft:#232a3e;
    --danger:#e0596b; --radius:8px; --radius-sm:6px;
  }
  *{box-sizing:border-box;}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
       background:var(--bg);color:var(--text);font-size:13px;-webkit-font-smoothing:antialiased;}
  #app{display:flex;flex-direction:column;height:100vh;}
  svg{display:block;}
  .icon{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round;flex-shrink:0;}

  header{display:flex;align-items:center;gap:8px;padding:9px 14px;background:var(--panel);
         border-bottom:1px solid var(--border-soft);flex-wrap:wrap;}
  header h1{font-size:13px;margin:0;font-weight:600;white-space:nowrap;color:var(--muted);
            letter-spacing:.02em;display:flex;align-items:center;gap:6px;}
  select, button, input{font-family:inherit;}
  select#bucketSelect, select#sortSelect{background:var(--panel2);color:var(--text);border:1px solid var(--border);
       border-radius:var(--radius-sm);padding:6px 8px;font-size:12.5px;}
  .bucket-stats{font-size:11px;color:var(--muted);white-space:nowrap;cursor:pointer;padding:4px 2px;}
  .bucket-stats:hover{color:var(--text);}
  .btn{background:transparent;color:var(--text);border:1px solid var(--border);border-radius:var(--radius-sm);
       padding:6px 10px;font-size:12.5px;cursor:pointer;display:inline-flex;align-items:center;gap:6px;
       transition:.12s;white-space:nowrap;}
  .btn:hover{background:var(--panel2);border-color:#3a3f4d;}
  .btn.primary{background:var(--accent);border-color:var(--accent);color:#fff;}
  .btn.primary:hover{filter:brightness(1.08);}
  .btn.danger{color:var(--danger);}
  .btn.danger:hover{background:rgba(224,89,107,.1);border-color:var(--danger);}
  .btn.small{padding:4px 8px;font-size:11.5px;}
  #searchBox{flex:1;min-width:110px;background:var(--panel2);border:1px solid var(--border);
       color:var(--text);border-radius:var(--radius-sm);padding:6px 10px;font-size:12.5px;}
  #searchBox:focus, select:focus, input:focus, textarea:focus{outline:none;border-color:var(--accent);}

  .breadcrumb{display:flex;align-items:center;gap:2px;padding:8px 14px;background:var(--panel);
       border-bottom:1px solid var(--border-soft);font-size:12px;color:var(--muted);flex-wrap:wrap;}
  .breadcrumb span{cursor:pointer;padding:3px 5px;border-radius:5px;display:inline-flex;align-items:center;gap:4px;}
  .breadcrumb span:hover{background:var(--panel2);color:var(--text);}
  .breadcrumb .sep{color:#3a3f4d;cursor:default;padding:0 2px;}

  #dropzone{flex:1;overflow-y:auto;position:relative;padding:12px 14px 84px;}
  #dropzone.dragover::after{content:'Dosyaları buraya bırak';position:fixed;inset:0;
       background:rgba(91,127,219,.08);border:2px dashed var(--accent);z-index:50;
       display:flex;align-items:center;justify-content:center;font-size:15px;color:var(--accent);
       pointer-events:none;}

  #grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(138px,1fr));gap:8px;}
  .item{background:var(--panel);border:1px solid var(--border-soft);border-radius:var(--radius);
        padding:8px;cursor:pointer;position:relative;user-select:none;transition:.1s;}
  .item:hover{border-color:#3a3f4d;}
  .item.selected{border-color:var(--accent);background:var(--accent-soft);}
  .thumb{width:100%;aspect-ratio:1/1;border-radius:var(--radius-sm);background:var(--panel2);display:flex;
         align-items:center;justify-content:center;overflow:hidden;margin-bottom:6px;color:var(--muted);
         position:relative;}
  .thumb svg{width:26px;height:26px;}
  .thumb-folder{background:rgba(224,180,57,.14);color:#e0b039;}
  .thumb-image{background:rgba(91,127,219,.14);color:#82a3ec;}
  .thumb-video{background:rgba(168,113,219,.14);color:#b990e8;}
  .thumb-pdf{background:rgba(224,89,107,.14);color:#e4869a;}
  .thumb-archive{background:rgba(219,150,91,.14);color:#e5ae7c;}
  .thumb-audio{background:rgba(78,201,150,.14);color:#7be0af;}
  .thumb-doc{background:rgba(91,158,219,.14);color:#82bced;}
  .thumb-sheet{background:rgba(105,201,91,.14);color:#98e082;}
  .thumb-file{background:var(--panel2);color:var(--muted);}
  .ext-badge{position:absolute;bottom:3px;right:3px;font-size:8px;background:rgba(17,19,24,.8);
       color:var(--muted);padding:1px 4px;border-radius:4px;letter-spacing:.02em;font-weight:600;}
  .item .name{font-size:12px;word-break:break-word;line-height:1.3;max-height:2.6em;overflow:hidden;}
  .item .meta{font-size:10.5px;color:var(--muted);margin-top:3px;}
  .item .menu-btn{position:absolute;top:5px;right:5px;background:rgba(17,19,24,.7);border:none;
       color:#fff;border-radius:5px;width:20px;height:20px;cursor:pointer;display:none;
       align-items:center;justify-content:center;}
  .item .menu-btn svg{width:13px;height:13px;}
  .item:hover .menu-btn{display:flex;}
  .select-btn{position:absolute;top:5px;left:5px;width:18px;height:18px;border-radius:5px;
       border:1.5px solid var(--border);background:rgba(17,19,24,.55);cursor:pointer;display:none;
       align-items:center;justify-content:center;padding:0;z-index:2;}
  .select-btn svg{color:#fff;}
  .item:hover .select-btn, .item.selected .select-btn{display:flex;}
  .item.selected .select-btn{background:var(--accent);border-color:var(--accent);}

  #grid.list-view{display:flex;flex-direction:column;gap:2px;}
  #grid.list-view .item{display:flex;align-items:center;gap:10px;padding:6px 8px;}
  #grid.list-view .thumb{width:26px;height:26px;aspect-ratio:auto;margin-bottom:0;flex-shrink:0;}
  #grid.list-view .thumb svg{width:15px;height:15px;}
  #grid.list-view .ext-badge{display:none;}
  #grid.list-view .name{flex:1;max-height:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  #grid.list-view .meta{margin-top:0;white-space:nowrap;flex-shrink:0;}
  #grid.list-view .menu-btn, #grid.list-view .select-btn{position:static;display:flex;flex-shrink:0;}
  #grid.list-view .select-btn{background:transparent;border-color:var(--border);}
  #grid.list-view .select-btn svg{color:var(--muted);}
  #grid.list-view .item.selected .select-btn{background:var(--accent);border-color:var(--accent);}
  #grid.list-view .item.selected .select-btn svg{color:#fff;}
  #grid.list-view .menu-btn{background:transparent;color:var(--muted);}

  #ctxmenu{position:fixed;background:var(--panel2);border:1px solid var(--border);border-radius:var(--radius);
       padding:4px;z-index:200;min-width:170px;box-shadow:0 8px 24px rgba(0,0,0,.35);display:none;}
  #ctxmenu button{display:flex;width:100%;text-align:left;background:none;border:none;color:var(--text);
       padding:7px 9px;border-radius:5px;cursor:pointer;font-size:12.5px;gap:8px;align-items:center;}
  #ctxmenu button:hover{background:var(--panel);}
  #ctxmenu button.danger{color:var(--danger);}
  #ctxmenu hr{border:none;border-top:1px solid var(--border-soft);margin:4px 2px;}

  .modal-back{position:fixed;inset:0;background:rgba(10,11,14,.6);display:none;align-items:center;
       justify-content:center;z-index:300;}
  .modal{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);
       padding:18px;width:min(420px,90vw);max-height:85vh;overflow-y:auto;}
  .modal h3{margin-top:0;font-size:13.5px;font-weight:600;color:var(--text);}
  .modal label{display:block;font-size:11px;color:var(--muted);margin:10px 0 4px;}
  .modal input, .modal select{width:100%;background:var(--panel2);border:1px solid var(--border);
       color:var(--text);border-radius:var(--radius-sm);padding:8px 9px;font-size:12.5px;}
  .modal textarea{width:100%;background:var(--panel2);border:1px solid var(--border);
       color:var(--text);border-radius:var(--radius-sm);padding:8px 9px;font-size:12.5px;
       font-family:inherit;resize:vertical;}
  .modal .row-btns{display:flex;gap:8px;justify-content:flex-end;margin-top:16px;}
  .link-row{display:flex;gap:6px;margin-top:6px;}
  .link-row input{flex:1;}

  #toasts{position:fixed;bottom:14px;right:14px;z-index:400;display:flex;flex-direction:column;gap:6px;}
  .toast{background:var(--panel2);border:1px solid var(--border);border-left:2px solid var(--accent);
       padding:8px 12px;border-radius:var(--radius-sm);font-size:12px;box-shadow:0 4px 14px rgba(0,0,0,.3);
       animation:slidein .15s ease;}
  .toast.error{border-left-color:var(--danger);}
  .toast.success{border-left-color:#4fae7c;}
  @keyframes slidein{from{transform:translateX(20px);opacity:0}to{transform:translateX(0);opacity:1}}

  #uploads{position:fixed;bottom:14px;left:14px;z-index:400;display:flex;flex-direction:column;gap:5px;
       max-width:260px;}
  .up-item{background:var(--panel2);border:1px solid var(--border);border-radius:var(--radius-sm);padding:7px 9px;
       font-size:11px;}
  .up-item .bar{height:3px;background:var(--border);border-radius:2px;margin-top:5px;overflow:hidden;}
  .up-item .bar > div{height:100%;background:var(--accent);width:0%;transition:.15s;}
  .up-item .u-pct{font-size:10px;color:var(--muted);margin-top:3px;}

  .selection-bar{position:sticky;bottom:0;background:var(--panel2);border:1px solid var(--border);
       border-radius:var(--radius);padding:7px 10px;display:none;align-items:center;gap:8px;margin-top:8px;}
  .selection-bar span{font-size:12px;color:var(--muted);margin-right:2px;}

  #previewModal .modal{display:flex;flex-direction:column;width:min(920px,94vw);
       max-height:92vh;overflow:hidden;padding:16px;}
  #previewModal .modal h3{flex-shrink:0;margin:0 0 10px;}
  #previewModal .modal .row-btns{flex-shrink:0;}
  .preview-wrap{position:relative;display:flex;align-items:center;justify-content:center;
       width:100%;height:74vh;flex-shrink:0;}
  .preview-wrap .previewBody{width:100%;height:100%;min-width:0;}
  #previewModal .previewBody{display:flex;align-items:center;justify-content:center;width:100%;height:100%;
       overflow:hidden;background:#000;border-radius:var(--radius-sm);}
  #previewModal img, #previewModal video{max-width:100%;max-height:100%;width:auto;height:auto;object-fit:contain;}
  #previewModal iframe{width:100%;height:100%;border:none;background:#fff;}
  #previewModal pre{max-height:100%;overflow:auto;width:100%;box-sizing:border-box;}
  .preview-nav{position:absolute;top:50%;transform:translateY(-50%);background:rgba(17,19,24,.6);
       border:1px solid var(--border);color:#fff;width:32px;height:32px;border-radius:50%;
       display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:5;padding:0;}
  .preview-nav:hover{background:rgba(17,19,24,.9);border-color:var(--accent);}
  .preview-nav.prev{left:6px;}
  .preview-nav.next{right:6px;}
  .preview-nav svg{width:17px;height:17px;stroke:currentColor;fill:none;stroke-width:2;
       stroke-linecap:round;stroke-linejoin:round;}

  .upload-row{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border-soft);}
  .upload-row:last-child{border-bottom:none;}
  .upload-row .icon{color:var(--muted);}
  .upload-row input{flex:1;}
  .upload-row .u-status{font-size:10.5px;color:var(--muted);white-space:nowrap;}
  .upload-row .u-status.dup{color:#d9a441;}
  .upload-list{max-height:260px;overflow-y:auto;margin-top:4px;}

  .load-more-wrap{display:flex;justify-content:center;margin-top:14px;}
  #loadMoreBtn{min-width:160px;justify-content:center;}

  @media (max-width:640px){
    header h1{display:none;}
    #grid{grid-template-columns:repeat(auto-fill,minmax(98px,1fr));gap:6px;}
    .modal{width:94vw;}
    #previewModal .modal{width:96vw;max-height:94vh;padding:12px;}
    .preview-wrap{height:62vh;}
    .preview-nav{width:28px;height:28px;}
    .preview-nav svg{width:15px;height:15px;}
    #previewModal .modal .row-btns{flex-wrap:wrap;}
  }
</style>
</head>
<body>
<div id="app">
  <header>
    <h1>S3 Manager</h1>
    <select id="bucketSelect"></select>
    <button class="btn" id="addBucketBtn">Bucket</button>
    <button class="btn" id="newFolderBtn">Klasör</button>
    <button class="btn primary" id="uploadBtn">Yükle</button>
    <button class="btn" id="urlBtn" title="Linkten yükle">Link</button>
    <input type="file" id="fileInput" multiple style="display:none;">
    <button class="btn small" id="viewToggleBtn" title="Görünümü değiştir"></button>
    <select id="sortSelect" title="Sırala">
      <option value="name_asc">İsim A-Z</option>
      <option value="name_desc">İsim Z-A</option>
      <option value="size_desc">Boyut (büyük-küçük)</option>
      <option value="size_asc">Boyut (küçük-büyük)</option>
      <option value="date_desc">Tarih (yeni-eski)</option>
      <option value="date_asc">Tarih (eski-yeni)</option>
    </select>
    <span id="bucketStats" class="bucket-stats" title="Yeniden hesaplamak için tıkla"></span>
    <input id="searchBox" placeholder="Ara...">
  </header>

  <div class="breadcrumb" id="breadcrumb"></div>

  <div id="dropzone">
    <div id="grid"></div>
    <div class="load-more-wrap" id="loadMoreWrap" style="display:none;">
      <button class="btn" id="loadMoreBtn" onclick="loadMore()">Daha Fazla Yükle</button>
    </div>
    <div class="selection-bar" id="selectionBar">
      <span id="selCount"></span>
      <button class="btn small" onclick="copySelectedLinks()">Linkleri Kopyala</button>
      <button class="btn small" onclick="openMoveModal()">Taşı</button>
      <button class="btn small danger" onclick="deleteSelected()">Sil</button>
      <button class="btn small" onclick="clearSelection()">Vazgeç</button>
    </div>
  </div>
</div>

<div id="ctxmenu"></div>
<div id="toasts"></div>
<div id="uploads"></div>

<!-- Bucket ekleme modal -->
<div class="modal-back" id="bucketModalBack">
  <div class="modal">
    <h3>Yeni Bucket Ekle</h3>
    <label>Anahtar (key, benzersiz)</label><input id="b_key" placeholder="örnek: bucket3">
    <label>Görünen İsim</label><input id="b_name" placeholder="Örnek Bucket">
    <label>Access Key ID</label><input id="b_ak">
    <label>Secret Access Key</label><input id="b_sk" type="password">
    <label>Bucket Adı</label><input id="b_bn">
    <label>Endpoint URL</label><input id="b_ep" placeholder="https://xxxx.r2.cloudflarestorage.com">
    <label>Region</label><input id="b_region" value="auto">
    <label>Public URL (opsiyonel)</label><input id="b_pub" placeholder="https://pub-xxx.r2.dev/">
    <div class="row-btns">
      <button class="btn" onclick="closeModal('bucketModalBack')">İptal</button>
      <button class="btn primary" onclick="submitAddBucket()">Ekle</button>
    </div>
  </div>
</div>

<!-- Yeni klasör modal -->
<div class="modal-back" id="folderModalBack">
  <div class="modal">
    <h3>Yeni Klasör</h3>
    <label>Klasör Adı</label><input id="folderName">
    <div class="row-btns">
      <button class="btn" onclick="closeModal('folderModalBack')">İptal</button>
      <button class="btn primary" onclick="submitNewFolder()">Oluştur</button>
    </div>
  </div>
</div>

<!-- Yeniden adlandırma modal -->
<div class="modal-back" id="renameModalBack">
  <div class="modal">
    <h3>Yeniden Adlandır</h3>
    <label>Yeni İsim</label><input id="renameInput">
    <div class="row-btns">
      <button class="btn" onclick="closeModal('renameModalBack')">İptal</button>
      <button class="btn primary" onclick="submitRename()">Kaydet</button>
    </div>
  </div>
</div>

<!-- Taşıma modal -->
<div class="modal-back" id="moveModalBack">
  <div class="modal">
    <h3>Taşı</h3>
    <label>Hedef Bucket</label><select id="moveBucket"></select>
    <label>Hedef Klasör (prefix)</label><input id="movePrefix" placeholder="örn: yedek/2026/">
    <div class="row-btns">
      <button class="btn" onclick="closeModal('moveModalBack')">İptal</button>
      <button class="btn primary" onclick="submitMove()">Taşı</button>
    </div>
  </div>
</div>

<!-- Link modal -->
<div class="modal-back" id="linkModalBack">
  <div class="modal">
    <h3 id="linkModalTitle">Link</h3>
    <label>Public Link</label>
    <div class="link-row"><input id="publicLinkInput" readonly><button class="btn small" onclick="copyInput('publicLinkInput')">Kopyala</button></div>
    <label>Signed Link Süresi</label>
    <select id="signExpiry">
      <option value="300">5 dakika</option>
      <option value="3600" selected>1 saat</option>
      <option value="86400">1 gün</option>
      <option value="604800">7 gün</option>
    </select>
    <div class="link-row"><input id="signedLinkInput" readonly placeholder="Oluştur'a basın"><button class="btn small" onclick="generateSignedLink()">Oluştur</button></div>
    <div class="row-btns">
      <button class="btn" onclick="closeModal('linkModalBack')">Kapat</button>
    </div>
  </div>
</div>

<!-- Yükleme onay modal: isim değiştir / hedef klasör / var olanları atla -->
<div class="modal-back" id="uploadConfirmBack">
  <div class="modal" style="width:min(480px,92vw);">
    <h3>Dosyaları Yükle</h3>
    <label>Hedef Klasör</label><input id="uploadTargetPrefix">
    <label>Dosyalar</label>
    <div class="upload-list" id="uploadList"></div>
    <div class="row-btns">
      <button class="btn" onclick="cancelUploadConfirm()">İptal</button>
      <button class="btn primary" onclick="confirmUpload()">Yükle</button>
    </div>
  </div>
</div>

<!-- Linkten yükleme modal -->
<div class="modal-back" id="urlModalBack">
  <div class="modal" style="width:min(480px,92vw);">
    <h3>Linkten Yükle</h3>
    <label>Hedef Klasör</label><input id="urlTargetPrefix">
    <label>Linkler (her satıra bir tane)</label>
    <textarea id="urlList" rows="6" placeholder="https://ornek.com/dosya.jpg"></textarea>
    <label>Dosya adı (opsiyonel, yalnızca tek link için)</label><input id="urlFilename">
    <div class="row-btns">
      <button class="btn" onclick="closeModal('urlModalBack')">İptal</button>
      <button class="btn primary" onclick="submitUrlUpload()">Yükle</button>
    </div>
  </div>
</div>

<!-- Önizleme modal -->
<div class="modal-back" id="previewModal">
  <div class="modal" style="width:min(900px,92vw);">
    <h3 id="previewTitle">Önizleme</h3>
    <div class="preview-wrap">
      <button class="preview-nav prev" onclick="navigatePreview(-1)" title="Önceki">
        <svg viewBox="0 0 24 24"><path d="M15 6l-6 6 6 6"/></svg>
      </button>
      <div class="previewBody" id="previewBody"></div>
      <button class="preview-nav next" onclick="navigatePreview(1)" title="Sonraki">
        <svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg>
      </button>
    </div>
    <div class="row-btns">
      <button class="btn small" onclick="copyPreviewPublicLink()">Kalıcı Link Kopyala</button>
      <button class="btn small" onclick="copyPreviewSignedLink()">Geçici Link Kopyala (24s)</button>
      <button class="btn small danger" onclick="deletePreviewFile()">Sil</button>
      <button class="btn" onclick="closePreviewModal()">Kapat</button>
    </div>
  </div>
</div>

<script>
// ---- Ince cizgili ikon seti (feather-benzeri) ----
const ICONS = {
  bucket: '<svg class="icon" viewBox="0 0 24 24"><path d="M4 6h16l-1.5 13a2 2 0 0 1-2 1.8H7.5a2 2 0 0 1-2-1.8L4 6z"/><path d="M4 6c0-1.5 3.6-2.5 8-2.5S20 4.5 20 6"/></svg>',
  plus: '<svg class="icon" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  folder: '<svg class="icon" viewBox="0 0 24 24" style="stroke:none;fill:currentColor"><path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4.4c.4 0 .77.16 1.06.44L11.6 7H19.5A1.5 1.5 0 0 1 21 8.5v9A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5v-11z"/></svg>',
  folderPlus: '<svg class="icon" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/><path d="M12 11v4M10 13h4"/></svg>',
  upload: '<svg class="icon" viewBox="0 0 24 24"><path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/></svg>',
  download: '<svg class="icon" viewBox="0 0 24 24"><path d="M12 4v12M7 11l5 5 5-5"/><path d="M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/></svg>',
  eye: '<svg class="icon" viewBox="0 0 24 24"><path d="M1.5 12S5 5 12 5s10.5 7 10.5 7-3.5 7-10.5 7S1.5 12 1.5 12z"/><circle cx="12" cy="12" r="3"/></svg>',
  link: '<svg class="icon" viewBox="0 0 24 24"><path d="M9 15l6-6"/><path d="M13 5.5l1.4-1.4a3.5 3.5 0 0 1 5 5L18 10.5"/><path d="M11 18.5L9.6 19.9a3.5 3.5 0 0 1-5-5L6 13.5"/></svg>',
  edit: '<svg class="icon" viewBox="0 0 24 24"><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>',
  move: '<svg class="icon" viewBox="0 0 24 24"><path d="M5 9l-3 3 3 3M19 9l3 3-3 3M9 5l3-3 3 3M9 19l3 3 3-3"/><path d="M2 12h20M12 2v20"/></svg>',
  trash: '<svg class="icon" viewBox="0 0 24 24"><path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2m3 0-1 13a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 7"/></svg>',
  dots: '<svg class="icon" viewBox="0 0 24 24" style="stroke-width:2.4"><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></svg>',
  image: '<svg class="icon" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>',
  video: '<svg class="icon" viewBox="0 0 24 24"><rect x="2" y="5" width="15" height="14" rx="2"/><path d="M22 8l-5 4 5 4V8z"/></svg>',
  pdf: '<svg class="icon" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>',
  archive: '<svg class="icon" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="17" rx="2"/><path d="M3 9h18M9 13h2"/></svg>',
  audio: '<svg class="icon" viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>',
  doc: '<svg class="icon" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h5"/></svg>',
  sheet: '<svg class="icon" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 3v18"/></svg>',
  file: '<svg class="icon" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>',
  home: '<svg class="icon" viewBox="0 0 24 24"><path d="M3 11l9-8 9 8"/><path d="M5 10v10a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V10"/></svg>',
  warn: '<svg class="icon" viewBox="0 0 24 24" style="width:12px;height:12px;color:#d9a441"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L1.8 18a1.5 1.5 0 0 0 1.3 2.2h17.8a1.5 1.5 0 0 0 1.3-2.2L13.7 3.9a1.5 1.5 0 0 0-2.6 0z"/></svg>',
  grid: '<svg class="icon" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
  list: '<svg class="icon" viewBox="0 0 24 24"><path d="M8 6h13M8 12h13M8 18h13"/><path d="M3 6h.01M3 12h.01M3 18h.01"/></svg>',
  check: '<svg viewBox="0 0 24 24" style="width:11px;height:11px" stroke="currentColor" stroke-width="3" fill="none" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>',
};

let state = {
  bucket: null, prefix: '', buckets: [], files: [], folders: [],
  selected: new Set(), ctxTargetKey: null, ctxTargetIsFolder: false,
  pendingUploads: [],
};

function showToast(msg, type='') {
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.textContent = msg;
  document.getElementById('toasts').appendChild(el);
  setTimeout(()=> el.remove(), 3500);
}

function closeModal(id){ document.getElementById(id).style.display='none'; }
function openModalEl(id){ document.getElementById(id).style.display='flex'; }

// Herhangi bir modalin arkaplanina (disina) tiklaninca kapat
document.querySelectorAll('.modal-back').forEach(back=>{
  back.addEventListener('mousedown', (e)=>{
    if (e.target !== back) return;
    if (back.id === 'previewModal') closePreviewModal();
    else back.style.display = 'none';
  });
});

async function api(url, opts) {
  const res = await fetch(url, opts);
  let data;
  try { data = await res.json(); } catch(e){ data = {}; }
  if (!res.ok || data.error) { showToast(data.error || 'Hata oluştu', 'error'); throw new Error(data.error||'error'); }
  return data;
}

async function loadBuckets() {
  const data = await api('/api/buckets');
  state.buckets = data.buckets;
  state.bucket = localStorage.getItem('s3m_bucket') || data.default;
  if (!state.buckets.find(b=>b.key===state.bucket)) state.bucket = data.default;
  const sel = document.getElementById('bucketSelect');
  sel.innerHTML = '';
  data.buckets.forEach(b=>{
    const o = document.createElement('option');
    o.value = b.key; o.textContent = b.name;
    sel.appendChild(o);
  });
  sel.value = state.bucket;
  sel.onchange = () => { state.bucket = sel.value; localStorage.setItem('s3m_bucket', state.bucket); state.prefix=''; refresh(); loadBucketStats(); };
  refreshMoveBucketOptions();
}

function refreshMoveBucketOptions() {
  const sel = document.getElementById('moveBucket');
  sel.innerHTML = '';
  state.buckets.forEach(b=>{
    const o = document.createElement('option'); o.value=b.key; o.textContent=b.name; sel.appendChild(o);
  });
}

function renderBreadcrumb() {
  const bc = document.getElementById('breadcrumb');
  bc.innerHTML = '';
  const rootSpan = document.createElement('span');
  rootSpan.innerHTML = ICONS.home + ' kök';
  rootSpan.onclick = () => { state.prefix=''; refresh(); };
  bc.appendChild(rootSpan);
  if (!state.prefix) return;
  const parts = state.prefix.split('/').filter(Boolean);
  let acc = '';
  parts.forEach(p=>{
    const sep = document.createElement('span'); sep.className='sep'; sep.textContent='/'; bc.appendChild(sep);
    acc += p + '/';
    const s = document.createElement('span'); s.textContent = p;
    const target = acc;
    s.onclick = () => { state.prefix = target; refresh(); };
    bc.appendChild(s);
  });
}

function extOf(name){ const i=name.lastIndexOf('.'); return i>=0?name.slice(i+1).toLowerCase():''; }
function isImage(ext){ return ['jpg','jpeg','png','gif','webp','svg','bmp','avif'].includes(ext); }
function isVideo(ext){ return ['mp4','webm','mov','mkv','avi'].includes(ext); }
function isPdf(ext){ return ext==='pdf'; }
function fmtSize(b){
  if (b<1024) return b+' B';
  if (b<1024*1024) return (b/1024).toFixed(1)+' KB';
  if (b<1024*1024*1024) return (b/1024/1024).toFixed(1)+' MB';
  return (b/1024/1024/1024).toFixed(2)+' GB';
}

state.bucketStatsCache = {};
async function loadBucketStats(force){
  const el = document.getElementById('bucketStats');
  const bucket = state.bucket;
  if (!force && state.bucketStatsCache[bucket]) {
    renderBucketStats(state.bucketStatsCache[bucket]);
    return;
  }
  el.textContent = 'Hesaplanıyor...';
  try {
    const data = await api(`/api/bucket-stats?bucket=${bucket}`);
    state.bucketStatsCache[bucket] = data;
    if (state.bucket === bucket) renderBucketStats(data);
  } catch (e) {
    if (state.bucket === bucket) el.textContent = '';
  }
}
function renderBucketStats(data){
  const el = document.getElementById('bucketStats');
  el.textContent = `${data.count.toLocaleString('tr-TR')} dosya · ${fmtSize(data.total_size)}${data.partial ? ' (kısmi)' : ''}`;
}
document.getElementById('bucketStats').onclick = () => loadBucketStats(true);
function wrapIcon(svg, color){
  return `<span style="color:${color};display:flex;">${svg}</span>`;
}
function iconFor(ext){
  if (isImage(ext)) return wrapIcon(ICONS.image, '#4fa3ff');
  if (isVideo(ext)) return wrapIcon(ICONS.video, '#f472b6');
  if (isPdf(ext)) return wrapIcon(ICONS.pdf, '#f87171');
  if (['zip','rar','7z','tar','gz'].includes(ext)) return wrapIcon(ICONS.archive, '#fb923c');
  if (['mp3','wav','flac','ogg'].includes(ext)) return wrapIcon(ICONS.audio, '#2dd4bf');
  if (['doc','docx'].includes(ext)) return wrapIcon(ICONS.doc, '#818cf8');
  if (['xls','xlsx','csv'].includes(ext)) return wrapIcon(ICONS.sheet, '#4ade80');
  return wrapIcon(ICONS.file, 'var(--muted)');
}

async function refresh() {
  renderBreadcrumb();
  const data = await api(`/api/list?bucket=${state.bucket}&prefix=${encodeURIComponent(state.prefix)}`);
  state.folders = data.folders; state.files = data.files;
  state.nextToken = data.next_token || null;
  state.selected.clear();
  renderGrid();
  updateSelectionBar();
  updateLoadMoreBtn();
}

async function loadMore(){
  if (!state.nextToken || state.loadingMore) return;
  state.loadingMore = true;
  const btn = document.getElementById('loadMoreBtn');
  const oldLabel = btn.textContent;
  btn.textContent = 'Yükleniyor...'; btn.disabled = true;
  try {
    const data = await api(`/api/list?bucket=${state.bucket}&prefix=${encodeURIComponent(state.prefix)}&token=${encodeURIComponent(state.nextToken)}`);
    state.folders = state.folders.concat(data.folders);
    state.files = state.files.concat(data.files);
    state.nextToken = data.next_token || null;
    renderGrid(document.getElementById('searchBox').value);
  } finally {
    state.loadingMore = false;
    btn.textContent = oldLabel; btn.disabled = false;
    updateLoadMoreBtn();
  }
}

function updateLoadMoreBtn(){
  document.getElementById('loadMoreWrap').style.display = state.nextToken ? 'flex' : 'none';
}

function sortFiles(files){
  const mode = state.sortBy || 'name_asc';
  const sorted = files.slice();
  sorted.sort((a, b) => {
    switch (mode) {
      case 'name_desc': return b.name.localeCompare(a.name, 'tr');
      case 'size_asc': return a.size - b.size;
      case 'size_desc': return b.size - a.size;
      case 'date_asc': return a.last_modified.localeCompare(b.last_modified);
      case 'date_desc': return b.last_modified.localeCompare(a.last_modified);
      case 'name_asc':
      default: return a.name.localeCompare(b.name, 'tr');
    }
  });
  return sorted;
}

function renderGrid(filterText='') {
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  const ft = filterText.toLowerCase();

  const foldersSorted = state.folders.slice().sort((a,b)=>a.name.localeCompare(b.name,'tr'));
  foldersSorted.filter(f=>f.name.toLowerCase().includes(ft)).forEach(f=>{
    const div = document.createElement('div');
    div.className = 'item';
    div.innerHTML = `<div class="thumb" style="color:#eab308">${ICONS.folder}</div><div class="name">${f.name}</div><div class="meta">klasör</div>
      <button class="menu-btn" onclick="event.stopPropagation();openCtx(event,'${f.prefix}',true)">${ICONS.dots}</button>`;
    div.ondblclick = () => { state.prefix = f.prefix; refresh(); };
    div.oncontextmenu = (e)=>{ e.preventDefault(); openCtx(e, f.prefix, true); };
    grid.appendChild(div);
  });

  const filesSorted = sortFiles(state.files).filter(f=>f.name.toLowerCase().includes(ft));
  filesSorted.forEach(f=>{
    const ext = extOf(f.name);
    const div = document.createElement('div');
    div.className = 'item' + (state.selected.has(f.key)?' selected':'');
    const extBadge = ext ? `<span class="ext-badge">${ext.slice(0,4).toUpperCase()}</span>` : '';
    div.innerHTML = `<button class="select-btn" onclick="event.stopPropagation();toggleSelect('${f.key}', this.closest('.item'))">${ICONS.check}</button>
      <div class="thumb">${iconFor(ext)}${extBadge}</div><div class="name">${f.name}</div>
      <div class="meta">${fmtSize(f.size)} · ${f.last_modified}</div>
      <button class="menu-btn" onclick="event.stopPropagation();openCtx(event,'${f.key}',false)">${ICONS.dots}</button>`;
    div.onclick = (e) => {
      if (e.ctrlKey || e.metaKey || e.shiftKey) { toggleSelect(f.key, div); }
      else { openPreview(f); }
    };
    div.oncontextmenu = (e)=>{ e.preventDefault(); openCtx(e, f.key, false); };
    grid.appendChild(div);
  });
}

function toggleSelect(key, div) {
  if (state.selected.has(key)) { state.selected.delete(key); div.classList.remove('selected'); }
  else { state.selected.add(key); div.classList.add('selected'); }
  updateSelectionBar();
}
function clearSelection(){ state.selected.clear(); renderGrid(document.getElementById('searchBox').value); updateSelectionBar(); }
function updateSelectionBar(){
  const bar = document.getElementById('selectionBar');
  if (state.selected.size>0) { bar.style.display='flex'; document.getElementById('selCount').textContent = state.selected.size+' seçildi'; }
  else bar.style.display='none';
}

async function deleteSelected(){
  if (!confirm(`${state.selected.size} öge silinsin mi?`)) return;
  await api('/api/delete', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({bucket: state.bucket, keys:[...state.selected]})});
  showToast('Silindi', 'success');
  refresh();
}
function copySelectedLinks(){
  const links = state.files.filter(f=>state.selected.has(f.key) && f.public_url).map(f=>f.public_url);
  navigator.clipboard.writeText(links.join('\n'));
  showToast('Linkler kopyalandı', 'success');
}
function openMoveModal(){
  refreshMoveBucketOptions();
  document.getElementById('movePrefix').value = '';
  openModalEl('moveModalBack');
}
async function submitMove(){
  const dstBucket = document.getElementById('moveBucket').value;
  const dstPrefix = document.getElementById('movePrefix').value;
  const keys = state.ctxTargetKey ? [state.ctxTargetKey] : [...state.selected];
  for (const k of keys) {
    await api('/api/move', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({src_bucket: state.bucket, dst_bucket: dstBucket, src_key: k, dst_prefix: dstPrefix})});
  }
  showToast('Taşındı', 'success');
  closeModal('moveModalBack');
  state.ctxTargetKey = null;
  refresh();
}

// ---- Context menu ----
function openCtx(e, key, isFolder) {
  e.preventDefault();
  state.ctxTargetKey = key; state.ctxTargetIsFolder = isFolder;
  const menu = document.getElementById('ctxmenu');
  let html = '';
  if (!isFolder) {
    html += `<button onclick="ctxPreview()">${ICONS.eye} Önizle</button>`;
    html += `<button onclick="ctxLinks()">${ICONS.link} Link Oluştur</button>`;
    html += `<button onclick="ctxDownload()">${ICONS.download} İndir</button>`;
  }
  html += `<button onclick="ctxRename()">${ICONS.edit} Yeniden Adlandır</button>`;
  html += `<button onclick="ctxMove()">${ICONS.move} Taşı</button>`;
  html += `<hr>`;
  html += `<button class="danger" onclick="ctxDelete()">${ICONS.trash} Sil</button>`;
  menu.innerHTML = html;
  menu.style.display = 'block';
  const x = Math.min(e.clientX, window.innerWidth-190);
  const y = Math.min(e.clientY, window.innerHeight-200);
  menu.style.left = x+'px'; menu.style.top = y+'px';
}
document.addEventListener('click', ()=>{ document.getElementById('ctxmenu').style.display='none'; });

function ctxPreview(){ const f = state.files.find(f=>f.key===state.ctxTargetKey); if (f) openPreview(f); }
function ctxDownload(){ generateSignedUrlAndGo(state.ctxTargetKey); }
async function generateSignedUrlAndGo(key){
  const data = await api(`/api/presign?bucket=${state.bucket}&key=${encodeURIComponent(key)}&expires=3600`);
  window.open(data.url, '_blank');
}
function ctxLinks(){
  document.getElementById('linkModalTitle').textContent = state.ctxTargetKey.split('/').pop();
  document.getElementById('signedLinkInput').value = '';
  api(`/api/public-url?bucket=${state.bucket}&key=${encodeURIComponent(state.ctxTargetKey)}`)
    .then(d=> document.getElementById('publicLinkInput').value = d.url)
    .catch(()=> document.getElementById('publicLinkInput').value = '(public url tanımlı değil)');
  openModalEl('linkModalBack');
}
async function generateSignedLink(){
  const expires = document.getElementById('signExpiry').value;
  const data = await api(`/api/presign?bucket=${state.bucket}&key=${encodeURIComponent(state.ctxTargetKey)}&expires=${expires}`);
  document.getElementById('signedLinkInput').value = data.url;
}
function copyInput(id){ const el = document.getElementById(id); el.select(); navigator.clipboard.writeText(el.value); showToast('Kopyalandı','success'); }

function ctxRename(){
  const current = state.ctxTargetIsFolder ? state.ctxTargetKey.replace(/\/$/,'').split('/').pop() : state.ctxTargetKey.split('/').pop();
  document.getElementById('renameInput').value = current;
  openModalEl('renameModalBack');
}
async function submitRename(){
  const newName = document.getElementById('renameInput').value.trim();
  if (!newName) return;
  await api('/api/rename', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({bucket: state.bucket, key: state.ctxTargetKey, new_name: newName})});
  showToast('Yeniden adlandırıldı', 'success');
  closeModal('renameModalBack');
  refresh();
}
function ctxMove(){ openMoveModal(); }
async function ctxDelete(){
  if (!confirm('Silinsin mi?')) return;
  await api('/api/delete', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({bucket: state.bucket, keys:[state.ctxTargetKey]})});
  showToast('Silindi', 'success');
  refresh();
}

// ---- Onizleme ----
function getVisibleFiles(){
  const ft = document.getElementById('searchBox').value.toLowerCase();
  return state.files.filter(f=>f.name.toLowerCase().includes(ft));
}

function openPreview(f){
  const visible = getVisibleFiles();
  state.previewIndex = visible.findIndex(x=>x.key===f.key);
  renderPreview(f);
  openModalEl('previewModal');
}

function navigatePreview(delta){
  const visible = getVisibleFiles();
  if (!visible.length) return;
  if (state.previewIndex == null) state.previewIndex = 0;
  state.previewIndex = (state.previewIndex + delta + visible.length) % visible.length;
  renderPreview(visible[state.previewIndex]);
}

async function renderPreview(f){
  state.previewKey = f.key;
  document.getElementById('previewTitle').textContent = f.name;
  const body = document.getElementById('previewBody');
  body.innerHTML = 'Yükleniyor...';
  const data = await api(`/api/presign?bucket=${state.bucket}&key=${encodeURIComponent(f.key)}&expires=600`);
  const ext = extOf(f.name);
  if (isImage(ext)) body.innerHTML = `<img src="${data.url}">`;
  else if (isVideo(ext)) body.innerHTML = `<video src="${data.url}" controls autoplay></video>`;
  else if (isPdf(ext)) body.innerHTML = `<iframe src="${data.url}"></iframe>`;
  else if (['txt','md','json','log','py','js','css','html','csv'].includes(ext)) {
    const r = await fetch(data.url); const txt = await r.text();
    body.innerHTML = `<pre style="color:#eee;text-align:left;padding:16px;white-space:pre-wrap;max-height:65vh;overflow:auto;">${txt.replace(/</g,'&lt;').slice(0,20000)}</pre>`;
  } else {
    body.innerHTML = `<div style="color:#eee;padding:30px;">Bu dosya türü için önizleme yok. <a href="${data.url}" target="_blank" style="color:var(--accent);">İndirmek için tıkla</a></div>`;
  }
}

document.addEventListener('keydown', (e)=>{
  const modal = document.getElementById('previewModal');
  if (modal.style.display !== 'flex') return;
  if (e.key === 'ArrowLeft') navigatePreview(-1);
  else if (e.key === 'ArrowRight') navigatePreview(1);
  else if (e.key === 'Escape') closePreviewModal();
});


function closePreviewModal(){
  const body = document.getElementById('previewBody');
  const vid = body.querySelector('video');
  if (vid) { vid.pause(); vid.removeAttribute('src'); vid.load(); }
  body.innerHTML = '';
  document.getElementById('previewModal').style.display = 'none';
}

async function copyPreviewPublicLink(){
  if (!state.previewKey) return;
  try {
    const d = await api(`/api/public-url?bucket=${state.bucket}&key=${encodeURIComponent(state.previewKey)}`);
    navigator.clipboard.writeText(d.url);
    showToast('Kalıcı link kopyalandı', 'success');
  } catch(e) {}
}

async function copyPreviewSignedLink(){
  if (!state.previewKey) return;
  const d = await api(`/api/presign?bucket=${state.bucket}&key=${encodeURIComponent(state.previewKey)}&expires=86400`);
  navigator.clipboard.writeText(d.url);
  showToast('Geçici link kopyalandı (24 saat)', 'success');
}

async function deletePreviewFile(){
  if (!state.previewKey) return;
  if (!confirm('Bu dosya silinsin mi?')) return;
  const key = state.previewKey;
  await api('/api/delete', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({bucket: state.bucket, keys:[key]})});
  showToast('Silindi', 'success');
  closePreviewModal();
  refresh();
}

// ---- Yeni klasor ----
document.getElementById('newFolderBtn').onclick = () => { document.getElementById('folderName').value=''; openModalEl('folderModalBack'); };
async function submitNewFolder(){
  const name = document.getElementById('folderName').value.trim();
  if (!name) return;
  await api('/api/mkdir', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({bucket: state.bucket, prefix: state.prefix, name})});
  showToast('Klasör oluşturuldu', 'success');
  closeModal('folderModalBack');
  refresh();
}

// ---- Bucket ekleme ----
document.getElementById('addBucketBtn').onclick = () => openModalEl('bucketModalBack');
async function submitAddBucket(){
  const payload = {
    key: document.getElementById('b_key').value.trim(),
    name: document.getElementById('b_name').value.trim(),
    access_key_id: document.getElementById('b_ak').value.trim(),
    secret_access_key: document.getElementById('b_sk').value.trim(),
    bucket_name: document.getElementById('b_bn').value.trim(),
    endpoint_url: document.getElementById('b_ep').value.trim(),
    region: document.getElementById('b_region').value.trim(),
    public_url: document.getElementById('b_pub').value.trim(),
  };
  await api('/api/buckets', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  showToast('Bucket eklendi', 'success');
  closeModal('bucketModalBack');
  await loadBuckets();
}

// ---- Yukleme: buton / drag&drop / ctrl+v -> once onay dialogu ----
document.getElementById('uploadBtn').onclick = () => document.getElementById('fileInput').click();
document.getElementById('fileInput').onchange = (e) => { stageFiles(e.target.files); e.target.value=''; };

const dz = document.getElementById('dropzone');
['dragenter','dragover'].forEach(ev=> dz.addEventListener(ev, e=>{ e.preventDefault(); dz.classList.add('dragover'); }));
['dragleave','drop'].forEach(ev=> dz.addEventListener(ev, e=>{ e.preventDefault(); dz.classList.remove('dragover'); }));
dz.addEventListener('drop', e=>{ if (e.dataTransfer.files.length) stageFiles(e.dataTransfer.files); });

document.addEventListener('paste', (e)=>{
  const items = e.clipboardData.files;
  if (items && items.length) stageFiles(items);
});

function stageFiles(fileList){
  const files = [...fileList];
  if (!files.length) return;
  state.pendingUploads = files.map((file, i) => {
    const name = file.name || `yapistirilan_${Date.now()}_${i}.png`;
    return { file, name, id: 'pu_'+i+'_'+Math.random().toString(36).slice(2) };
  });
  document.getElementById('uploadTargetPrefix').value = state.prefix;
  renderUploadConfirmList();
  openModalEl('uploadConfirmBack');
}

function existingNames(){
  return new Set(state.files.map(f=>f.name));
}

function renderUploadConfirmList(){
  const list = document.getElementById('uploadList');
  const targetPrefix = document.getElementById('uploadTargetPrefix').value;
  const sameFolder = targetPrefix === state.prefix;
  const existing = sameFolder ? existingNames() : new Set();
  list.innerHTML = '';
  state.pendingUploads.forEach(pu => {
    const row = document.createElement('div');
    row.className = 'upload-row';
    const dup = existing.has(pu.name);
    row.innerHTML = `${iconFor(extOf(pu.name))}
      <input type="text" value="${pu.name}" data-id="${pu.id}">
      <span class="u-status ${dup?'dup':''}" data-status-for="${pu.id}">${dup? ICONS.warn+' zaten var, atlanacak' : fmtSize(pu.file.size)}</span>`;
    const inp = row.querySelector('input');
    inp.oninput = () => {
      pu.name = inp.value.trim();
      const nowDup = existing.has(pu.name);
      row.querySelector('.u-status').className = 'u-status ' + (nowDup?'dup':'');
      row.querySelector('.u-status').innerHTML = nowDup ? ICONS.warn+' zaten var, atlanacak' : fmtSize(pu.file.size);
    };
    list.appendChild(row);
  });
}
document.getElementById('uploadTargetPrefix').addEventListener('input', renderUploadConfirmList);

function cancelUploadConfirm(){
  state.pendingUploads = [];
  closeModal('uploadConfirmBack');
}

function confirmUpload(){
  const targetPrefix = document.getElementById('uploadTargetPrefix').value;
  const sameFolder = targetPrefix === state.prefix;
  const existing = sameFolder ? existingNames() : new Set();
  const toUpload = state.pendingUploads.filter(pu => pu.name && !existing.has(pu.name));
  const skipped = state.pendingUploads.length - toUpload.length;
  closeModal('uploadConfirmBack');
  toUpload.forEach(pu => queueUpload(pu.file, pu.name, targetPrefix));
  if (skipped > 0) showToast(`${skipped} dosya zaten var oldugu icin atlandi`, '');
  state.pendingUploads = [];
}

// ---- Yukleme kuyrugu: ayni anda en fazla N dosya, cok sayida/buyuk dosyada
// tarayiciyi/agi tikamamak icin. Her dosyanin kendi ilerleme cubugu var. ----
const MAX_CONCURRENT_UPLOADS = 3;
state.uploadQueue = [];
state.activeUploads = 0;
state.uploadsPendingRefresh = false;

function queueUpload(file, name, prefix){
  state.uploadQueue.push({file, name, prefix});
  processUploadQueue();
}

function processUploadQueue(){
  while (state.activeUploads < MAX_CONCURRENT_UPLOADS && state.uploadQueue.length > 0) {
    const item = state.uploadQueue.shift();
    state.activeUploads++;
    const done = () => {
      state.activeUploads--;
      processUploadQueue();
      if (state.activeUploads === 0 && state.uploadQueue.length === 0 && state.uploadsPendingRefresh) {
        state.uploadsPendingRefresh = false;
        refresh();
        loadBucketStats(true);
      }
    };
    if (item.url) uploadFromUrl(item.url, item.name, item.prefix, done);
    else uploadOne(item.file, item.name, item.prefix, done);
  }
}

function uploadOne(file, name, prefix, onDone){
  const id = 'up_' + Math.random().toString(36).slice(2);
  const box = document.createElement('div');
  box.className = 'up-item';
  box.id = id;
  box.innerHTML = `<div>${name}</div><div class="bar"><div></div></div><div class="u-pct">${fmtSize(file.size)}</div>`;
  document.getElementById('uploads').appendChild(box);

  const fd = new FormData();
  fd.append('bucket', state.bucket);
  fd.append('prefix', prefix);
  fd.append('filename', name);
  fd.append('file', file);

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/upload');
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const pct = Math.round((e.loaded/e.total)*100);
      box.querySelector('.bar > div').style.width = pct + '%';
      box.querySelector('.u-pct').textContent = pct + '%';
    }
  };
  xhr.onload = () => {
    box.remove();
    if (xhr.status >= 200 && xhr.status < 300) {
      showToast(name + ' yüklendi', 'success');
      if (prefix === state.prefix) state.uploadsPendingRefresh = true;
    } else {
      showToast(name + ' yüklenemedi', 'error');
    }
    onDone();
  };
  xhr.onerror = () => { box.remove(); showToast(name + ' yüklenemedi', 'error'); onDone(); };
  xhr.send(fd);
}

// ---- Linkten yukleme: sunucu dosyayi indirip dogrudan bucket'a gonderir ----
document.getElementById('urlBtn').onclick = () => {
  document.getElementById('urlTargetPrefix').value = state.prefix;
  document.getElementById('urlList').value = '';
  document.getElementById('urlFilename').value = '';
  openModalEl('urlModalBack');
};

function submitUrlUpload(){
  const prefix = document.getElementById('urlTargetPrefix').value;
  const urls = document.getElementById('urlList').value.split('\n').map(s=>s.trim()).filter(Boolean);
  if (!urls.length) return;
  const custom = urls.length === 1 ? document.getElementById('urlFilename').value.trim() : '';
  closeModal('urlModalBack');
  urls.forEach(u => state.uploadQueue.push({url: u, name: custom, prefix}));
  processUploadQueue();
}

async function uploadFromUrl(url, customName, prefix, onDone){
  let label = customName;
  if (!label) {
    try { label = decodeURIComponent(url.split('?')[0].split('/').pop()); } catch(e) { label = ''; }
  }
  label = label || url;
  const box = document.createElement('div');
  box.className = 'up-item';
  box.innerHTML = `<div></div><div class="u-pct">Linkten indiriliyor...</div>`;
  box.firstChild.textContent = label;
  document.getElementById('uploads').appendChild(box);
  try {
    const res = await fetch('/api/upload-url', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({bucket: state.bucket, prefix, url, filename: customName})});
    const data = await res.json().catch(()=>({}));
    if (!res.ok || data.error) throw new Error(data.error || 'hata');
    showToast(data.key.split('/').pop() + ' yüklendi', 'success');
    if (prefix === state.prefix) state.uploadsPendingRefresh = true;
  } catch (e) {
    showToast(label + ': ' + e.message, 'error');
  } finally {
    box.remove();
    onDone();
  }
}

// ---- Arama ----
document.getElementById('searchBox').oninput = (e) => renderGrid(e.target.value);

// ---- Sıralama ----
state.sortBy = localStorage.getItem('s3m_sort') || 'name_asc';
document.getElementById('sortSelect').value = state.sortBy;
document.getElementById('sortSelect').onchange = (e) => {
  state.sortBy = e.target.value;
  localStorage.setItem('s3m_sort', state.sortBy);
  renderGrid(document.getElementById('searchBox').value);
};

// ---- Görünüm (ızgara / liste) ----
state.viewMode = localStorage.getItem('s3m_view') || 'grid';
function applyViewMode(){
  document.getElementById('grid').className = state.viewMode === 'list' ? 'list-view' : '';
  document.getElementById('viewToggleBtn').innerHTML = state.viewMode === 'list' ? ICONS.grid : ICONS.list;
}
document.getElementById('viewToggleBtn').onclick = () => {
  state.viewMode = state.viewMode === 'list' ? 'grid' : 'list';
  localStorage.setItem('s3m_view', state.viewMode);
  applyViewMode();
};

// ---- Init ----
(async function init(){
  document.querySelector('header h1').innerHTML = ICONS.bucket + ' S3 Manager';
  document.getElementById('addBucketBtn').innerHTML = ICONS.plus + ' Bucket';
  document.getElementById('newFolderBtn').innerHTML = ICONS.folderPlus + ' Klasör';
  document.getElementById('uploadBtn').innerHTML = ICONS.upload + ' Yükle';
  document.getElementById('urlBtn').innerHTML = ICONS.link + ' Link';
  applyViewMode();
  await loadBuckets();
  await refresh();
  loadBucketStats();
})();
</script>
</body>
</html>
"""


@app.route('/')
def index():
    return render_template_string(INDEX_HTML)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)