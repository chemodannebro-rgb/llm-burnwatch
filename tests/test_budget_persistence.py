from __future__ import annotations

import json
import stat

import pytest

from llm_burnwatch.budget import load_budget, save_budget
from llm_burnwatch.pricing_import import import_pricing

LITELLM_SAMPLE = {
    "gpt-4o": {
        "input_cost_per_token": 0.000005,
        "output_cost_per_token": 0.000015,
    },
}


# --- load_budget: missing / malformed file -----------------------------------


def test_load_budget_returns_none_when_file_does_not_exist(tmp_path):
    assert load_budget(tmp_path / "budget.json") is None


def test_load_budget_returns_none_and_warns_on_invalid_json(tmp_path, capsys):
    path = tmp_path / "budget.json"
    path.write_text("{not valid json", encoding="utf-8")

    assert load_budget(path) is None
    assert "could not read budget file" in capsys.readouterr().err


def test_load_budget_returns_none_and_warns_on_missing_keys(tmp_path, capsys):
    path = tmp_path / "budget.json"
    path.write_text(json.dumps({"monthly_usd": 100.0}), encoding="utf-8")

    assert load_budget(path) is None
    assert "could not read budget file" in capsys.readouterr().err


def test_load_budget_returns_none_and_warns_on_non_numeric_values(tmp_path, capsys):
    path = tmp_path / "budget.json"
    path.write_text(
        json.dumps({"monthly_usd": "a lot", "warn_at_fraction": 0.8}), encoding="utf-8"
    )

    assert load_budget(path) is None
    assert "could not read budget file" in capsys.readouterr().err


def test_load_budget_rejects_bool_values_even_though_bool_is_an_int_subclass(tmp_path, capsys):
    path = tmp_path / "budget.json"
    path.write_text(
        json.dumps({"monthly_usd": True, "warn_at_fraction": 0.8}), encoding="utf-8"
    )

    assert load_budget(path) is None
    assert "could not read budget file" in capsys.readouterr().err


# --- save_budget / load_budget round-trip + atomic write ---------------------


def test_save_and_load_budget_round_trips(tmp_path):
    path = tmp_path / "config" / "llm-burnwatch" / "budget.json"
    save_budget(path, 100.0, 0.8)

    assert path.exists()
    assert load_budget(path) == {"monthly_usd": 100.0, "warn_at_fraction": 0.8}


def test_save_budget_creates_parent_directories(tmp_path):
    path = tmp_path / "config" / "llm-burnwatch" / "budget.json"
    assert not path.parent.exists()
    save_budget(path, 50.0, 0.5)
    assert path.parent.is_dir()


def test_save_budget_leaves_no_tmp_file_behind_on_success(tmp_path):
    path = tmp_path / "config" / "llm-burnwatch" / "budget.json"
    save_budget(path, 50.0, 0.5)

    leftovers = [p for p in path.parent.iterdir() if p.name.startswith(".budget-")]
    assert leftovers == []


def test_save_budget_overwrites_existing_file(tmp_path):
    path = tmp_path / "budget.json"
    path.write_text(json.dumps({"monthly_usd": 1.0, "warn_at_fraction": 0.1}), encoding="utf-8")

    save_budget(path, 200.0, 0.9)

    assert load_budget(path) == {"monthly_usd": 200.0, "warn_at_fraction": 0.9}


def test_save_budget_writes_file_with_restrictive_permissions(tmp_path):
    path = tmp_path / "budget.json"
    save_budget(path, 100.0, 0.8)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode & 0o077 == 0  # not group/world readable/writable/executable


# --- coexistence with pricing.json in the same XDG config directory --------


def test_budget_json_and_pricing_json_coexist_in_same_config_directory(tmp_path):
    config_dir = tmp_path / "llm-burnwatch"
    budget_path = config_dir / "budget.json"
    pricing_path = config_dir / "pricing.json"

    save_budget(budget_path, 100.0, 0.8)
    pricing_source = tmp_path / "pricing_source.json"
    pricing_source.write_text(json.dumps(LITELLM_SAMPLE), encoding="utf-8")
    import_pricing(str(pricing_source), pricing_path)

    # Both files exist side by side, and neither write disturbed the other.
    assert load_budget(budget_path) == {"monthly_usd": 100.0, "warn_at_fraction": 0.8}
    assert json.loads(pricing_path.read_text(encoding="utf-8"))["models"]

    # Writing budget again after pricing.json already exists must not disturb it.
    pricing_before = pricing_path.read_text(encoding="utf-8")
    save_budget(budget_path, 200.0, 0.5)
    assert pricing_path.read_text(encoding="utf-8") == pricing_before
    assert load_budget(budget_path) == {"monthly_usd": 200.0, "warn_at_fraction": 0.5}
