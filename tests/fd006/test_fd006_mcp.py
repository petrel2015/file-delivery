"""Controller-owned FD-006 draft. No network/provider/model calls.
Unit discovery covers contract structure/routing. Run installed_gate explicitly
against a wheel installed outside the checkout; never substitute source PYTHONPATH.
"""
import asyncio
from contextlib import ExitStack, contextmanager, redirect_stderr
import hashlib
import importlib
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tarfile
import site
import tomllib
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]  # FINAL location tests/fd006/test_*.py
if not (ROOT / 'pyproject.toml').exists():
    ROOT = Path.cwd()
# Tool -> module, function, representative complete public arguments.
ROUTES = {
    'plan': ('planning', 'plan', dict(paths=['/synthetic/input'], root='/synthetic')),
    'pack': ('archive', 'pack', dict(paths=['/synthetic/input'], root='/synthetic', output_dir='/output')),
    'verify': ('archive', 'verify', dict(bundle_dir='/bundle', password_file='/bundle/password.txt')),
    'deliver_local': ('ledger', 'deliver_local', dict(paths=['/synthetic/input'], root='/synthetic', state_dir='/state', store_dir='/store', key='local_1')),
    'status_local': ('ledger', 'status', dict(state_dir='/state', key='local_1')),
    'deliver_qiniu': ('remote', 'deliver', dict(paths=['/synthetic/input'], root='/synthetic', state_dir='/state', config_path='/config', key='remote_1', ttl_seconds=90, retention_days=2)),
    'status_qiniu': ('remote', 'status', dict(state_dir='/state', key='remote_1')),
    'revoke_qiniu': ('remote', 'revoke', dict(state_dir='/state', config_path='/config', key='remote_1')),
    'cleanup_qiniu': ('remote', 'cleanup', dict(state_dir='/state', config_path='/config', dry_run=True)),
    'send_email': ('notification', 'send', dict(state_dir='/state', delivery_key='remote_1', smtp_config='/smtp', recipient='reader@example.test', notification_key='mail_1', contacts_path='/contacts')),
    'status_email': ('notification', 'status', dict(state_dir='/state', notification_key='mail_1')),
}

def normalized(value):
    if isinstance(value, os.PathLike): return os.fspath(value)
    if isinstance(value, (list, tuple)): return [normalized(x) for x in value]
    return value

async def invalid_call(client, name, args):
    from mcp import MCPError
    try:
        result = await client.call_tool(name, args)
    except MCPError as exc:
        assert exc.error.code == -32602, repr(exc)
    else:
        assert result.is_error, serialized(result)

def serialized(result):
    return json.dumps(result.model_dump(mode='json'), ensure_ascii=False)

def payload(result):
    data = result.structured_content
    if data is not None:
        return data
    for block in result.content:
        if getattr(block, 'type', None) == 'text':
            try:
                value = json.loads(block.text)
                if isinstance(value, dict):
                    return value
            except (ValueError, TypeError):
                pass
    raise AssertionError('No dictionary error payload in MCP result')

@contextmanager
def patched_core(module_name, function_name, *, value=None, exception=None):
    """Support both module imports and from-imports, without constraining style."""
    module = importlib.import_module('file_delivery.' + module_name)
    target = getattr(module, function_name)
    signature = inspect.signature(target)
    server_module = importlib.import_module('file_delivery.mcp_server')
    replacement = mock.Mock(return_value=value, side_effect=exception)
    with ExitStack() as stack:
        for name, obj in list(vars(server_module).items()):
            if obj is target:
                stack.enter_context(mock.patch.object(server_module, name, replacement))
        stack.enter_context(mock.patch.object(module, function_name, replacement))
        yield replacement, signature

