# Register Telegram watchdog to start at Windows logon for this user.
# Run once from an elevated or normal PowerShell in the project root:
#   powershell -ExecutionPolicy Bypass -File scripts\start_telegram_at_login.ps1

$Project = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Py = Join-Path $Project "venv\Scripts\python.exe"
$Watch = Join-Path $Project "run_telegram_watchdog.py"
$TaskName = "KalshiTelegramWatchdog"

if (-not (Test-Path $Py)) { throw "Missing venv python: $Py" }
if (-not (Test-Path $Watch)) { throw "Missing watchdog: $Watch" }

$action = New-ScheduledTaskAction -Execute $Py -Argument "`"$Watch`"" -WorkingDirectory $Project
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output "Registered and started task: $TaskName"
Write-Output "Telegram bridge will auto-start at each Windows login."
