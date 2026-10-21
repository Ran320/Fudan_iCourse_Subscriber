"""Recover notes from already-cached source material before network backfill."""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ai.content_quality import ppt_has_content, transcript_has_content
from src.ai.summarizer import Summarizer
from src.data.database import Database
from src.pipeline.lecture_runner import LectureRunner
from src.runtime import config
from src.runtime.reporter import Reporter


def recover() -> dict:
    db = Database()
    runner = LectureRunner.__new__(LectureRunner)
    runner._db = db
    runner._summarizer = Summarizer()
    runner._reporter = Reporter()
    result = {'recovered': [], 'requires_school_source': [], 'failed': []}
    for course_id in config.COURSE_IDS:
        course = db.conn.execute('SELECT title FROM courses WHERE course_id = ?', (course_id,)).fetchone()
        title = course[0] if course else course_id
        rows = db.conn.execute('SELECT * FROM lectures WHERE course_id = ? AND summary IS NULL', (course_id,)).fetchall()
        for record in rows:
            row = dict(record)
            sub_id = row['sub_id']
            transcript = row.get('transcript') or ''
            if not transcript_has_content(transcript) and not ppt_has_content(db.get_done_ppt_pages(sub_id)):
                result['requires_school_source'].append(sub_id)
                continue
            try:
                runner._summarize(sub_id, title, transcript, None)
                db.mark_processed(sub_id)
                db.clear_error(sub_id)
                result['recovered'].append(sub_id)
                print(f'CACHED_NOTE_RECOVERED {course_id} {sub_id} {row["sub_title"]}', flush=True)
            except Exception as error:
                result['failed'].append({'sub_id': sub_id, 'error': str(error)[:500]})
    db.conn.close()
    return result


if __name__ == '__main__':
    print('CACHED_RECOVERY', json.dumps(recover(), ensure_ascii=False))