class MCPContractTests(unittest.TestCase):
    def test_skill_structure(self):  # AC-1; semantics separately reviewed
        p = ROOT / '.agents/skills/file-delivery/SKILL.md'
        text = p.read_text()
        self.assertTrue(text.startswith('---\n'))
        import yaml
        front = yaml.safe_load(text.split('---', 2)[1])
        self.assertEqual(front['name'], 'file-delivery')
        self.assertIsInstance(front['description'], str)
        self.assertTrue(front['description'].strip())
        self.assertNotIn('/Users/aquarist', text)

    def test_package_and_optional_isolation(self):  # AC-2
        config = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
        self.assertEqual(config.get('dependencies', []), [])
        self.assertEqual(config['optional-dependencies']['archive'], ['pyzipper==0.4.0'])
        self.assertEqual(config['optional-dependencies']['qiniu'], ['qiniu==7.18.0'])
        self.assertEqual(config['optional-dependencies']['mcp'], ['mcp==2.2.0'])
        self.assertEqual(config['scripts']['file-delivery-mcp'], 'file_delivery.mcp_server:main')
        # A subprocess import blocker proves lazy optional loading even in SDK env.
        code = '''import sys,importlib.abc,runpy
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname == "mcp" or fullname.startswith("mcp."): raise ModuleNotFoundError("blocked optional mcp")
sys.meta_path.insert(0,Block())
import file_delivery
assert not any(x == "mcp" or x.startswith("mcp.") for x in sys.modules)
sys.argv=["file-delivery","plan",sys.argv[1],"--root",sys.argv[2],"--json"]
runpy.run_module("file_delivery",run_name="__main__")
'''
        with tempfile.TemporaryDirectory() as d:
            d = str(Path(d).resolve())
            f = Path(d) / 'input.txt'; f.write_text('offline')
            env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'), PYTHONDONTWRITEBYTECODE='1')
            proc = subprocess.run([sys.executable, '-c', code, str(f), d], capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)['status'], 'planned')
            blocked_main = code[:code.index('import file_delivery')] + "from file_delivery.mcp_server import main\nraise SystemExit(main())\n"
            missing = subprocess.run([sys.executable, '-c', blocked_main], capture_output=True, text=True, env=env)
            self.assertEqual(missing.returncode, 2, missing.stderr)
            self.assertEqual(missing.stdout, '')
            self.assertTrue(missing.stderr.strip())
            self.assertNotIn('Traceback', missing.stderr)
        from mcp.server import MCPServer
        from file_delivery.mcp_server import create_server
        self.assertIsInstance(create_server(), MCPServer)

    def test_installed_stdio_roundtrip(self):  # AC-2 clean install + AC-5
        # Controller runs against a clean fixed candidate. Snapshot its actual
        # HEAD dynamically: no hardcoded commit or installation from live source.
        with tempfile.TemporaryDirectory(prefix='fd006-install-') as directory:
            work = Path(directory).resolve()
            env = {k: v for k, v in os.environ.items()
                   if k not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')}
            env.update(PYTHONDONTWRITEBYTECODE='1', PIP_DISABLE_PIP_VERSION_CHECK='1',
                       PIP_NO_INDEX='1', PIP_CONFIG_FILE=os.devnull)
            counter = 0
            def run(args, *, expected=0):
                nonlocal counter
                counter += 1
                try:
                    result = subprocess.run([str(x) for x in args], cwd=work,
                                            env=env, capture_output=True,
                                            timeout=100, check=False)
                except subprocess.TimeoutExpired:
                    self.fail(f'Installed qualification step {counter} timed out')
                # Private temporary logs; diagnostics cannot echo arbitrary build
                # text, environment, secrets or absolute config contents.
                (work / f'step-{counter}.stdout').write_bytes(result.stdout)
                (work / f'step-{counter}.stderr').write_bytes(result.stderr)
                if result.returncode != expected:
                    digest = hashlib.sha256(result.stdout + result.stderr).hexdigest()
                    self.fail(f'Installed qualification step {counter} exit={result.returncode}, expected={expected}, output_sha256={digest}')
                return result
            snapshot = work / 'candidate.tar'
            run(['git', '-C', ROOT, 'archive', '--format=tar', '--output', snapshot, 'HEAD'])
            source = work / 'build-source'; source.mkdir()
            with tarfile.open(snapshot) as archive:
                archive.extractall(source, filter='data')
            wheels = work / 'wheels'; wheels.mkdir()
            run([sys.executable, '-m', 'pip', 'wheel', '--no-build-isolation',
                 '--no-deps', '--no-index', '--wheel-dir', wheels, source])
            wheel_files = list(wheels.glob('file_delivery-*.whl'))
            self.assertEqual(len(wheel_files), 1)
            wheel = wheel_files[0]
            base = work / 'base-env'
            full = work / 'full-env'
            run([sys.executable, '-m', 'venv', base])
            run([sys.executable, '-m', 'venv', '--system-site-packages', full])
            bp = base / 'bin/python'; fp = full / 'bin/python'
            # Nested venv --system-site-packages sees the interpreter's *base*
            # packages, not necessarily its parent SDK venv. Add qualified SDK
            # site-packages only, never source/candidate paths or .pth execution.
            full_site = Path(json.loads(run([fp, '-c', 'import json,sysconfig;print(json.dumps(sysconfig.get_path("purelib")))']).stdout))
            qualified_sites = [p for p in site.getsitepackages()
                               if Path(p).is_relative_to(Path(sys.prefix))]
            self.assertTrue(qualified_sites, 'Run controller using qualified SDK venv')
            (full_site / 'controller-qualified-sdk.pth').write_text(''.join(str(Path(p).resolve()) + '\n' for p in qualified_sites))
            for python in (bp, fp):
                run([python, '-m', 'pip', 'install', '--ignore-installed', '--no-deps', '--no-index', wheel])
                check = 'import file_delivery,json,sys;from pathlib import Path;p=Path(file_delivery.__file__).resolve();assert p.is_relative_to(Path(sys.prefix).resolve());print(json.dumps({"installed":True}))'
                self.assertEqual(json.loads(run([python, '-c', check]).stdout), {'installed':True})
            run([bp, '-c', 'import importlib.util;assert importlib.util.find_spec("mcp") is None'])
            input_dir = work / 'base-input'; input_dir.mkdir()
            text = input_dir / 'input.txt'; text.write_text('base offline input')
            result = run([base / 'bin/file-delivery', 'plan', text, '--root', input_dir, '--json'])
            self.assertEqual(json.loads(result.stdout)['status'], 'planned')
            missing = run([base / 'bin/file-delivery-mcp'], expected=2)
            self.assertEqual(missing.stdout, b'')
            self.assertTrue(missing.stderr.strip())
            self.assertNotIn(b'Traceback', missing.stderr)
            run([fp, '-c', 'from importlib.metadata import version;assert version("mcp")=="2.2.0";assert version("pyzipper")=="0.4.0";assert version("qiniu")=="7.18.0"'])
            evidence = asyncio.run(installed_gate(full / 'bin/file-delivery-mcp'))
            self.assertTrue(evidence['official_stdio_auto'])
            self.assertTrue(evidence['official_stdio_legacy_initialize'])
            self.assertTrue(evidence['local_roundtrip'])

    def test_all_routes_once_and_defaults(self):  # AC-3
        from mcp import Client
        from file_delivery.mcp_server import create_server
        async def scenario():
            for tool, (mod, fn, args) in ROUTES.items():
                expected = {'schema_version': 1, 'status': 'controller-synthetic', 'task_id': tool, 'password_file': '/private/password.txt'}
                with patched_core(mod, fn, value=expected) as (spy, signature):
                    async with Client(create_server()) as client:
                        result = await client.call_tool(tool, args)
                    self.assertFalse(result.is_error, serialized(result))
                    self.assertEqual(result.structured_content, expected)
                    spy.assert_called_once()
                    bound = signature.bind(*spy.call_args.args, **spy.call_args.kwargs)
                    for key, value in args.items():
                        self.assertEqual(normalized(bound.arguments[key]), value, (tool, key))
            for tool, mod, fn, args, defaults in [
                ('cleanup_qiniu', 'remote', 'cleanup', {'state_dir':'/state'}, {'dry_run':True}),
                ('deliver_qiniu','remote','deliver', {k:v for k,v in ROUTES['deliver_qiniu'][2].items() if k not in ('ttl_seconds','retention_days')}, {'ttl_seconds':604800,'retention_days':30}),
            ]:
                with patched_core(mod, fn, value={'status':'ok'}) as (spy, sig):
                    async with Client(create_server()) as client:
                        r = await client.call_tool(tool, args)
                    self.assertFalse(r.is_error, serialized(r)); spy.assert_called_once()
                    bound = sig.bind(*spy.call_args.args, **spy.call_args.kwargs); bound.apply_defaults()
                    for key,value in defaults.items(): self.assertEqual(bound.arguments[key],value)
        asyncio.run(scenario())

    def test_schema_rejections_and_annotations(self):  # AC-4
        from mcp import Client
        from file_delivery.mcp_server import create_server
        async def scenario():
            async with Client(create_server()) as client:
                listed = {t.name:t for t in (await client.list_tools()).tools}
            self.assertEqual(set(listed),set(ROUTES))
            for tool,t in listed.items():
                props=t.input_schema.get('properties',{})
                self.assertTrue(set(ROUTES[tool][2]).issubset(props))
                self.assertFalse(set(props)&{'store','transport','checkpoint','password','access_key','secret_key','url','body'})
                from mcp.types import ToolAnnotations
                a=t.annotations or ToolAnnotations()
                if tool in ('plan','verify','status_local','status_qiniu','status_email'):
                    self.assertIs(a.read_only_hint,True)
                else: self.assertIsNot(a.read_only_hint,True)
                if tool in ('pack','send_email'): self.assertIsNot(a.idempotent_hint,True)
                if tool in ('deliver_qiniu','revoke_qiniu','send_email'): self.assertIsNot(a.open_world_hint,False)
            cases=[]
            for tool,(_,_,args) in ROUTES.items():
                cases.extend([(tool,dict(args,unexpected_field='bad')),(tool,dict(args,state_dir=19))] if 'state_dir' in args else [(tool,dict(args,unexpected_field='bad'))])
            cases += [('plan',{'root':'/tmp'}),('plan',{'paths':'not-list','root':'/tmp'}),('plan',{'paths':[1],'root':'/tmp'}),('deliver_qiniu',dict(ROUTES['deliver_qiniu'][2],ttl_seconds=True)),('deliver_qiniu',dict(ROUTES['deliver_qiniu'][2],retention_days='2')),('cleanup_qiniu',{'state_dir':'/state','dry_run':1}),('cleanup_qiniu',{'state_dir':'/state','dry_run':'false'})]
            for tool,args in cases:
                mod,fn,_=ROUTES[tool]
                with patched_core(mod,fn,value={'status':'UNEXPECTED'}) as (spy,_):
                    async with Client(create_server()) as client: await invalid_call(client,tool,args)
                    spy.assert_not_called()
        asyncio.run(scenario())

    def test_safe_errors_no_retry_and_recovery(self):  # AC-3/4
        from mcp import Client
        from file_delivery.errors import DeliveryError
        from file_delivery.mcp_server import create_server
        marker='SYNTHETIC_SECRET_MUST_NOT_ESCAPE'
        async def scenario():
            cases=[('deliver_qiniu',c) for c in ('TASK_REVOKED','LINK_EXPIRED','IDEMPOTENCY_CONFLICT','REMOTE_UNKNOWN')]+[('send_email','SMTP_UNKNOWN'),('status_local','TASK_NOT_FOUND')]
            for tool,code in cases:
                mod,fn,args=ROUTES[tool]
                with patched_core(mod,fn,exception=DeliveryError(code,marker)) as (spy,_):
                    async with Client(create_server()) as client:r=await client.call_tool(tool,args)
                    self.assertTrue(r.is_error);self.assertEqual(payload(r)['error']['code'],code)
                    self.assertNotIn(marker,serialized(r));spy.assert_called_once()
            with patched_core('planning','plan',exception=RuntimeError(marker)) as (spy,_):
                async with Client(create_server()) as client:
                    r=await client.call_tool('plan',ROUTES['plan'][2]);self.assertTrue(r.is_error)
                    self.assertEqual(payload(r)['error']['code'],'INTERNAL_ERROR');self.assertNotIn(marker,serialized(r))
                    spy.side_effect=None;spy.return_value={'status':'planned','schema_version':1}
                    good=await client.call_tool('plan',ROUTES['plan'][2]);self.assertFalse(good.is_error)
                    self.assertEqual(spy.call_count,2)
            # Acceptance remains a single core call: wrapper must not retry accepted mail.
            with patched_core('notification','send',value={'schema_version':1,'status':'channel-accepted','reused':True}) as (spy,_):
                async with Client(create_server()) as client:
                    r=await client.call_tool('send_email',ROUTES['send_email'][2])
                self.assertEqual(r.structured_content['status'],'channel-accepted');spy.assert_called_once()
        stderr=io.StringIO()
        with redirect_stderr(stderr):
            asyncio.run(scenario())
        self.assertNotIn(marker,stderr.getvalue())

    def test_host_reference_exists(self):  # AC-6 structure only, external gate below
        p=ROOT / '.agents/skills/file-delivery/references/host-integration.md'
        self.assertTrue(p.is_file());self.assertTrue(p.read_text().strip())
        self.assertNotIn('/Users/aquarist',p.read_text())

async def installed_gate(command):
    """AC-5 external controller gate. command MUST be installed outside checkout."""
    from mcp import Client,StdioServerParameters
    command=Path(command).resolve()
    if not command.is_file() or command.is_relative_to(ROOT.resolve()):
        raise AssertionError('Use a real installed entrypoint outside repository')
    with tempfile.TemporaryDirectory(prefix='fd006-wire-') as d:
        base=Path(d).resolve();root=base/'输入';root.mkdir();source=root/'报告 文本.txt';source.write_text('Unicode 文件内容\n')
        expected_hash=hashlib.sha256(source.read_bytes()).hexdigest()
        env={'PYTHONDONTWRITEBYTECODE':'1','PATH':os.environ.get('PATH','/usr/bin:/bin')}
        # Transparent byte forwarding also records raw server stdout: SDK parsing
        # alone could ignore a non-JSON banner and would not prove protocol purity.
        proxy = """import subprocess,sys,threading
p=subprocess.Popen([sys.argv[1]],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
def feed():
 try:
  for line in sys.stdin.buffer:p.stdin.write(line);p.stdin.flush()
 finally:p.stdin.close()
def err():
 with open(sys.argv[3],'wb') as f:
  for line in p.stderr:f.write(line)
threading.Thread(target=feed,daemon=True).start()
t=threading.Thread(target=err);t.start()
with open(sys.argv[2],'wb') as f:
 for line in p.stdout:
  f.write(line);f.flush();sys.stdout.buffer.write(line);sys.stdout.buffer.flush()
p.wait();t.join()
"""
        for mode in ('auto','legacy'):
            wirelog=base/('stdout-'+mode);errlog=base/('stderr-'+mode)
            params=StdioServerParameters(command=sys.executable,args=['-c',proxy,str(command),str(wirelog),str(errlog)],cwd=str(base),env=env)
            async with Client(params,mode=mode) as c:
                assert {t.name for t in (await c.list_tools()).tools}==set(ROUTES)
                async def call(name,args):
                    r=await c.call_tool(name,args)
                    assert not r.is_error,serialized(r)
                    assert isinstance(r.structured_content,dict)
                    return r.structured_content
                plan=await call('plan',{'paths':[str(source)],'root':str(root)})
                assert plan['files'][0]['sha256']==expected_hash
                bundle=base/('bundle-'+mode)
                packaged=await call('pack',{'paths':[str(source)],'root':str(root),'output_dir':str(bundle)})
                verified=await call('verify',{'bundle_dir':str(bundle)})
                assert verified['archive_sha256']==packaged['archive_sha256']
                args={'paths':[str(source)],'root':str(root),'state_dir':str(base/('state-'+mode)),'store_dir':str(base/('store-'+mode)),'key':'wire_1'}
                first=await call('deliver_local',args)
                archive_before=Path(first['artifact_path']).read_bytes();password_before=Path(first['password_file']).read_bytes()
                second=await call('deliver_local',args)
                assert second['reused'] is True and second['task_id']==first['task_id']
                assert archive_before==Path(second['artifact_path']).read_bytes() and password_before==Path(second['password_file']).read_bytes()
                assert hashlib.sha256(archive_before).hexdigest()==first['archive_sha256']
                status=await call('status_local',{'state_dir':args['state_dir'],'key':'wire_1'})
                assert status['task_id']==first['task_id']
                missing=await c.call_tool('status_local',{'state_dir':args['state_dir'],'key':'absent'})
                assert missing.is_error and payload(missing)['error']['code']=='TASK_NOT_FOUND'
                forbidden=base/('must-not-exist-'+mode)
                await invalid_call(c,'pack',{'paths':[str(source)],'root':str(root),'output_dir':str(forbidden),'unexpected':True})
                assert not forbidden.exists()
                # A second valid request proves process remains usable after errors.
                assert (await call('plan',{'paths':[str(source)],'root':str(root)}))['files']==plan['files']
            for line in wirelog.read_bytes().splitlines():
                if line.strip():
                    frame=json.loads(line);assert isinstance(frame,dict) and frame.get('jsonrpc')=='2.0'
            assert password_before.decode().strip() not in wirelog.read_text()
            assert password_before.decode().strip() not in errlog.read_text()
        assert source.read_text()=='Unicode 文件内容\n'
    return {'official_stdio_auto':True,'official_stdio_legacy_initialize':True,'local_roundtrip':True,'provider_calls':0,'model_calls':0}

if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--installed-command':
        print(json.dumps(asyncio.run(installed_gate(sys.argv[2]))))
    else: unittest.main()
