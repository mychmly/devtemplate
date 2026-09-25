"""Five-stage protocol. Old event semantics/rendering stay in queue_model.

Removed tasks exist only as historical records (status=None), never as a sixth stage.
"""
import copy
import json
from queue_model import (require, exact, text, relative_path, authority,
                         apply as apply_v1, projections as project_v1, blockers as blockers_v1,
                         children)

CONFIG = 'charter/config.json'
ARCHIVE = '.shell/queue/archive'
WINDOW = {'批准', '领取', '交付'}
TO_OLD = {'登记': '候裁', '批准': '批准执行', '领取': '批准执行',
          '交付': '批准执行', '通过': '收官', None: '驳回'}
DEFAULT = {'version': 1, 'window_capacity': 8, 'truth_whitelist': ['truth/goals.md']}


def config(value):
    exact(value, {'version', 'window_capacity', 'truth_whitelist'}, '配置')
    require(type(value['version']) is int and value['version'] == 1, '配置版本不支持。', 'config')
    require(type(value['window_capacity']) is int and value['window_capacity'] > 0,
            '窗口容量须为正整数。', 'config')
    paths = value['truth_whitelist']
    require(isinstance(paths, list) and len(paths) == len(set(paths)), 'truth 白名单须为无重复路径列表。', 'config')
    for path in paths:
        relative_path(path)
        require(path.startswith('truth/') and not any(c in path for c in '*?[]'),
                '白名单只能登记 truth 内明确的文件路径，不接通配符。', 'config')
    return copy.deepcopy(value)


