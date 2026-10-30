"""Tests for the prior-aware confidence statistics (scripts.ci_health.stats)."""

from scripts.ci_health.stats import (
    CONF_HIGH_P,
    CONF_MED_P,
    beta_binomial_pmf,
    beta_binomial_upper_tail,
    learn_prior,
    posterior_mean,
    regression_confidence,
)

_SEVERITY = {"low": 0, "medium": 1, "high": 2, "unknown": -1}


class TestBetaMath:
    def test_pmf_sums_to_one(self):
        a, b = learn_prior(10, 200)
        total = sum(beta_binomial_pmf(j, 5, a, b) for j in range(6))
        assert abs(total - 1.0) < 1e-9, total

    def test_posterior_mean_learns_baseline(self):
        a, b = learn_prior(10, 200)
        assert abs(posterior_mean(a, b) - 10.5 / 201) < 1e-9

    def test_upper_tail_monotone_in_k(self):
        a, b = learn_prior(10, 200)
        assert (
            beta_binomial_upper_tail(3, 5, a, b)
            < beta_binomial_upper_tail(2, 5, a, b)
            < beta_binomial_upper_tail(1, 5, a, b)
        )

    def test_upper_tail_zero_runs(self):
        a, b = learn_prior(0, 100)
        assert beta_binomial_upper_tail(0, 0, a, b) == 1.0


class TestRegressionConfidence:
    def test_clean_test_fresh_failure_is_high(self):
        # Pristine test (0 fails in 400 days) failing 3/3 since onset.
        label, burst_p, p0 = regression_confidence(0, 400, 3, 3)
        assert label == "high", (label, burst_p)
        assert burst_p <= CONF_HIGH_P
        assert p0 < 0.01

    def test_known_flake_small_burst_is_low(self):
        # ~5% flake (20/400) failing 1 of 3 since onset -> expected noise.
        label, burst_p, _ = regression_confidence(20, 400, 1, 3)
        assert label == "low", (label, burst_p)
        assert burst_p > CONF_MED_P

    def test_known_flake_big_burst_is_high(self):
        # ~5% flake failing 5/5 -> a genuine burst even for a flake.
        label, burst_p, _ = regression_confidence(20, 400, 5, 5)
        assert label == "high", (label, burst_p)

    def test_no_pre_history_is_unknown(self):
        # Already failing at window start: no clean baseline to learn from.
        assert regression_confidence(0, 0, 1, 1) == ("unknown", None, None)
        assert regression_confidence(5, 10, 1, 0) == ("unknown", None, None)

    def test_confidence_escalates_with_burst_size(self):
        # For a fixed flaky history, more post-onset fails -> non-decreasing
        # severity (low -> medium -> high) as the burst gets more surprising.
        sev = [_SEVERITY[regression_confidence(20, 400, k, 5)[0]] for k in range(1, 6)]
        assert sev == sorted(sev), sev
        assert sev[0] <= sev[-1] and sev[-1] == _SEVERITY["high"], sev

    def test_cleaner_baseline_is_more_sensitive(self):
        # Same post-onset burst is more surprising against a cleaner history.
        _, burst_clean, _ = regression_confidence(1, 400, 2, 4)
        _, burst_flaky, _ = regression_confidence(40, 400, 2, 4)
        assert burst_clean < burst_flaky


class TestBetaQuantileAndLowerBound:
    """Beta CDF/quantile and the post-onset fail-rate lower bound."""

    def test_betainc_endpoints(self):
        from scripts.ci_health.stats import betainc

        assert betainc(2, 3, 0.0) == 0.0
        assert betainc(2, 3, 1.0) == 1.0

    def test_betainc_uniform_is_identity(self):
        from scripts.ci_health.stats import betainc

        # Beta(1,1) is Uniform(0,1): its CDF is the identity.
        for x in (0.1, 0.3, 0.5, 0.9):
            assert abs(betainc(1, 1, x) - x) < 1e-6

    def test_betainc_symmetry(self):
        from scripts.ci_health.stats import betainc

        # I_x(a,b) == 1 - I_{1-x}(b,a)
        assert abs(betainc(2, 5, 0.3) - (1 - betainc(5, 2, 0.7))) < 1e-9

    def test_quantile_inverts_cdf(self):
        from scripts.ci_health.stats import beta_quantile, betainc

        for a, b, q in [(2, 5, 0.05), (7.5, 60.5, 0.5), (0.5, 0.5, 0.9)]:
            x = beta_quantile(a, b, q)
            assert abs(betainc(a, b, x) - q) < 1e-4

    def test_lower_bound_calibration(self):
        from scripts.ci_health.stats import regression_rate_lower_bound as lb

        # Matches the live-data decisions used to design the gate.
        assert lb(7, 66) >= 0.05  # flagged (durable-ish)
        assert lb(6, 75) < 0.05  # not flagged (cluster noise)
        assert lb(2, 2) > lb(3, 6) > lb(7, 66)  # stronger evidence -> higher
        assert lb(0, 0) == 0.0  # degenerate guard

    def test_lower_bound_in_unit_interval(self):
        from scripts.ci_health.stats import regression_rate_lower_bound as lb

        for f, t in [(1, 1), (2, 2), (5, 50), (10, 88), (50, 50)]:
            v = lb(f, t)
            assert 0.0 <= v <= 1.0
