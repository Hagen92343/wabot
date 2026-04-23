"""Unit tests for MetricsRegistry (Phase 8 C8.4)."""

from __future__ import annotations

import threading

import pytest

from whatsbot.http.metrics import (
    DEFAULT_LATENCY_BUCKETS,
    MetricsRegistry,
)

# ---- counter ---------------------------------------------------------


def test_counter_starts_at_zero_without_increment() -> None:
    r = MetricsRegistry()
    assert r.counter_value("messages_total") == 0.0


def test_counter_increment_accumulates_per_label_set() -> None:
    r = MetricsRegistry()
    r.increment("messages_total", labels={"direction": "in"})
    r.increment("messages_total", labels={"direction": "in"})
    r.increment("messages_total", labels={"direction": "out"})

    assert r.counter_value("messages_total", labels={"direction": "in"}) == 2
    assert r.counter_value("messages_total", labels={"direction": "out"}) == 1
    # Different label key means different series.
    assert r.counter_value("messages_total") == 0


def test_counter_value_kwarg_accepts_custom_increment() -> None:
    r = MetricsRegistry()
    r.increment("bytes_total", value=42, labels={"kind": "image"})
    r.increment("bytes_total", value=8, labels={"kind": "image"})
    assert r.counter_value("bytes_total", labels={"kind": "image"}) == 50


def test_help_text_is_set_once_and_preserved_across_calls() -> None:
    r = MetricsRegistry()
    r.increment("m_total", help_text="first")
    r.increment("m_total", help_text="")  # blank must not clobber
    rendered = r.render()
    assert "# HELP m_total first" in rendered


# ---- gauge -----------------------------------------------------------


def test_gauge_set_records_latest_value() -> None:
    r = MetricsRegistry()
    r.set_gauge("sessions_active", 3)
    r.set_gauge("sessions_active", 5)
    assert r.gauge_value("sessions_active") == 5


def test_gauge_per_label_is_independent() -> None:
    r = MetricsRegistry()
    r.set_gauge("circuit_state", 1, labels={"service": "meta", "state": "open"})
    r.set_gauge("circuit_state", 0, labels={"service": "meta", "state": "closed"})
    assert r.gauge_value(
        "circuit_state", labels={"service": "meta", "state": "open"}
    ) == 1
    assert r.gauge_value(
        "circuit_state", labels={"service": "meta", "state": "closed"}
    ) == 0


# ---- histogram -------------------------------------------------------


def test_histogram_buckets_count_up_to_upper_bound() -> None:
    r = MetricsRegistry()
    r.observe("latency_seconds", 0.03)  # hits every bucket >=0.05
    r.observe("latency_seconds", 0.15)
    r.observe("latency_seconds", 3.0)

    rendered = r.render()
    # 0.05 bucket catches the 0.03 observation
    assert 'latency_seconds_bucket{le="0.05"} 1' in rendered
    # 0.2 bucket catches the 0.03 + 0.15
    assert 'latency_seconds_bucket{le="0.2"} 2' in rendered
    # +Inf always equals the total count.
    assert 'latency_seconds_bucket{le="+Inf"} 3' in rendered
    assert "latency_seconds_count 3" in rendered


def test_histogram_sum_matches_total_observations() -> None:
    r = MetricsRegistry()
    r.observe("h", 0.1)
    r.observe("h", 0.2)
    r.observe("h", 0.3)
    rendered = r.render()
    # 0.1 + 0.2 + 0.3 = 0.6. We allow for the rendered precision.
    assert "h_sum " in rendered
    line = next(ln for ln in rendered.splitlines() if ln.startswith("h_sum"))
    value = float(line.split()[-1])
    assert abs(value - 0.6) < 1e-9


def test_histogram_uses_custom_buckets_on_first_observe() -> None:
    r = MetricsRegistry()
    r.observe("h", 0.5, buckets=(1.0, 10.0))
    rendered = r.render()
    assert 'h_bucket{le="1"} 1' in rendered
    assert 'h_bucket{le="10"} 1' in rendered


def test_histogram_total_accessor() -> None:
    r = MetricsRegistry()
    r.observe("h", 0.1, labels={"path": "webhook"})
    r.observe("h", 0.2, labels={"path": "webhook"})
    assert r.histogram_total("h", labels={"path": "webhook"}) == 2


def test_default_latency_buckets_cover_p95_budget() -> None:
    # The Spec §20 budget is <700 ms P95 — the default bucket ladder
    # must straddle that value so we can read P95 off a dashboard.
    assert 0.5 in DEFAULT_LATENCY_BUCKETS
    assert 1.0 in DEFAULT_LATENCY_BUCKETS


# ---- render format ---------------------------------------------------


def test_render_empty_registry_is_empty_string() -> None:
    r = MetricsRegistry()
    assert r.render() == ""


def test_render_integer_values_render_without_trailing_zero() -> None:
    r = MetricsRegistry()
    r.increment("m", value=3)
    rendered = r.render()
    assert "\nm 3\n" in rendered
    assert "\nm 3.0" not in rendered


def test_render_escapes_label_values() -> None:
    r = MetricsRegistry()
    r.increment("m", labels={"msg": 'has "quote" and \\slash'})
    rendered = r.render()
    # The quote and backslash are escaped per Prometheus spec.
    assert r"\"quote\"" in rendered
    assert r"\\slash" in rendered


def test_render_includes_type_comment_per_series() -> None:
    r = MetricsRegistry()
    r.increment("c")
    r.set_gauge("g", 1)
    r.observe("h", 0.1)
    rendered = r.render()
    assert "# TYPE c counter" in rendered
    assert "# TYPE g gauge" in rendered
    assert "# TYPE h histogram" in rendered


def test_render_sorts_label_permutations_stable() -> None:
    r = MetricsRegistry()
    r.increment("m", labels={"x": "2"})
    r.increment("m", labels={"x": "1"})
    rendered = r.render()
    lines = [ln for ln in rendered.splitlines() if ln.startswith("m{")]
    # Sorted output — "1" before "2".
    assert lines[0].startswith('m{x="1"}')
    assert lines[1].startswith('m{x="2"}')


# ---- thread-safety ---------------------------------------------------


def test_concurrent_increments_do_not_lose_values() -> None:
    r = MetricsRegistry()

    def worker() -> None:
        for _ in range(1000):
            r.increment("m", labels={"t": "x"})

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert r.counter_value("m", labels={"t": "x"}) == 4000


# ---- parameterised labels --------------------------------------------


@pytest.mark.parametrize(
    "labels,expected_fragment",
    [
        (None, "m 1"),
        ({}, "m 1"),
        ({"a": "1"}, 'm{a="1"} 1'),
        ({"b": "2", "a": "1"}, 'm{a="1",b="2"} 1'),
    ],
)
def test_label_rendering_is_stable(
    labels: dict[str, str] | None, expected_fragment: str
) -> None:
    r = MetricsRegistry()
    r.increment("m", labels=labels)
    assert expected_fragment in r.render()
