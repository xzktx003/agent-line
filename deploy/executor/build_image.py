#!/usr/bin/env python3
"""Extract a minimal executor root filesystem from an explicit local image."""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SOURCE_IMAGE_RE = re.compile(r'^sha256:[0-9a-f]{64}$')
IMAGE_ID_RE = re.compile(r'^sha256:[0-9a-f]{64}$')
TOOL_POLICY = (
    'explicit POSIX tools + git HTTPS + Python3.12 standard library; '
    'no package manager, curl, compiler, GPU tools, Docker client'
)

EXTRACT_SCRIPT = r'''
import os,pathlib,re,shutil,subprocess,sys,tarfile,tempfile
if sys.version_info[:2] != (3,12) or not pathlib.Path('/usr/lib/python3.12').is_dir():
 raise RuntimeError('Source requires Python3.12 and /usr/lib/python3.12')
root=pathlib.Path(tempfile.mkdtemp())
copied=set()
def copy(path):
 p=pathlib.Path(path)
 if str(p) in copied or not p.is_file():return
 copied.add(str(p));dst=root/str(p).lstrip('/');dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dst)
 output=subprocess.run(['ldd',str(p)],capture_output=True,text=True).stdout
 for dep in re.findall(r'(/[\w./+\-]+)',output):
  if pathlib.Path(dep).is_file():copy(dep)
for tool in 'bash sh env mkdir touch cat printf ls pwd git python3 rm cp mv grep sed find head tail wc chmod stat test sleep tar timeout xargs true false uname readlink dirname sort diff cmp id'.split():
 path=shutil.which(tool)
 if not path:raise RuntimeError('Missing '+tool)
 copy(path)
 if tool in ('sh','bash'):copy('/bin/'+tool)
for path in pathlib.Path('/usr/lib/python3.12').rglob('*'):
 if path.is_file() and '__pycache__' not in path.parts:copy(path)
for path in ['/usr/lib/git-core/git-remote-http','/usr/lib/git-core/git-remote-https','/etc/ssl/certs/ca-certificates.crt','/etc/nsswitch.conf']:
 copy(path)
for path in ('tmp','workspace','codex-home','root','etc','usr/local/bin'):(root/path).mkdir(parents=True,exist_ok=True)
(root/'tmp').chmod(0o1777)
with tarfile.open(fileobj=__import__('sys').stdout.buffer,mode='w|') as tar:
 for path in sorted(root.iterdir()):tar.add(path,arcname=path.name)
'''


class QuietArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        raise ValueError('arguments')


def _parser():
    parser = QuietArgumentParser(description=__doc__)
    parser.add_argument('--source-image', required=True,
                        help='完整的本地 sha256 镜像 ID')
    parser.add_argument('--output', required=True,
                        help='仓库 var 内尚不存在的输出目录')
    return parser


def _output_parts(raw):
    var_root = ROOT / 'var'
    if not var_root.is_dir():
        raise ValueError('output')
    requested = Path(raw)
    if not requested.is_absolute():
        requested = ROOT / requested
    normalized = Path(os.path.abspath(requested))
    relative = normalized.relative_to(var_root)
    parts = relative.parts
    if not parts or any(part in ('', '.', '..') for part in parts):
        raise ValueError('output')
    return parts


def _check_directory(fd):
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022):
        raise ValueError('output_directory_ownership')


