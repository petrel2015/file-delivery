"""Offline wheel/install/console acceptance; business imports occur only in children."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv

PROJECT = Path(__file__).resolve().parents[2]


class InstallAcceptance(unittest.TestCase):
    _installation = None

    def command(self, argv, cwd, env, timeout=60):
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        self.assertEqual(result.returncode, 0, '\n'.join([str(argv), result.stdout, result.stderr]))
        return result

    def installation(self):
        cls = type(self)
        if cls._installation is not None:
            return cls._installation
        self.assertTrue((PROJECT / 'pyproject.toml').is_file(), 'AC-1: pyproject.toml is missing')
        # Diagnose verifier dependencies before invoking the candidate build backend.
        # Equivalent backends remain allowed: missing declared tools block this
        # verifier rather than asking the Worker to rewrite its backend choice.
        import importlib.metadata as metadata
        import tomllib
        for tool in ('pip', 'setuptools', 'wheel', 'packaging'):
            try:
                metadata.version(tool)
            except metadata.PackageNotFoundError:
                self.fail('HARNESS_BUILD_TOOLS_MISSING: ' + tool)
        from packaging.requirements import Requirement
        configuration = tomllib.loads((PROJECT / 'pyproject.toml').read_text())
        for text in configuration.get('build-system', {}).get('requires', []):
            requirement = Requirement(text)
            if requirement.marker and not requirement.marker.evaluate({'extra': ''}):
                continue
            try:
                version = metadata.version(requirement.name)
            except metadata.PackageNotFoundError:
                self.fail('HARNESS_BUILD_TOOLS_MISSING: ' + requirement.name)
            self.assertIn(version, requirement.specifier,
                          'HARNESS_BUILD_TOOLS_MISSING: incompatible ' + str(requirement))
        temporary = tempfile.TemporaryDirectory(prefix='fd-offline-install-')
        cls.addClassCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        source, wheels, outside, environment = [root / p for p in ('source', 'wheels', 'outside', 'venv')]
        source.mkdir()
        wheels.mkdir()
        outside.mkdir()
        env = {k: v for k, v in os.environ.items()
               if k not in ('PYTHONPATH', 'PYTHONHOME') and not k.startswith('PIP_')}
        env.update(PYTHONDONTWRITEBYTECODE='1', PIP_NO_INDEX='1', PIP_DISABLE_PIP_VERSION_CHECK='1',
                   PIP_CONFIG_FILE=os.devnull)
        # Build only a copied tracked snapshot; build/egg-info cannot dirty the candidate.
        listing = self.command(['git', '-C', str(PROJECT), 'ls-files', '-z'], outside, env).stdout
        for relative in filter(None, listing.split('\0')):
            original = PROJECT / relative
            self.assertFalse(original.is_symlink(), 'Controller snapshot contains symlink: ' + relative)
            destination = source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, destination)
        # No build-dependency downloads. Missing alternate backend dependencies
        # are verifier setup failures, not permission to demand a backend rewrite.
        self.command([sys.executable, '-m', 'pip', 'wheel', '--no-index', '--no-deps',
                      '--no-build-isolation', '--wheel-dir', str(wheels), str(source)], outside, env, 90)
        built = list(wheels.glob('*.whl'))
        self.assertEqual(len(built), 1, 'Expected one locally built distribution')
        venv.EnvBuilder(with_pip=True).create(environment)  # ensurepip uses bundled wheels.
        python = environment / 'bin/python'
        self.command([str(python), '-I', '-m', 'pip', 'install', '--no-index', '--no-deps', str(built[0])], outside, env)
        console = environment / 'bin/file-delivery'
        self.assertTrue(console.is_file(), 'AC-1: installed file-delivery console entry missing')
        cls._installation = (python, console, outside, env)
        return cls._installation

    def test_ac1_offline_install_console_help(self):
        _, console, outside, env = self.installation()
        for arguments in (['--help'], ['plan', '--help']):
            with self.subTest(arguments=arguments):
                result = self.command([str(console), *arguments], outside, env)
                self.assertTrue(result.stdout.strip(), 'Help must be visible')

    def test_ac1_installed_console_plans_outside_repository(self):
        _, console, outside, env = self.installation()
        inputs = outside / 'inputs'
        inputs.mkdir(exist_ok=True)
        source = inputs / '报告 one.txt'
        source.write_bytes(b'offline-install-smoke')
        result = self.command([str(console), 'plan', str(source), '--root', str(inputs), '--json'], outside, env)
        value = json.loads(result.stdout)
        self.assertEqual(value['status'], 'planned')
        self.assertEqual(value['file_count'], 1)
        self.assertEqual(value['files'][0]['path'], source.name)
        self.assertEqual(value['files'][0]['size_bytes'], len(source.read_bytes()))

    def test_ac1_installed_metadata_and_import_location(self):
        python, _, outside, env = self.installation()
        script = '''import importlib.metadata as m, json, pathlib, sys
import file_delivery
owners = m.packages_distributions().get("file_delivery", [])
assert len(owners) == 1, owners
d = m.distribution(owners[0])
print(json.dumps({"module": str(pathlib.Path(file_delivery.__file__).resolve()),
 "prefix": sys.prefix, "requires_python": d.metadata.get("Requires-Python"),
 "requires_dist": d.requires or []}))
'''
        value = json.loads(self.command([str(python), '-I', '-B', '-c', script], outside, env).stdout)
        self.assertTrue(Path(value['module']).is_relative_to(Path(value['prefix'])))
        self.assertFalse(Path(value['module']).is_relative_to(PROJECT))
        # These are controller build dependencies; no third-party business import.
        from packaging.requirements import Requirement
        from packaging.specifiers import SpecifierSet
        self.assertIsNotNone(value['requires_python'], 'Declare the supported Python versions')
        versions = SpecifierSet(value['requires_python'])
        self.assertIn('3.11', versions)
        self.assertIn('.'.join(map(str, sys.version_info[:3])), versions)
        for requirement in value['requires_dist']:
            parsed = Requirement(requirement)
            if parsed.marker is None or parsed.marker.evaluate({'extra': ''}):
                self.fail('AC-1: non-stdlib runtime dependency: ' + parsed.name)
