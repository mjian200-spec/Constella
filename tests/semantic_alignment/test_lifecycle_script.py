from __future__ import annotations

import pytest

from constella.semantic_alignment import require_complete_alignment


def test_lifecycle_rejects_partial_alignment_before_next_cycle():
    report = {
        "runner": {
            "selected_package_count": 10,
            "success_count": 9,
            "failed_count": 1,
        }
    }

    with pytest.raises(RuntimeError, match="9/10 packages succeeded"):
        require_complete_alignment(report)


def test_lifecycle_accepts_complete_alignment():
    require_complete_alignment({
        "runner": {
            "selected_package_count": 10,
            "success_count": 10,
            "failed_count": 0,
        }
    })
