"""Configuração lida de variáveis de ambiente e, opcionalmente, de um arquivo .env.

Ordem de precedência: variáveis já definidas no ambiente > arquivo .env.
O .env padrão fica na raiz do projeto; TRADING_MCP_ENV_FILE aponta para outro.

Erros de configuração não derrubam o servidor: ficam em `Settings.errors` e são
mostrados como erro de tool quando o MT5 é usado (as demais tools continuam funcionando).
"""

from __future__ import annotations

import codecs
import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _decode(data: bytes) -> str:
    """Decodifica o .env como o Bloco de Notas/PowerShell podem salvá-lo (UTF-8 com/sem BOM, UTF-16, ANSI)."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _parse_value(raw: str) -> str:
    value = raw.strip()
    # Valor entre aspas: vale o que está até a aspa de fechamento; o resto (ex.: "# comentário") é ignorado.
    if value[:1] in ("'", '"'):
        closing = value.find(value[0], 1)
        if closing != -1:
            return value[1:closing]
    # Comentário no fim da linha só conta se houver espaço antes do #, como no python-dotenv.
    for marker in (" #", "\t#"):
        if marker in value:
            value = value.split(marker, 1)[0]
    return value.strip()


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in _decode(path.read_bytes()).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        values[key] = _parse_value(value)
    return values


def _get(env: dict[str, str], key: str) -> str | None:
    value = os.environ.get(key, env.get(key))
    if value is None:
        return None
    value = value.strip()
    return value or None


def _get_int(env: dict[str, str], key: str, default: int | None, errors: list[str]) -> int | None:
    value = _get(env, key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        errors.append(f"{key} deve ser um número inteiro, recebido: {value!r}")
        return default


@dataclass(frozen=True)
class Settings:
    # Caminho do terminal64.exe de uma instalação do MT5 dedicada a este servidor.
    # Vazio = o MetaTrader5 usa o terminal padrão instalado na máquina.
    mt5_path: str | None = None
    mt5_login: int | None = None
    mt5_password: str | None = field(default=None, repr=False)
    mt5_server: str | None = None
    mt5_timeout_ms: int = 60_000
    # Sufixo dos símbolos da conta (ex.: "m" na Exness Standard -> EURUSDm).
    # Vazio = detecção automática.
    symbol_suffix: str | None = None
    # Máximo de candles buscados do MT5 por chamada.
    max_bars: int = 5_000
    # Exigido pela SEC: "Seu Nome seu@email.com".
    sec_user_agent: str | None = None
    # Arquivo .env efetivamente usado (None = nenhum encontrado).
    env_file: str | None = None
    # Problemas encontrados na configuração do MT5.
    errors: tuple[str, ...] = ()


def default_env_file() -> Path:
    custom = os.environ.get("TRADING_MCP_ENV_FILE")
    return Path(custom) if custom else PROJECT_ROOT / ".env"


def load_settings(env_file: Path | None = None) -> Settings:
    env_file = env_file or default_env_file()
    errors: list[str] = []
    try:
        env = _load_env_file(env_file)
    except OSError as exc:
        env = {}
        errors.append(f"Não foi possível ler {env_file}: {exc}")

    max_bars = _get_int(env, "MAX_BARS", 5_000, errors) or 5_000
    if not 1 <= max_bars <= 50_000:
        errors.append(f"MAX_BARS deve estar entre 1 e 50000, recebido: {max_bars}")
        max_bars = 5_000

    return Settings(
        mt5_path=_get(env, "MT5_PATH"),
        mt5_login=_get_int(env, "MT5_LOGIN", None, errors),
        mt5_password=_get(env, "MT5_PASSWORD"),
        mt5_server=_get(env, "MT5_SERVER"),
        mt5_timeout_ms=_get_int(env, "MT5_TIMEOUT_MS", 60_000, errors) or 60_000,
        symbol_suffix=_get(env, "SYMBOL_SUFFIX"),
        max_bars=max_bars,
        sec_user_agent=_get(env, "SEC_USER_AGENT"),
        env_file=str(env_file) if env_file.is_file() else None,
        errors=tuple(errors),
    )
