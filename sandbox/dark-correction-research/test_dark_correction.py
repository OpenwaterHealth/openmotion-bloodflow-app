import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dark_correction import histogram_moments

pytestmark = pytest.mark.unit


def test_histogram_moments_returns_weighted_mean_variance_and_contrast():
    hist = np.array([0, 1, 2, 1], dtype=float)

    moments = histogram_moments(hist)

    assert moments.total == 4.0
    assert moments.mean == pytest.approx(2.0)
    assert moments.variance == pytest.approx(0.5)
    assert moments.std == pytest.approx(np.sqrt(0.5))
    assert moments.contrast == pytest.approx(np.sqrt(0.5) / 2.0)
