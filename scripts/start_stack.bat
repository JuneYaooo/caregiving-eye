@echo off
REM 启动视频+云台栈: go2rtc(需补丁版, 见README) + xiaomi-onvif 桥
REM 前置: config.json 已配置; go2rtc.exe / xiaomi-onvif.exe 已下载到本目录 (见 README 下载地址)
cd /d %~dp0
start "go2rtc" go2rtc.exe
timeout /t 4 /nobreak >nul
start "onvif" xiaomi-onvif.exe -go2rtc=http://127.0.0.1:1984 -rtsp=rtsp://127.0.0.1:8554 -camera=living=:8891
echo 栈已启动: RTSP=rtsp://127.0.0.1:8554/living  ONVIF=:8891
