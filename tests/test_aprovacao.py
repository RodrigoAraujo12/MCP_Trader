"""Janela de aprovação: só o código de saída 7 aprova; o script não deixa um teclado distraído aprovar."""

from __future__ import annotations

import subprocess
import sys

import pytest

from trading_mcp import aprovacao


def _fake_run(returncode=None, error=None):
    def run(title, text, timeout_s, dry=False):
        if error is not None:
            raise error
        return subprocess.CompletedProcess(args=[], returncode=returncode)
    return run


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [(7, "aprovada"), (3, "recusada"), (0, "erro_na_janela"), (1, "erro_na_janela"), (5, "erro_na_janela")],
)
def test_only_exit_code_7_approves(monkeypatch, returncode, expected):
    monkeypatch.setattr(aprovacao, "_run", _fake_run(returncode))
    assert aprovacao.ask_windows("t", "x", 1) == expected


def test_timeout_and_os_errors_never_approve(monkeypatch):
    monkeypatch.setattr(aprovacao, "_run", _fake_run(error=subprocess.TimeoutExpired("powershell", 1)))
    assert aprovacao.ask_windows("t", "x", 1) == "sem_resposta"
    monkeypatch.setattr(aprovacao, "_run", _fake_run(error=OSError("não achou")))
    assert aprovacao.ask_windows("t", "x", 1) == "erro_na_janela"


def test_script_has_no_keyboard_path_to_approve():
    script = aprovacao._SCRIPT
    assert "$send.Text = 'Enviar ordem'" in script and "&" not in "Enviar ordem"
    assert "$send.UseMnemonic = $false" in script and "$send.TabStop = $false" in script
    assert "$send.Enabled = $false" in script and "ElapsedMilliseconds -ge $delay" in script
    assert "$form.AcceptButton = $cancel" in script and "$form.CancelButton = $cancel" in script
    assert "[void]$cancel.Focus()" in script
    assert script.count("$state.ok = $true") == 1  # só no clique de mouse do botão de enviar
    # Só o clique esquerdo do mouse aprova: teclado (setas + Enter/espaço) e PerformClick geram Click, não MouseClick.
    assert "$send.Add_MouseClick(" in script and "MouseButtons]::Left" in script
    assert "$send.Add_Click(" not in script
    assert "$send.Add_GotFocus({ [void]$cancel.Focus() })" in script  # foco no botão volta para Cancelar
    # A única saída que aprova é condicionada ao clique.
    assert script.count("exit 7") == 1 and "if ($state.ok) { exit 7 } else { exit 3 }" in script
    assert aprovacao.ENABLE_DELAY_MS >= 2000


def test_text_goes_by_environment_not_by_the_command_line(monkeypatch):
    seen = {}

    def run(command, env, **kwargs):
        seen.update(command=command, env=env)
        return subprocess.CompletedProcess(args=command, returncode=3)

    monkeypatch.setattr(subprocess, "run", run)
    assert aprovacao.ask_windows("Título", "'; exit 7; $(calc)", 1) == "recusada"
    assert "exit 7; $(calc)" not in " ".join(seen["command"])
    assert seen["env"]["TRADING_MCP_APROVACAO_TEXTO"] == aprovacao._b64("'; exit 7; $(calc)")
    assert seen["command"][0].lower().endswith("windowspowershell\\v1.0\\powershell.exe")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Forms só no Windows")
def test_window_builds_without_showing():
    assert aprovacao.self_test() is True  # monta a janela e sai sem mostrá-la
