"""Deterministic full-file transport: truth whitelist + in-window task packages only."""
import json
from queue_model import require, sha, line, unique_object
from queue_v2 import CONFIG, WINDOW, config, projections

PACK_FORMAT = 'devtemplate-state/1'


def load_config(store):
    from task_queue import safe
    p = safe(store.root, CONFIG)
    require(p.is_file(), '缺少 charter/config.json；先从模板取得配置，再显式升级。', 'config')
    return config(json.loads(p.read_bytes().decode('utf-8'), object_pairs_hook=unique_object))


def edit_token(store, raw):
    from task_queue import safe
    p = safe(store.root, CONFIG)
    return sha(raw + b'\0' + (p.read_bytes() if p.is_file() else b''))


def collect(store, raw, events, tasks):
    from task_queue import safe
    require(tasks.protocol == 2, '旧账须先 upgrade --preview 并显式升级。', 'upgrade_required')
    store.verify_files(tasks)
    sources = {}
    for name in tasks.config['truth_whitelist']:
        path = safe(store.root, name)
        require(path.is_file(), f'白名单文件缺失或不是普通文件：{name}', 'state_source')
        sources[name] = path.read_bytes()
    managed = projections(tasks)
    for ident, task in tasks.items():
        if task['status'] in WINDOW:
            prefix = f'queue/tasks/{ident}/'
            sources.update({p: data for p, data in managed.items() if p.startswith(prefix)})
    for name, data in sources.items():
        try:
            value = data.decode('utf-8')
        except UnicodeError:
            require(False, f'状态源不是 UTF-8 文本：{name}', 'state_source')
        require('\x00' not in value, f'状态源含二进制内容：{name}', 'state_source')
    metadata = {'format': PACK_FORMAT, 'seq': len(events), 'ledger_sha256': sha(raw),
                'config_sha256': sha(safe(store.root, CONFIG).read_bytes()),
                'sources': [{'path': store.gpath(n), 'sha256': sha(v), 'bytes': len(v)} for n, v in sorted(sources.items())]}
    return sha(line(metadata).encode()), metadata, sources


def snapshot(store, raw, events, tasks):
    token, metadata, sources = collect(store, raw, events, tasks)
    # Queue writers share a lock. Plain truth-file writers do not: detect changes, don't mix readings.
    again = collect(store, raw, events, tasks)
    require(token == again[0] and store.ledger.read_bytes() == raw,
            '装配期间来源发生变化，请重新获取状态。', 'conflict')
    return token, metadata, sources


def cached(store, token, metadata, sources):
    from task_queue import safe
    home = f'.shell/local/state/{token}'
    manifest = safe(store.root, home + '/manifest.json')
    require(manifest.is_file(), '此状态包尚未生成或缓存缺失；先 state get。', 'state_cache')
    doc = json.loads(manifest.read_bytes().decode('utf-8'), object_pairs_hook=unique_object)
    require(set(doc) == {'context','metadata','files'} and doc['context'] == token and doc['metadata'] == metadata,
            '状态包清单被改写；重新 state get 重建。', 'state_cache')
    require(isinstance(doc['files'], list) and len(doc['files']) == len(sources), '状态包文件清单不完整。', 'state_cache')
    for index, (name, data) in enumerate(sorted(sources.items())):
        row = doc['files'][index]
        require(set(row) == {'source','parts'} and row['source'] == store.gpath(name) and
                isinstance(row['parts'], list) and row['parts'], '状态包文件项不合法。', 'state_cache')
        pieces = []
        for part, rel in enumerate(row['parts']):
            expected = f'{home}/content/{index:04d}-{part:04d}.txt'
            require(rel == store.gpath(expected), '状态包分片路径不合法。', 'state_cache')
            p = safe(store.root, expected)
            require(p.is_file(), '状态包分片缺失。', 'state_cache')
            pieces.append(p.read_bytes())
        require(b''.join(pieces) == data, '状态包正文缺失或被修改；重新 state get。', 'state_cache')
    return doc


def get(store, raw, events, tasks, chunk_chars=24000):
    from task_queue import safe
    require(type(chunk_chars) is int and chunk_chars > 0, '分片字符上限必须为正整数。', 'input')
    token, meta, sources = snapshot(store, raw, events, tasks)
    source_token = token
    token = f'{token}-{chunk_chars}'
    home = f'.shell/local/state/{token}'
    doc = {'context': token, 'metadata': meta, 'files': []}
    for i, (name, data) in enumerate(sorted(sources.items())):
        value = data.decode('utf-8')
        pieces = [value[n:n+chunk_chars] for n in range(0, len(value), chunk_chars)] or ['']
        parts = []
        for j, piece in enumerate(pieces):
            rel = f'{home}/content/{i:04d}-{j:04d}.txt'
            store.atomic_write(safe(store.root, rel), piece.encode('utf-8'))
            parts.append(store.gpath(rel))
        doc['files'].append({'source': store.gpath(name), 'parts': parts})
    manifest = home + '/manifest.json'
    store.atomic_write(safe(store.root, manifest), (json.dumps(doc, ensure_ascii=False, indent=2)+'\n').encode())
    require(snapshot(store, raw, events, tasks)[0] == source_token, '送达前来源已变，重新获取。', 'conflict')
    cached(store, token, meta, sources)
    return {'ok': True, 'context': token, 'seq': len(events), 'base': str(store.repo),
            'manifest': store.gpath(manifest), 'files': doc['files'],
            'note': '正文仅为 truth 白名单与窗口任务包原文；按顺序读完各源的所有分片。版本校验不证明模型已经理解。'}


def check(store, raw, events, tasks, context):
    require(context, '此写入须携带 state get 返回的 --context；请先读取状态包。', 'state_required')
    token, meta, sources = snapshot(store, raw, events, tasks)
    source_token, sep, chunk_size = context.partition('-')
    require(sep and chunk_size.isdecimal() and int(chunk_size) > 0, '状态版本格式错误。', 'state_cache')
    require(source_token == token, '状态包已经过期；重新 state get 并读取变化，不沿用旧上下文。', 'stale_context')
    cached(store, context, meta, sources)
    return {'ok': True, 'context': context, 'seq': len(events)}
