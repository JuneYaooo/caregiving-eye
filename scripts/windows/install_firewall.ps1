# 以管理员身份运行: 给 Python 加 CS2 握手入站豁免 (摄像头回包来自动态端口)
# 用法: 右键"以管理员身份运行", 或 powershell -File install_firewall.ps1
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (py -3.9 -c "import sys; print(sys.executable)") }
Write-Host "Python: $py"
netsh advfirewall firewall delete rule name="caregiving_eye_py" | Out-Null
netsh advfirewall firewall add rule name="caregiving_eye_py" dir=in action=allow program="$py" enable=yes profile=any
Write-Host "防火墙规则 caregiving_eye_py 已添加"
