"""Audit existing notes and current recordings without modifying the database."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.runtime import config


def invalid_summary_reason(summary: str | None) -> str | None:
    if not summary:
        return None
    # A warning attached to substantive notes is not itself an invalid note.
    if len(summary) > 2500:
        return None
    patterns = (
        r"不包含.{0,30}(?:教学内容|实质性|实际的课程)",
        r"(?:确认为|主要为|主要由|均为|全部为).{0,20}(?:无效数据|无效噪声|无意义)",
        r"无法作为有效的复习材料",
        r"(?:无法|不能).{0,20}(?:生成|整理).{0,15}(?:课程笔记|课程总结|知识点)",
        r"不具备.{0,15}(?:教学|学习|复习)价值",
    )
    for pattern in patterns:
        if re.search(pattern, summary, re.S):
            return "summary_reports_unusable_source"
    return None


def model_probe() -> dict:
    from openai import OpenAI
    report = {"working": [], "failures": [], "available_models": {}}
    for provider in config.resolve_model_providers():
        client = OpenAI(api_key=provider['api_key'], base_url=provider['base_url'],
                        timeout=35, max_retries=0)
        configured = provider['models']
        available = []
        try:
            available = [model.id for model in client.models.list()]
            report['available_models'][provider['name']] = [
                model for model in available if 'qwen' in model.lower() or 'deepseek' in model.lower()
            ][:35]
        except Exception as error:
            report['failures'].append({'provider': provider['name'], 'stage': 'list_models',
                                       'error': str(error)[:700]})
        candidates = list(dict.fromkeys(configured + ([
            'Qwen/Qwen3.5-397B-A17B', 'Qwen/Qwen3-235B-A22B-Instruct-2507',
            'deepseek-ai/DeepSeek-V3.2',
        ] if provider['name'] == 'modelscope' else [])))
        for model in candidates:
            if available and model not in available:
                report['failures'].append({'provider': provider['name'], 'model': model,
                                           'error': 'Model absent from provider model list'})
                continue
            try:
                response = client.chat.completions.create(
                    model=model, messages=[{'role': 'user', 'content': 'Reply with OK only.'}],
                    max_tokens=128, timeout=35,
                )
                if not response.choices or not response.choices[0].message.content:
                    raise ValueError('Empty model response')
                report['working'].append({'provider': provider['name'], 'model': model})
                break
            except Exception as error:
                report['failures'].append({'provider': provider['name'], 'model': model,
                                           'error': str(error)[:700]})
    return report


def audit() -> dict:
    from src.api.webvpn import WebVPNSession
    from src.api.icourse import ICourseClient
    connection = sqlite3.connect(config.DB_PATH)
    connection.row_factory = sqlite3.Row
    report = {'models': model_probe(), 'courses': [], 'invalid_notes': [], 'errors': []}
    rows = [dict(row) for row in connection.execute('SELECT * FROM lectures')]
    current_ids = set(config.COURSE_IDS)
    rows = [row for row in rows if str(row['course_id']) in current_ids]
    for row in rows:
        reason = invalid_summary_reason(row.get('summary'))
        if reason:
            report['invalid_notes'].append({key: row.get(key) for key in
                                            ('course_id', 'sub_id', 'sub_title', 'date')}
                                          | {'reason': reason,
                                             'summary_chars': len(row['summary']),
                                             'transcript_chars': len(row.get('transcript') or '')})
    courses = {str(row['course_id']): dict(row) for row in
               connection.execute('SELECT * FROM courses') if str(row['course_id']) in current_ids}
    for course_id in config.COURSE_IDS:
        courses.setdefault(course_id, {'course_id': course_id, 'title': '', 'teacher': ''})
    vpn = None
    for attempt in range(5):
        try:
            vpn = WebVPNSession()
            vpn.login()
            vpn.authenticate_icourse()
            break
        except Exception as error:
            if attempt == 4:
                report['errors'].append('School login failed: ' + type(error).__name__ + ': ' + str(error)[:500])
                vpn = None
            else:
                time.sleep(5)
    if vpn:
        client = ICourseClient(vpn)
        for course_id, course in courses.items():
            stored = [row for row in rows if str(row['course_id']) == course_id]
            entry = dict(course)
            entry.update(stored_lectures=len(stored), notes=sum(bool(row.get('summary')) for row in stored),
                         latest_note=max((row.get('date') or '' for row in stored if row.get('summary')), default=''),
                         missing_notes=sum(not row.get('summary') for row in stored),
                         failures=sum(bool(row.get('error_msg')) for row in stored))
            try:
                detail = client.get_course_detail(course_id)
                playable = [row for row in detail['lectures'] if row.get('has_playback')]
                known = {str(row['sub_id']) for row in stored if row.get('summary')}
                entry.update(title=detail['title'], teacher=detail['teacher'],
                             available_recordings=len(playable),
                             latest_available=max((row.get('date') or row.get('sub_title', '')[:10]
                                                   for row in playable), default=''),
                             available_without_notes=sum(str(row['sub_id']) not in known for row in playable))
            except Exception as error:
                entry['error'] = str(error)[:500]
            report['courses'].append(entry)
    connection.close()
    return report


if __name__ == '__main__':
    result = audit()
    Path('notes-audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('NOTES_AUDIT_BEGIN')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print('NOTES_AUDIT_END')
