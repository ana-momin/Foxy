"""State and identity: the guarantees that make Foxy a monitor, not a script."""

from app.models import Signal, domain_of


def _sig(**kw):
    base = dict(
        source="x",
        external_id="1",
        title="Acme",
        url="https://x.com/a/status/1",
    )
    base.update(kw)
    return Signal(**base)


def test_fingerprint_is_stable_for_the_same_item():
    assert _sig().fingerprint == _sig().fingerprint


def test_fingerprint_differs_across_items():
    assert _sig(external_id="1").fingerprint != _sig(external_id="2").fingerprint


def test_same_id_on_different_sources_is_not_the_same_item():
    assert _sig(source="x").fingerprint != _sig(source="linkedin").fingerprint


def test_entity_key_links_a_company_across_sources():
    """An early signal on X and the later YC listing must resolve to one
    entity, otherwise the confirmation can never be threaded to its alert."""
    a = _sig(source="x", company_name="Acme AI")
    b = _sig(source="yc_directory", external_id="99", company_name="Acme AI")
    assert a.entity_key == b.entity_key


def test_entity_key_ignores_cosmetic_differences():
    assert _sig(company_name="Acme AI").entity_key == _sig(company_name="acme.ai").entity_key


def test_official_sources_are_marked():
    assert _sig(source="yc_directory").is_official
    assert _sig(source="yc_launches").is_official
    assert _sig(source="speedrun").is_official
    assert not _sig(source="x").is_official
    assert not _sig(source="linkedin").is_official


def test_domain_extraction():
    assert domain_of("https://www.talos-us.com/") == "talos-us.com"
    assert domain_of("http://acme.ai/path?q=1") == "acme.ai"
    assert domain_of(None) == ""


def test_notes_do_not_duplicate():
    s = _sig()
    s.add_note("checked")
    s.add_note("checked")
    assert s.notes == ["checked"]
