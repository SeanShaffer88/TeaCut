import io
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import teacut


class SafetyTests(unittest.TestCase):
    def test_publication_collision_and_failure_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            dst = Path(folder) / 'new' / 'out.mkv'
            engine = teacut.Engine('mock')
            def write(args, *unused):
                stage = Path(args[-1])
                self.assertEqual(stage.suffix, '.mkv')
                self.assertNotEqual(stage, dst)
                stage.write_bytes(b'finished')
            with patch.object(engine, '_run', side_effect=write), patch('teacut._looks_ok', return_value=True):
                engine.run(['-i', 'src', str(dst)], teacut.Job())
                self.assertEqual(dst.read_bytes(), b'finished')
                dst.write_bytes(b'competitor')
                with self.assertRaisesRegex(teacut.TeaCutError, 'Cannot publish'):
                    engine.run([str(dst)], teacut.Job())
                self.assertEqual(dst.read_bytes(), b'competitor')
                self.assertEqual(list(dst.parent.iterdir()), [dst])
            with patch.object(engine, '_run', side_effect=teacut.Cancelled()):
                with self.assertRaises(teacut.Cancelled):
                    engine.run([str(dst)], teacut.Job())
            self.assertEqual(list(dst.parent.iterdir()), [dst])

    def test_competing_writer_at_commit_and_unsupported_link(self):
        with tempfile.TemporaryDirectory() as folder:
            dst = Path(folder) / 'out.mp4'
            engine = teacut.Engine('mock')
            def race(args, *unused):
                Path(args[-1]).write_bytes(b'ours')
                dst.write_bytes(b'theirs')
            with patch.object(engine, '_run', side_effect=race), patch('teacut._looks_ok', return_value=True):
                with self.assertRaises(teacut.TeaCutError):
                    engine.run([str(dst)], teacut.Job())
            self.assertEqual(dst.read_bytes(), b'theirs')
            dst.unlink()
            with patch.object(engine, '_run', side_effect=lambda args, *a: Path(args[-1]).write_bytes(b'ours')), patch('teacut._looks_ok', return_value=True), patch('teacut.os.link', side_effect=OSError('unsupported')):
                with self.assertRaises(teacut.TeaCutError):
                    engine.run([str(dst)], teacut.Job())
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_keep_remove_create_parents(self):
        engine = teacut.Engine('mock')
        info = teacut.MediaInfo('src'); info.duration = 10
        with tempfile.TemporaryDirectory() as folder, patch.object(engine, 'probe', return_value=info), patch.object(engine, '_cut_piece'), patch.object(engine, '_concat_copy'):
            for operation in ('keep', 'remove', 'cut'):
                dst = Path(folder) / operation / 'new' / 'out.mkv'
                if operation == 'cut':
                    engine.cut('src', str(dst), 2, 4, teacut.Job(), remove=True)
                else:
                    engine.cut_segments('src', str(dst), [(2, 3), (5, 6)], teacut.Job(), remove=operation == 'remove')
                self.assertTrue(dst.parent.is_dir())
                self.assertEqual(list(dst.parent.iterdir()), [])

    def test_cancel_during_child_assignment_reaps_child(self):
        job = teacut.Job()
        class Process:
            stdout = io.StringIO('')
            stderr = io.StringIO('')
            returncode = None
            killed = False
            waited = False
            def kill(self): self.killed = True; self.returncode = -9
            def wait(self): self.waited = True; return self.returncode
            def poll(self): return self.returncode
        proc = Process()
        def create(*args, **kw): job.cancel(); return proc
        with tempfile.TemporaryDirectory() as folder, patch('teacut.subprocess.Popen', side_effect=create):
            with self.assertRaises(teacut.Cancelled):
                teacut.Engine('mock').run([str(Path(folder) / 'out.mkv')], job)
            self.assertEqual(list(Path(folder).iterdir()), [])
        self.assertTrue(proc.killed and proc.waited)
        self.assertIsNone(job.proc)

    def test_preview_deadline(self):
        with patch('teacut.subprocess.run', side_effect=subprocess.TimeoutExpired('mock', 5)) as run:
            self.assertIsNone(teacut.extract_frame('mock', 'src', 'dst'))
            self.assertEqual(run.call_args.kwargs['timeout'], 5)

    def test_preview_coalesces_and_cleans_after_stop(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def extract(ff, src, dst, at, width):
            calls.append(src)
            if src == 'old': started.set(); release.wait(2)
            Path(dst).write_bytes(b'png')
            if src == 'latest': finished.set()
            return dst
        with patch('teacut.extract_frame', side_effect=extract):
            worker = teacut.PreviewWorker('mock')
            try:
                worker.request('widget', 'old'); self.assertTrue(started.wait(2))
                worker.request('widget', 'middle'); worker.request('widget', 'latest')
                release.set(); self.assertTrue(finished.wait(2))
            finally:
                release.set(); worker.stop()
            self.assertEqual(calls, ['old', 'latest'])
            self.assertFalse(worker.thread.is_alive())
            self.assertFalse(os.path.exists(worker.directory))

    def test_preview_stale_result_and_shutdown_inflight(self):
        started, release = threading.Event(), threading.Event()
        def extract(ff, src, dst, at, width):
            started.set()
            release.wait(2)
            Path(dst).write_bytes(b'png')
            return dst
        with patch('teacut.extract_frame', side_effect=extract):
            worker = teacut.PreviewWorker('mock')
            worker.request('widget', 'old')
            self.assertTrue(started.wait(2))
            worker.request('widget', 'new')
            stopper = threading.Thread(target=worker.stop)
            stopper.start()
            self.assertTrue(os.path.isdir(worker.directory))
            release.set()
            stopper.join(2)
            self.assertFalse(stopper.is_alive())
            self.assertFalse(os.path.exists(worker.directory))
            self.assertEqual(worker.take(), [])

    def test_invalid_output_is_not_published(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = teacut.Engine('mock')
            dst = Path(folder) / 'out.mkv'
            with patch.object(engine, '_run', side_effect=lambda args, *a: Path(args[-1]).write_bytes(b'bad')), patch('teacut._looks_ok', return_value=False):
                with self.assertRaises(teacut.TeaCutError):
                    engine.run([str(dst)], teacut.Job())
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_split_preserves_finished_output(self):
        engine = teacut.Engine('mock'); info = teacut.MediaInfo('src.mkv'); info.duration = 10
        with tempfile.TemporaryDirectory() as folder, patch.object(engine, 'probe', return_value=info):
            def piece(src, dst, *args):
                if Path(dst).name.endswith('02.mkv'): raise teacut.Cancelled()
                Path(dst).write_bytes(b'finished')
            with patch.object(engine, '_cut_piece', side_effect=piece):
                with self.assertRaises(teacut.Cancelled): engine.split('src.mkv', folder, 2, teacut.Job())
            self.assertEqual((Path(folder) / 'src_part01.mkv').read_bytes(), b'finished')


if __name__ == '__main__':
    unittest.main()
