# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""End-to-end helper to clone, patch, and build Vandium x ungoogled on Windows."""

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from subprocess import run

try:
    import winreg  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - not on non-Windows
    winreg = None

# Python modules depot_tools expects when VPYTHON is bypassed
REQUIRED_PY_MODULES = (
    "httplib2",
    "socks",      # PySocks provides httplib2.socks
    "colorama",
    "requests",
)


STATE_VERSION = 1
STATEFUL_STEPS = ("clone", "prune_binaries", "patches", "domain_substitution")
STEP_TO_INDEX = {name: index for index, name in enumerate(STATEFUL_STEPS)}


def run_cmd(cmd, cwd=None, env=None):
    print('[win-build] Executing:', ' '.join(cmd))
    run(cmd, check=True, cwd=cwd, env=env)


def _safe_filename_stem(path: Path) -> str:
    stem = path.name or "checkout"
    return ''.join(char if char.isalnum() else '_' for char in stem)


def _state_path(state_dir: Path, output: Path) -> Path:
    resolved = output.resolve(strict=False)
    digest = hashlib.sha256(str(resolved).encode('utf-8')).hexdigest()[:12]
    return state_dir / f'{_safe_filename_stem(resolved)}_{digest}.json'


class BuildState:
    def __init__(self, path: Path, metadata: dict[str, str]):
        self.path = path
        self.metadata = metadata
        self.state: dict[str, object] = {
            'version': STATE_VERSION,
            'metadata': metadata,
            'completed_steps': []
        }
        self._load()

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f'[win-build] Warning: ignoring corrupt state file {self.path}: {exc}')
            self._remove_file()
            return
        if data.get('version') != STATE_VERSION or data.get('metadata') != self.metadata:
            self._remove_file()
            return
        completed = data.get('completed_steps')
        if isinstance(completed, list):
            self.state['completed_steps'] = [step for step in completed if step in STEP_TO_INDEX]
        else:
            self._remove_file()

    def _remove_file(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.state, indent=2), encoding='utf-8')
        except OSError as exc:
            print(f'[win-build] Warning: failed to write build state to {self.path}: {exc}')

    def has_completed(self, step: str) -> bool:
        return step in self.state['completed_steps']

    def invalidate_from(self, step: str) -> None:
        step_index = STEP_TO_INDEX[step]
        completed = [
            existing for existing in self.state['completed_steps']
            if STEP_TO_INDEX.get(existing, -1) < step_index
        ]
        if len(completed) != len(self.state['completed_steps']):
            self.state['completed_steps'] = completed
            self._write()

    def mark_complete(self, step: str) -> None:
        self.invalidate_from(step)
        if step not in self.state['completed_steps']:
            self.state['completed_steps'].append(step)
            self._write()

def find_patch_binary() -> Path | None:
    candidate = shutil.which('patch')
    if candidate:
        return Path(candidate)
    for env_var in ('ProgramFiles', 'ProgramFiles(x86)'):
        base = os.environ.get(env_var)
        if not base:
            continue
        git_patch = Path(base) / 'Git' / 'usr' / 'bin' / 'patch.exe'
        if git_patch.exists():
            return git_patch
    repo_patch = Path(__file__).resolve().parent / 'third_party' / 'patch' / 'patch.exe'
    if repo_patch.exists():
        return repo_patch
    return None

def ensure_visual_studio():
    if os.name != 'nt':
        raise SystemExit('This helper is intended for Windows hosts only.')
    if not any(key in os.environ for key in ('VCINSTALLDIR', 'VSINSTALLDIR')):
        print('[win-build] Warning: Visual Studio environment variables not detected.\n'
              '           Run this script from a Developer Command Prompt or ensure MSVC is configured.')


def ensure_pip():
    try:
        import pip  # noqa: F401
    except ModuleNotFoundError:
        import ensurepip
        print('[win-build] Bootstrapping pip via ensurepip...')
        ensurepip.bootstrap()
    else:
        run_cmd([sys.executable, '-m', 'pip', 'install', '--upgrade', 'pip', 'setuptools', 'wheel'])




