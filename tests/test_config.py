from __future__ import annotations

from pathlib import Path

import pytest

from trading_mcp.config import load_settings

KEYS = ("MT5_PATH", "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_TIMEOUT_MS", "SYMBOL_SUFFIX", "MAX_BARS", "SEC_USER_AGENT")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)


def _write(tmp_path: Path, text: str, encoding: str) -> Path:
    path = tmp_path / ".env"
    path.write_bytes(text.encode(encoding))
    return path


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    s = load_settings(tmp_path / "nao_existe.env")
    assert s.mt5_path is None and s.max_bars == 5_000 and s.mt5_timeout_ms == 60_000
    assert s.env_file is None and s.errors == ()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_first_key_survives_bom_and_utf16(tmp_path: Path, encoding: str) -> None:
    # Bloco de Notas / PowerShell 5.1 gravam com BOM ou em UTF-16.
    path = _write(tmp_path, "MT5_PATH=C:\\MT5-Demo\\terminal64.exe\nMT5_LOGIN=123\n", encoding)
    s = load_settings(path)
    assert s.mt5_path == "C:\\MT5-Demo\\terminal64.exe"
    assert s.mt5_login == 123


def test_ansi_file_with_accented_password(tmp_path: Path) -> None:
    path = _write(tmp_path, "MT5_PASSWORD=senhaçã\n", "cp1252")
    assert load_settings(path).mt5_password == "senhaçã"


def test_inline_comments_and_quotes(tmp_path: Path) -> None:
    text = 'MT5_SERVER=Exness-MT5Trial9  # demo\nMT5_PASSWORD="abc #123"\nSYMBOL_SUFFIX=m#x\n# comentario\nexport MAX_BARS=3000\n'
    s = load_settings(_write(tmp_path, text, "utf-8"))
    assert s.mt5_server == "Exness-MT5Trial9"
    assert s.mt5_password == "abc #123"  # entre aspas, o # faz parte do valor
    assert s.symbol_suffix == "m#x"  # sem espaço antes, não é comentário
    assert s.max_bars == 3000


def test_quoted_value_followed_by_comment(tmp_path: Path) -> None:
    text = 'MT5_PATH="C:\\Program Files\\MT5 Demo\\terminal64.exe" # instalação demo\nSEC_USER_AGENT=\'Fulano fulano@x.com\'  # SEC\n'
    s = load_settings(_write(tmp_path, text, "utf-8"))
    assert s.mt5_path == "C:\\Program Files\\MT5 Demo\\terminal64.exe"
    assert s.sec_user_agent == "Fulano fulano@x.com"


def test_invalid_numbers_are_collected_not_raised(tmp_path: Path) -> None:
    s = load_settings(_write(tmp_path, "MT5_LOGIN=abc\nMAX_BARS=999999\nMT5_TIMEOUT_MS=x\n", "utf-8"))
    assert s.mt5_login is None and s.max_bars == 5_000 and s.mt5_timeout_ms == 60_000
    assert len(s.errors) == 3
    assert any("MT5_LOGIN" in e for e in s.errors)


def test_environment_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MT5_SERVER", "DoAmbiente")
    s = load_settings(_write(tmp_path, "MT5_SERVER=DoArquivo\n", "utf-8"))
    assert s.mt5_server == "DoAmbiente"


def test_password_not_in_repr(tmp_path: Path) -> None:
    s = load_settings(_write(tmp_path, "MT5_PASSWORD=segredo123\n", "utf-8"))
    assert s.mt5_password == "segredo123"
    assert "segredo123" not in repr(s)


def test_env_file_override_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write(tmp_path, "SYMBOL_SUFFIX=c\n", "utf-8")
    monkeypatch.setenv("TRADING_MCP_ENV_FILE", str(path))
    s = load_settings()
    assert s.symbol_suffix == "c"
    assert s.env_file == str(path)
