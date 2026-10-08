#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local face-evidence hint, never a full scene classifier or delete decision."""
from pathlib import Path
from threading import Lock
import cv2

_LOCK=Lock()
_DETECTOR=None
_CHECKED=False


def _face_detector():
    global _DETECTOR, _CHECKED
    if not _CHECKED:
        with _LOCK:
            if not _CHECKED:
                _CHECKED=True
                try:
                    xml=Path(cv2.data.haarcascades)/'haarcascade_frontalface_default.xml'
                    if xml.is_file():
                        model=cv2.CascadeClassifier(str(xml))
                        if not model.empty():
                            _DETECTOR=model
                except Exception:
                    _DETECTOR=None
    return _DETECTOR


def content_scene_hint(gray):
    """Reuse a small gray preview from scoring; never reopen a source file."""
    result={'label':'未识别', 'face_count':0,
            'method':'OpenCV 正脸检测（可能漏检，未提供广义场景识别）'}
    detector=_face_detector()
    if detector is None or gray is None:
        return result
    try:
        height,width=gray.shape[:2]
        if min(height,width)<48:
            return result
        factor=min(1.0,240.0/max(height,width))
        if factor<1.0:
            gray=cv2.resize(gray,(max(1,round(width*factor)),
                                  max(1,round(height*factor))),
                            interpolation=cv2.INTER_AREA)
        faces=detector.detectMultiScale(cv2.equalizeHist(gray),
                                       scaleFactor=1.15,minNeighbors=4,minSize=(22,22))
        if len(faces):
            result['label']='人像候选'
            result['face_count']=min(99,len(faces))
    except Exception:
        pass
    return result
