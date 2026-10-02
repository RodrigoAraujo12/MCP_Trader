from __future__ import annotations

from pathlib import Path

import pytest

from trading_mcp.config import load_settings

KEYS = ("MT5_PATH", "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_TIMEOUT_MS", "SYMBOL_SUFFIX", "MAX_BARS", "SEC_USER_AGENT",
        "JOURNAL_PATH", "JOURNAL_EXPORT_DIR", "INSTRUMENTOS", "RISCO_POR_OPERACAO_PCT", "PERDA_MAXIMA_DIA_PCT",
        "PERDA_MAXIMA_SEMANA_PCT", "PROPOSTAS_PATH")


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


def test_journal_paths_default_outside_appdata_and_overridable(tmp_path: Path) -> None:
    s = load_settings(tmp_path / "nao_existe.env")
    assert s.journal_path == Path.home() / "trading-mcp" / "journal.sqlite3"
    assert "AppData" not in str(s.journal_path)
    text = f"JOURNAL_PATH={tmp_path / 'j.sqlite3'}\nJOURNAL_EXPORT_DIR={tmp_path / 'exp'}\n"
    path = _write(tmp_path, text, "utf-8")
    s = load_settings(path)
    assert s.journal_path == tmp_path / "j.sqlite3" and s.journal_export_dir == tmp_path / "exp"


def test_instruments_default_and_custom(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "nao_existe.env").instruments[:3] == ("USTEC", "US30", "JP225")
    s = load_settings(_write(tmp_path, "INSTRUMENTOS= ustec, xauusd ,USTEC,,dxy\n", "utf-8"))
    assert s.instruments == ("USTEC", "XAUUSD", "DXY")


def test_journal_paths_expand_home_and_resolve_relative_to_project(tmp_path: Path) -> None:
    from trading_mcp.config import PROJECT_ROOT

    s = load_settings(_write(tmp_path, "JOURNAL_PATH=~/j.sqlite3\nJOURNAL_EXPORT_DIR=backups\n", "utf-8"))
    assert s.journal_path == Path.home() / "j.sqlite3"
    assert s.journal_export_dir == PROJECT_ROOT / "backups"


def test_risk_limits_default_to_the_user_rules(tmp_path: Path) -> None:
    s = load_settings(tmp_path / "nao_existe.env")
    assert (s.risk_per_trade_pct, s.daily_loss_pct, s.weekly_loss_pct) == (1.25, 5.0, 25.0)
    assert s.propostas_path.name == "propostas.sqlite3"


def test_risk_limits_accept_comma_decimals(tmp_path: Path) -> None:
    path = _write(tmp_path, "RISCO_POR_OPERACAO_PCT=1,5\nPERDA_MAXIMA_DIA_PCT=4\nPERDA_MAXIMA_SEMANA_PCT=20\n", "utf-8")
    s = load_settings(path)
    assert (s.risk_per_trade_pct, s.daily_loss_pct, s.weekly_loss_pct) == (1.5, 4.0, 20.0) and s.errors == ()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("RISCO_POR_OPERACAO_PCT=abc\n", "número"),
        ("PERDA_MAXIMA_DIA_PCT=0\n", "entre 0"),
        ("PERDA_MAXIMA_SEMANA_PCT=150\n", "entre 0"),
        ("RISCO_POR_OPERACAO_PCT=6\n", "fora de ordem"),  # 6% por operação > 5% no dia
    ],
)
def test_invalid_risk_limits_are_reported(tmp_path: Path, text: str, message: str) -> None:
    s = load_settings(_write(tmp_path, text, "utf-8"))
    assert any(message in e for e in s.errors)
    assert (s.risk_per_trade_pct, s.daily_loss_pct, s.weekly_loss_pct) == (1.25, 5.0, 25.0)
