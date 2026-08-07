"""M0 package import test."""

import ourcoin


def test_package_import() -> None:
    assert ourcoin.__version__ == "0.1.0"
