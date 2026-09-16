"""Population structure invariants.

Referential integrity matters more here than it looks: the graph load runs with
``--skip-bad-relationships=false``, so an edge pointing at a node that does not
exist fails the whole import with a message about a CSV line rather than about
the generator that produced it. These tests fail at the source instead.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from fincrime.config import load_config
from fincrime.population import build
from fincrime.reference import BUSINESS_ARCHETYPES, RETAIL_ARCHETYPES
from fincrime.rng import streams
from fincrime.schema import EDGE_TABLES, NODES_BY_NAME


@pytest.fixture(scope="module")
def cfg():
    # A small window keeps the fixture fast; population size is what these
    # tests actually exercise and it is unaffected by the window.
    return load_config("config/scale-dev.yaml")


@pytest.fixture(scope="module")
def pop(cfg):
    return build(cfg, streams(cfg.seed))


def test_counts_match_config(cfg, pop):
    assert pop.tables["individual"].height == cfg.individuals
    assert pop.tables["legal_entity"].height == cfg.legal_entities


def test_every_customer_has_a_checking_account(cfg, pop):
    checking = pop.account_type == "checking"
    individual_owners = set(pop.account_owner[checking & ~pop.account_owner_is_entity].tolist())
    entity_owners = set(pop.account_owner[checking & pop.account_owner_is_entity].tolist())
    assert individual_owners == set(range(cfg.individuals))
    assert entity_owners == set(range(cfg.legal_entities))


def test_primary_keys_are_unique(pop):
    for name, df in pop.tables.items():
        table = NODES_BY_NAME.get(name)
        if table is None:
            continue
        key = table.key.name
        assert df[key].n_unique() == df.height, f"{name}.{key} has duplicates"


@pytest.mark.parametrize(
    "edge",
    [e for e in EDGE_TABLES if not e.ground_truth and not e.name.startswith("txn_")],
    ids=lambda e: e.name,
)
def test_edge_endpoints_resolve(pop, edge):
    """Every edge endpoint must name a node that exists.

    This is the check that stands between a generator bug and a bulk import
    that fails an hour into the mvp build.
    """
    df = pop.tables.get(edge.name)
    if df is None or df.height == 0:
        pytest.skip(f"{edge.name} is empty")
    start_key = NODES_BY_NAME[edge.start].key.name
    end_key = NODES_BY_NAME[edge.end].key.name
    starts = set(pop.tables[edge.start][start_key].to_list())
    ends = set(pop.tables[edge.end][end_key].to_list())
    assert set(df["start_id"].to_list()) <= starts, f"{edge.name} has dangling start_id"
    assert set(df["end_id"].to_list()) <= ends, f"{edge.name} has dangling end_id"


def test_households_share_addresses(pop):
    """The background must contain legitimate shared addresses in quantity.

    Shared address is a headline signal for mule and synthetic-identity rings.
    If essentially nobody legitimately shares one, the signal is free and the
    typology becomes trivially detectable.
    """
    per_address = pop.tables["individual_resides_at"].group_by("end_id").len()
    shared = per_address.filter(pl.col("len") > 1).height
    assert shared > per_address.height * 0.3


def test_some_entities_share_a_registered_address(pop):
    """Serviced offices: the legitimate twin of a shell-network address cluster."""
    per_address = pop.tables["entity_registered_at"].group_by("end_id").len()
    assert per_address.filter(pl.col("len") > 3).height > 0


def test_devices_are_shared_but_not_universally(pop):
    """Household sharing, measured on the personal devices only.

    The device table also holds business devices now, so its height is no
    longer a proxy for individual sharing - it exceeds the population.
    """
    n_ind = pop.tables["individual"].height
    n_personal = len(np.unique(pop.individual_device))
    assert n_personal < n_ind, "no device sharing at all"
    assert n_personal > n_ind * 0.5, "device sharing is implausibly widespread"


def test_every_business_banks_from_a_few_stable_devices(pop):
    """The other half of the shared-device signal.

    Entity transactions drew a uniformly random device from the whole pool,
    which put every company's payments across every device in the dataset and
    made "unrelated accounts on one device" fire on everybody. A business has
    to own a small, fixed set.
    """
    n_ent = pop.tables["legal_entity"].height
    assert pop.entity_device.shape[0] == n_ent
    per_entity = np.array([len(np.unique(row)) for row in pop.entity_device])
    assert per_entity.min() >= 1
    assert per_entity.max() <= 3

    # Business and personal devices must not overlap: a company sharing the
    # finance machine with an unrelated customer's phone is the exact false
    # signal this is meant to remove.
    personal = set(np.unique(pop.individual_device).tolist())
    business = set(np.unique(pop.entity_device).tolist())
    assert not (personal & business)


def test_ip_pool_is_not_artificially_concentrated(pop):
    """Guards the decision in identifiers.py to avoid RFC 5737.

    A documentation-range pool would give 762 addresses for the whole
    population, forcing dozens of unrelated customers onto each IP and baking
    a false mule signal into every entity.
    """
    n_ip = pop.tables["ip_address"].height
    n_ind = pop.tables["individual"].height
    assert n_ip > n_ind * 0.5
    assert pop.tables["ip_address"]["address"].n_unique() == n_ip


def test_beneficial_ownership_sums_to_one_hundred(pop):
    per_entity = (
        pop.tables["individual_bo_of_entity"]
        .group_by("end_id")
        .agg(pl.col("pct").sum().alias("total"))
    )
    assert per_entity.filter((pl.col("total") - 100.0).abs() > 0.05).height == 0


def test_every_entity_has_an_owner(pop):
    owned_directly = set(pop.tables["individual_bo_of_entity"]["end_id"].to_list())
    owned_by_parent = set(pop.tables["entity_bo_of_entity"]["end_id"].to_list())
    all_entities = set(pop.tables["legal_entity"]["entity_id"].to_list())
    assert all_entities == owned_directly | owned_by_parent


def test_multi_layer_ownership_exists(pop):
    """Legitimate holding structures - the twin of shell layering."""
    assert pop.tables["entity_bo_of_entity"].height > 0


def test_holding_companies_have_a_coherent_industry(pop):
    holdings = pop.tables["legal_entity"].filter(pl.col("behavior_archetype") == "holding_company")
    if holdings.height:
        assert set(holdings["naics"].to_list()) == {"551112"}


def test_archetypes_are_all_represented(pop):
    assert set(pop.tables["individual"]["behavior_archetype"].unique()) == {
        a.name for a in RETAIL_ARCHETYPES
    }
    assert set(pop.tables["legal_entity"]["behavior_archetype"].unique()) <= {
        a.name for a in BUSINESS_ARCHETYPES
    }


def test_cards_only_hang_off_retail_checking_accounts(pop):
    funded = pop.account_type[pop.card_account]
    assert set(np.unique(funded)) == {"checking"}
    assert not pop.account_owner_is_entity[pop.card_account].any()


def test_accounts_open_before_the_window_ends(cfg, pop):
    opened = pop.account_opened
    assert opened.max() <= np.datetime64(cfg.window.end, "D")


def test_generation_is_reproducible(cfg):
    a = build(cfg, streams(cfg.seed))
    b = build(cfg, streams(cfg.seed))
    for name in a.tables:
        assert a.tables[name].equals(b.tables[name]), f"{name} differs between runs"