def ensure_python3_alias(repo_root: Path):
    if os.name != 'nt':
        return
    exe_path = Path(sys.executable).resolve()
    shim_created = False
    if exe_path.name.lower() == 'python.exe':
        alias_path = exe_path.with_name('python3.exe')
        if not alias_path.exists():
            try:
                shutil.copy2(exe_path, alias_path)
            except OSError as exc:
                print(f'[win-build] Warning: could not create python3.exe shim beside python.exe ({exc}).')
            else:
                print(f'[win-build] Created python3.exe shim alongside {exe_path}.')
                shim_created = True
    shim_cmd = repo_root / 'python3.cmd'
    if not shim_cmd.exists():
        try:
            shim_cmd.write_text(f'@"{exe_path}" %*', encoding='utf-8')
        except OSError as exc:
            print(f'[win-build] Warning: could not write python3.cmd shim ({exc}).')
        else:
            print(f'[win-build] Created python3.cmd shim at {shim_cmd}.')
            shim_created = True
    if shim_created:
        current = os.environ.get('PATH', '')
        repo_str = str(repo_root)
        if repo_str not in current.split(os.pathsep):
            os.environ['PATH'] = repo_str + os.pathsep + current if current else repo_str
def ensure_python_modules():
    missing = []
    for module in REQUIRED_PY_MODULES:
        try:
            __import__(module)
        except ModuleNotFoundError:
            missing.append(module)
    if not missing:
        return
    print(f"[win-build] Installing required Python modules: {', '.join(missing)}")
    run_cmd([sys.executable, '-m', 'pip', 'install', *missing])


def _path_contains(path_value: str, needle: str) -> bool:
    entries = [entry.strip() for entry in path_value.split(os.pathsep) if entry.strip()]
    return any(entry.lower() == needle.lower() for entry in entries)


def ensure_depot_tools_path(depot_tools: Path):
    depot_str = str(depot_tools)
    if not depot_tools.exists():
        return

    current = os.environ.get('PATH', '')
    if not _path_contains(current, depot_str):
        os.environ['PATH'] = depot_str + os.pathsep + current if current else depot_str

    if not winreg:
        return

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment', 0, winreg.KEY_READ) as key:
            existing, _ = winreg.QueryValueEx(key, 'Path')
    except FileNotFoundError:
        existing = ''

    if _path_contains(existing, depot_str):
        return

    new_path = existing + (';' if existing and not existing.endswith(';') else '') + depot_str
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment', 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, 'Path', 0, winreg.REG_EXPAND_SZ, new_path)
    except OSError as exc:
        print(f'[win-build] Warning: Failed to update user PATH in registry: {exc}')
        return

    try:
        run(['setx', 'PATH', new_path], check=True)
    except Exception as exc:  # pragma: no cover - defensive
        print(f'[win-build] Warning: setx PATH failed ({exc}). You may need to restart manually.')
    else:
        print('[win-build] Added depot_tools to user PATH (restart terminals to use gclient globally).')


