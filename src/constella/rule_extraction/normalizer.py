from __future__ import annotations

import re


_COMPARATORS = (("不低于", "≥"), ("不少于", "≥"), ("不超过", "≤"), ("小于", "<"), ("低于", "<"), ("大于", ">"), ("高于", ">"), ("约", "≈"))


def normalize_state_text(value: str) -> str:
    """Small display-oriented normalizer; never changes the original state."""
    text = value.strip().replace("～", "–").replace("~", "–")
    for source, target in _COMPARATORS:
        if text.startswith(source):
            text = target + " " + text[len(source):].strip()
            break
    text = re.sub(r"(?<=\d)\s*(?=(?:mm|cm|m|MPa|kPa|A|V|°C|℃|°|%|L\s*/\s*min)\b)", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text
