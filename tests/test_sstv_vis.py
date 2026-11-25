"""What counts as an SSTV transmission (sstvdec.find_vis_headers); needs only numpy/scipy, not the SSTV libraries."""

from __future__ import annotations

from typing import Any


def _tones(seq: list[tuple[float, float]], sr: int = 48_000) -> Any:
    import numpy as np
    ph, out = 0.0, []
    for f, s in seq:
        n = int(sr * s)
        t = ph + 2 * np.pi * f * np.arange(n) / sr
        out.append(np.sin(t))
        ph = float(t[-1] + 2 * np.pi * f / sr) if n else ph
    return np.concatenate(out).astype(np.float32) * 0.5


def test_a_transmission_is_counted_only_on_a_parity_valid_vis_header():
    """2026-11-23 OBS-R07: an APRS packet was shown as "1 SSTV tx". A transmission now needs a VIS header (leader,
    break, leader, start bit, 7 bits, even parity, stop bit), not just whatever the image library returned."""
    import numpy as np
    from aobs.scheduling.sstvdec import find_vis_headers
    vis = 8                                                     # Robot 36
    bits = [(vis >> i) & 1 for i in range(7)]
    hdr = [(1900, 0.3), (1200, 0.01), (1900, 0.3), (1200, 0.03)] + [(1100 if b else 1300, 0.03) for b in bits] + \
          [(1100 if sum(bits) % 2 else 1300, 0.03), (1200, 0.03), (1500, 0.5)]
    sr = 48_000
    pad = [(0, 1.0)]
    found = find_vis_headers(_tones(pad + hdr + pad, sr), sr)
    assert [h["vis"] for h in found] == [8] and abs(found[0]["at_s"] - 1.0) < 0.05
    bad_parity = hdr[:11] + [(1300 if sum(bits) % 2 else 1100, 0.03)] + hdr[12:]
    assert find_vis_headers(_tones(pad + bad_parity + pad, sr), sr) == []
    rng = np.random.default_rng(3)
    assert find_vis_headers(rng.uniform(-1, 1, sr * 5).astype(np.float32), sr) == []          # FM noise
    afsk = [(1200 if i % 3 else 2200, 1 / 1200) for i in range(1200 * 2)]                         # APRS AFSK 1200
    assert find_vis_headers(_tones(pad + afsk + pad, sr), sr) == []