def config_bytes(value):
    return (json.dumps(config(value), ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode()


def occupied(tasks):
    return sorted(k for k, t in tasks.items() if t['status'] in WINDOW and not children(tasks, k))


def check_capacity(tasks, policy):
    require(len(occupied(tasks)) <= policy['window_capacity'],
            f"窗口容量不足：占用 {len(occupied(tasks))}，上限 {policy['window_capacity']}；先处理在列任务或明确调整容量。", 'capacity')


def legacy(tasks):
    result = copy.deepcopy(dict(tasks))
    for t in result.values():
        t['status'] = TO_OLD[t['status']]
        # v2 never uses legacy renderer with a new projection version.
    return result


def blockers(tasks, ident):
    if getattr(tasks, 'protocol', 1) == 1:
        return blockers_v1(tasks, ident)
    old = legacy(tasks)
    reasons = blockers_v1(old, ident)
    if tasks[ident]['status'] != '批准' and not reasons:
        reasons.append('只有批准且未认领的任务可以领取')
    return [s.replace('批准执行', '批准').replace('未收官', '未通过').replace('为驳回', '已取消退出队列') for s in reasons]


def home(item):
    if item['status'] is None and not item.get('frozen_files'):
        return f"{ARCHIVE}/{item['id']}"
    return f"queue/tasks/{item['id']}"


def upgrade(tasks, event):
    frozen = project_v1(tasks)
    for ident, t in tasks.items():
        previous = t['status']
        if previous in {'收官', '驳回'}:
            prefix = f'queue/tasks/{ident}/'
            t['frozen_files'] = {p: v for p, v in frozen.items() if p.startswith(prefix)}
        if previous == '批准执行':
            t['status'] = '交付' if t['delivery'] else ('领取' if t['assignee'] else '批准')
        else:
            t['status'] = {'候裁': '登记', '收官': '通过', '驳回': None}[previous]
        if not t.get('frozen_files'):
            t['revision'] = event['seq']
            t['history'].append(event)
    tasks.protocol = 2


def apply(tasks, event):
    exact(event, {'seq', 'request', 'fingerprint', 'at', 'actor', 'operation', 'data',
                  'projection_version', 'protocol', 'config', 'context'}, '新事件信封')
    require(type(event['protocol']) is int and event['protocol'] == 2 and
            type(event['projection_version']) is int and event['projection_version'] == 2,
            '不支持事件协议。', 'format')
    require(type(event['seq']) is int and event['seq'] > 0, '事件序号不合法。', 'format')
    policy = config(event['config'])
    import re
    require(isinstance(event['context'], str) and (event['context'] == '' or re.fullmatch(r'[a-f0-9]{64}-[1-9][0-9]*', event['context'])), '状态版本不合法。', 'format')
    # Reuse the legacy envelope validator on an isolated minimal event.
    probe = {k: event[k] for k in ('seq','request','fingerprint','at','actor','operation','data','projection_version')}
    # Global operations have no task; validate common fields without inventing task records.
    from datetime import datetime
    import re
    text(event['request'], '请求号', 200); text(event['actor'], '操作者', 200)
    require(re.fullmatch('[a-f0-9]{64}', event['fingerprint'] or ''), '请求指纹不合法。', 'format')
    require(datetime.fromisoformat(event['at']).tzinfo is not None, '时间须带时区。', 'format')
    op, data = event['operation'], event['data']
    require(isinstance(data, dict), '载荷须为对象。', 'format')
    if op == 'upgrade':
        require(tasks.protocol == 1, '协议已经升级。', 'format')
        exact(data, {'authority', 'source_sha256'}, '升级')
        authority(data['authority'])
        require(re.fullmatch('[a-f0-9]{64}', data['source_sha256']), '升级源指纹不合法。', 'format')
        upgrade(tasks, event)
        tasks.config = policy
        check_capacity(tasks, policy)
        return
    require(tasks.protocol == 2, '须先显式升级旧账。', 'upgrade_required')
    if op == 'config':
        exact(data, {'authority'}, '配置变更'); authority(data['authority'])
        tasks.config = policy
        check_capacity(tasks, policy)
        return
    require(policy == tasks.config, '任务事件使用了未登记的配置。', 'config')
    before = copy.deepcopy(dict(tasks))
    if op == 'rework':
        exact(data, {'id', 'expect', 'basis'}, '返工')
        ident = data['id']; require(ident in tasks, '任务不存在。', 'identity')
        t = tasks[ident]
        require(type(data['expect']) is int and t['revision'] == data['expect'], '任务版本已变。', 'conflict')
        require(t['status'] == '交付' and t['assignee'] == event['actor'], '返工须持有已交付任务的认领。', 'owner')
        text(data['basis'], '返工依据')
        t.update(status='领取', delivery=None, revision=event['seq'])
        t['history'].append(event)
    else:
        ident = data.get('id')
        if ident in tasks:
            t = tasks[ident]
            if op in {'deliver', 'replan'}:
                require(t['status'] == '领取', '先返工回到领取，不能沿用已交付资格继续修改。', 'state')
            if op == 'close':
                require(t['status'] == '交付', '只有交付状态才能通过。', 'state' if t['status'] == '登记' else 'acceptance')
        old = legacy(tasks)
        touched = apply_v1(old, probe)
        for key in touched:
            t = old[key]
            old_status = t['status']
            stage = {'候裁': '登记', '收官': '通过', '驳回': None}.get(old_status)
            if old_status == '批准执行':
                if op in {'create', 'approve', 'release'}: stage = '批准'
                elif op in {'claim', 'replan'}: stage = '领取'
                elif op == 'deliver': stage = '交付'
                else: stage = before[key]['status']
            t['status'] = stage
            if op == 'release': t['delivery'] = None
            t['history'][-1] = event
            tasks[key] = t
    check_capacity(tasks, policy)


def projections(tasks):
    if getattr(tasks, 'protocol', 1) == 1:
        return project_v1(tasks)
    result = {CONFIG: config_bytes(tasks.config)}
    for ident, item in tasks.items():
        if item.get('frozen_files'):
            result.update(item['frozen_files']); continue
        # Existing baselines/receipts are rendered by their original versions.
        one = copy.deepcopy(item)
        one['status'] = TO_OLD[item['status']]
        one['history'] = [e for e in one['history'] if e['operation'] not in {'upgrade', 'rework'}]
        old = project_v1({ident: one})
        dest = home(item); prefix = f'queue/tasks/{ident}'
        for path, content in old.items():
            if path != prefix + '/task.md': result[dest + path[len(prefix):]] = content
        p = item['proposal']
        goal = f"# {p['title']}\n\n## 范围\n\n{p['scope']}\n\n## 验收标准与验证方法\n\n{p['criteria']}\n"
        plan = f"# 执行计划\n\n{p['plan']}\n"
        result[dest + '/goal.md'] = goal.encode()
        result[dest + '/plan.md'] = plan.encode()
        meta = {k: item[k] for k in ('id','revision','assignee','parent','deps','round')}
        if item['status'] is not None: meta['status'] = item['status']
        body = '# ' + p['title'] + '\n\n'
        if item['status'] is None: body += '> 已取消并移出队列。此件仅为历史记录，不具有当前任务状态。\n\n'
        body += '```json\n' + json.dumps(meta, ensure_ascii=False, indent=2) + '\n```\n\n'
        body += f"## 登记依据\n\n{p['origin']}\n\n## 任务包\n\n- [规划目标](goal.md)\n- [执行计划](plan.md)\n"
        body += '\n## 批准基线与回执\n\n'
        for path in sorted(result):
            if path.startswith(dest + '/') and basename(path).startswith(('approval-', 'receipt-', 'legacy-')):
                name = basename(path); body += f'- [{name}]({name})\n'
        body += '\n## 过程记录\n\n'
        for e in item['history']:
            body += f"- #{e['seq']}｜{e['operation']}｜{e['actor']}｜{json.dumps(e['data'], ensure_ascii=False, sort_keys=True)}\n"
        body += '\n## 接手说明\n\n' + (item['handoff'] or '尚无接手说明。') + '\n'
        body += '\n> 工具生成；状态来自机器账，不可直接编辑。授权为 Agent 代书，未独立见证。\n'
        result[dest + '/task.md'] = body.rstrip('\r\n').encode() + b'\n'
    return result


def basename(path):
    return path.rsplit('/', 1)[-1]
