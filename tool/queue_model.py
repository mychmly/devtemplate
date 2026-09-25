"""Queue format v1: deterministic validation, replay and Markdown rendering.

No filesystem writes, clocks, locks or external services in this module.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime

FORMAT = 'devtemplate-queue/1'
HEADER = {'format': FORMAT}
PROJECTION_VERSION = 2
TERMINAL = {'收官', '驳回'}
PROPOSAL_KEYS = {'title', 'origin', 'scope', 'criteria', 'plan'}
SECTIONS = {'来源与目的': 'origin', '范围': 'scope', '验收标准与验证方法': 'criteria', '执行计划': 'plan'}
LABELS = {'create': '登记', 'revise': '修订提案', 'approve': '批准执行', 'claim': '领取',
          'release': '让出', 'handoff': '交接', 'replan': '修订计划', 'deliver': '交付',
          'close': '验收通过', 'revoke': '撤销', 'cancel': '取消', 'import': '导入候裁任务'}


class QueueError(Exception):
    def __init__(self, message, code='rejected'):
        super().__init__(message)
        self.code = code


def require(condition, message, code='rejected'):
    if not condition:
        raise QueueError(message, code)


def text(value, name, limit=1024 * 1024):
    require(isinstance(value, str) and value.strip() and '\x00' not in value,
            f'{name} 必须为非空文本。')
    require(len(value.encode('utf-8')) <= limit, f'{name} 过长，上限 {limit} 字节。')
    return value


def exact(value, keys, name):
    require(isinstance(value, dict) and set(value) == set(keys), f'{name} 字段不合格式。', 'format')


def task_id(value):
    require(isinstance(value, str) and re.fullmatch(r'T[0-9]{4,}', value), '任务号格式错误。')
    n = int(value[1:])
    require(n > 0 and value == f'T{n:04d}', '任务号须为规范的正整数编号。')
    return value


def proposal(value):
    exact(value, PROPOSAL_KEYS, '方案')
    for key, item in value.items():
        text(item, key)
        require('待填写' not in item, f'{key} 仍含“待填写”，先完成方案。')
    require('\n' not in value['title'], '题名只能有一行。')
    return value


def parse_proposal(markdown):
    text(markdown, '方案文件')
    # Import v0 Markdown headers only as source text; live proposals have no state fields.
    body = re.sub(r'\A---\n.*?\n---\n', '', markdown, count=1, flags=re.S)
    titles = re.findall(r'^# (.+)$', body, re.M)
    require(len(titles) == 1, '方案须恰有一个一级标题。')
    result = {'title': titles[0].strip()}
    for heading, key in SECTIONS.items():
        matches = list(re.finditer(rf'^## {re.escape(heading)}\s*$', body, re.M))
        require(len(matches) == 1, f'方案须恰有一个“{heading}”节。')
        rest = body[matches[0].end():]
        result[key] = re.split(r'^## ', rest, maxsplit=1, flags=re.M)[0].strip()
    return proposal(result)


def authority(value):
    exact(value, {'by', 'basis'}, '授权依据')
    text(value['by'], '授权人', 200)
    text(value['basis'], '用户指令依据')
    return value


def relative_path(value):
    text(value, '文件路径', 4096)
    require(not value.startswith('/') and '\\' not in value and
            all(p not in ('', '.', '..') for p in value.split('/')) and ':' not in value,
            '文件路径须为规范的 Git 仓库相对路径，不得越界。')
    return value


def sha(data):
    return hashlib.sha256(data).hexdigest()


def line(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n'


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f'JSON 字段重复：{key}。', 'format')
        result[key] = value
    return result


def decode(raw):
    try:
        require(raw.endswith(b'\n'), '账本末行不完整；保留原件，不猜测补齐。', 'corrupt')
        records = [json.loads(s, object_pairs_hook=unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite')))
                   for s in raw.decode('utf-8').splitlines()]
        require(records and records[0] == HEADER, '账本格式版本不受支持。', 'format')
        require(raw == ''.join(line(r) for r in records).encode(), '账本不是规范序列化，拒绝继续写入。', 'corrupt')
        replay(records[1:])
        return records[1:]
    except (UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise QueueError(f'账本不可解析：{exc}。请保留原件并核对备份或 Git。', 'corrupt') from exc


def encode(events):
    return (line(HEADER) + ''.join(line(e) for e in events)).encode('utf-8')


def parents(tasks, ident):
    result, seen = [], {ident}
    parent = tasks[ident]['parent']
    while parent:
        require(parent in tasks, f'{ident} 的父任务 {parent} 不存在。', 'graph')
        require(parent not in seen, '父子关系成环。', 'graph')
        seen.add(parent)
        result.append(parent)
        parent = tasks[parent]['parent']
    return result


def children(tasks, ident, recursive=False):
    result = {key for key, item in tasks.items() if item['parent'] == ident}
    if recursive:
        pending = list(result)
        while pending:
            current = pending.pop()
            found = {key for key, item in tasks.items() if item['parent'] == current} - result
            result.update(found)
            pending.extend(found)
    return result


def dependencies(tasks, ident):
    result = set(tasks[ident]['deps'])
    for parent in parents(tasks, ident):
        result.update(tasks[parent]['deps'])
    return result


def validate_graph(tasks):
    for ident, item in tasks.items():
        ancestors = parents(tasks, ident)
        require(isinstance(item['deps'], list) and len(item['deps']) == len(set(item['deps'])),
                f'{ident} 的依赖不是无重复列表。', 'graph')
        for dep in item['deps']:
            require(dep in tasks and dep != ident, f'{ident} 的依赖 {dep} 不存在或指向自己。', 'graph')
            require(dep not in ancestors and ident not in parents(tasks, dep), '祖先与后代之间不能另设依赖。', 'graph')
    waits = {ident: dependencies(tasks, ident) | children(tasks, ident) for ident in tasks}
    # Iterative topological elimination avoids recursion limits on long task chains.
    resolved = set()
    while len(resolved) < len(tasks):
        ready = {ident for ident, deps in waits.items() if ident not in resolved and deps <= resolved}
        require(ready, '父等子与继承依赖合并后成环，任务未写入。', 'graph')
        resolved.update(ready)


def blockers(tasks, ident):
    item = tasks[ident]
    result = []
    if item['status'] != '批准执行':
        result.append('任务未处于批准执行状态')
    if item['assignee'] is not None:
        result.append(f"已由 {item['assignee']} 认领")
    if any(tasks[p]['status'] != '批准执行' for p in parents(tasks, ident)):
        result.append('祖先未处于批准执行状态')
    for dep in sorted(dependencies(tasks, ident)):
        if tasks[dep]['status'] != '收官':
            result.append(f"前置 {dep} 为{tasks[dep]['status']}，未收官")
    if any(tasks[c]['status'] not in TERMINAL for c in children(tasks, ident)):
        result.append('仍有未终态的子任务')
    return result


def new_task(ident, content, parent, deps, event, legacy=None):
    task_id(ident)
    proposal(content)
    if parent is not None:
        task_id(parent)
    require(isinstance(deps, list), '依赖必须是列表。')
    for dep in deps:
        task_id(dep)
    return {'id': ident, 'proposal': copy.deepcopy(content), 'parent': parent, 'deps': list(deps),
            'status': '候裁', 'assignee': None, 'revision': event['seq'], 'created': event['at'],
            'round': 0, 'approvals': [], 'delivery': None, 'receipts': [], 'handoff': '',
            'history': [], 'legacy': legacy, 'terminal_seq': None}


def approve(tasks, item, auth, event):
    authority(auth)
    require(item['status'] == '候裁', '只有候裁任务可以批准。', 'state')
    require(all(tasks[p]['status'] == '批准执行' for p in parents(tasks, item['id'])),
            '先批准父任务，再批准子任务。', 'state')
    item['status'] = '批准执行'
    item['round'] += 1
    item['delivery'] = None
    item['approvals'].append({'round': item['round'], 'seq': event['seq'],
                             'proposal': copy.deepcopy(item['proposal']), 'parent': item['parent'],
                             'deps': list(item['deps']), 'authority': auth,
                             'projection_version': event.get('projection_version', 1)})


def require_owner(item, actor):
    require(item['status'] == '批准执行' and item['assignee'] == actor,
            '此操作要求操作者持有批准执行任务的认领。', 'owner')


def relation_change(tasks, item, parent, deps):
    for p in {item['parent'], parent} - {None}:
        require(p in tasks and tasks[p]['status'] == '候裁',
                '改变父任务成员关系前，相关父任务须处于候裁。', 'state')
    require(all(d in tasks and tasks[d]['status'] != '驳回' for d in deps),
            '新依赖不能指向不存在或已驳回的任务。', 'graph')
    item['parent'], item['deps'] = parent, deps
    validate_graph(tasks)


def apply(tasks, event):
    fields = {'seq', 'request', 'fingerprint', 'at', 'actor', 'operation', 'data'}
    exact(event, fields | ({'projection_version'} if 'projection_version' in event else set()), '事件信封')
    version = event.get('projection_version', 1)
    require(type(version) is int and version in {1, PROJECTION_VERSION},
            '不支持此投影版本；使用兼容工具，不改写历史来绕过。', 'format')
    require(type(event['seq']) is int and event['seq'] > 0, '序号必须为正整数。', 'format')
    text(event['request'], '请求号', 200)
    text(event['actor'], '操作者', 200)
    require(re.fullmatch(r'[a-f0-9]{64}', event['fingerprint'] or ''), '请求指纹错误。', 'format')
    try:
        stamp = datetime.fromisoformat(event['at'])
        require(stamp.tzinfo is not None, '时间必须带时区。', 'format')
    except (ValueError, TypeError):
        raise QueueError('事件时间不合法。', 'format')
    op, data, actor = event['operation'], event['data'], event['actor']
    require(op in LABELS and isinstance(data, dict), '未知操作或载荷。', 'format')
    touched = []
    if op == 'import':
        exact(data, {'tasks', 'authority'}, '导入')
        authority(data['authority'])
        require(not tasks and isinstance(data['tasks'], list) and data['tasks'], '只能向空账导入非空候裁列表。')
        for row in data['tasks']:
            exact(row, {'id', 'proposal', 'parent', 'deps', 'legacy'}, '导入任务')
            require(row['id'] not in tasks, '导入编号重复。')
            text(row['legacy'], '旧记录')
            tasks[row['id']] = new_task(row['id'], row['proposal'], row['parent'], row['deps'], event, row['legacy'])
            touched.append(row['id'])
        validate_graph(tasks)
    elif op == 'create':
        exact(data, {'id', 'proposal', 'parent', 'deps', 'authority'}, '登记')
        expected = f"T{max([int(k[1:]) for k in tasks] or [0]) + 1:04d}"
        require(data['id'] == expected, '编号必须由账本连续分配，不得复用。', 'identity')
        item = new_task(data['id'], data['proposal'], data['parent'], data['deps'], event)
        tasks[item['id']] = item
        relation_change(tasks, item, data['parent'], data['deps'])
        if data['authority'] is not None:
            approve(tasks, item, data['authority'], event)
        touched = [item['id']]
    else:
        ident = data.get('id')
        require(ident in tasks, f'任务 {ident} 不存在。', 'identity')
        item = tasks[ident]
        require(type(data.get('expect')) is int and data['expect'] == item['revision'],
                f"任务版本已变更；当前 revision={item['revision']}。请重读，不覆盖较新记录。", 'conflict')
        require(item['status'] not in TERMINAL, '任务已封存；纠错请新建任务。', 'sealed')
        touched = [ident]
        base = {'id', 'expect'}
        if op == 'revise':
            exact(data, base | {'proposal', 'parent', 'deps', 'basis'}, '提案修订')
            require(item['status'] == '候裁', '已批准的范围和标准不可直接修改；先撤销。', 'state')
            proposal(data['proposal']); text(data['basis'], '修订依据')
            if data['parent'] != item['parent'] or data['deps'] != item['deps']:
                relation_change(tasks, item, data['parent'], data['deps'])
            item['proposal'] = copy.deepcopy(data['proposal'])
        elif op == 'approve':
            exact(data, base | {'authority'}, '批准')
            approve(tasks, item, data['authority'], event)
        elif op == 'claim':
            exact(data, base, '领取')
            reasons = blockers(tasks, ident)
            require(not reasons, '不能领取：' + '；'.join(reasons), 'not_ready')
            item['assignee'] = actor
        elif op in {'handoff', 'release'}:
            exact(data, base | {'text'}, '交接')
            require_owner(item, actor); text(data['text'], '接手说明')
            item['handoff'] = data['text']
            if op == 'release':
                item['assignee'] = None
        elif op == 'replan':
            exact(data, base | {'plan', 'basis'}, '计划修订')
            require_owner(item, actor); text(data['plan'], '计划'); text(data['basis'], '修订依据')
            item['proposal']['plan'] = data['plan']
        elif op == 'deliver':
            exact(data, base | {'artifacts', 'receipt', 'verification', 'summary'}, '交付')
            require_owner(item, actor)
            require(all(tasks[c]['status'] in TERMINAL for c in children(tasks, ident)), '孩子未了结，不能集成交付。')
            require(isinstance(data['artifacts'], list) and data['artifacts'], '交付至少引用一个实物文件。')
            seen = set()
            for ref in data['artifacts']:
                exact(ref, {'path', 'sha256'}, '工件引用'); relative_path(ref['path'])
                require(ref['path'] not in seen, '工件路径重复。')
                seen.add(ref['path'])
                require(isinstance(ref['sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', ref['sha256']), '工件指纹不合法。')
            text(data['receipt'], '验证回执'); text(data['summary'], '交付摘要')
            require('待填写' not in data['receipt'], '回执仍含待填写内容。')
            require(data['verification'] in {'passed', 'failed', 'unverified'}, '验证申报值不合法。')
            delivery = {'seq': event['seq'], 'round': item['round'], **copy.deepcopy(data)}
            item['delivery'] = delivery
            item['receipts'].append(delivery)
        elif op == 'close':
            exact(data, base | {'authority'}, '验收')
            authority(data['authority'])
            require(item['status'] == '批准执行', '只能验收批准执行中的任务。', 'state')
            delivery = item['delivery']
            require(delivery and delivery['round'] == item['round'] and delivery['verification'] == 'passed',
                    '本次批准后没有验证申报为 passed 的交付；不得用旧轮次或未验证回执收官。', 'acceptance')
            require(all(tasks[c]['status'] in TERMINAL and tasks[c]['terminal_seq'] < delivery['seq']
                        for c in children(tasks, ident)), '父任务须在孩子全部了结后重新集成交付。', 'acceptance')
            item['status'], item['assignee'], item['terminal_seq'] = '收官', None, event['seq']
        elif op in {'revoke', 'cancel'}:
            exact(data, base | {'authority', 'tree', 'expect_seq'}, op)
            authority(data['authority']); require(type(data['tree']) is bool, 'tree 必须为布尔值。')
            require((type(data['expect_seq']) is int and data['expect_seq'] == event['seq'] - 1)
                    if data['tree'] else data['expect_seq'] is None,
                    '子树操作须核对当前全局 seq；有并发变化时重读整组任务。', 'conflict')
            if op == 'revoke':
                require(item['status'] == '批准执行', '只有批准执行中的任务能撤销。', 'state')
            targets = {ident} | (children(tasks, ident, True) if data['tree'] else set())
            targets = {i for i in targets if tasks[i]['status'] not in TERMINAL and
                       (op == 'cancel' or tasks[i]['status'] == '批准执行')}
            for key in targets:
                outside = children(tasks, key) - targets
                require(all(tasks[c]['status'] in TERMINAL or (op == 'revoke' and tasks[c]['status'] == '候裁')
                            for c in outside), '先处理孩子，或显式使用 --tree 整笔处理。', 'state')
            touched = sorted(targets, key=lambda i: (-len(parents(tasks, i)), int(i[1:])))
            for key in touched:
                tasks[key]['status'] = '候裁' if op == 'revoke' else '驳回'
                tasks[key]['assignee'] = None
                tasks[key]['delivery'] = None
                if op == 'cancel':
                    tasks[key]['terminal_seq'] = event['seq']
        else:
            raise QueueError('未实现的操作。', 'format')
    for ident in touched:
        tasks[ident]['revision'] = event['seq']
        tasks[ident]['projection_version'] = version
        tasks[ident]['history'].append(event)
    return touched


def replay(events):
    tasks, requests = {}, set()
    for seq, event in enumerate(events, 1):
        require(isinstance(event, dict) and event.get('seq') == seq, '账本全局序号不连续。', 'corrupt')
        require(event.get('request') not in requests, '账本请求号重复。', 'corrupt')
        apply(tasks, event)
        requests.add(event['request'])
    return tasks


def plan_text(content):
    return (f"# {content['title']}\n\n" + ''.join(f'## {heading}\n\n{content[key]}\n\n'
            for heading, key in SECTIONS.items()))


def generated_bytes(content, version):
    # No field means original v1 bytes, including its historical EOF blank line.
    # New events opt in; do not retroactively re-render sealed volumes or old baselines.
    return (content if version == 1 else content.rstrip('\r\n') + '\n').encode()


def projections(tasks):
    result = {}
    for ident, item in tasks.items():
        home = f'queue/tasks/{ident}'
        meta = {key: item[key] for key in ('id', 'status', 'revision', 'assignee', 'parent', 'created')}
        meta['depends_on'] = item['deps']
        header = '---\n' + ''.join(f'{k}: {json.dumps(v, ensure_ascii=False)}\n' for k, v in meta.items()) + '---\n\n'
        body = header + plan_text(item['proposal'])
        body += '> 自动生成的阅读视图；正本是 .shell/queue/ledger.jsonl。不要直接改本文件。\n\n'
        body += '## 批准基线\n\n'
        for baseline in item['approvals']:
            name = f"approval-{baseline['round']:03d}.md"
            body += f"- 第 {baseline['round']} 轮：[基线]({name})，账本序号 {baseline['seq']}。\n"
            result[f'{home}/{name}'] = generated_bytes('> 历史批准基线，不是当前任务记录。\n\n' +
                f"批准人：{baseline['authority']['by']}\n依据：{baseline['authority']['basis']}\n\n" +
                f"父任务：{baseline['parent']}\n依赖：{', '.join(baseline['deps']) or '无'}\n\n" +
                plan_text(baseline['proposal']), baseline.get('projection_version', 1))
        if not item['approvals']:
            body += '尚未批准。\n'
        body += '\n## 交付与验证\n\n'
        for delivery in item['receipts']:
            name = f"receipt-{delivery['seq']:06d}.md"
            result[f'{home}/{name}'] = delivery['receipt'].encode()
            body += (f"- 第 {delivery['round']} 轮，序号 {delivery['seq']}：{delivery['summary']}；"
                     f"验证申报 `{delivery['verification']}`，[回执]({name})。\n")
            for ref in delivery['artifacts']:
                # Artifact coordinates are Git-root-relative, not necessarily template-root-relative.
                body += f"  - Git 仓库路径 `{ref['path']}`，SHA-256 `{ref['sha256']}`。\n"
        if not item['receipts']:
            body += '尚未交付。\n'
        body += '\n## 决定与过程记录\n\n'
        for event in item['history']:
            data = event['data']
            body += f"- #{event['seq']}｜{event['at']}｜{event['actor']}｜{LABELS[event['operation']]}"
            auth = data.get('authority')
            if auth:
                body += f"｜授权人 {auth['by']}；用户指令依据：{auth['basis']}（Agent 代书，未独立见证）"
            if data.get('basis'):
                body += '｜依据：' + data['basis']
            if data.get('text'):
                body += '｜接手说明：' + data['text']
            body += '\n'
        if item['legacy']:
            result[f'{home}/legacy-import.md'] = ('> 导入前原始 Markdown，仅供追溯，未重建历史授权。\n\n' + item['legacy']).encode()
            body += '\n[导入前原件](legacy-import.md)\n'
        body += '\n## 接手说明\n\n' + (item['handoff'] or '尚无接手备忘；读取任务状态、批准基线与最新交付后再行动。') + '\n'
        result[f'{home}/task.md'] = generated_bytes(body, item.get('projection_version', 1))
    return result
