"""Aprovação do usuário fora do chat (etapa F): uma janela do Windows com os dados exatos da ordem.

O servidor abre a janela num processo do PowerShell e espera o clique pelo código de saída desse processo: só o clique
em "Enviar ordem" aprova. Nada passa por arquivo que o modelo pudesse escrever. Sem clique até o fim do prazo, o
processo é encerrado (a janela fecha) e a ordem não é enviada.

Contra aprovação por acidente (a janela pode tomar o foco enquanto o usuário digita): só um clique com o botão
esquerdo do MOUSE em "Enviar ordem", depois de 2 s, aprova (evento MouseClick; teclado e PerformClick só geram Click,
que não aprova). O botão não tem letra de atalho nem para no Tab, e se receber o foco (pelas setas, por exemplo) o
foco volta para "Cancelar", que começa com o foco e responde ao Enter e ao Esc.
"""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

APPROVED = "aprovada"
DECLINED = "recusada"
NO_ANSWER = "sem_resposta"
WINDOW_ERROR = "erro_na_janela"
ENABLE_DELAY_MS = 2000
_APPROVED_EXIT, _DECLINED_EXIT, _DRY_EXIT = 7, 3, 5  # nem 0 nem 1: um erro do PowerShell nunca vira aprovação
# Caminho completo: com só "powershell.exe" o Windows procuraria primeiro na pasta atual.
_POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$decode = { param($v) [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($v)) }
$text = & $decode $env:TRADING_MCP_APROVACAO_TEXTO
$title = & $decode $env:TRADING_MCP_APROVACAO_TITULO
$delay = [int]$env:TRADING_MCP_APROVACAO_ATRASO_MS
$state = @{ ok = $false }

$form = New-Object System.Windows.Forms.Form
$form.Text = $title
$form.TopMost = $true
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.ShowInTaskbar = $true
$form.Font = New-Object System.Drawing.Font('Segoe UI', 10)
$form.ClientSize = New-Object System.Drawing.Size(560, 470)

$box = New-Object System.Windows.Forms.TextBox
$box.Multiline = $true
$box.ReadOnly = $true
$box.TabStop = $false
$box.ScrollBars = 'Vertical'
$box.Text = $text -replace "`r?`n", "`r`n"
$box.Location = New-Object System.Drawing.Point(16, 16)
$box.Size = New-Object System.Drawing.Size(528, 380)

$send = New-Object System.Windows.Forms.Button
$send.Text = 'Enviar ordem'
$send.UseMnemonic = $false
$send.TabStop = $false
$send.Enabled = $false
$send.Location = New-Object System.Drawing.Point(16, 414)
$send.Size = New-Object System.Drawing.Size(170, 40)

$cancel = New-Object System.Windows.Forms.Button
$cancel.Text = 'Cancelar'
$cancel.UseMnemonic = $false
$cancel.Location = New-Object System.Drawing.Point(374, 414)
$cancel.Size = New-Object System.Drawing.Size(170, 40)

$form.CancelButton = $cancel
$form.AcceptButton = $cancel
$form.Controls.AddRange(@($box, $send, $cancel))

$watch = [Diagnostics.Stopwatch]::StartNew()
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = [Math]::Max(1, $delay)
$timer.Add_Tick({ $send.Enabled = $true; $timer.Stop() })
$send.Add_MouseClick({
    param($sender, $e)
    if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left -and $watch.ElapsedMilliseconds -ge $delay) {
        $state.ok = $true
        $form.Close()
    }
})
$send.Add_GotFocus({ [void]$cancel.Focus() })
$cancel.Add_Click({ $form.Close() })
$form.Add_Shown({ $form.Activate(); [void]$cancel.Focus(); $timer.Start() })

if ($env:TRADING_MCP_APROVACAO_SECO -eq '1') { $form.Dispose(); exit 5 }
[void]$form.ShowDialog()
if ($state.ok) { exit 7 } else { exit 3 }
"""


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _run(title: str, text: str, timeout_s: float, dry: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TRADING_MCP_APROVACAO_TEXTO"] = _b64(text)
    env["TRADING_MCP_APROVACAO_TITULO"] = _b64(title)
    env["TRADING_MCP_APROVACAO_ATRASO_MS"] = str(ENABLE_DELAY_MS)
    env["TRADING_MCP_APROVACAO_SECO"] = "1" if dry else "0"
    encoded = base64.b64encode(_SCRIPT.encode("utf-16-le")).decode("ascii")
    command = [str(_POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # sem janela de console; a janela de aprovação aparece
    return subprocess.run(command, env=env, timeout=timeout_s, capture_output=True, creationflags=flags, check=False,
                          stdin=subprocess.DEVNULL)


def ask_windows(title: str, text: str, timeout_s: float) -> str:
    """Mostra a janela e devolve 'aprovada', 'recusada', 'sem_resposta' ou 'erro_na_janela'."""
    try:
        done = _run(title, text, timeout_s)
    except subprocess.TimeoutExpired:
        return NO_ANSWER
    except OSError:
        return WINDOW_ERROR
    if done.returncode == _APPROVED_EXIT:
        return APPROVED
    return DECLINED if done.returncode == _DECLINED_EXIT else WINDOW_ERROR


def self_test(timeout_s: float = 30) -> bool:
    """Monta a janela sem mostrá-la (confere o PowerShell e o Windows Forms); True se deu certo."""
    try:
        return _run("teste", "teste", timeout_s, dry=True).returncode == _DRY_EXIT
    except (subprocess.TimeoutExpired, OSError):
        return False