def _open_dir_at(parent_fd, name):
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        _check_directory(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _create_output(parts):
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(ROOT, flags)
    try:
        _check_directory(fd)
        next_fd = _open_dir_at(fd, 'var')
        os.close(fd)
        fd = next_fd
        for name in parts[:-1]:
            next_fd = _open_dir_at(fd, name)
            os.close(fd)
            fd = next_fd
        os.mkdir(parts[-1], mode=0o700, dir_fd=fd)
        output_fd = _open_dir_at(fd, parts[-1])
        os.fchmod(output_fd, 0o700)
        return output_fd, ROOT / 'var' / Path(*parts)
    finally:
        os.close(fd)


def _private_file(directory_fd, name):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    return os.fdopen(fd, 'w', encoding='utf-8')


def _run_logged(directory_fd, phase, command, *, archive_fd=None):
    with _private_file(directory_fd, phase + '.stdout.log') as stdout_log, \
            _private_file(directory_fd, phase + '.stderr.log') as stderr_log:
        if archive_fd is None:
            completed = subprocess.run(command, stdout=stdout_log, stderr=stderr_log,
                                       check=False, shell=False)
        else:
            with os.fdopen(archive_fd, 'wb', closefd=False) as archive:
                completed = subprocess.run(command, stdout=archive, stderr=stderr_log,
                                           check=False, shell=False)
                archive.flush()
                os.fsync(archive.fileno())
        stdout_log.flush()
        stderr_log.flush()
        os.fsync(stdout_log.fileno())
        os.fsync(stderr_log.fileno())
    return completed.returncode


def _logged_text(directory_fd, name, maximum=256):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(name, flags, dir_fd=directory_fd)
    try:
        value = os.read(fd, maximum + 1)
    finally:
        os.close(fd)
    if len(value) > maximum:
        raise ValueError('command_output')
    return value.decode('ascii').strip()


def _save_json(directory_fd, name, value):
    payload = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _source_id(directory_fd, source_image):
    command = ['docker', 'image', 'inspect', '--format={{.Id}}', source_image]
    if _run_logged(directory_fd, 'inspect', command) != 0:
        raise RuntimeError('inspect')
    image_id = _logged_text(directory_fd, 'inspect.stdout.log')
    if not IMAGE_ID_RE.fullmatch(image_id) or image_id != source_image:
        raise RuntimeError('inspect_identity')
    return image_id


def _build(directory_fd, output_path, source_image):
    image_id = _source_id(directory_fd, source_image)
    archive_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    archive_fd = os.open('minimal-rootfs.tar', archive_flags, 0o600, dir_fd=directory_fd)
    command = [
        'docker', 'run', '--rm', '--network', 'none', '--read-only',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--tmpfs', '/tmp:rw,nosuid,nodev', '--env', 'PYTHONDONTWRITEBYTECODE=1',
        '--entrypoint', 'python3', image_id, '-c', EXTRACT_SCRIPT,
    ]
    try:
        if _run_logged(directory_fd, 'extract', command, archive_fd=archive_fd) != 0:
            raise RuntimeError('extract')
    finally:
        os.close(archive_fd)

    if _run_logged(directory_fd, 'import',
                   ['docker', 'import', str(output_path / 'minimal-rootfs.tar')]) != 0:
        raise RuntimeError('import')
    imported_id = _logged_text(directory_fd, 'import.stdout.log')
    if not IMAGE_ID_RE.fullmatch(imported_id):
        raise RuntimeError('import_identity')
    record = {'sourceImage': image_id, 'image': imported_id, 'toolPolicy': TOOL_POLICY}
    _save_json(directory_fd, 'image-build.json', record)
    return record


def main(argv=None):
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    except ValueError:
        print('失败阶段: 参数校验')
        return 2

    if not SOURCE_IMAGE_RE.fullmatch(args.source_image):
        print('失败阶段: 镜像标识校验')
        return 2
    try:
        output_parts = _output_parts(args.output)
    except (ValueError, OSError):
        print('失败阶段: 输出目录校验')
        return 2

    output_fd = None
    phase = '输出目录创建'
    try:
        output_fd, output_path = _create_output(output_parts)
        phase = '镜像提取或导入'
        record = _build(output_fd, output_path, args.source_image)
        print(json.dumps({'status': 'complete', 'image': record['image']}, ensure_ascii=False))
        return 0
    except RuntimeError as exc:
        print('失败阶段: ' + str(exc))
        return 1
    except Exception:
        print('失败阶段: ' + phase)
        return 1
    finally:
        if output_fd is not None:
            os.close(output_fd)


if __name__ == '__main__':
    raise SystemExit(main())
