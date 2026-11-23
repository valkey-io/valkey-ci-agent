"""Property-based tests for scripts.ci_health.stats (hypothesis).

These fuzz the mathematical invariants of the hand-rolled Beta-Binomial
implementation rather than checking fixed points, and cross-check the PMF
against an independent math.gamma-based reference (a different code path).
"""

import math

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from scripts.ci_health.stats import (
    beta_binomial_pmf,
    beta_binomial_upper_tail,
    learn_prior,
    posterior_mean,
    regression_confidence,
)

# Positive Beta parameters, bounded to keep sums well-conditioned.
_param = st.floats(
    min_value=0.5, max_value=200.0, allow_nan=False, allow_infinity=False
)
_m = st.integers(min_value=1, max_value=40)
_SEV = {"low": 0, "medium": 1, "high": 2}


def _pmf_reference(j, m, a, b):
    """Independent Beta-Binomial PMF via math.gamma (no logs)."""

    def beta(x, y):
        return math.gamma(x) * math.gamma(y) / math.gamma(x + y)

    return math.comb(m, j) * beta(j + a, m - j + b) / beta(a, b)


class TestBetaBinomialInvariants:
    @given(a=_param, b=_param, m=_m)
    def test_pmf_sums_to_one(self, a, b, m):
        total = sum(beta_binomial_pmf(j, m, a, b) for j in range(m + 1))
        assert math.isclose(total, 1.0, abs_tol=1e-6)

    @given(a=_param, b=_param, m=_m, data=st.data())
    def test_pmf_is_a_probability(self, a, b, m, data):
        j = data.draw(st.integers(0, m))
        p = beta_binomial_pmf(j, m, a, b)
        assert 0.0 <= p <= 1.0 + 1e-9

    @given(a=_param, b=_param, m=_m)
    def test_upper_tail_monotone_and_normalized(self, a, b, m):
        tails = [beta_binomial_upper_tail(k, m, a, b) for k in range(m + 2)]
        assert math.isclose(tails[0], 1.0, abs_tol=1e-6)  # P(K >= 0) == 1
        for lo, hi in zip(tails[1:], tails[:-1]):
            assert lo <= hi + 1e-12  # non-increasing in k

    @given(a=_param, b=_param, m=_m, data=st.data())
    def test_tail_equals_one_minus_cdf(self, a, b, m, data):
        k = data.draw(st.integers(0, m))
        tail = beta_binomial_upper_tail(k, m, a, b)
        cdf_below = sum(beta_binomial_pmf(j, m, a, b) for j in range(k))
        assert math.isclose(tail, 1.0 - cdf_below, abs_tol=1e-6)

    @given(
        a=st.floats(0.5, 20.0, allow_nan=False, allow_infinity=False),
        b=st.floats(0.5, 20.0, allow_nan=False, allow_infinity=False),
        m=st.integers(1, 15),
        data=st.data(),
    )
    def test_pmf_matches_gamma_reference(self, a, b, m, data):
        # Bounded so math.gamma doesn't overflow; validates the lgamma path.
        j = data.draw(st.integers(0, m))
        assert math.isclose(
            beta_binomial_pmf(j, m, a, b),
            _pmf_reference(j, m, a, b),
            rel_tol=1e-9,
            abs_tol=1e-12,
        )


class TestPosteriorMean:
    @given(hist_total=st.integers(1, 1000), data=st.data())
    def test_bounds(self, hist_total, data):
        hf = data.draw(st.integers(0, hist_total))
        m = posterior_mean(*learn_prior(hf, hist_total))
        assert 0.0 < m < 1.0

    @given(hist_total=st.integers(1, 1000), data=st.data())
    def test_increases_with_failures(self, hist_total, data):
        lo = data.draw(st.integers(0, hist_total))
        hi = data.draw(st.integers(lo, hist_total))
        assert posterior_mean(*learn_prior(lo, hist_total)) <= posterior_mean(
            *learn_prior(hi, hist_total)
        )


class TestRegressionConfidenceProperties:
    @settings(deadline=None)
    @given(
        pre_total=st.integers(1, 1000), post_total=st.integers(1, 30), data=st.data()
    )
    def test_bigger_burst_never_less_surprising(self, pre_total, post_total, data):
        # As post_fails rises, burst_p is non-increasing and severity non-decreasing.
        pre_fails = data.draw(st.integers(0, pre_total))
        prev_bp, prev_sev = 2.0, -1
        for k in range(post_total + 1):
            label, bp, p0 = regression_confidence(pre_fails, pre_total, k, post_total)
            assert label in _SEV
            assert bp is not None and 0.0 <= bp <= 1.0 + 1e-9
            assert 0.0 < p0 < 1.0
            assert bp <= prev_bp + 1e-12
            assert _SEV[label] >= prev_sev
            prev_bp, prev_sev = bp, _SEV[label]

    @settings(deadline=None)
    @given(
        pre_total=st.integers(10, 1000), post_total=st.integers(1, 20), data=st.data()
    )
    def test_cleaner_baseline_is_more_surprising(self, pre_total, post_total, data):
        post_fails = data.draw(st.integers(1, post_total))
        more = data.draw(st.integers(0, pre_total))
        fewer = data.draw(st.integers(0, more))
        assume(fewer <= more)
        _, bp_clean, _ = regression_confidence(fewer, pre_total, post_fails, post_total)
        _, bp_flaky, _ = regression_confidence(more, pre_total, post_fails, post_total)
        assert bp_clean <= bp_flaky + 1e-12

    @given(post_total=st.integers(1, 40), data=st.data())
    def test_unknown_without_pre_history(self, post_total, data):
        pf = data.draw(st.integers(0, post_total))
        assert regression_confidence(0, 0, pf, post_total) == ("unknown", None, None)

    @given(pre_total=st.integers(1, 100), data=st.data())
    def test_unknown_without_post_runs(self, pre_total, data):
        pref = data.draw(st.integers(0, pre_total))
        assert regression_confidence(pref, pre_total, 0, 0) == ("unknown", None, None)
