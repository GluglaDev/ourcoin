import json

from ourcoin.cli import main


def test_chain_cli_initializes_reopens_validates_and_reindexes(tmp_path, capsys) -> None:
    arguments = ["chain", "info", "--data-dir", str(tmp_path)]

    assert main(arguments) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["height"] == 0
    assert first["block_count"] == 1
    assert first["issued_supply_atoms"] == 0

    assert main(arguments) == 0
    reopened = json.loads(capsys.readouterr().out)
    assert reopened["tip_hash"] == first["tip_hash"]
    assert reopened["database_path"] == first["database_path"]

    assert main(["chain", "validate", "--data-dir", str(tmp_path)]) == 0
    validation = json.loads(capsys.readouterr().out)
    assert validation == {
        "account_count": 0,
        "block_count": 1,
        "height": 0,
        "tip_hash": first["tip_hash"],
        "valid": True,
    }

    assert main(["chain", "reindex", "--data-dir", str(tmp_path)]) == 0
    reindex = json.loads(capsys.readouterr().out)
    assert reindex["reindexed"] is True
    assert reindex["tip_hash"] == first["tip_hash"]


def test_chain_validate_does_not_create_missing_database(tmp_path, capsys) -> None:
    assert main(["chain", "validate", "--data-dir", str(tmp_path)]) == 2

    captured = capsys.readouterr()
    assert "does not exist" in captured.err
    assert not any(tmp_path.iterdir())