def main():
    parser = argparse.ArgumentParser(
        description='Clone Chromium, apply Vandium x ungoogled patches, and build on Windows.')
    parser.add_argument('-o', '--output', type=Path, default=Path('chromium'),
                        help='Chromium checkout directory (default: %(default)s)')
    parser.add_argument('--pgo', default='win64',
                        choices=('win32', 'win64', 'win-arm64'),
                        help='PGO profile to fetch during clone (default: %(default)s)')
    parser.add_argument('--gn-dir', default='out/Vandium',
                        help='GN output directory relative to the checkout (default: %(default)s)')
    parser.add_argument('--targets', nargs='+', default=['chrome'],
                        help='Ninja targets to build (default: %(default)s)')
    parser.add_argument('--skip-clone', action='store_true',
                        help='Skip cloning if the checkout already exists and is up to date.')
    parser.add_argument('--skip-build', action='store_true',
                        help='Skip GN/Ninja build steps.')
    parser.add_argument('--gn-args', type=Path,
                        help='Custom args.gn template to copy instead of flags.gn.')

    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    utils_dir = repo_root / 'utils'
    build_dir = repo_root / 'build'
    build_dir.mkdir(exist_ok=True)

    chromium_version = (repo_root / 'chromium_version.txt').read_text(encoding='utf-8').strip()
    patch_revision = (repo_root / 'revision.txt').read_text(encoding='utf-8').strip()
    print(f'[win-build] Target Chromium: {chromium_version} (Vanadium revision {patch_revision})')

    state_dir = build_dir / 'win_build_state'
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = _state_path(state_dir, args.output)
    state = BuildState(state_path, {
        'chromium_version': chromium_version,
        'patch_revision': patch_revision,
        'pgo_profile': args.pgo,
    })

    ensure_visual_studio()
    ensure_pip()
    ensure_python3_alias(repo_root)
    ensure_python_modules()

    patch_bin = find_patch_binary()
    if not patch_bin:
        print("[win-build] Warning: Could not locate a GNU patch binary. Install Git for Windows or provide PATCH_BIN.")

    checkout_git = args.output / '.git'

    # Step 1: Clone Chromium sources (unless skipped)
    if args.skip_clone:
        print('[win-build] Skipping clone step per user request.')
    elif not state.has_completed('clone') or not checkout_git.exists():
        state.invalidate_from('clone')
        run_cmd([sys.executable, str(utils_dir / 'clone.py'), '-o', str(args.output), '-p', args.pgo])
        if not checkout_git.exists():
            raise SystemExit(f'Chromium checkout missing at {args.output} after clone step.')
        state.mark_complete('clone')
    else:
        print('[win-build] Chromium checkout already prepared for this release; skipping clone.')

    if not checkout_git.exists():
        raise SystemExit(f'Chromium checkout not found at {args.output}. Run without --skip-clone to initialize it.')

    # Step 2: Prune binaries
    if state.has_completed('prune_binaries'):
        print('[win-build] Skipping binary pruning; already pruned for this release.')
    else:
        state.invalidate_from('prune_binaries')
        run_cmd([sys.executable, str(utils_dir / 'prune_binaries.py'),
                 str(args.output), str(repo_root / 'pruning.list')])
        state.mark_complete('prune_binaries')

    # Step 3: Apply patches
    if state.has_completed('patches'):
        print('[win-build] Skipping patch application; already applied for this release.')
    else:
        state.invalidate_from('patches')
        patch_env = os.environ.copy()
        if patch_bin:
            patch_env['PATCH_BIN'] = str(patch_bin)
        run_cmd([sys.executable, str(utils_dir / 'patches.py'), 'apply',
                 str(args.output), str(repo_root / 'patches')], env=patch_env)
        state.mark_complete('patches')

    # Step 4: Domain substitution cache
    domsub_cache = build_dir / 'domsubcache.tar.gz'
    if state.has_completed('domain_substitution'):
        print('[win-build] Skipping domain substitution cache; already generated for this release.')
    else:
        state.invalidate_from('domain_substitution')
        if domsub_cache.exists():
            domsub_cache.unlink()
        run_cmd([sys.executable, str(utils_dir / 'domain_substitution.py'), 'apply',
                 '-r', str(repo_root / 'domain_regex.list'),
                 '-f', str(repo_root / 'domain_substitution.list'),
                 '-c', str(domsub_cache), str(args.output)])
        state.mark_complete('domain_substitution')

    if args.skip_build:
        print('[win-build] Build step skipped. Chromium tree prepared with patches applied.')
        return

    # Step 5: Configure environment for build tools
    depot_tools = args.output / 'uc_staging' / 'depot_tools'
    if not depot_tools.exists():
        raise SystemExit(f'Depot_tools not found at {depot_tools}. Did clone.py finish successfully?')

    env = os.environ.copy()
    env.setdefault('DEPOT_TOOLS_WIN_TOOLCHAIN', '0')
    env['PATH'] = str(depot_tools) + os.pathsep + env.get('PATH', '')
    ensure_depot_tools_path(depot_tools)

    # Step 6: Prepare GN output directory and args
    gn_dir = args.output / args.gn_dir.replace('/', os.sep).replace('\\', os.sep)
    gn_dir.mkdir(parents=True, exist_ok=True)
    args_template = args.gn_args if args.gn_args else (repo_root / 'flags.gn')
    shutil.copy(args_template, gn_dir / 'args.gn')
    print(f'[win-build] Copied GN args from {args_template} to {gn_dir / "args.gn"}')

    # Step 7: Generate build files with GN
    run_cmd(['gn', 'gen', str(gn_dir), '--fail-on-unused-args'], cwd=args.output, env=env)

    # Step 8: Build using Ninja
    ninja_cmd = ['ninja', '-C', str(gn_dir)] + args.targets
    run_cmd(ninja_cmd, cwd=args.output, env=env)

    print('[win-build] Build completed successfully.')


if __name__ == '__main__':
    main()

