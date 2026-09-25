"""Real CLI/process/Git integration tests; all project writes stay in temporary repos.

Run: python3 -B -m unittest discover -s gate -p 'test_*.py' -v
Optional QUEUE_TEST_LOG points to an external JSONL transcript file.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

SOURCE = Path(__file__).resolve().parents[1]
TEMPLATE_FILES = ('.gitignore', 'AGENTS.md', 'LICENSE', 'README.md',
    'charter/AGENTS.md', 'truth/AGENTS.md', 'truth/goals.md',
    'queue/AGENTS.md', 'queue/templates/task.md', 'queue/templates/receipt.md', 'queue/tasks/.gitkeep',
    'gate/AGENTS.md', 'gate/checks.md', 'gate/test_task_queue.py', 'gate/subagent-e2e.md',
    'tool/AGENTS.md', 'tool/catalog.md', 'tool/task_queue.py', 'tool/queue_model.py', 'tool/queue-usage.md', 'tool/queue_v2.py', 'tool/state_pack.py', 'tool/shell.py', 'charter/config.json',
    'eval/AGENTS.md', 'eval/catalog.md', 'reference/AGENTS.md', 'object/AGENTS.md')
ENV = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
ENV.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
           GIT_TERMINAL_PROMPT='0', PYTHONDONTWRITEBYTECODE='1')
TRANSCRIPT = os.environ.get('QUEUE_TEST_LOG')
PROPOSAL = '''# 集成测试任务（虚构用户）

## 来源与目的
测试批准人提出的模拟需求，不是真实用户授权。

## 范围
只修改 object/result.txt，不联网，不发布。

## 验收标准与验证方法
实际读取文件，内容须为 result OK；命令与输出保存在交付回执。

## 执行计划
1. 修改指定文件。
2. 执行检查并交付结果。
'''
AUTH = ['--by', '测试批准人（虚构）', '--basis', '集成测试中模拟用户明确同意；非真实授权']


def run(args, cwd, expected=0):
    p = subprocess.run([str(a) for a in args], cwd=cwd, env=ENV, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if TRANSCRIPT:
        entry = {'cwd': str(cwd), 'args': list(map(str, args)), 'exit': p.returncode,
                 'stdout': p.stdout, 'stderr': p.stderr}
        with open(TRANSCRIPT, 'a') as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + '\n')
    if expected is not None and p.returncode != expected:
        raise AssertionError(f'{args}\nexit={p.returncode}, wanted={expected}\n{p.stdout}\n{p.stderr}')
    return p


class Repo:
    def __init__(self, parent, name='repo', nested=False, initialize=True, legacy_hooks=False):
        self.repo = Path(parent) / name
        self.repo.mkdir()
        self.root = self.repo / 'workflow' if nested else self.repo
        # Never clone the user's actual task history, object payloads or credentials into fixtures.
        for name in TEMPLATE_FILES:
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE / name, destination)
        self.git('init', '-q')
        self.git('add', '.')
        self.git('commit', '-qm', 'Template test baseline; no real user task')
        if legacy_hooks:
            hooks = self.repo / 'old-hooks'
            hooks.mkdir()
            for name in ('pre-commit', 'pre-push'):
                path = hooks / name
                path.write_text('#!/bin/sh\nprintf "%s\\n" ' + name + ' >> legacy-ran.txt\n')
                path.chmod(0o755)
            self.git('config', 'core.hooksPath', 'old-hooks')
        self.input = Path(parent) / ('inputs-' + uuid.uuid4().hex)
        self.input.mkdir()
        self.proposal = self.input / 'proposal.md'
        self.proposal.write_text(PROPOSAL)
        self.receipt = self.input / 'receipt.md'
        self.receipt.write_text('# 验证回执\n\n模拟初始回执；具体测试会写真实输出。未独立验证。\n')
        if initialize:
            self.call('init')

    def git(self, *args, expected=0):
        return run(['git', '-c', 'user.name=Queue Test Fixture', '-c', 'user.email=fixture@example.invalid',
                    '-c', 'commit.gpgsign=false', *args], self.repo, expected)

    def argv(self, *args):
        return [sys.executable, '-B', str(self.root / 'tool/task_queue.py'), '--root', str(self.root), *map(str, args)]

    def call(self, *args, expected=0):
        p = run(self.argv(*args), self.repo, expected)
        raw = p.stdout if p.returncode == 0 else p.stderr
        try:
            return json.loads(raw)
        except ValueError:
            raise AssertionError(f'CLI did not return JSON:\n{p.stdout}\n{p.stderr}')

    def write(self, op, ident=None, *args, actor='Agent A', request=None, expect=None, expected=0):
        auto_revision = expect is None
        flags = [op]
        if ident:
            if expect is None:
                expect = self.call('history', ident)['tasks'][0]['revision']
            flags += [ident, '--expect', str(expect)]
        flags += ['--actor', actor, '--request', request or uuid.uuid4().hex, *map(str, args)]
        for attempt in range(40):
            state = self.call('state-get', expected=None)
            if state.get('code') == 'upgrade_required':
                preview = self.call('upgrade', '--preview')
                self.call('upgrade', '--expect-source', preview['source_sha256'], '--actor', actor,
                          '--request', 'fixture-upgrade', *AUTH)
                state = self.call('state-get')
                if ident and auto_revision:
                    flags[flags.index('--expect') + 1] = str(self.status(ident)['revision'])
            actual = flags + (['--context', state['context']] if state.get('ok') else [])
            p = run(self.argv(*actual), self.repo, None)
            result = json.loads(p.stdout if p.returncode == 0 else p.stderr)
            if expected == 0 and op == 'create' and result.get('code') == 'stale_context':
                continue  # same logical request; refresh only after explicit stale rejection
            if expected is not None and p.returncode != expected:
                raise AssertionError(f'{actual}\nexit={p.returncode} wanted={expected}\n{p.stdout}{p.stderr}')
            return result
        raise AssertionError('fixture retries exhausted')

    def create(self, approve=True, parent=None, deps=(), **kwargs):
        args = ['--proposal', str(self.proposal)]
        if approve:
            args += ['--approve', *AUTH]
        if parent:
            args += ['--parent', parent]
        for dep in deps:
            args += ['--dep', dep]
        return self.write('create', None, *args, **kwargs)

    def status(self, ident):
        return self.call('status', ident)['tasks'][0]

    def raw(self):
        return (self.root / '.shell/queue/ledger.jsonl').read_bytes()

    def events(self):
        return [json.loads(s) for s in self.raw().splitlines()[1:]]

    def deliver(self, ident, result='passed', actor='Agent A'):
        if self.status(ident)['status'] == '交付':
            self.write('rework', ident, '--basis', '测试中重新验证并交付', actor=actor)
        artifact = self.root / 'object/result.txt'
        artifact.write_text('result OK\n')
        check = run([sys.executable, '-c', 'from pathlib import Path; assert Path("object/result.txt").read_text() == "result OK\\n"; print("RESULT PASS")'], self.root)
        self.receipt.write_text('# 真实命令检查回执\n\n未独立验证；批准人是测试模拟。\n\n'
            + f'工作目录：{self.root}\n命令：Python 读取 object/result.txt 并断言内容。\n'
            + f'退出码：{check.returncode}\n\n```text\n{check.stdout}```\n')
        return self.write('deliver', ident, '--artifact', artifact.relative_to(self.repo).as_posix(),
                          '--receipt', self.receipt, '--verification', result, '--summary', '实际执行内容检查', actor=actor)

    def commit(self):
        self.git('add', '.')
        return self.git('commit', '-qm', 'Integration-test queue transaction', expected=None)


class QueueIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='taskqueue-test-')
        self.addCleanup(self.tmp.cleanup)
        self.r = Repo(self.tmp.name)

    def test_01_init_doctor_and_real_successful_commit(self):
        self.assertEqual(self.r.call('doctor')['protection'], 'ready')
        self.assertEqual(self.r.call('init')['seq'], 1)
        self.assertEqual(self.r.commit().returncode, 0)
        tracked = self.r.git('ls-files').stdout
        self.assertIn('.shell/queue/ledger.jsonl', tracked)
        self.assertNotIn('.shell/local/', tracked)

    def test_02_full_lifecycle_real_verification_and_commit(self):
        ident = self.r.create()['task']
        self.r.write('claim', ident)
        self.r.deliver(ident)
        self.assertEqual(self.r.status(ident)['status'], '交付')
        self.r.write('close', ident, *AUTH)
        item = self.r.status(ident)
        self.assertEqual((item['status'], item['assignee']), ('通过', None))
        self.assertEqual(self.r.commit().returncode, 0)
        self.assertIn('未独立见证', (self.r.root / f'queue/tasks/{ident}/task.md').read_text())

    def test_03_candidate_cannot_claim_or_close(self):
        ident = self.r.create(approve=False)['task']
        before = self.r.raw()
        self.assertEqual(self.r.write('claim', ident, expected=1)['code'], 'not_ready')
        self.assertEqual(self.r.write('close', ident, *AUTH, expected=1)['code'], 'state')
        self.assertEqual(self.r.raw(), before)

    def test_04_close_requires_current_successful_delivery(self):
        ident = self.r.create()['task']
        self.r.write('claim', ident)
        self.assertEqual(self.r.write('close', ident, *AUTH, expected=1)['code'], 'acceptance')
        self.r.deliver(ident, 'failed')
        self.assertEqual(self.r.write('close', ident, *AUTH, expected=1)['code'], 'acceptance')
        self.r.deliver(ident, 'unverified')
        self.assertEqual(self.r.write('close', ident, *AUTH, expected=1)['code'], 'acceptance')

    def test_05_wrong_owner_cannot_deliver_release_or_replan(self):
        ident = self.r.create()['task']
        self.r.write('claim', ident)
        for op, args in [('release', ['--text', '交接']), ('handoff', ['--text', '交接']),
                         ('replan', ['--plan', str(self.r.proposal), '--basis', '改进步骤'])]:
            self.assertEqual(self.r.write(op, ident, *args, actor='Agent B', expected=1)['code'], 'owner')

    def test_06_handoff_and_release_do_not_grant_new_scope(self):
        ident = self.r.create()['task']
        self.r.write('claim', ident)
        baseline = self.r.root / f'queue/tasks/{ident}/approval-001.md'
        original = baseline.read_bytes()
        self.r.write('release', ident, '--text', '停在验证前；下一位重读批准基线。')
        self.assertTrue(self.r.status(ident)['ready'])
        self.r.write('claim', ident, actor='Agent B')
        self.assertEqual(baseline.read_bytes(), original)

    def test_07_revoke_reapprove_cannot_reuse_old_delivery(self):
        ident = self.r.create()['task']
        self.r.write('claim', ident)
        self.r.deliver(ident)
        self.r.write('revoke', ident, *AUTH)
        self.assertIsNone(self.r.status(ident)['assignee'])
        self.r.write('approve', ident, *AUTH)
        self.assertEqual(self.r.status(ident)['round'], 2)
        self.assertEqual(self.r.write('close', ident, *AUTH, expected=1)['code'], 'acceptance')
        self.assertEqual(len(list((self.r.root / f'queue/tasks/{ident}').glob('receipt-*'))), 1)

    def test_08_frozen_scope_cannot_be_revised(self):
        ident = self.r.create()['task']
        before = self.r.raw()
        self.r.write('revise', ident, '--proposal', self.r.proposal, '--basis', '更改范围', expected=1)
        self.assertEqual(self.r.raw(), before)

    def test_09_active_owner_can_replan_with_history(self):
        ident = self.r.create()['task']
        self.r.write('claim', ident)
        plan = self.r.input / 'plan.md'; plan.write_text('先补检查，再改文件。')
        self.r.write('replan', ident, '--plan', plan, '--basis', '不改变范围和标准')
        self.assertEqual(self.r.events()[-1]['operation'], 'replan')
        self.assertIn('先补检查', (self.r.root / f'queue/tasks/{ident}/task.md').read_text())

    def test_10_closed_and_cancelled_are_immutable(self):
        ident = self.r.create(approve=False)['task']
        self.r.write('cancel', ident, *AUTH)
        before = self.r.raw()
        self.assertEqual(self.r.write('approve', ident, *AUTH, expected=1)['code'], 'sealed')
        self.assertEqual(self.r.raw(), before)
        next_id = self.r.create()['task']
        self.assertEqual(next_id, 'T0002')

    def test_11_nonexistent_and_rejected_dependencies_rejected(self):
        self.r.create(deps=['T9999'], expected=1)
        ident = self.r.create(approve=False)['task']
        self.r.write('cancel', ident, *AUTH)
        self.r.create(deps=[ident], expected=1)

    def test_12_inherited_dependency_and_parent_integration(self):
        dep = self.r.create()['task']
        parent = self.r.create(approve=False, deps=[dep])['task']
        child = self.r.create(approve=False, parent=parent)['task']
        self.r.write('approve', parent, *AUTH)
        self.r.write('approve', child, *AUTH)
        self.r.write('claim', child, expected=1)
        self.r.write('claim', parent, expected=1)
        self.r.write('claim', dep); self.r.deliver(dep); self.r.write('close', dep, *AUTH)
        self.r.write('claim', child); self.r.deliver(child); self.r.write('close', child, *AUTH)
        self.r.write('claim', parent); self.r.deliver(parent); self.r.write('close', parent, *AUTH)
        self.assertEqual(self.r.commit().returncode, 0)

    def test_13_combined_tree_and_dependency_cycle_rejected(self):
        parent = self.r.create(approve=False)['task']
        child = self.r.create(approve=False, parent=parent)['task']
        other = self.r.create(approve=False, deps=[child])['task']
        before = self.r.raw()
        result = self.r.write('revise', parent, '--proposal', self.r.proposal,
                              '--dep', other, '--basis', '模拟成环', expected=1)
        self.assertEqual(result['code'], 'graph')
        self.assertEqual(self.r.raw(), before)

    def test_14_child_cannot_be_added_to_active_or_terminal_parent(self):
        parent = self.r.create()['task']
        self.r.create(approve=False, parent=parent, expected=1)
        self.r.write('cancel', parent, *AUTH)
        self.r.create(approve=False, parent=parent, expected=1)

    def test_15_tree_revoke_is_one_transaction_and_releases_children(self):
        parent = self.r.create(approve=False)['task']
        child = self.r.create(approve=False, parent=parent)['task']
        self.r.write('approve', parent, *AUTH); self.r.write('approve', child, *AUTH)
        self.r.write('claim', child)
        before = len(self.r.events())
        self.r.write('revoke', parent, *AUTH, '--tree', '--expect-seq', str(before))
        self.assertEqual(len(self.r.events()), before + 1)
        for ident in (parent, child):
            row = self.r.status(ident)
            self.assertEqual((row['status'], row['assignee']), ('登记', None))

    def test_16_stale_task_and_subtree_versions_rejected(self):
        ident = self.r.create()['task']; old = self.r.status(ident)['revision']
        self.r.write('claim', ident)
        self.r.write('release', ident, '--text', '旧版本', expect=old, expected=1)
        self.r.write('cancel', ident, *AUTH, '--tree', '--expect-seq', str(old), expected=1)

    def test_17_repeated_request_is_idempotent_and_changed_input_rejected(self):
        first = self.r.create(request='same-request')
        second = self.r.create(request='same-request')
        self.assertEqual(first['task'], second['task'])
        self.assertTrue(second['already_applied'])
        self.assertEqual(len(self.r.events()), 2)
        self.r.proposal.write_text(PROPOSAL.replace('集成测试任务', '另一个任务'))
        self.assertEqual(self.r.create(request='same-request', expected=1)['code'], 'idempotency')

    def test_18_two_real_processes_race_to_claim(self):
        ident = self.r.create()['task']; revision = self.r.status(ident)['revision']
        def attempt(actor):
            return self.r.write('claim', ident, actor=actor, expect=revision, expected=None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, ['Agent A', 'Agent B']))
        self.assertEqual(sum(o['ok'] for o in outcomes), 1)
        self.assertEqual(len([e for e in self.r.events() if e['operation'] == 'claim']), 1)

    def test_19_parallel_registration_has_no_duplicate_or_lost_ids(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(lambda _: self.r.create(approve=False)['task'], range(12)))
        self.assertEqual(sorted(ids), [f'T{i:04d}' for i in range(1, 13)])
        self.assertEqual(self.r.call('doctor')['seq'], 13)

    def test_20_tampered_projection_is_detected_and_repair_preserves_difference(self):
        ident = self.r.create()['task']
        path = self.r.root / f'queue/tasks/{ident}/task.md'
        expected = path.read_bytes(); path.write_text('误改内容\n')
        self.assertEqual(self.r.call('doctor', expected=1)['code'], 'projection')
        result = self.r.call('repair')
        self.assertEqual(path.read_bytes(), expected)
        saved = Path(result['saved_differences']) / f'queue/tasks/{ident}/task.md'
        self.assertEqual(saved.read_text(), '误改内容\n')

    def test_21_tampered_receipt_and_extra_file_are_not_silently_accepted(self):
        ident = self.r.create()['task']; self.r.write('claim', ident); self.r.deliver(ident)
        revision = self.r.status(ident)['revision']
        home = self.r.root / f'queue/tasks/{ident}'
        receipt = next(home.glob('receipt-*')); original = receipt.read_bytes()
        receipt.write_text('被覆盖的回执')
        (home / 'unregistered.md').write_text('未登记的新件')
        self.r.write('close', ident, *AUTH, expect=revision, expected=1)
        self.r.call('repair')
        self.assertEqual(receipt.read_bytes(), original)
        self.assertFalse((home / 'unregistered.md').exists())

    def test_22_real_commit_rejects_partial_staging_and_staged_blob_tamper(self):
        ident = self.r.create()['task']
        self.r.git('add', '.')
        self.r.write('claim', ident)
        self.r.git('add', '.shell/queue/ledger.jsonl')
        self.assertNotEqual(self.r.git('commit', '-qm', 'must fail', expected=None).returncode, 0)
        self.r.git('add', '.')
        path = self.r.root / f'queue/tasks/{ident}/task.md'
        original = path.read_bytes(); path.write_text('只污染暂存，工作副本随后还原')
        self.r.git('add', str(path.relative_to(self.r.repo)))
        path.write_bytes(original)
        self.assertNotEqual(self.r.git('commit', '-qm', 'staged bad blob', expected=None).returncode, 0)
        self.assertEqual(self.r.commit().returncode, 0)

    def test_23_real_commit_rejects_history_rewrite(self):
        self.r.create(); self.assertEqual(self.r.commit().returncode, 0)
        path = self.r.root / '.shell/queue/ledger.jsonl'
        raw = path.read_bytes(); path.write_bytes(raw.replace(b'Agent A', b'Agent Z'))
        self.r.git('add', '.')
        self.assertNotEqual(self.r.git('commit', '-qm', 'rewrite history', expected=None).returncode, 0)
        path.write_bytes(raw)

    def test_24_real_commit_rejects_sealed_volume_changes(self):
        ident = self.r.create(approve=False)['task']; self.r.write('cancel', ident, *AUTH)
        self.assertEqual(self.r.commit().returncode, 0)
        (self.r.root / f'.shell/queue/archive/{ident}/extra.md').write_text('封存后增加')
        self.r.git('add', '.')
        self.assertNotEqual(self.r.git('commit', '-qm', 'sealed extra', expected=None).returncode, 0)

    def test_25_runtime_files_cannot_be_committed(self):
        self.r.git('add', '.')
        self.r.git('add', '-f', '.shell/local/queue.lock')
        self.assertNotEqual(self.r.git('commit', '-qm', 'runtime leak', expected=None).returncode, 0)

    def test_26_changed_artifact_rejected_at_close_and_commit(self):
        ident = self.r.create()['task']; self.r.write('claim', ident); self.r.deliver(ident)
        (self.r.root / 'object/result.txt').write_text('结果已变化')
        self.assertEqual(self.r.write('close', ident, *AUTH, expected=1)['code'], 'artifact')
        self.assertNotEqual(self.r.commit().returncode, 0)
        self.r.deliver(ident); self.r.write('close', ident, *AUTH)
        self.assertEqual(self.r.commit().returncode, 0)

    def test_27_closed_task_artifact_can_evolve_in_later_task(self):
        ident = self.r.create()['task']; self.r.write('claim', ident); self.r.deliver(ident); self.r.write('close', ident, *AUTH)
        self.assertEqual(self.r.commit().returncode, 0)
        next_id = self.r.create()['task']; self.r.write('claim', next_id)
        (self.r.root / 'object/result.txt').write_text('后续任务中的修改；旧交付引用仍记旧指纹')
        self.assertEqual(self.r.commit().returncode, 0)

    def test_28_disconnected_hook_refuses_normal_writes(self):
        before = self.r.raw()
        self.r.git('config', 'core.hooksPath', 'other-hooks')
        self.assertEqual(self.r.create(expected=1)['code'], 'protection')
        self.assertEqual(self.r.raw(), before)

    def test_29_corrupt_or_duplicate_json_is_rejected_not_repaired_by_guess(self):
        self.r.create()
        path = self.r.root / '.shell/queue/ledger.jsonl'; original = path.read_bytes()
        path.write_bytes(original[:-3])
        self.r.call('repair', expected=1)
        self.assertEqual(path.read_bytes(), original[:-3])
        path.write_bytes(original.replace(b'"seq":1', b'"seq":1,"seq":1'))
        self.r.call('doctor', expected=1)

    def test_30_legacy_hooks_and_other_hook_events_are_preserved(self):
        other = Repo(self.tmp.name, 'legacy-hook-project', legacy_hooks=True)
        self.assertEqual(other.commit().returncode, 0)
        other.git('hook', 'run', 'pre-push')
        observed = (other.repo / 'legacy-ran.txt').read_text()
        self.assertIn('pre-commit', observed); self.assertIn('pre-push', observed)

    def test_31_nested_template_and_existing_project_are_supported(self):
        other = Repo(self.tmp.name, 'nested-project', nested=True)
        (other.repo / 'README.md').write_text('原项目说明，不覆盖')
        ident = other.create()['task']; other.write('claim', ident); other.deliver(ident); other.write('close', ident, *AUTH)
        self.assertEqual(other.commit().returncode, 0)
        self.assertEqual((other.repo / 'README.md').read_text(), '原项目说明，不覆盖')

    def test_32_clone_requires_local_setup_then_replays_same_state(self):
        ident = self.r.create()['task']; self.assertEqual(self.r.commit().returncode, 0)
        dest = Path(self.tmp.name) / 'clone'
        run(['git', 'clone', '--no-local', str(self.r.repo), str(dest)], self.tmp.name)
        p = run([sys.executable, '-B', dest / 'tool/task_queue.py', 'doctor'], dest, expected=1)
        self.assertIn('保护未就绪', p.stderr)
        run([sys.executable, '-B', dest / 'tool/task_queue.py', 'init'], dest)
        status = run([sys.executable, '-B', dest / 'tool/task_queue.py', 'status', ident], dest)
        self.assertEqual(json.loads(status.stdout)['tasks'][0]['status'], '批准')

    def test_33_symlink_artifact_and_managed_symlink_are_rejected(self):
        if not hasattr(os, 'symlink'):
            self.skipTest('symlinks unavailable')
        ident = self.r.create()['task']; self.r.write('claim', ident)
        target = self.r.input / 'outside.txt'; target.write_text('outside')
        link = self.r.root / 'object/link.txt'; link.symlink_to(target)
        self.r.write('deliver', ident, '--artifact', 'object/link.txt', '--receipt', self.r.receipt,
                     '--verification', 'passed', '--summary', '不许跟随链接', expected=1)
        path = self.r.root / f'queue/tasks/{ident}/task.md'; path.unlink(); path.symlink_to(target)
        self.r.call('repair', expected=1)
        self.assertEqual(target.read_text(), 'outside')

    def test_34_missing_authority_and_blank_proposal_do_not_mutate_ledger(self):
        before = self.r.raw()
        self.r.proposal.write_text(PROPOSAL.replace('只修改 object/result.txt', '待填写'))
        self.r.create(expected=1)
        self.assertEqual(self.r.raw(), before)
        self.r.proposal.write_text(PROPOSAL)
        ident = self.r.create(approve=False)['task']
        before = self.r.raw()
        self.r.write('approve', ident, '--by', '', '--basis', '', expected=1)
        self.assertEqual(self.r.raw(), before)

    def legacy(self, repo, ident='T0001', state='候裁', deps='[]'):
        home = repo.root / f'queue/tasks/{ident}'; home.mkdir()
        raw = f'---\nid: {ident}\nstatus: {state}\nparent: 无\ndepends_on: {deps}\nassignee: 无\ncreated: 2026-09-24\n---\n\n' + PROPOSAL
        (home / 'task.md').write_text(raw)
        return raw

    def test_35_candidate_migration_preview_backup_import_and_numbering(self):
        other = Repo(self.tmp.name, 'migration', initialize=False)
        original = self.legacy(other, 'T0001', deps='[T0003]')
        self.legacy(other, 'T0003')
        preview = other.call('migrate', '--preview')
        self.assertFalse((other.root / '.shell').exists())
        other.call('migrate', '--expect-source', preview['source_sha256'], '--actor', 'Agent A',
                   '--request', 'import-once', *AUTH)
        self.assertEqual(other.status('T0001')['status'], '候裁')
        self.assertIn(original, (other.root / 'queue/tasks/T0001/legacy-import.md').read_text())
        backups = list((other.root / '.shell/local/recovery').rglob('task.md'))
        self.assertTrue(any(p.read_text() == original for p in backups))
        self.assertEqual(other.create()['task'], 'T0004')
        self.assertEqual(other.commit().returncode, 0)

    def test_36_migration_never_guesses_old_authorization_or_overwrites(self):
        other = Repo(self.tmp.name, 'migration-active', initialize=False)
        old = self.legacy(other, state='批准执行')
        other.call('migrate', '--preview', expected=1)
        other.call('init', expected=1)
        self.assertEqual((other.root / 'queue/tasks/T0001/task.md').read_text(), old)
        self.assertFalse((other.root / '.shell/queue/ledger.jsonl').exists())

    def test_37_migration_stale_preview_rejected(self):
        other = Repo(self.tmp.name, 'migration-stale', initialize=False)
        self.legacy(other)
        preview = other.call('migrate', '--preview')
        path = other.root / 'queue/tasks/T0001/task.md'; path.write_text(path.read_text() + '\n已更改\n')
        other.call('migrate', '--expect-source', preview['source_sha256'], '--actor', 'Agent A',
                   '--request', 'import-once', *AUTH, expected=1)
        self.assertFalse((other.root / '.shell/queue/ledger.jsonl').exists())

    def paused_child(self, phase, args):
        marker = Path(self.tmp.name) / ('paused-' + uuid.uuid4().hex)
        code = '''import sys,time,importlib.util
from pathlib import Path
root,marker,phase=sys.argv[1:4]
sys.path.insert(0,str(Path(root)/'tool'))
import task_queue as q
def pause():
 Path(marker).write_text('paused')
 time.sleep(60)
if phase=='before-ledger':
 original=q.os.replace
 def replace(src,dst):
  if str(dst).endswith('/.shell/queue/ledger.jsonl'): pause()
  return original(src,dst)
 q.os.replace=replace
elif phase=='after-ledger':
 original=q.Store.publish
 def publish(self,tasks,*args,**kwargs):
  pause()
  return original(self,tasks,*args,**kwargs)
 q.Store.publish=publish
elif phase=='lock':
 with q.Store(root).locked(): pause()
 sys.exit(0)
sys.exit(q.main(['--root',root]+sys.argv[4:]))
'''
        p = subprocess.Popen([sys.executable, '-B', '-c', code, str(self.r.root), str(marker), phase, *args],
                             cwd=self.r.repo, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: p.kill() if p.poll() is None else None)
        deadline = time.monotonic() + 10
        while not marker.exists() and p.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not marker.exists():
            if p.poll() is None:
                p.kill()
            out, err = p.communicate()
            self.fail(f'Child did not reach actual I/O pause: {out} {err}')
        return p

    def crash_create_args(self):
        return ['create', '--proposal', str(self.r.proposal), '--actor', 'Agent A', '--request', 'crash-create', '--context', self.r.call('state-get')['context']]

    def test_38_kill_before_ledger_replace_leaves_old_valid_state(self):
        old = self.r.raw()
        p = self.paused_child('before-ledger', self.crash_create_args())
        p.kill(); p.communicate(timeout=5)
        self.assertEqual(self.r.raw(), old)
        self.assertEqual(self.r.call('status')['seq'], 1)
        self.assertEqual(self.r.call(*self.crash_create_args())['seq'], 2)

    def test_39_kill_after_commit_recovers_projection_without_duplicate_event(self):
        p = self.paused_child('after-ledger', self.crash_create_args())
        p.kill(); p.communicate(timeout=5)
        self.assertEqual(len(self.r.events()), 2)
        retried = self.r.call(*self.crash_create_args())
        self.assertTrue(retried['already_applied'])
        self.assertEqual(self.r.call('doctor')['seq'], 2)
        self.assertTrue((self.r.root / 'queue/tasks/T0001/task.md').exists())

    def test_40_real_lock_contention_and_killed_owner_auto_release(self):
        args = self.crash_create_args()
        p = self.paused_child('lock', [])
        result = self.r.call('--wait', '0', *args, expected=1)
        self.assertEqual(result['code'], 'busy')
        p.kill(); p.communicate(timeout=5)
        self.assertTrue(self.r.call(*self.crash_create_args())['ok'])

    def test_41_tree_failure_is_not_a_partial_multi_task_write(self):
        parent = self.r.create(approve=False)['task']; child = self.r.create(approve=False, parent=parent)['task']
        self.r.write('approve', parent, *AUTH); self.r.write('approve', child, *AUTH)
        before = self.r.raw()
        self.r.write('revoke', parent, *AUTH, expected=1)
        self.assertEqual(self.r.raw(), before)
        self.assertEqual(self.r.status(child)['status'], '批准')

    def test_42_hook_failure_is_fail_closed_and_no_verify_limit_is_explicit(self):
        self.r.create()
        self.r.git('add', '.')
        code = self.r.root / 'tool/queue_model.py'
        original = code.read_bytes(); code.write_text('raise RuntimeError("test checker crash")\n')
        self.assertNotEqual(self.r.git('commit', '-qm', 'checker crash', expected=None).returncode, 0)
        code.write_bytes(original)
        # Same-user bypass is deliberately outside the guarantee, not a hidden claim of security.
        path = self.r.root / 'queue/tasks/T0001/task.md'; path.write_text('tampered projection')
        self.r.git('add', '.')
        self.assertEqual(self.r.git('commit', '--no-verify', '-qm', 'test-only explicit bypass').returncode, 0)
        self.r.call('doctor', expected=1)

    def test_43_deep_subtree_cancel_is_single_event_and_terminal(self):
        root = self.r.create(approve=False)['task']
        left = self.r.create(approve=False, parent=root)['task']
        right = self.r.create(approve=False, parent=root)['task']
        leaf = self.r.create(approve=False, parent=left)['task']
        for ident in (root, left, right, leaf):
            self.r.write('approve', ident, *AUTH)
        before = len(self.r.events())
        self.r.write('cancel', root, *AUTH, '--tree', '--expect-seq', str(before))
        self.assertEqual(len(self.r.events()), before + 1)
        self.assertEqual(self.r.call('status')['tasks'], [])
        self.assertTrue(all(self.r.call('history', i)['tasks'][0]['removed'] for i in (root, left, right, leaf)))

    def test_44_normal_projection_updates_do_not_accumulate_recovery_copies(self):
        ident = self.r.create()['task']; self.r.write('claim', ident)
        self.r.write('handoff', ident, '--text', '等待检查')
        self.assertFalse((self.r.root / '.shell/local/recovery').exists())

    def test_45_user_authorization_record_can_be_attached_without_reasking(self):
        ident = self.r.create()['task']
        event = self.r.events()[1]
        self.assertEqual(event['data']['authority']['by'], AUTH[1])
        self.assertEqual(self.r.status(ident)['status'], '批准')
        self.assertEqual(len(self.r.events()), 2)

    def test_46_live_legacy_hook_rejection_still_blocks_commit(self):
        other = Repo(self.tmp.name, 'legacy-reject', legacy_hooks=True)
        (other.repo / 'old-hooks/pre-commit').write_text('#!/bin/sh\nexit 7\n')
        other.git('add', '.')
        self.assertNotEqual(other.git('commit', '-qm', 'must honor old hook', expected=None).returncode, 0)

    def test_47_relocation_can_reinstall_known_local_hook_configuration(self):
        new_repo = Path(self.tmp.name) / 'moved repository'
        shutil.move(self.r.repo, new_repo)
        self.r.repo = self.r.root = new_repo
        self.r.call('doctor', expected=1)
        self.r.call('init')
        self.assertEqual(self.r.call('doctor')['protection'], 'ready')
        self.assertEqual(self.r.commit().returncode, 0)

    def test_48_worktree_specific_configuration_and_unlisted_legacy_hook(self):
        other = Repo(self.tmp.name, 'worktree-config', initialize=False)
        other.git('config', 'extensions.worktreeConfig', 'true')
        old = other.repo / 'custom-hooks'; old.mkdir()
        hook = old / 'pre-auto-gc'; hook.write_text('#!/bin/sh\nprintf kept > custom-hook-ran.txt\n'); hook.chmod(0o755)
        other.git('config', '--worktree', 'core.hooksPath', 'custom-hooks')
        other.call('init')
        other.git('hook', 'run', 'pre-auto-gc')
        self.assertEqual((other.repo / 'custom-hook-ran.txt').read_text(), 'kept')

    def test_49_migration_preview_checks_graph_before_any_changes(self):
        other = Repo(self.tmp.name, 'migration-invalid', initialize=False)
        self.legacy(other, deps='[T9999]')
        other.call('migrate', '--preview', expected=1)
        self.assertFalse((other.root / '.shell').exists())

    def test_50_runtime_identity_change_can_be_reinitialized(self):
        path = self.r.root / '.shell/local/install.json'
        info = json.loads(path.read_text()); info['python'] = '/old/python/path'
        path.write_text(json.dumps(info))
        self.r.call('doctor', expected=1)
        self.r.call('init')
        self.assertEqual(self.r.call('doctor')['protection'], 'ready')

    def test_51_candidate_revision_preserves_omitted_relationships(self):
        dep = self.r.create(approve=False)['task']
        parent = self.r.create(approve=False)['task']
        child = self.r.create(approve=False, parent=parent, deps=[dep])['task']
        self.r.write('revise', child, '--proposal', self.r.proposal, '--basis', '只细化文字')
        row = self.r.status(child)
        self.assertEqual((row['parent'], row['deps']), (parent, [dep]))
        self.r.write('revise', child, '--proposal', self.r.proposal, '--basis', '明确移除关系', '--clear-parent', '--clear-deps')
        row = self.r.status(child)
        self.assertEqual((row['parent'], row['deps']), (None, []))

    def test_52_revise_retry_uses_original_request_intent(self):
        ident = self.r.create(approve=False)['task']; old = self.r.status(ident)['revision']
        flags = ['--proposal', str(self.r.proposal), '--basis', '修订']
        self.r.write('revise', ident, *flags, request='revise-once', expect=old)
        self.r.write('approve', ident, *AUTH)
        result = self.r.write('revise', ident, *flags, request='revise-once', expect=old)
        self.assertTrue(result['already_applied'])
        self.assertEqual(self.r.status(ident)['status'], '批准')

    def test_53_new_generated_approval_passes_git_whitespace_check(self):
        ident = self.r.create()['task']
        self.r.git('add', '.')
        checked = self.r.git('diff', '--cached', '--check', expected=None)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        data = (self.r.root / f'queue/tasks/{ident}/approval-001.md').read_bytes()
        self.assertTrue(data.endswith(b'\n') and not data.endswith(b'\n\n'))

    def legacy_v1(self, terminal=True):
        """Construct an unversioned v1 fixture; no historical formatter is used as oracle."""
        ident = self.r.create()['task']
        if terminal:
            self.r.write('claim', ident); self.r.deliver(ident); self.r.write('close', ident, *AUTH)
        ledger = self.r.root / '.shell/queue/ledger.jsonl'
        rows = [json.loads(x) for x in ledger.read_text().splitlines()]
        events = rows[2:]
        for event in events:
            for key in ('projection_version','protocol','config','context'): event.pop(key, None)
            event['seq'] -= 1
            if 'expect' in event['data']: event['data']['expect'] -= 1
        ledger.write_text(''.join(json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n' for r in [rows[0], *events]))
        import importlib.util
        spec = importlib.util.spec_from_file_location('legacy_model_fixture', SOURCE / 'tool/queue_model.py')
        model = importlib.util.module_from_spec(spec); spec.loader.exec_module(model)
        for path in (self.r.root / 'queue/tasks'/ident).iterdir(): path.unlink()
        for name, content in model.projections(model.replay(events)).items():
            target = self.r.root / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(content)
        self.r.call('doctor')
        self.assertEqual(self.r.commit().returncode, 0)
        home = self.r.root / f'queue/tasks/{ident}'
        return ident, {p.name: p.read_bytes() for p in home.iterdir() if p.is_file()}, ledger.read_bytes()

    def test_54_legacy_sealed_volume_remains_byte_identical_after_new_writes(self):
        ident, before, ledger_before = self.legacy_v1()
        other = self.r.create()['task']
        self.r.call('repair')
        home = self.r.root / f'queue/tasks/{ident}'
        self.assertEqual({p.name: p.read_bytes() for p in home.iterdir()}, before)
        self.assertTrue(self.r.raw().startswith(ledger_before))
        self.assertFalse((self.r.root / f'queue/tasks/{other}/approval-001.md').read_bytes().endswith(b'\n\n'))
        self.r.git('add', '.')
        self.assertEqual(self.r.git('diff', '--cached', '--check', expected=None).returncode, 0)
        self.assertEqual(self.r.git('commit', '-qm', 'new format next to frozen legacy').returncode, 0)

    def test_55_legacy_baseline_is_preserved_across_reapproval_and_repair(self):
        ident, before, _ = self.legacy_v1(terminal=False)
        self.r.write('revoke', ident, *AUTH); self.r.write('approve', ident, *AUTH)
        home = self.r.root / f'queue/tasks/{ident}'
        self.assertEqual((home / 'approval-001.md').read_bytes(), before['approval-001.md'])
        self.assertFalse((home / 'approval-002.md').read_bytes().endswith(b'\n\n'))
        (home / 'approval-001.md').write_bytes(before['approval-001.md'].rstrip(b'\n') + b'\n')
        self.r.call('repair')
        self.assertEqual((home / 'approval-001.md').read_bytes(), before['approval-001.md'])

    def test_56_new_projection_ends_once_but_receipt_bytes_are_not_normalized(self):
        ident = self.r.create()['task']; self.r.write('claim', ident)
        p = self.r.root / f'queue/tasks/{ident}/task.md'
        for memo in ('接手说明\n\n', '接手说明\r\n\r\n'):
            self.r.write('handoff', ident, '--text', memo)
            self.assertTrue(p.read_bytes().endswith(b'\n'))
            self.assertFalse(p.read_bytes().endswith((b'\n\n', b'\r\n')))
        artifact = self.r.root / 'object/result.txt'; artifact.write_text('result OK\n')
        raw = '# 原样证据\n\n留存原始换行。\n\n'; self.r.receipt.write_text(raw)
        self.r.write('deliver', ident, '--artifact', 'object/result.txt', '--receipt', self.r.receipt,
                     '--verification', 'passed', '--summary', '原样证据')
        self.assertEqual(next(p.parent.glob('receipt-*')).read_text(), raw)

    def test_57_create_and_status_report_resolvable_paths(self):
        result = self.r.create(); ident = result['task']; files = result['files']
        self.assertEqual(files['base'], str(self.r.repo.resolve()))
        self.assertEqual(files['ledger'], '.shell/queue/ledger.jsonl')
        task = files['tasks'][0]
        self.assertEqual(task['id'], ident)
        self.assertEqual(task['task'], f'queue/tasks/{ident}/task.md')
        self.assertEqual(task['approval'], f'queue/tasks/{ident}/approval-001.md')
        self.assertIsNone(task['receipt'])
        self.assertEqual(set(files['stage_paths']), {files['ledger'], *task['package']})
        for name in files['stage_paths']:
            self.assertFalse(Path(name).is_absolute())
            self.assertTrue((Path(files['base']) / name).is_file())
        queried = self.r.call('status', ident)['files']
        self.assertEqual(queried['tasks'], files['tasks'])
        self.assertNotIn('stage_paths', queried)

    def test_58_delivery_and_retry_return_original_operation_paths(self):
        ident = self.r.create()['task']; self.r.write('claim', ident)
        artifact = self.r.root / 'object/result.txt'; artifact.write_text('result OK\n')
        rev = self.r.status(ident)['revision']
        args = ['--artifact', 'object/result.txt', '--receipt', str(self.r.receipt),
                '--verification', 'passed', '--summary', '回执路径定位']
        first = self.r.write('deliver', ident, *args, request='path-retry', expect=rev)
        receipt = first['files']['tasks'][0]['receipt']
        self.assertTrue(receipt.endswith('receipt-000004.md'))
        self.assertIn('object/result.txt', first['files']['stage_paths'])
        self.r.write('handoff', ident, '--text', '后续事件不应改变旧请求的文件定位')
        again = self.r.write('deliver', ident, *args, request='path-retry', expect=rev)
        self.assertTrue(again['already_applied'])
        self.assertEqual(again['files'], first['files'])

    def test_59_nested_template_returns_git_root_paths_for_queue_and_artifacts(self):
        other = Repo(self.tmp.name, 'path-nested', nested=True)
        ident = other.create()['task']; other.write('claim', ident)
        artifact = other.repo / 'external-object.txt'; artifact.write_text('外部于模板、仍在 Git 仓内')
        result = other.write('deliver', ident, '--artifact', 'external-object.txt', '--receipt', other.receipt,
                             '--verification', 'passed', '--summary', '嵌套坐标')
        files = result['files']
        self.assertEqual(files['base'], str(other.repo.resolve()))
        self.assertTrue(files['tasks'][0]['receipt'].startswith('workflow/queue/tasks/'))
        self.assertIn('external-object.txt', files['stage_paths'])
        self.assertNotIn('workflow/external-object.txt', files['stage_paths'])
        other.git('add', '.')
        self.assertEqual(other.git('diff', '--cached', '--check', expected=None).returncode, 0)
        self.assertEqual(other.git('commit', '-qm', 'nested path response').returncode, 0)

    def test_60_tree_operation_returns_every_affected_task(self):
        parent = self.r.create(approve=False)['task']
        child = self.r.create(approve=False, parent=parent)['task']
        self.r.write('approve', parent, *AUTH); self.r.write('approve', child, *AUTH)
        seq = len(self.r.events())
        result = self.r.write('revoke', parent, *AUTH, '--tree', '--expect-seq', str(seq))
        self.assertEqual({t['id'] for t in result['files']['tasks']}, {parent, child})
        self.assertEqual(set(result['files']['stage_paths']), {
            '.shell/queue/ledger.jsonl', f'queue/tasks/{parent}/task.md', f'queue/tasks/{child}/task.md'})

    def test_61_repair_returns_real_paths_and_keeps_untracked_recovery_out_of_stage_list(self):
        ident = self.r.create()['task']; self.assertEqual(self.r.commit().returncode, 0)
        home = self.r.root / f'queue/tasks/{ident}'
        (home / 'task.md').write_text('误改')
        (home / 'untracked.md').write_text('不要丢失')
        result = self.r.call('repair'); files = result['files']
        self.assertIn(f'queue/tasks/{ident}/task.md', files['stage_paths'])
        self.assertNotIn(f'queue/tasks/{ident}/untracked.md', files['stage_paths'])
        self.assertFalse(any('.shell/local/' in p for p in files['stage_paths']))
        self.assertTrue(Path(result['saved_differences']).is_dir())

    def test_62_unknown_projection_version_is_rejected(self):
        self.r.create(); path = self.r.root / '.shell/queue/ledger.jsonl'
        rows = [json.loads(s) for s in path.read_text().splitlines()]
        self.assertEqual(rows[1]['projection_version'], 2)
        rows[1]['projection_version'] = 999
        path.write_text(''.join(json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n' for r in rows))
        self.assertEqual(self.r.call('doctor', expected=1)['code'], 'format')


if __name__ == '__main__':
    unittest.main(verbosity=2)
