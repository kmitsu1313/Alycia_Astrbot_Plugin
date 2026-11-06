# 注册 Windows 计划任务：每 N 分钟跑一次 manictime_export.py，把 ManicTime 摘要写进 AstrBot 的 plugin_data。
# 用法（PowerShell）：  .\install_task.ps1            # 每 5 分钟
#                      .\install_task.ps1 -EveryMinutes 3
#                      .\install_task.ps1 -Uninstall
param(
    [int]$EveryMinutes = 5,
    [string]$TaskName = "AlyciaJournal-ManicTimeExport",
    [switch]$Uninstall
)

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "已删除计划任务 $TaskName"
    exit 0
}

$script = Join-Path $PSScriptRoot "manictime_export.py"
if (-not (Test-Path $script)) { throw "找不到 $script" }

# 计划任务对 WindowsApps 下的 python 别名（reparse point）支持不好，解析成真实 exe；优先无窗口的 pythonw。
$real = & python -c "import sys; print(sys.executable)"
if (-not $real) { throw "找不到 python" }
$pyw = Join-Path (Split-Path $real) "pythonw.exe"
$exe = if (Test-Path $pyw) { $pyw } else { $real }

$action   = New-ScheduledTaskAction -Execute $exe -Argument "`"$script`"" -WorkingDirectory $PSScriptRoot
$trigger  = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "已注册 $TaskName：每 $EveryMinutes 分钟运行"
Write-Host "  $exe `"$script`""
Write-Host "输出：C:\Users\uuuiii13\data\plugin_data\astrbot_plugin_alycia_journal\manictime\latest.json"
