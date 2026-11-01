# 以管理员身份运行: 给 Python 加 CS2 握手入站豁免 (摄像头回包来自动态端口)
# 用法: 右键"以管理员身份运行", 或 powershell -File install_firewall.ps1
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (py -3.9 -c "import sys; print(sys.executable)") }
Write-Host "Python: $py"
netsh advfirewall firewall delete rule name="caregiving_eye_py" | Out-Null
# 只放行: 家庭网络(private) + UDP + 本地子网来源；公共 Wi-Fi 下不开放
netsh advfirewall firewall add rule name="caregiving_eye_py" dir=in action=allow program="$py" enable=yes profile=private protocol=UDP remoteip=localsubnet
Write-Host "防火墙规则 caregiving_eye_py 已添加（仅家庭网络、UDP、本地子网）。若网络被识别为公用，请在 Windows 设置里改为专用网络。"
