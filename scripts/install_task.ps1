# Enregistre (ou retire) la tâche planifiée Windows qui lance le bot à intervalle régulier.
#
#   .\scripts\install_task.ps1                      toutes les 60 minutes
#   .\scripts\install_task.ps1 -EveryMinutes 30
#   .\scripts\install_task.ps1 -Uninstall
#
# La tâche tourne sous votre compte, uniquement quand vous êtes connecté, sans fenêtre visible.
# Si l'ordinateur était éteint à l'heure prévue, elle se lance dès que possible ensuite.
param(
    [int]$EveryMinutes = 60,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$name = "Wikipedia-TCG-bot"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "Tâche « $name » supprimée."
    return
}

$script = Join-Path $PSScriptRoot "run_bot.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "Ouvre les boosters gratuits des jeux de cartes Wikipédia (dépôt Wikipedia-TCG-bot)." `
    -Force | Out-Null
Write-Host "Tâche « $name » enregistrée : toutes les $EveryMinutes minutes."
Write-Host "Journal : .state\logs\bot.log   Historique des tirages : .state\history.jsonl"
