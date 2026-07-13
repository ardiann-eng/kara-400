import importlib
import json

from cryptography.fernet import Fernet


def test_bybit_credentials_are_encrypted_at_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    import config
    import core.db

    importlib.reload(config)
    db_module = importlib.reload(core.db)
    db = db_module.UserDB(
        file_path=str(tmp_path / "users.json"),
        db_path=str(tmp_path / "kara.db"),
    )
    from models.schemas import User

    user = User(chat_id="1", paper_balance_usd=100)
    user.bybit_api_key = "plain-api-key"
    user.bybit_api_secret = "plain-api-secret"
    user.bybit_authorized = True
    db.users[user.chat_id] = user
    db.save()

    raw = json.loads((tmp_path / "users.json").read_text(encoding="utf-8"))["1"]
    assert raw["bybit_api_key"].startswith("gAAAA")
    assert raw["bybit_api_secret"].startswith("gAAAA")
    assert "plain-api" not in (tmp_path / "users.json").read_text(encoding="utf-8")

    reloaded = db_module.UserDB(
        file_path=str(tmp_path / "users.json"),
        db_path=str(tmp_path / "kara.db"),
    )
    assert reloaded.get_user("1").bybit_api_key == "plain-api-key"
    assert reloaded.get_user("1").bybit_api_secret == "plain-api-secret"
