"""Execute the actual release block with inert Git and GitHub commands."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


class ReleaseWorkflowTests(unittest.TestCase):
    def test_release_tag_guard(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / '.github/workflows/build.yml').read_text(encoding='utf-8')
        block = textwrap.dedent(workflow.split('        run: |\n')[-1])
        version = re.search(r'^VERSION = "(.+?)"', (root / 'teacut.py').read_text(encoding='utf-8'), re.M).group(1)
        tag = 'v' + version
        build, other, tag_object = 'a' * 40, 'b' * 40, 'c' * 40
        bash = shutil.which('bash')
        if os.name == 'nt':
            bash = str(Path(os.environ['ProgramFiles']) / 'Git/bin/bash.exe')
        self.assertTrue(bash and Path(bash).is_file(), 'Bash required for workflow verification')
        prelude = r'''
git() {
  printf '%s\n' "$@" >> git.log
  if [[ "$*" == 'rev-parse HEAD' ]]; then
    printf '%s\n' "$TEST_BUILD"
  elif [[ "$#" == 5 && "$1" == ls-remote && "$2" == --exit-code && "$3" == origin && "$4" == "refs/tags/$TEST_TAG" && "$5" == "refs/tags/$TEST_TAG^{}" ]]; then
    printf '%s' "$TEST_REFS"
    return "$TEST_REMOTE_STATUS"
  else
    return 99
  fi
}
gh() {
  printf '%s\n' "$@" >> gh.log
  [[ "$1 $2" == 'release create' ]]
}
python() { "$TEST_PYTHON" "$@"; }
'''
        cases = [
            ('lightweight-match', f'{build}\trefs/tags/{tag}\n', 0, 'push', f'refs/tags/{tag}', tag, 0),
            ('lightweight-mismatch', f'{other}\trefs/tags/{tag}\n', 0, 'push', f'refs/tags/{tag}', tag, 1),
            ('annotated-match', f'{tag_object}\trefs/tags/{tag}\n{build}\trefs/tags/{tag}^{{}}\n', 0, 'push', f'refs/tags/{tag}', tag, 0),
            ('annotated-mismatch', f'{build}\trefs/tags/{tag}\n{other}\trefs/tags/{tag}^{{}}\n', 0, 'push', f'refs/tags/{tag}', tag, 1),
            ('absent', '', 2, 'workflow_dispatch', 'refs/heads/main', 'main', 0),
            ('remote-failure', '', 128, 'workflow_dispatch', 'refs/heads/main', 'main', 128),
            ('partial-remote-failure', f'{build}\trefs/tags/{tag}\n', 128, 'workflow_dispatch', 'refs/heads/main', 'main', 128),
            ('source-tag-mismatch', '', 2, 'push', 'refs/tags/vwrong', 'vwrong', 1),
            ('manual-branch', '', 2, 'workflow_dispatch', 'refs/heads/feature', 'feature', 1),
        ]
        for name, refs, status, event, ref, ref_name, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                work = Path(folder)
                shutil.copyfile(root / 'teacut.py', work / 'teacut.py')
                env = dict(os.environ, TEST_BUILD=build, TEST_TAG=tag, TEST_REFS=refs,
                           TEST_REMOTE_STATUS=str(status), TEST_PYTHON=sys.executable,
                           GITHUB_EVENT_NAME=event, GITHUB_REF=ref, GITHUB_REF_NAME=ref_name,
                           GITHUB_SHA=other)
                result = subprocess.run([bash, '--noprofile', '--norc', '-c', prelude + block],
                                        cwd=work, env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                gh_args = (work / 'gh.log').read_text().splitlines() if (work / 'gh.log').exists() else []
                if expected == 0:
                    self.assertEqual(gh_args, ['release', 'view', tag, 'release', 'create', tag,
                                              'dist/TeaCut.exe', '--target', build, '--title',
                                              f'TeaCut {tag}', '--notes-file', 'notes.md', '--generate-notes'])
                    self.assertEqual((work / 'git.log').read_text().splitlines(),
                                     ['rev-parse', 'HEAD', 'ls-remote', '--exit-code', 'origin',
                                      f'refs/tags/{tag}', f'refs/tags/{tag}^{{}}'])
                else:
                    self.assertEqual(gh_args, [])


if __name__ == '__main__':
    unittest.main()
