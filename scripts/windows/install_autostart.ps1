# 开机自启：登录 Windows 后自动以守护模式启动米执事（python main.py serve）
# 用法：在仓库根目录右键“用 PowerShell 运行”，或：
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1 -Remove   # 取消
# 守护进程会顺带拉起 go2rtc 和 xiaomi-onvif（仓库根目录里有这两个程序时），崩溃或卡死自动重启。
# 想让电脑没人登录也运行，可以在“任务计划程序”里把该任务改为“不管用户是否登录都要运行”。
param([switch]$Remove, [string]$ServeArgs = "")
$ErrorActionPreference = "Stop"
$name = "MiButler"
if ($Remove) {
    schtasks /Delete /TN $name /F
    Write-Host "已取消开机自启"
    exit
}
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (py -3 -c "import sys; print(sys.executable)") }
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
if (Test-Path $pyw) { $py = $pyw }   # 用 pythonw 不弹黑窗口；输出都在 events\logs 里
$action = New-ScheduledTaskAction -Execute $py -Argument "`"$root\main.py`" serve $ServeArgs" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "已设置开机自启：登录后运行 $py `"$root\main.py`" serve $ServeArgs"
Write-Host "立即启动：schtasks /Run /TN $name    查看日志：$root\events\logs\"
