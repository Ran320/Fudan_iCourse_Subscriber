import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from src.ai.content_quality import InsufficientContentError, invalid_summary_reason, transcript_has_content
from src.data.database import Database
from scripts.prepare_notes_repair import prepare


SPEECH = ('操作系统负责协调处理器内存文件存储设备的访问权限，进程调度根据优先级选择任务，'
          '虚拟地址通过页表映射物理地址，中断机制保存寄存器现场并在处理结束后恢复执行。') * 5


class ContentTests(unittest.TestCase):
    def test_noise_and_diagnostic_markers_are_not_teaching_content(self):
        self.assertFalse(transcript_has_content('The. 嗯。' * 100))
        self.assertFalse(transcript_has_content('[注意：从 11 分钟处起已超过 30 分钟未检测到语音，音频可能已中断或录音设备出现故障。]'))
        self.assertTrue(transcript_has_content(SPEECH))

    def test_invalid_disclaimer_is_distinct_from_partial_notes(self):
        self.assertTrue(invalid_summary_reason('本次提供的录音文本确认为无效数据，不包含实际的教学内容。'))
        self.assertIsNone(invalid_summary_reason('录音有少量杂音。### 内存管理\n' + SPEECH))
        self.assertIsNone(invalid_summary_reason('### 材料范围说明\n本节仅依据PPT整理。\n' + SPEECH))

    def test_repair_scope_preserves_good_notes_and_historical_courses(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(str(Path(directory) / 'test.sqlite'))
            for cid in ('current', 'historical'):
                db.upsert_course(cid, cid, '')
            rows = [('bad', 'current', '确认为无效数据，不包含教学内容。', 'The. ' * 50),
                    ('good', 'current', SPEECH, SPEECH),
                    ('missing', 'current', None, SPEECH),
                    ('old', 'historical', '确认为无效数据，不包含教学内容。', 'The. ')]
            for sid, cid, summary, transcript in rows:
                db.insert_lecture(sid, cid, sid, '2026-10-01')
                db.update_transcript(sid, transcript)
                if summary:
                    db.update_summary(sid, summary, 'test')
                db.mark_processed(sid)
            result = prepare(db, ['current'])
            self.assertEqual(result['invalid_notes'], ['bad'])
            self.assertIsNone(db.get_lecture('bad')['summary'])
            self.assertIsNone(db.get_lecture('bad')['transcript'])
            self.assertIsNone(db.get_lecture('bad')['processed_at'])
            self.assertEqual(db.get_lecture('good')['summary'], SPEECH)
            self.assertEqual(db.get_lecture('missing')['transcript'], SPEECH)
            self.assertIsNotNone(db.get_lecture('old')['processed_at'])
            self.assertIsNotNone(db.get_lecture('old')['summary'])
            for _ in range(3):
                db.update_error('missing', 'transcribe', 'test')
            self.assertIn('missing', db.get_exhausted_sub_ids('current'))
            db.conn.close()


class RunnerTests(unittest.TestCase):
    def runner_class(self):
        modules = {}
        ppt = types.ModuleType('src.pipeline.ppt_pipeline')
        ppt.PPTPipeline = object
        modules[ppt.__name__] = ppt
        transcriber = types.ModuleType('src.ai.transcriber')
        transcriber.IncompleteAudioError = type('IncompleteAudioError', (Exception,), {})
        transcriber.NoAudioStreamError = type('NoAudioStreamError', (Exception,), {})
        modules[transcriber.__name__] = transcriber
        bucketer = types.ModuleType('src.ai.bucketer')
        bucketer.assemble = lambda transcript, segments, pages: ('\n'.join(page['text'] for page in pages), 'flat')
        modules[bucketer.__name__] = bucketer
        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location('runner_under_test', Path(__file__).parents[1] / 'src/pipeline/lecture_runner.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.LectureRunner

    def test_ppt_only_note_is_labelled_and_invalid_output_is_not_saved(self):
        cls = self.runner_class()
        runner = cls.__new__(cls)
        saved, errors, prompts = [], [], []
        runner._db = types.SimpleNamespace(
            get_done_ppt_pages=lambda _: [{'text': SPEECH}],
            update_summary=lambda *args: saved.append(args),
            update_error=lambda *args: errors.append(args),
        )
        runner._reporter = types.SimpleNamespace(info=lambda _: None)
        runner._summarizer = types.SimpleNamespace(summarize=lambda title, text: (prompts.append(text) or SPEECH, 'test'))
        note = runner._summarize('sub', 'course', 'The. ' * 100, None)
        self.assertTrue(note.startswith('### 材料范围说明'))
        self.assertNotIn('The.', prompts[0])
        saved.clear()
        runner._summarizer = types.SimpleNamespace(summarize=lambda *_: ('确认为无效数据，不包含教学内容。', 'test'))
        with self.assertRaises(InsufficientContentError):
            runner._summarize('sub', 'course', SPEECH, None)
        self.assertEqual(saved, [])
        self.assertEqual(errors[-1][1], 'content_quality')


if __name__ == '__main__':
    unittest.main()
