"""Conservative checks for empty/noise-only source material and non-notes."""
from __future__ import annotations

import re


class InsufficientContentError(ValueError):
    """The available material cannot support a course note."""


def invalid_summary_reason(summary: str | None) -> str | None:
    if not summary or len(summary) > 2500:
        return None
    patterns = (
        r"不包含.{0,30}(?:教学内容|实质性|实际的课程)",
        r"(?:确认为|主要为|主要由|均为|全部为).{0,20}(?:无效数据|无效噪声|无意义)",
        r"无法作为有效的复习材料",
        r"(?:无法|不能).{0,20}(?:生成|整理).{0,15}(?:课程笔记|课程总结|知识点)",
        r"不具备.{0,15}(?:教学|学习|复习)价值",
    )
    return next(('summary_reports_unusable_source' for pattern in patterns
                 if re.search(pattern, summary, re.S)), None)


def transcript_has_content(transcript: str | None) -> bool:
    text = transcript or ''
    text = re.sub(r'\[注意[：:].*?\]', '', text, flags=re.S)
    chinese = re.findall(r'[\u4e00-\u9fff]', text)
    words = re.findall(r'[a-zA-Z]{2,}', text.lower())
    words = [word for word in words if word not in {'the', 'yeah', 'okay', 'ok', 'uh', 'um', 'hmm'}]
    return ((len(chinese) >= 100 and len(set(chinese)) >= 25)
            or (len(words) >= 40 and len(set(words)) >= 15))


def ppt_has_content(pages: list[dict]) -> bool:
    text = '\n'.join(str(page.get('text') or '') for page in pages)
    return transcript_has_content(text)
