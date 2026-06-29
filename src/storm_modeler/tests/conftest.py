"""Shared test fixtures.

The replay cases are generated on the fly into a temp directory (no synthetic
data is shipped in the repo — it was too easily mistaken for a real download).
``storm_modeler.data.synthetic`` builds them deterministically, so these are
byte-identical to the old shipped fixtures.
"""

from __future__ import annotations

import pytest

from storm_modeler.data import synthetic


@pytest.fixture(scope="session")
def tornado_case(tmp_path_factory):
    return synthetic.build_tornado_case(tmp_path_factory.mktemp("cases"))


@pytest.fixture(scope="session")
def ap_case(tmp_path_factory):
    return synthetic.build_ap_case(tmp_path_factory.mktemp("cases"))
