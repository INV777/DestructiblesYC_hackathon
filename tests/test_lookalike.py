from settlebot.lookalike import compare_domains, compare_names, email_domain


def test_domain_lookalikes_are_named():
    known = "vrllogistics.com"
    assert compare_domains(known, "vrllogistics.com") is None
    assert compare_domains(known, "billing.vrllogistics.com") is None          # own subdomain is fine
    assert compare_domains(known, "vrllogistic.com") == "one letter dropped"
    assert compare_domains(known, "vrll0gistics.com") is not None              # 0 for o
    assert compare_domains(known, "vrllogistics.co") .startswith("same name with a different ending")
    assert compare_domains(known, "vrl-logistics.com") == "hyphen added or removed"
    assert compare_domains(known, "vrllogistcis.com") == "two letters swapped"
    assert compare_domains(known, "vrllogistics-payments.com").startswith("the real name with extra words")
    assert compare_domains(known, "tciexpress.in") is None                     # unrelated


def test_cyrillic_and_rn_homoglyphs():
    assert compare_domains("amazon.com", "arnazon.com") is not None
    assert compare_domains("amazon.com", "аmazon.com") is not None             # Cyrillic a


def test_name_lookalikes():
    assert compare_names("VRL Logistics Limited", "VRL Logistics Ltd.") is None   # same entity, same form
    assert compare_names("VRL Logistics Limited", "VRL Logistics Pvt Ltd") == \
        "same name, different legal form (a different legal entity)"
    assert compare_names("VRL Logistics Limited", "VRL Logistlcs Limited") is not None
    assert compare_names("TCI Express Limited", "TCl Express Limited") is not None
    assert compare_names("VRL Logistics Limited", "TCI Express Limited") is None


def test_email_domain():
    assert email_domain("Accounts <ap@vrllogistics.com>") == "vrllogistics.com"
    assert email_domain("vrllogistics.com") == "vrllogistics.com"
    assert email_domain(None) is None


def test_missing_legal_form_is_not_a_different_form():
    assert compare_names("VRL Logistics Limited", "VRL Logistics") is None
