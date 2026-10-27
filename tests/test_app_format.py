from duallink.app import RateMeter, fmt_eta, fmt_rate


def test_fmt_rate_units():
    assert fmt_rate(25_000_000) == "200 Mbps"
    assert fmt_rate(25_000_000, "MB/s") == "25.0 MB/s"
    assert fmt_rate(1_250_000, "Mbps") == "10.0 Mbps"
    assert fmt_rate(150_000_000, "MB/s") == "150 MB/s"
    assert fmt_rate(1_250_000, "nonsense") == "10.0 Mbps"


def test_fmt_eta():
    assert fmt_eta(None) == "-"
    assert fmt_eta(9.6) == "10s"
    assert fmt_eta(125) == "2m 05s"
    assert fmt_eta(3 * 3600 + 7 * 60) == "3h 07m"
    assert fmt_eta(1e9) == "-"


def test_rate_meter_smooths_and_estimates():
    m = RateMeter(smoothing=0.5)
    m.update(0, 0.0)
    assert m.update(10_000_000, 1.0) == 10_000_000
    assert m.update(30_000_000, 2.0) == 15_000_000  # halfway to the new 20 MB/s
    assert m.eta(150_000_000) == 10.0
    assert RateMeter().eta(100) is None
