# Lance le bot depuis n'importe quel dossier : c'est ce que fait la tâche planifiée.
# Arguments facultatifs, transmis tels quels : .\run_bot.ps1 --sites wikitcg_net --dry-run
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
& (Join-Path $root ".venv\Scripts\python.exe") -m wikitcg_bot run @args
exit $LASTEXITCODE
