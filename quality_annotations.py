#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Conservative, explainable technical-quality hints; no deletes or scene claims."""
import math


def _metric(score, key, default=50.0):
    try:
        number = float(getattr(score, key, default))
        if math.isfinite(number):
            return max(0.0, min(100.0, number))
    except (ValueError, TypeError, OverflowError):
        pass
    return default


def annotate_quality(score, weighted_score=None):
    """Use existing cached Rank dimensions; no file access, model, or extra decode."""
    focus = _metric(score, 'sharpness')
    exposure = _metric(score, 'exposure')
    noise = _metric(score, 'noise')
    dynamic = _metric(score, 'dynamic_range')
    overall = _metric(score, 'overall_score')
    if weighted_score is not None:
        try:
            candidate = float(weighted_score)
            if math.isfinite(candidate):
                overall = max(0.0, min(100.0, candidate))
        except (TypeError, ValueError, OverflowError):
            pass
    flags = []
    if focus < 28:
        flags.append('疑似失焦')
    elif focus < 48:
        flags.append('清晰度需复核')
    if exposure < 28:
        flags.append('明显曝光风险')
    elif exposure < 48:
        flags.append('曝光需复核')
    if noise < 35:
        flags.append('疑似噪点偏高')
    if dynamic < 25:
        flags.append('动态范围偏窄')
    severe = int(focus < 28) + int(exposure < 28) + int(noise < 25)
    if severe >= 2 or (overall < 44 and len(flags) >= 2):
        tier = '建议重点复核'
    elif overall >= 78 and focus >= 65 and exposure >= 58 and noise >= 50:
        tier = '优质候选'
    else:
        tier = '一般候选'
    meta = getattr(score, 'meta', None)
    hint = (meta.get('scene_hint') or {}) if isinstance(meta, dict) else {}
    scene = hint.get('label') if isinstance(hint, dict) else '未识别'
    if scene != '人像候选':
        scene = '未识别'
    else:
        flags.append('画面中检测到人脸（可能漏检）')
    return {
        'tier': tier,
        'tags': (flags or ['技术指标较均衡' if tier == '优质候选' else '未发现明显技术缺陷'])[:4],
        'scene': scene,
        'manual_only': True,
        'method': '传统技术指标规则；不等于场景语义或可信概率',
    }
