#!/usr/bin/env python3
"""Local task queue CLI. Python standard library only; no server or network.

Protects normal operations and checked Git commits, not hostile same-user writes.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone

from queue_model import (QueueError, require, text, sha, line, parse_proposal, decode, encode,
                         replay, TERMINAL, task_id, relative_path, PROJECTION_VERSION)
from queue_v2 import projections, blockers, CONFIG, ARCHIVE, WINDOW, home, config_bytes, config, occupied
import state_pack

LEDGER = '.shell/queue/ledger.jsonl'
LOCAL = '.shell/local'
MANAGED = 'queue/tasks'
HOOK_NAMES = ('applypatch-msg', 'pre-applypatch', 'post-applypatch', 'pre-commit',
              'pre-merge-commit', 'prepare-commit-msg', 'commit-msg', 'post-commit',
              'pre-rebase', 'post-checkout', 'post-merge', 'pre-push', 'pre-receive',
              'update', 'post-receive', 'post-update', 'push-to-checkout', 'post-rewrite',
              'sendemail-validate', 'fsmonitor-watchman', 'reference-transaction',
              'proc-receive', 'post-index-change')


def git(root, *args, check=True):
    env = dict(os.environ)
    # Git hooks may set a relative GIT_DIR; preserve GIT_INDEX_FILE (actual commit index).
    env.pop('GIT_DIR', None)
    env.pop('GIT_WORK_TREE', None)
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, env=env)
    if check and result.returncode:
        raise QueueError('Git 操作失败：' + result.stderr.decode(errors='replace').strip(), 'git')
    return result


def safe(root, relative):
    relative_path(relative)
    current = root
    for part in Path(relative).parts:
        current = current / part
        require(not current.is_symlink(), f'受管路径不能是符号链接：{relative}。', 'path')
    return current


def read_input(path, label):
    p = Path(path)
    require(p.is_file(), f'{label} 文件不存在：{p}。', 'input')
    require(p.stat().st_size <= 1024 * 1024, f'{label} 超过 1 MiB；大数据请提供引用与核对结果。', 'input')
    return text(p.read_text(encoding='utf-8'), label)


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        result = git(self.root, 'rev-parse', '--show-toplevel', check=False)
        require(result.returncode == 0, '保护未就绪：模板须位于 Git 工作仓库中；先由 Agent 完成仓库接入。', 'setup')
        self.repo = Path(os.fsdecode(result.stdout).strip()).resolve()
        self.gitdir = Path(os.fsdecode(git(self.root, 'rev-parse', '--absolute-git-dir').stdout).strip()).resolve()
        common = Path(os.fsdecode(git(self.root, 'rev-parse', '--git-common-dir').stdout).strip())
        common = (self.root / common).resolve() if not common.is_absolute() else common.resolve()
        require(common == self.gitdir, '本版队列只在主工作树写账，不支持分支工作树各持一份账；请回主工作树操作。', 'setup')
        self.prefix = self.root.relative_to(self.repo).as_posix()
        self.prefix = '' if self.prefix == '.' else self.prefix + '/'
        self.ledger = safe(self.root, LEDGER)
        self.local = safe(self.root, LOCAL)
        self.pending = safe(self.root, LOCAL + '/pending.json')
        self.install = safe(self.root, LOCAL + '/install.json')
        self.hooks = self.gitdir / 'taskqueue-hooks'

    def gpath(self, relative):
        return self.prefix + relative

    def response_files(self, tasks, selected=None, stage_paths=None):
        """Explicit coordinates, always relative to the Git root, never guessed names."""
        rows = []
        for ident in sorted(tasks if selected is None else selected, key=lambda key: int(key[1:])):
            item = tasks[ident]
            task_home = home(item)
            baseline = item['approvals'][-1] if item['approvals'] else None
            delivery = item['delivery']
            rows.append({'id': ident, 'task': self.gpath(f'{task_home}/task.md'),
                         'approval': self.gpath(f"{task_home}/approval-{baseline['round']:03d}.md") if baseline else None,
                         'receipt': self.gpath(f"{task_home}/receipt-{delivery['seq']:06d}.md") if delivery else None,
                         'legacy': self.gpath(f'{task_home}/legacy-import.md') if item['legacy'] else None,
                         'package': sorted(self.gpath(p) for p in projections(tasks) if p.startswith(task_home + '/'))})
        result = {'base': str(self.repo), 'ledger': self.gpath(LEDGER), 'tasks': rows}
        if stage_paths is not None:
            result['stage_paths'] = sorted(set(stage_paths))
        return result

    def operation_files(self, events, seq):
        # Retries describe the original operation, even if later events now exist.
        before = projections(replay(events[:seq - 1]))
        tasks = replay(events[:seq])
        after = projections(tasks)
        selected = [ident for ident, item in tasks.items() if item['revision'] == seq]
        paths = {self.gpath(name) for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
        paths.add(self.gpath(LEDGER))
        if events[seq - 1]['operation'] in {'deliver', 'close'}:
            for ident in selected:
                delivery = tasks[ident]['delivery']
                if delivery:
                    paths.update(ref['path'] for ref in delivery['artifacts'])
        return self.response_files(tasks, selected, paths)

    def tree(self, revision='HEAD'):
        result = git(self.repo, 'ls-tree', '-rz', revision, check=False)
        if result.returncode:
            if revision == 'HEAD' and git(self.repo, 'rev-parse', '--verify', 'HEAD', check=False).returncode:
                return {}
            raise QueueError('无法读取 Git 基线。', 'git')
        output = {}
        for row in result.stdout.split(b'\0'):
            if row:
                attrs, path = row.split(b'\t', 1)
                mode, kind, oid = attrs.decode().split()
                output[os.fsdecode(path)] = (mode, oid)
        return output

    def index(self):
        result = {}
        for row in git(self.repo, 'ls-files', '--stage', '-z').stdout.split(b'\0'):
            if row:
                attrs, path = row.split(b'\t', 1)
                mode, oid, stage = attrs.decode().split()
                require(stage == '0', '暂存区有未解决的合并冲突，不能提交。', 'index')
                result[os.fsdecode(path)] = (mode, oid)
        return result

    def blob(self, tree, name):
        row = tree.get(name)
        if row is None:
            return None
        require(row[0] in {'100644', '100755'}, f'受检文件不是普通文件：{name}。', 'path')
        return git(self.repo, 'cat-file', 'blob', row[1]).stdout

    def head_events(self):
        tree = self.tree()
        raw = self.blob(tree, self.gpath(LEDGER))
        return tree, raw, decode(raw) if raw is not None else []

    @contextlib.contextmanager
    def locked(self, seconds=10):
        self.local.mkdir(parents=True, exist_ok=True)
        lockpath = safe(self.root, LOCAL + '/queue.lock')
        handle = open(lockpath, 'a+b')
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0'); handle.flush()
        deadline = time.monotonic() + seconds
        try:
            while True:
                try:
                    if os.name == 'nt':
                        import msvcrt
                        handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                        raise QueueError(f'当前位置无法提供队列文件锁：{exc}。未继续写账。', 'setup') from exc
                    require(time.monotonic() < deadline, '其他队列操作仍在写入，请稍后重试；不要删除锁文件。', 'busy')
                    time.sleep(0.03)
            yield
        finally:
            # Never unlink the lock inode: competing processes must lock the same file.
            handle.close()

    def atomic_write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='tmp-', dir=self.local)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.replace(tmp, path)
            if os.name != 'nt':
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def actual_files(self, include_config=False):
        found = {}
        roots = [safe(self.root, p) for p in (MANAGED, ARCHIVE)]
        for base in roots:
            require(not base.exists() or base.is_dir(), '受管区不是目录。', 'path')
        for directory, dirs, files in (row for base in roots for row in os.walk(base, followlinks=False)):
            for name in dirs + files:
                p = Path(directory) / name
                require(not p.is_symlink(), f'任务区不能放符号链接：{p.name}。', 'path')
            for name in files:
                p = Path(directory) / name
                rel = p.relative_to(self.root).as_posix()
                if rel == MANAGED + '/.gitkeep' or name == '.DS_Store':
                    continue
                require(p.is_file(), '受管区仅支持普通文件。', 'path')
                found[rel] = p.read_bytes()
        if include_config and safe(self.root, CONFIG).is_file():
            found[CONFIG] = safe(self.root, CONFIG).read_bytes()
        return found

    def verify_files(self, tasks):
        expected = projections(tasks)
        actual = self.actual_files(include_config=CONFIG in expected)
        bad = sorted(k for k in expected.keys() | actual.keys() if expected.get(k) != actual.get(k))
        require(not bad, '任务视图或附件与机器账不一致：' + '、'.join(bad[:5]) +
                '。先用 repair 保留差异并重建，不要继续手改。', 'projection')

    def publish(self, tasks, previous=None):
        expected = projections(tasks)
        actual = self.actual_files(include_config=CONFIG in expected)
        # Normal generated updates need no backup; preserve unexpected human differences.
        previous = previous or {}
        differing = {k: v for k, v in actual.items() if expected.get(k) != v and previous.get(k) != v}
        saved = None
        if differing:
            saved = safe(self.root, LOCAL + '/recovery/' + uuid.uuid4().hex)
            for name, content in differing.items():
                p = saved / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(content)
        for name, content in expected.items():
            if actual.get(name) != content:
                self.atomic_write(safe(self.root, name), content)
        for name in actual.keys() - expected.keys():
            safe(self.root, name).unlink()
        for base in (safe(self.root, MANAGED), safe(self.root, ARCHIVE)):
            if base.exists():
                for directory, _, _ in os.walk(base, topdown=False):
                    p = Path(directory)
                    if p != base and not any(p.iterdir()): p.rmdir()
        return str(saved) if saved else None

    def read(self):
        require(self.ledger.is_file(), '保护未就绪：未发现机器账。空模板先 init；旧 Markdown 任务先 migrate --preview。', 'setup')
        raw = self.ledger.read_bytes()
        events = decode(raw)
        return raw, events, replay(events)

    def preserve_prefix(self, raw):
        _, old, _ = self.head_events()
        require(old is None or raw.startswith(old), '机器账改写了 Git HEAD 中已有历史；本次拒绝，不自动覆盖。', 'history')

    def recover(self):
        if self.pending.exists():
            pending = json.loads(self.pending.read_text())
            raw = self.ledger.read_bytes() if self.ledger.exists() else b''
            if sha(raw) == pending['new']:
                tasks = replay(decode(raw))
                old = safe(self.root, LOCAL + '/last-good.jsonl').read_bytes()
                require(sha(old) == pending['old'], '恢复备份与操作前指纹不同，未覆盖现物。', 'corrupt')
                previous = projections(replay(decode(old))) if old else {}
                self.publish(tasks, previous)
            else:
                require(sha(raw) == pending['old'], '恢复日志与账本不符；保留全部文件，需核对 Git 或备份。', 'corrupt')
            self.pending.unlink()
        for path in self.local.glob('tmp-*'):
            if path.is_file() and not path.is_symlink():
                path.unlink()

    def commit(self, old, events, tasks):
        raw = encode(events)
        # Write-ahead intent distinguishes not-committed from committed-but-unprojected.
        self.atomic_write(safe(self.root, LOCAL + '/last-good.jsonl'), old)
        self.atomic_write(self.pending, line({'old': sha(old), 'new': sha(raw)}).encode())
        try:
            self.atomic_write(self.ledger, raw)
            previous = projections(replay(decode(old))) if old else {}
            self.publish(tasks, previous)
            self.pending.unlink()
        except Exception as exc:
            current = self.ledger.read_bytes() if self.ledger.exists() else b''
            committed = current == raw
            message = ('机器账已经保存，投影尚未完成。用同一请求号重试或运行 repair，不要重复生成请求号。'
                       if committed else '尚未落账；保留恢复日志，可用同一请求号重试。')
            raise QueueError(message + f' 原因：{exc}', 'committed_pending' if committed else 'write_failed') from exc

    def hook_source(self, name, old_setting):
        # Existing hooks retain arguments, stdin and Git working directory.
        script = '#!/bin/sh\n# devtemplate-taskqueue-hook-v1\nset -eu\n'
        script += 'repo=$(git rev-parse --show-toplevel)\n'
        if old_setting is None:
            script += 'old_dir="$(git rev-parse --absolute-git-dir)/hooks"\n'
        elif Path(old_setting).expanduser().is_absolute():
            script += 'old_dir=' + shlex.quote(str(Path(old_setting).expanduser())) + '\n'
        else:
            script += 'old_dir="$repo"/' + shlex.quote(old_setting) + '\n'
        script += 'old="$old_dir"/' + shlex.quote(name) + '\n'
        if name in {'pre-commit', 'pre-merge-commit'}:
            script += 'if [ -x "$old" ]; then "$old" "$@" || exit $?; fi\n'
            script += ('exec ' + shlex.quote(sys.executable) + ' "$repo"/' + shlex.quote(self.gpath('tool/task_queue.py')) +
                       ' --root "$repo"/' + shlex.quote(self.prefix.rstrip('/') or '.') + ' check --staged\n')
        else:
            script += 'if [ -x "$old" ]; then exec "$old" "$@"; fi\nexit 0\n'
        return script.encode()

    def install_hooks(self):
        setting = git(self.repo, 'config', '--get', 'core.hooksPath', check=False)
        setting = os.fsdecode(setting.stdout).strip() if setting.returncode == 0 else None
        resolved = (self.repo / Path(setting).expanduser()).resolve() if setting else self.gitdir / 'hooks'
        if self.install.exists():
            info = json.loads(self.install.read_text())
            require(info['prefix'] == self.prefix, '模板位置已变化；请先核对原队列与钩子归属。', 'setup')
            old_setting = info['previous']
            known = {self.hooks.resolve(), (self.repo / Path(old_setting).expanduser()).resolve() if old_setting else self.gitdir / 'hooks'}
            if info.get('managed'):
                known.add(Path(info['managed']).resolve())
            require(resolved in known,
                    'hooksPath 在接入后被另行修改；未覆盖，请核对后重新接入。', 'setup')
        else:
            require(resolved != self.hooks.resolve() and not self.hooks.exists(),
                    '检测到已有队列钩子或另一个实例；不覆盖，先核对 .shell/local/install.json。', 'setup')
            old_setting = setting
            info = {'prefix': self.prefix, 'previous': old_setting, 'python': sys.executable,
                    'managed': str(self.hooks)}
            self.atomic_write(self.install, line(info).encode())
        self.hooks.mkdir(parents=True, exist_ok=True)
        old_dir = (self.repo / Path(old_setting).expanduser()).resolve() if old_setting else self.gitdir / 'hooks'
        names = set(HOOK_NAMES)
        if old_dir.is_dir():
            names.update(p.name for p in old_dir.iterdir() if p.is_file() and not p.name.endswith('.sample'))
        for name in names:
            p = self.hooks / name
            require(not p.is_symlink(), '队列钩子位置不能是符号链接。', 'path')
            p.write_bytes(self.hook_source(name, old_setting))
            p.chmod(0o755)
        worktree_config = git(self.repo, 'config', '--bool', 'extensions.worktreeConfig', check=False).stdout.strip() == b'true'
        git(self.repo, 'config', '--worktree' if worktree_config else '--local', 'core.hooksPath', str(self.hooks))
        info.update(python=sys.executable, managed=str(self.hooks))
        self.atomic_write(self.install, line(info).encode())

    def protection(self):
        require(self.install.is_file(), '保护未就绪：未安装本地提交检查；运行 init。', 'protection')
        info = json.loads(self.install.read_text())
        setting = git(self.repo, 'config', '--get', 'core.hooksPath', check=False)
        require(setting.returncode == 0 and (self.repo / os.fsdecode(setting.stdout).strip()).resolve() == self.hooks.resolve(),
                '保护未就绪：提交钩子接线已改变；运行 init 核对，不继续写账。', 'protection')
        require(info['prefix'] == self.prefix and info['python'] == sys.executable,
                '保护未就绪：模板位置或 Python 运行环境已变化；先重新 init。', 'protection')
        for name in ('pre-commit', 'pre-merge-commit'):
            p = self.hooks / name
            require(p.is_file() and not p.is_symlink() and os.access(p, os.X_OK) and
                    p.read_bytes() == self.hook_source(name, info['previous']),
                    '保护未就绪：提交检查缺失或被改写；先重新 init。', 'protection')
        runtime = self.gpath(LOCAL + '/queue.lock')
        require(git(self.repo, 'check-ignore', '--no-index', '-q', runtime, check=False).returncode == 0,
                '运行态目录未被忽略；先恢复模板 .gitignore 中的 /.shell/local/。', 'protection')

    def ensure_ignore(self):
        path = safe(self.root, '.gitignore')
        raw = path.read_text() if path.exists() else ''
        if '/.shell/local/' not in raw.splitlines():
            path.write_text(raw.rstrip('\n') + '\n\n# 队列锁、恢复日志与本机接线不入库；机器账须入库。\n/.shell/local/\n')

    def artifacts(self, paths):
        require(paths, '至少提供一个 --artifact（Git 仓库相对文件路径）。', 'input')
        result = []
        for name in paths:
            relative_path(name)
            require(not name.startswith('.git/') and not name.startswith(self.gpath('.shell/')) and
                    not name.startswith(self.gpath(MANAGED + '/')), '工件不能引用队列自身或 Git 内部文件。', 'path')
            p = safe(self.repo, name)
            require(p.is_file(), f'工件不存在：{name}。', 'artifact')
            before = p.stat()
            value = hashlib.sha256()
            with p.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    value.update(chunk)
            after = p.stat()
            require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
                    f'读取时工件发生变化：{name}，请重试。', 'conflict')
            result.append({'path': name, 'sha256': value.hexdigest()})
        return result

    def verify_delivery(self, item, index=None):
        delivery = item['delivery']
        if not delivery:
            return
        for ref in delivery['artifacts']:
            if index is None:
                actual = self.artifacts([ref['path']])[0]['sha256']
            else:
                content = self.blob(index, ref['path'])
                require(content is not None, f"交付工件尚未完整暂存：{ref['path']}。", 'artifact')
                actual = sha(content)
            require(actual == ref['sha256'], f"工件 {ref['path']} 已不同于交付版本；重新交付与验证，不能使用旧证据。", 'artifact')

    def check_staged(self):
        index = self.index()
        ledger_key = self.gpath(LEDGER)
        raw = self.blob(index, ledger_key)
        require(raw is not None, '机器账尚未暂存；将 .shell/queue 与 queue/tasks 同批暂存。', 'index')
        events, tasks = decode(raw), replay(decode(raw))
        head, old_raw, old_events = self.head_events()
        require(old_raw is None or raw.startswith(old_raw), '暂存账本改写或删除了既有历史，提交拒绝。', 'history')
        require(not any(p.startswith(self.gpath(LOCAL + '/')) for p in index), '运行态目录被加入暂存，提交拒绝。', 'index')
        expected = {self.gpath(k): v for k, v in projections(tasks).items()}
        actual = {k: self.blob(index, k) for k in index if (k.startswith(self.gpath(MANAGED + '/')) or k.startswith(self.gpath(ARCHIVE + '/')) or (tasks.protocol == 2 and k == self.gpath(CONFIG)))
                  and k != self.gpath(MANAGED + '/.gitkeep')}
        require(actual == expected, '暂存任务视图或附件与账本不一致；不可只暂存部分队列文件。', 'projection')
        for ident, item in replay(old_events).items():
            if item['status'] in TERMINAL | {'通过', None}:
                prefix = self.gpath(home(item) + '/')
                require({p: row for p, row in head.items() if p.startswith(prefix)} ==
                        {p: row for p, row in index.items() if p.startswith(prefix)},
                        f'{ident} 已在 Git 中封存，整卷不能增删改。', 'sealed')
        for item in tasks.values():
            delivery = item['delivery']
            if delivery and (delivery['seq'] > len(old_events) or (item['terminal_seq'] or 0) > len(old_events)):
                self.verify_delivery(item, index)
        # Check actual index blobs, never trust a matching working copy instead.
        require(self.ledger.read_bytes() == raw, '工作账与暂存账不一致；请同批暂存后再提交。', 'index')
        require(self.actual_files(include_config=tasks.protocol == 2) == projections(tasks), '工作任务视图与暂存状态不一致。', 'index')
        for relative in ('tool/task_queue.py', 'tool/queue_model.py', 'tool/queue_v2.py', 'tool/state_pack.py', 'tool/shell.py'):
            require(self.blob(index, self.gpath(relative)) == safe(self.root, relative).read_bytes(),
                    f'检查器 {relative} 与暂存版本不同；请完整暂存工具变更。', 'index')
        require(self.index() == index, '检查期间暂存区发生变化，请重新提交。', 'conflict')
        return {'ok': True, 'checked': 'staged', 'seq': len(events)}


def make_event(events, op, data, actor, request, fingerprint):
    return {'seq': len(events) + 1, 'request': request, 'fingerprint': fingerprint,
            'at': datetime.now(timezone.utc).isoformat(timespec='microseconds'),
            'actor': actor, 'operation': op, 'data': data, 'projection_version': PROJECTION_VERSION}


def get_authority(args):
    require(args.by and args.basis, '需记录 --by 授权人和 --basis 用户指令依据；不要求用户重复批准。', 'authority')
    return {'by': args.by, 'basis': args.basis}


def migration(store):
    actual = store.actual_files()
    rows = []
    for name, content in sorted(actual.items()):
        require(name.count('/') == 3 and name.endswith('/task.md'),
                '自动导入仅支持无附件的候裁任务；其他旧记录须另行核对，不猜测历史。', 'migration')
        old = content.decode('utf-8')
        import re
        match = re.match(r'\A---\n(.*?)\n---\n', old, re.S)
        require(match, f'{name} 缺少旧版字段头。', 'migration')
        fields = {}
        for row in match[1].splitlines():
            require(':' in row, '旧字段格式不明。', 'migration')
            k, v = row.split(':', 1)
            require(k not in fields, '旧字段重复。', 'migration')
            fields[k] = v.strip()
        ident = name.split('/')[2]
        task_id(ident)
        require(fields.get('id') == ident and fields.get('status') == '候裁' and fields.get('assignee') == '无',
                '只自动导入未认领的候裁任务；已批准或终态不能从散文推断授权。', 'migration')
        raw_deps = fields.get('depends_on', '')
        require(raw_deps.startswith('[') and raw_deps.endswith(']'), '旧依赖列表无法识别。', 'migration')
        deps = [s.strip() for s in raw_deps[1:-1].split(',') if s.strip()]
        rows.append({'id': ident, 'proposal': parse_proposal(old), 'parent': None if fields.get('parent') == '无' else fields.get('parent'),
                     'deps': deps, 'legacy': old})
    require(rows, '没有可导入的旧候裁任务；空模板使用 init。', 'migration')
    digest = sha(''.join(line({'path': k, 'sha256': sha(v)}) for k, v in sorted(actual.items())).encode())
    return rows, digest


def parser():
    result = argparse.ArgumentParser(description='本地任务队列：Agent 代书用户指令，程序维护状态与一致性。')
    result.add_argument('--root', default=str(Path(__file__).resolve().parents[1]), help='模板根，默认本工具所在模板')
    result.add_argument('--wait', type=float, default=10, help='等待队列锁秒数')
    sub = result.add_subparsers(dest='command', required=True)
    sub.add_parser('init', help='接入 Git 钩子并初始化空账；保留既有钩子')
    sub.add_parser('doctor', help='核对保护接线、账本与视图')
    p = sub.add_parser('check'); p.add_argument('--staged', action='store_true')
    sub.add_parser('repair', help='保留投影差异后重建；不猜测修改坏账')
    for op in ('status', 'history'):
        p = sub.add_parser(op); p.add_argument('id', nargs='?')
    p = sub.add_parser('state-get'); p.add_argument('--chunk-chars', type=int, default=24000)
    p = sub.add_parser('state-check'); p.add_argument('--context', required=True)
    sub.add_parser('config-show')
    for op in ('upgrade', 'config-set'):
        p = sub.add_parser(op)
        p.add_argument('--preview', action='store_true'); p.add_argument('--expect-source')
        p.add_argument('--file'); p.add_argument('--context'); p.add_argument('--recover', action='store_true')
        p.add_argument('--actor'); p.add_argument('--request'); p.add_argument('--by'); p.add_argument('--basis')
    p = sub.add_parser('migrate', help='只导入旧版未认领候裁记录，不推断历史授权')
    p.add_argument('--preview', action='store_true'); p.add_argument('--expect-source')
    p.add_argument('--actor'); p.add_argument('--request'); p.add_argument('--by'); p.add_argument('--basis')
    for op in ('create', 'revise', 'approve', 'claim', 'release', 'handoff', 'replan', 'deliver', 'close', 'revoke', 'cancel', 'rework'):
        p = sub.add_parser(op)
        if op != 'create':
            p.add_argument('id'); p.add_argument('--expect', type=int, required=True, help='刚读到的任务 revision')
        p.add_argument('--context', help='state get 返回的状态版本；每个调用者独立携带')
        p.add_argument('--actor', required=True, help='本次执行者，归因而非身份认证')
        p.add_argument('--request', required=True, help='本次逻辑请求的稳定唯一号，重试沿用')
        if op in ('create', 'revise'):
            p.add_argument('--proposal', required=True); p.add_argument('--parent')
            p.add_argument('--dep', action='append', default=[] if op == 'create' else None)
        if op == 'revise':
            p.add_argument('--clear-parent', action='store_true')
            p.add_argument('--clear-deps', action='store_true')
        if op == 'create':
            p.add_argument('--approve', action='store_true'); p.add_argument('--by'); p.add_argument('--basis')
        if op in ('approve', 'close', 'revoke', 'cancel'):
            p.add_argument('--by', required=True); p.add_argument('--basis', required=True)
        if op in ('revoke', 'cancel'):
            p.add_argument('--tree', action='store_true', help='对子树整笔撤销或取消，不自动扩张授权范围')
            p.add_argument('--expect-seq', type=int, help='--tree 时必填，刚读到的全局 seq')
        if op in ('handoff', 'release'):
            p.add_argument('--text', required=True)
        if op == 'replan':
            p.add_argument('--plan', required=True, help='计划文本文件'); p.add_argument('--basis', required=True)
        if op in {'revise', 'rework'}:
            p.add_argument('--basis', required=True)
        if op == 'deliver':
            p.add_argument('--artifact', action='append', required=True, help='Git 仓库相对普通文件路径')
            p.add_argument('--receipt', required=True); p.add_argument('--verification', choices=('passed', 'failed', 'unverified'), required=True)
            p.add_argument('--summary', required=True)
    return result


def execute(args):
    require(args.wait >= 0, '--wait 不得为负数。', 'input')
    store = Store(args.root)
    if args.command == 'migrate' and args.preview:
        require(not store.ledger.exists(), '已有机器账，不能重复旧版迁移。', 'migration')
        rows, digest = migration(store)
        data = {'tasks': rows, 'authority': {'by': '预检', 'basis': '仅内存校验，不构成用户授权'}}
        replay([make_event([], 'import', data, 'preview', 'preview', sha(line(data).encode()))])
        return {'ok': True, 'preview': True, 'tasks': [r['id'] for r in rows], 'source_sha256': digest,
                'note': '仅预检，未导入；需要用户同意迁移，不代表批准这些任务执行。'}
    with store.locked(args.wait):
        store.recover()
        op = args.command
        if op in {'init', 'migrate'}:
            if not store.ledger.exists():
                if op == 'init':
                    require(not store.actual_files(), '存在旧任务；不覆盖。先 migrate --preview 核对。', 'migration')
                    events = []
                else:
                    rows, digest = migration(store)
                    require(args.expect_source == digest, '旧记录已变化或未提供预检指纹；重新 migrate --preview。', 'conflict')
                    text(args.actor, '操作者', 200); text(args.request, '请求号', 200)
                    data = {'tasks': rows, 'authority': get_authority(args)}
                    events = [make_event([], 'import', data, args.actor, args.request, sha(line(data).encode()))]
                if not events:
                    policy = state_pack.load_config(store)
                    event = make_event([], 'upgrade', {'authority': {'by': '实例接入', 'basis': '初始化当前模板协议，不批准任何任务'}, 'source_sha256': sha(b'')}, 'system:init', 'init-protocol-2', sha(line(policy).encode()))
                    event.update(protocol=2, config=policy, context='')
                    events = [event]
                tasks = replay(events)  # Validate the whole import before changing hooks or ledger.
                store.ensure_ignore(); store.install_hooks(); store.protection()
                store.commit(b'', events, tasks)
            else:
                require(op == 'init', '已经有机器账；迁移不得重复执行。', 'migration')
                store.ensure_ignore(); store.install_hooks(); store.protection()
            raw, events, tasks = store.read()
            store.preserve_prefix(raw); store.verify_files(tasks)
            stage = [store.gpath(p) for p in (LEDGER, '.gitignore', 'tool/task_queue.py', 'tool/queue_model.py', 'tool/queue_v2.py', 'tool/state_pack.py', 'tool/shell.py', CONFIG)]
            stage.extend(store.gpath(p) for p in projections(tasks))
            return {'ok': True, 'protection': 'ready', 'tasks': len(tasks), 'seq': len(events),
                    'files': store.response_files(tasks, stage_paths=stage),
                    'note': '已保留原 Git 钩子。账本与任务视图须同批提交；授权来源是 Agent 代书，未独立认证。'}
        store.protection()
        raw, events, tasks = store.read()
        store.preserve_prefix(raw)
        if op == 'repair':
            before = store.actual_files(include_config=tasks.protocol == 2)
            saved = store.publish(tasks)
            store.verify_files(tasks)
            expected = projections(tasks)
            tracked = {os.fsdecode(p) for p in git(store.repo, 'ls-files', '-z').stdout.split(b'\0') if p}
            stage = [store.gpath(p) for p in before.keys() | expected.keys()
                     if before.get(p) != expected.get(p) and (p in expected or store.gpath(p) in tracked)]
            return {'ok': True, 'repaired': True, 'saved_differences': saved, 'seq': len(events),
                    'files': store.response_files(tasks, stage_paths=stage)}
        if op in {'upgrade', 'config-show', 'config-set'}:
            return control(store, args, raw, events, tasks)
        store.verify_files(tasks)
        if op == 'state-get': return state_pack.get(store, raw, events, tasks, args.chunk_chars)
        if op == 'state-check': return state_pack.check(store, raw, events, tasks, args.context)
        if op in {'doctor', 'check'}:
            if getattr(args, 'staged', False):
                return store.check_staged()
            return {'ok': True, 'protection': 'ready', 'seq': len(events), 'tasks': sum(t['status'] is not None for t in tasks.values()), 'protocol': tasks.protocol}
        if op in {'status', 'history'}:
            require(args.id is None or args.id in tasks, '任务不存在。', 'identity')
            selected = tasks if args.id is None else {args.id: tasks[args.id]}
            if op == 'status':
                require(args.id is None or tasks[args.id]['status'] is not None, '任务已取消移出队列；使用 task history 查历史。', 'removed')
                selected = {k: t for k, t in selected.items() if t['status'] is not None}
            rows = []
            for ident, t in selected.items():
                row = {k: t[k] for k in ('id', 'revision', 'assignee', 'parent', 'deps', 'round')}
                if t['status'] is not None: row['status'] = t['status']
                else: row['removed'] = True
                row.update(title=t['proposal']['title'], ready=not blockers(tasks, ident), blocked=blockers(tasks, ident))
                if op == 'history': row['events'] = t['history']
                rows.append(row)
            return {'ok': True, 'seq': len(events), 'protocol': tasks.protocol,
                    'window': {'capacity': tasks.config['window_capacity'], 'occupied': occupied(tasks)} if tasks.protocol == 2 else None,
                    'files': store.response_files(tasks, selected), 'tasks': rows}
        text(args.actor, '操作者', 200); text(args.request, '请求号', 200)
        data = {} if op == 'create' else {'id': args.id, 'expect': args.expect}
        if op in {'create', 'revise'}:
            data.update(proposal=parse_proposal(read_input(args.proposal, '方案')), parent=args.parent, deps=args.dep)
        if op == 'revise':
            require(not (args.parent and args.clear_parent) and not (args.dep and args.clear_deps),
                    '设置与清空关系不能同时指定。', 'input')
            # Fingerprint the request intent, not whichever defaults exist on a later retry.
            data['parent'] = None if args.clear_parent else (args.parent if args.parent else {'keep': True})
            data['deps'] = [] if args.clear_deps else (args.dep if args.dep is not None else {'keep': True})
        if op == 'create':
            data['authority'] = get_authority(args) if args.approve else None
            require(args.approve or not (args.by or args.basis), '提供授权依据时须显式 --approve；否则仅登记候裁。', 'input')
        if op in {'approve', 'close', 'revoke', 'cancel'}:
            data['authority'] = get_authority(args)
        if op in {'revoke', 'cancel'}:
            data['tree'] = args.tree
            data['expect_seq'] = args.expect_seq
        if op in {'handoff', 'release'}:
            data['text'] = args.text
        if op == 'replan':
            data.update(plan=read_input(args.plan, '计划'), basis=args.basis)
        if op in {'revise', 'rework'}:
            data['basis'] = args.basis
        if op == 'deliver':
            data.update(artifacts=store.artifacts(args.artifact), receipt=read_input(args.receipt, '回执'),
                        verification=args.verification, summary=args.summary)
        fingerprint = sha(line({'actor': args.actor, 'operation': op, 'data': data}).encode())
        prior = next((e for e in events if e['request'] == args.request), None)
        if prior:
            require(prior['fingerprint'] == fingerprint, '同一请求号对应不同输入；拒绝重复执行，请核对原请求。', 'idempotency')
            return {'ok': True, 'already_applied': True, 'seq': prior['seq'], 'current_seq': len(events),
                    'task': prior['data'].get('id'), 'files': store.operation_files(events, prior['seq'])}
        require(tasks.protocol == 2, '旧账只读；先 upgrade --preview 并明确升级。', 'upgrade_required')
        state_pack.check(store, raw, events, tasks, args.context)
        if op == 'create':
            data['id'] = f"T{max([int(k[1:]) for k in tasks] or [0]) + 1:04d}"
        if op == 'revise':
            require(args.id in tasks, '任务不存在。', 'identity')
            if data['parent'] == {'keep': True}:
                data['parent'] = tasks[args.id]['parent']
            if data['deps'] == {'keep': True}:
                data['deps'] = list(tasks[args.id]['deps'])
        event = make_event(events, op, data, args.actor, args.request, fingerprint)
        event.update(protocol=2, config=tasks.config, context=args.context)
        new_events = events + [event]
        updated = replay(new_events)
        if op == 'close':
            store.verify_delivery(updated[args.id])
        state_pack.check(store, raw, events, tasks, args.context)
        store.commit(raw, new_events, updated)
        return {'ok': True, 'already_applied': False, 'seq': event['seq'], 'task': data.get('id'),
                'files': store.operation_files(new_events, event['seq']),
                'changed': [({'id': k, 'status': t['status'], 'revision': t['revision']} if t['status'] is not None else {'id': k, 'removed': True, 'revision': t['revision']}) for k, t in updated.items()
                            if t['revision'] == event['seq']]}


def control(store, args, raw, events, tasks):
    op = args.command
    token = state_pack.edit_token(store, raw)
    if op == 'config-show':
        return {'ok': True, 'config': state_pack.load_config(store), 'seq': len(events), 'edit_token': token,
                'recorded': tasks.config, 'note': 'edit_token 仅用于显式配置修复，不是任务写入状态版本。'}
    policy = state_pack.load_config(store) if op == 'upgrade' else config(json.loads(read_input(args.file, '配置'), object_pairs_hook=__import__('queue_model').unique_object))
    data = {'authority': get_authority(args)} if not args.preview else {'authority': {'by':'预检','basis':'只读预检，不构成授权'}}
    operation = 'upgrade' if op == 'upgrade' else 'config'
    if op == 'upgrade': data['source_sha256'] = args.expect_source or token
    intent = sha(line({'operation': operation, 'data': data, 'config': policy, 'actor': args.actor}).encode())
    prior = next((e for e in events if e['request'] == args.request), None) if args.request else None
    if prior:
        require(prior['fingerprint'] == intent, '同号输入不同。', 'idempotency')
        return {'ok': True, 'already_applied': True, 'seq': prior['seq'], 'current_seq': len(events), 'files': store.operation_files(events, prior['seq'])}
    if op == 'upgrade':
        require(tasks.protocol == 1, '已经是新协议，不重复升级。', 'upgrade_required')
        store.verify_files(tasks)
    else:
        require(tasks.protocol == 2, '先升级。', 'upgrade_required')
        if args.recover:
            expected = projections(tasks)
            actual = store.actual_files(include_config=True)
            require({k:v for k,v in expected.items() if k != CONFIG} == {k:v for k,v in actual.items() if k != CONFIG},
                    '配置修复不能绕过任务投影检查；先 repair。', 'projection')
        else:
            store.verify_files(tasks)
    event = make_event(events, operation, data, args.actor or 'preview', args.request or 'preview', intent)
    event.update(protocol=2, config=policy, context=args.context or '')
    updated = replay(events + [event])
    if args.preview:
        return {'ok': True, 'preview': True, 'source_sha256': token, 'window_occupied': occupied(updated),
                'tasks': [{'id': k, 'stage': t['status']} for k, t in updated.items()], 'note': '旧事件与封卷不改写；升级只追加协议事件。'}
    text(args.actor, '执行者', 200); text(args.request, '请求号', 200)
    if op == 'upgrade' or args.recover:
        require(args.expect_source == token, '预检指纹已变或未提供 --expect-source。', 'conflict')
    else:
        state_pack.check(store, raw, events, tasks, args.context)
    # Verify every newly configured source before committing (never silently omit a file).
    for path in policy['truth_whitelist']:
        p = safe(store.root, path)
        require(p.is_file(), f'白名单文件不存在：{path}', 'state_source')
        require('\x00' not in p.read_bytes().decode('utf-8'), '白名单文件须为文本。', 'state_source')
    if op == 'upgrade':
        store.atomic_write(safe(store.root, LOCAL + '/upgrade/' + token + '/ledger.jsonl'), raw)
    store.commit(raw, events + [event], updated)
    return {'ok': True, 'already_applied': False, 'seq': event['seq'], 'files': store.operation_files(events + [event], event['seq'])}


def normalize(argv):
    argv = list(argv)
    i = 0
    while i < len(argv) and argv[i] in {'--root','--wait'}: i += 2
    if i < len(argv):
        group = argv[i]
        if group == 'task' and i + 1 < len(argv):
            argv.pop(i)
            argv[i] = {'register':'create', 'pass':'close'}.get(argv[i], argv[i])
        elif group in {'state', 'config'} and i + 1 < len(argv):
            argv[i:i+2] = [group + '-' + argv[i+1]]
    return argv


def main(argv=None):
    args = parser().parse_args(normalize(sys.argv[1:] if argv is None else argv))
    try:
        result = execute(args)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (QueueError, OSError, ValueError, KeyError, TypeError) as exc:
        code = exc.code if isinstance(exc, QueueError) else 'io_or_format'
        print(json.dumps({'ok': False, 'code': code, 'message': str(exc),
                          'committed': code == 'committed_pending'}, ensure_ascii=False), file=sys.stderr)
        return 2 if code == 'committed_pending' else 1


if __name__ == '__main__':
    sys.exit(main())
