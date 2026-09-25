"""Protocol 2 black-box tests: no automatic context refresh when exercising guards."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from test_task_queue import Repo, AUTH, PROPOSAL, run


class StateQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='statequeue-')
        self.addCleanup(self.tmp.cleanup)
        self.r = Repo(self.tmp.name)

    def get(self, repo=None, size=24000):
        return (repo or self.r).call('state', 'get', '--chunk-chars', size)

    def cmd(self, op, ident=None, *flags, token=None, expected=0, request=None, repo=None):
        r = repo or self.r
        args = ['task', op]
        if ident:
            args += [ident, '--expect', str(r.call('history', ident)['tasks'][0]['revision'])]
        args += ['--actor', 'Agent A', '--request', request or uuid.uuid4().hex]
        if token is not None: args += ['--context', token]
        return r.call(*args, *flags, expected=expected)

    def set_config(self, value, recover=False):
        p = self.r.input / 'config.json'; p.write_text(json.dumps(value,ensure_ascii=False))
        flags = ['config','set','--file',str(p),'--actor','Agent A','--request',uuid.uuid4().hex,*AUTH]
        if recover:
            flags += ['--recover','--expect-source', self.r.call('config','show')['edit_token']]
        else:
            flags += ['--context',self.get()['context']]
        return self.r.call(*flags)

    def sources(self, pack, repo=None):
        r = repo or self.r
        return {row['source']: b''.join((r.repo / p).read_bytes() for p in row['parts']) for row in pack['files']}

    def test_01_five_explicit_stages_and_pass_authority(self):
        t=self.r.create(approve=False)['task']; self.assertEqual(self.r.status(t)['status'],'登记')
        self.r.write('approve',t,*AUTH); self.assertEqual(self.r.status(t)['status'],'批准')
        self.r.write('claim',t); self.assertEqual(self.r.status(t)['status'],'领取')
        self.r.deliver(t); self.assertEqual(self.r.status(t)['status'],'交付')
        self.r.write('close',t,*AUTH); self.assertEqual(self.r.status(t)['status'],'通过')
        self.assertEqual(self.r.call('status')['window']['occupied'],[])
        self.assertEqual(set(self.sources(self.get())),{'truth/goals.md'})

    def test_02_eight_is_admission_limit_not_output_truncation(self):
        ids=[self.r.create()['task'] for _ in range(8)]
        before=self.r.raw(); result=self.r.create(expected=1)
        self.assertEqual(result['code'],'capacity'); self.assertEqual(self.r.raw(),before)
        shown={p.split('/')[2] for p in self.sources(self.get()) if p.startswith('queue/tasks/')}
        self.assertEqual(shown,set(ids))
        self.r.write('claim',ids[0]); self.r.deliver(ids[0]); self.assertEqual(self.r.create(expected=1)['code'],'capacity')
        self.r.write('close',ids[0],*AUTH); self.assertEqual(self.r.create()['task'],'T0009')

    def test_03_two_approvals_compete_for_last_slot(self):
        for _ in range(7): self.r.create()
        a=self.r.create(approve=False)['task']; b=self.r.create(approve=False)['task']
        token=self.get()['context']; revisions={t:self.r.status(t)['revision'] for t in (a,b)}
        def attempt(t):
            return self.r.call('approve',t,'--expect',revisions[t],'--context',token,'--actor',t,'--request',t,*AUTH,expected=None)
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(attempt,[a,b]))
        self.assertEqual(sum(r['ok'] for r in results),1)
        self.assertEqual(len(self.r.call('status')['window']['occupied']),8)
        loser=(a,b)[0 if not results[0]['ok'] else 1]
        self.assertEqual(self.r.write('approve',loser,*AUTH,expected=1)['code'],'capacity')

    def test_04_parent_does_not_occupy_but_is_in_pack_after_children_finish(self):
        parent=self.r.create(approve=False)['task']; child=self.r.create(approve=False,parent=parent)['task']
        self.r.write('approve',parent,*AUTH); self.r.write('approve',child,*AUTH)
        self.assertEqual(self.r.call('status')['window']['occupied'],[child])
        self.r.write('claim',child); self.r.deliver(child); self.r.write('close',child,*AUTH)
        self.assertEqual(self.r.call('status')['window']['occupied'],[])
        self.assertIn(f'queue/tasks/{parent}/goal.md',self.sources(self.get()))
        self.assertNotIn(f'queue/tasks/{child}/goal.md',self.sources(self.get()))

    def test_05_cancel_is_removal_and_not_dependency_completion(self):
        dep=self.r.create()['task']; t=self.r.create(deps=[dep])['task']
        self.r.write('cancel',dep,*AUTH)
        self.assertEqual(self.r.call('status',dep,expected=1)['code'],'removed')
        self.assertFalse((self.r.root/f'queue/tasks/{dep}').exists())
        hist=self.r.call('history',dep)['tasks'][0]
        self.assertNotIn('status',hist); self.assertTrue(hist['removed'])
        self.assertEqual(self.r.write('claim',t,expected=1)['code'],'not_ready')
        self.assertTrue((self.r.root/f'.shell/queue/archive/{dep}/task.md').is_file())
        self.assertEqual(self.r.create()['task'],'T0003')
        self.assertEqual(self.r.commit().returncode,0)

    def test_06_payload_is_exact_whitelisted_truth_plus_all_window_files(self):
        secret=self.r.root/'truth/not-listed.md'; secret.write_text('not for context')
        active=self.r.create()['task']; pending=self.r.create(approve=False)['task']
        packet=self.sources(self.get(size=7))
        paths=self.r.call('status',active)['files']['tasks'][0]['package']
        self.assertEqual(set(packet),{'truth/goals.md',*paths})
        for name,data in packet.items(): self.assertEqual(data,(self.r.repo/name).read_bytes())
        self.assertNotIn(f'queue/tasks/{pending}/task.md',packet)
        self.assertFalse(any(p.startswith(('charter/','.git/','object/')) for p in packet))

    def test_07_full_truth_preserves_crlf_bom_and_final_blank_lines(self):
        p=self.r.root/'truth/full.txt'; raw=b'\xef\xbb\xbfalpha\r\n\r\n'+('中文🙂'*100).encode()+b'\n\n'; p.write_bytes(raw)
        cfg=self.r.call('config','show')['config']; cfg['truth_whitelist']=['truth/full.txt']
        self.set_config(cfg)
        self.assertEqual(self.sources(self.get(size=3)),{'truth/full.txt':raw})

    def test_08_stale_truth_blocks_old_and_new_entry_points(self):
        t=self.r.create()['task']; token=self.get()['context']; before=self.r.raw()
        p=self.r.root/'truth/goals.md'; p.write_text(p.read_text()+'\n新目标\n')
        for op in ['claim']:
            self.assertEqual(self.cmd(op,t,token=token,expected=1)['code'],'stale_context')
        self.assertEqual(self.r.call('claim',t,'--expect',self.r.status(t)['revision'],'--actor','Agent A','--request','old-alias','--context',token,expected=1)['code'],'stale_context')
        self.assertEqual(self.r.raw(),before)
        self.cmd('claim',t,token=self.get()['context'])

    def test_09_no_context_is_not_a_bypass(self):
        t=self.r.create()['task']
        self.assertEqual(self.cmd('claim',t,expected=1)['code'],'state_required')

    def test_10_cache_tamper_blocks_until_rebuilt(self):
        t=self.r.create()['task']; pack=self.get(); path=self.r.repo/pack['files'][0]['parts'][0]
        path.write_text('tampered')
        self.assertEqual(self.cmd('claim',t,token=pack['context'],expected=1)['code'],'state_cache')
        rebuilt=self.get(); self.assertEqual(rebuilt['context'],pack['context'])
        self.cmd('claim',t,token=rebuilt['context'])

    def test_11_two_delivery_chunk_sizes_do_not_invalidate_readers(self):
        one=self.get(size=7); two=self.get(size=120)
        self.assertNotEqual(one['context'],two['context'])
        for p in (one,two):
            self.r.call('state','check','--context',p['context'])
            self.assertEqual(self.sources(p)['truth/goals.md'],(self.r.root/'truth/goals.md').read_bytes())

    def test_12_missing_whitelist_fails_and_explicit_config_recovery_works(self):
        p=self.r.root/'truth/goals.md'; p.rename(p.with_suffix('.save'))
        self.assertEqual(self.r.call('state','get',expected=1)['code'],'state_source')
        cfg=self.r.call('config','show')['config']; cfg['truth_whitelist']=[]
        self.set_config(cfg,recover=True)
        self.assertEqual(self.get()['files'],[])

    def test_13_whitelist_rejects_outside_traversal_glob_duplicate_and_links(self):
        cfg=self.r.call('config','show')['config']; token=self.get()['context']; before=self.r.raw()
        link=self.r.root/'truth/link.md'; link.symlink_to(self.r.root/'README.md')
        for paths in [['object/a.md'],['truth/../README.md'],['truth/*'],['truth/goals.md']*2,['truth/link.md']]:
            cfg['truth_whitelist']=paths; p=self.r.input/'bad.json'; p.write_text(json.dumps(cfg))
            self.r.call('config','set','--file',p,'--context',token,'--actor','A','--request',uuid.uuid4().hex,*AUTH,expected=1)
            self.assertEqual(self.r.raw(),before)

    def test_14_config_capacity_change_is_checked_and_stales_context(self):
        t=self.r.create()['task']; old=self.get()['context']; cfg=self.r.call('config','show')['config'];cfg['window_capacity']=1
        self.set_config(cfg)
        self.assertEqual(self.cmd('claim',t,token=old,expected=1)['code'],'stale_context')
        self.assertEqual(self.r.create(expected=1)['code'],'capacity')
        self.assertEqual(self.r.commit().returncode,0)
        (self.r.root/'charter/config.json').write_text(json.dumps(dict(cfg,window_capacity=999)))
        self.assertEqual(self.r.call('doctor',expected=1)['code'],'projection')
        self.r.call('repair');self.assertEqual(self.r.call('config','show')['config']['window_capacity'],1)

    def test_15_rework_release_revoke_do_not_reuse_delivery(self):
        t=self.r.create()['task']; self.r.write('claim',t);self.r.deliver(t)
        self.r.write('rework',t,'--basis','用户反馈需修正');self.assertEqual(self.r.status(t)['status'],'领取')
        self.assertEqual(self.r.write('close',t,*AUTH,expected=1)['code'],'acceptance')
        self.r.deliver(t);self.r.write('release',t,'--text','接续重验')
        self.assertEqual(self.r.status(t)['status'],'批准');self.assertEqual(self.r.write('close',t,*AUTH,expected=1)['code'],'acceptance')
        self.r.write('revoke',t,*AUTH);self.assertEqual(self.r.status(t)['status'],'登记')

    def test_16_retry_works_with_original_expired_token(self):
        t=self.r.create()['task'];token=self.get()['context'];rev=self.r.status(t)['revision']
        args=['claim',t,'--context',token,'--expect',str(rev),'--actor','Agent A','--request','stable-request']
        first=self.r.call(*args);self.r.write('handoff',t,'--text','later')
        again=self.r.call(*args);self.assertTrue(again['already_applied']);self.assertEqual(again['files'],first['files'])

    def test_17_new_clone_rebuilds_pack_without_copying_local_state(self):
        self.r.create();self.get();self.assertEqual(self.r.commit().returncode,0)
        clone=Path(self.tmp.name)/'clone';run(['git','clone',str(self.r.repo),str(clone)],Path(self.tmp.name))
        self.assertFalse((clone/'.shell/local').exists())
        argv=[sys.executable,'-B',str(clone/'tool/shell.py')]
        run([*argv,'init'],clone)
        out=json.loads(run([*argv,'state','get'],clone).stdout)
        self.assertEqual(self.sources(out,repo=type('Root',(),{'repo':clone})()),self.sources(self.get()))

    def test_18_nested_template_paths_are_git_relative(self):
        r=Repo(self.tmp.name,'nested',nested=True);r.create()
        pack=self.get(repo=r)
        self.assertTrue(all(f['source'].startswith('workflow/') for f in pack['files']))
        self.assertEqual(self.sources(pack,r)['workflow/truth/goals.md'],(r.root/'truth/goals.md').read_bytes())

    def test_19_staged_config_and_archive_tampering_blocked(self):
        t=self.r.create()['task'];self.r.write('cancel',t,*AUTH);self.r.git('add','.')
        path=self.r.root/'charter/config.json';old=path.read_bytes();path.write_text('{}');self.r.git('add','charter/config.json');path.write_bytes(old)
        self.assertNotEqual(self.r.git('commit','-qm','bad config index',expected=None).returncode,0)
        self.r.git('add','.');self.assertEqual(self.r.git('commit','-qm','good').returncode,0)
        p=self.r.root/f'.shell/queue/archive/{t}/task.md';p.write_text('changed');self.r.git('add','.')
        self.assertNotEqual(self.r.git('commit','-qm','changed archive',expected=None).returncode,0)

    def test_20_recovery_cannot_bypass_task_projection_errors(self):
        t=self.r.create()['task'];(self.r.root/f'queue/tasks/{t}/task.md').write_text('bad')
        cfg=self.r.call('config','show')['config'];p=self.r.input/'config.json';p.write_text(json.dumps(cfg))
        edit=self.r.call('config','show')['edit_token']
        result=self.r.call('config','set','--recover','--expect-source',edit,'--file',p,'--actor','A','--request','repair-config',*AUTH,expected=1)
        self.assertEqual(result['code'],'projection')

    def test_21_chunk_manifest_paths_cannot_escape_cache(self):
        pack=self.get();p=self.r.repo/pack['manifest'];doc=json.loads(p.read_text())
        doc['files'][0]['parts']=['truth/goals.md'];p.write_text(json.dumps(doc))
        self.assertEqual(self.r.call('state','check','--context',pack['context'],expected=1)['code'],'state_cache')

    def test_22_nonwhitelisted_changes_do_not_stale_packet(self):
        pack=self.get();(self.r.root/'object/ordinary.txt').write_text('outside packet')
        self.r.call('state','check','--context',pack['context'])

    def test_23_sealed_package_remains_identical_and_is_not_resent(self):
        t=self.r.create()['task'];self.r.write('claim',t);self.r.deliver(t);self.r.write('close',t,*AUTH)
        p=self.r.root/f'queue/tasks/{t}';before={f.name:f.read_bytes() for f in p.iterdir()}
        self.r.create();self.r.call('repair');self.assertEqual(before,{f.name:f.read_bytes() for f in p.iterdir()})
        self.assertFalse(any(f'/{t}/' in n for n in self.sources(self.get())))

    def test_24_template_cli_help_lists_tree(self):
        p=run([sys.executable,'-B',str(self.r.root/'tool/shell.py'),'--help'],self.r.repo)
        self.assertIn('task register',p.stdout); self.assertIn('state get',p.stdout)

if __name__ == '__main__': unittest.main()
