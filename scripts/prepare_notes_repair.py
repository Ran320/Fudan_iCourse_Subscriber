"""Prepare a targeted retry of this semester's invalid/missing notes.

The calling workflow backs up the original encrypted data branch first.
Historical courses and valid notes are left untouched.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.audit_notes import model_probe
from src.ai.content_quality import invalid_summary_reason, transcript_has_content
from src.data.database import Database
from src.runtime import config


def prepare(db: Database, course_ids: list[str]) -> dict:
    result = {'invalid_notes': [], 'missing_notes': []}
    with db._lock, db.conn:
        for course_id in course_ids:
            rows = db.conn.execute('SELECT * FROM lectures WHERE course_id = ?', (course_id,)).fetchall()
            for record in rows:
                row = dict(record)
                bad = invalid_summary_reason(row.get('summary'))
                if row.get('summary') and not bad:
                    continue
                result['invalid_notes' if bad else 'missing_notes'].append(row['sub_id'])
                transcript = row.get('transcript')
                if bad or not transcript_has_content(transcript):
                    transcript = None
                db.conn.execute('''UPDATE lectures SET transcript = ?, summary = NULL,
                    summary_model = NULL, processed_at = NULL, emailed_at = NULL,
                    error_count = 0, error_stage = 'repair_pending',
                    error_msg = 'Queued for source recovery and note regeneration'
                    WHERE sub_id = ?''', (transcript, row['sub_id']))
    return result


def report_status(db: Database, course_ids: list[str]) -> list[dict]:
    report = []
    for course_id in course_ids:
        rows = [dict(row) for row in db.conn.execute('SELECT * FROM lectures WHERE course_id = ?', (course_id,))]
        course = db.conn.execute('SELECT title FROM courses WHERE course_id = ?', (course_id,)).fetchone()
        report.append({'course_id': course_id, 'title': course[0] if course else course_id,
                       'notes': sum(bool(row.get('summary')) for row in rows),
                       'latest_note': max((row.get('sub_title', '')[:10] for row in rows if row.get('summary')), default=''),
                       'outstanding': [{'sub_id': row['sub_id'], 'title': row['sub_title'],
                                        'stage': row.get('error_stage'), 'reason': row.get('error_msg')}
                                       for row in rows if not row.get('summary')]})
    return report


if __name__ == '__main__':
    if '--report-only' in sys.argv:
        database = Database()
        print('REPAIR_STATUS', json.dumps(report_status(database, config.COURSE_IDS), ensure_ascii=False))
        database.conn.close()
        raise SystemExit(0)
    models = model_probe()
    print('MODEL_PREFLIGHT', json.dumps(models, ensure_ascii=False))
    if not models['working']:
        raise SystemExit('No usable summary model; no database records were changed.')
    overrides = {item['provider']: [item['model']] for item in models['working']}
    if os.environ.get('GITHUB_ENV'):
        with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as stream:
            stream.write('ICS_MODEL_OVERRIDES=' + json.dumps(overrides) + '\n')
    database = Database()
    result = prepare(database, config.COURSE_IDS)
    database.conn.close()
    print('REPAIR_QUEUED', json.dumps(result, ensure_ascii=False))
