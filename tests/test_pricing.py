"""Tests for model pricing and id normalisation."""

from __future__ import annotations

import unittest

from sessionkit import pricing


class NormaliseTest(unittest.TestCase):
    """Model ids arrive with deployment suffixes and provider prefixes."""

    def test_context_variant_suffix_is_stripped(self) -> None:
        """``claude-opus-5[1m]`` bills at standard rates — no long-context premium."""
        self.assertEqual(pricing.normalise("claude-opus-5[1m]"), "claude-opus-5")
        self.assertEqual(pricing.rates_for("claude-opus-5[1m]"),
                         pricing.rates_for("claude-opus-5"))

    def test_bedrock_prefix_is_stripped(self) -> None:
        self.assertEqual(pricing.normalise("anthropic.claude-sonnet-5"), "claude-sonnet-5")

    def test_dated_snapshot_falls_back_to_alias(self) -> None:
        self.assertEqual(pricing.normalise("claude-haiku-4-5-20251001"), "claude-haiku-4-5")

    def test_exact_alias_is_untouched(self) -> None:
        self.assertEqual(pricing.normalise("claude-sonnet-5"), "claude-sonnet-5")


class RatesTest(unittest.TestCase):
    """Rate lookup, defaults, and the unknown-model ledger."""

    def test_known_model(self) -> None:
        self.assertAlmostEqual(pricing.rates_for("claude-opus-5").input, 5.0 / 1_000_000)

    def test_cache_multipliers(self) -> None:
        rates = pricing.rates_for("claude-opus-5")
        self.assertAlmostEqual(rates.cache_write, rates.input * 1.25)
        self.assertAlmostEqual(rates.cache_read, rates.input * 0.10)

    def test_synthetic_model_is_free_and_not_reported_unknown(self) -> None:
        self.assertEqual(pricing.rates_for("<synthetic>").input, 0.0)
        self.assertNotIn("<synthetic>", pricing.unknown_models())

    def test_unknown_model_falls_back_and_is_recorded(self) -> None:
        pricing.rates_for("claude-imaginary-9")
        self.assertIn("claude-imaginary-9", pricing.unknown_models())
        self.assertEqual(pricing.rates_for("claude-imaginary-9"), pricing.DEFAULT)


class CostTest(unittest.TestCase):
    """Cost arithmetic across all four token buckets."""

    def test_all_buckets_contribute(self) -> None:
        value = pricing.cost("claude-opus-5", 1_000_000, 0, 0, 0)
        self.assertAlmostEqual(value, 5.0)
        value = pricing.cost("claude-opus-5", 0, 1_000_000, 0, 0)
        self.assertAlmostEqual(value, 25.0)
        value = pricing.cost("claude-opus-5", 0, 0, 1_000_000, 0)
        self.assertAlmostEqual(value, 0.5)
        value = pricing.cost("claude-opus-5", 0, 0, 0, 1_000_000)
        self.assertAlmostEqual(value, 6.25)

    def test_zero_usage_is_free(self) -> None:
        self.assertEqual(pricing.cost("claude-opus-5", 0, 0, 0, 0), 0.0)

    def test_opus_costs_more_than_haiku(self) -> None:
        args = (1000, 1000, 0, 0)
        self.assertGreater(pricing.cost("claude-opus-5", *args),
                           pricing.cost("claude-haiku-4-5", *args))


class TrackerParityTest(unittest.TestCase):
    """SPEC.md §4.1: `pricing.py` and the tracker's `server/src/pricing.ts` are two
    hand-maintained tables over one set of facts and must agree to the cent. This transcribes
    pricing.ts's `PRICING` const (read directly from claude-project-tracker/server/src/pricing.ts
    as of 2026-09-03) so a rate drifting on either side without the other fails here. Update
    this table in lockstep with any edit to either pricing.py or pricing.ts.
    """

    TRACKER_PRICING_TS: dict[str, tuple[float, float]] = {
        "claude-fable-5": (10.00, 50.00),
        "claude-mythos-5": (10.00, 50.00),
        "claude-opus-5": (5.00, 25.00),
        "claude-opus-4-8": (5.00, 25.00),
        "claude-opus-4-7": (5.00, 25.00),
        "claude-opus-4-6": (5.00, 25.00),
        "claude-opus-4-5": (5.00, 25.00),
        "claude-sonnet-5": (3.00, 15.00),
        "claude-sonnet-4-6": (3.00, 15.00),
        "claude-sonnet-4-5": (3.00, 15.00),
        "claude-haiku-4-5": (1.00, 5.00),
    }

    def test_rates_mirror_tracker_pricing_ts(self) -> None:
        for model, (in_per_m, out_per_m) in self.TRACKER_PRICING_TS.items():
            rates = pricing.rates_for(model)
            self.assertAlmostEqual(rates.input, in_per_m / 1_000_000, msg=model)
            self.assertAlmostEqual(rates.output, out_per_m / 1_000_000, msg=model)

    def test_no_model_exists_in_only_one_table(self) -> None:
        self.assertEqual(set(pricing.PRICING), set(self.TRACKER_PRICING_TS))

    def test_cache_multipliers_match_tracker(self) -> None:
        # server/src/pricing.ts CACHE_WRITE_MULTIPLIER / CACHE_READ_MULTIPLIER.
        self.assertEqual(pricing.CACHE_WRITE_MULTIPLIER, 1.25)
        self.assertEqual(pricing.CACHE_READ_MULTIPLIER, 0.10)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
