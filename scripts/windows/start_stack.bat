@echo off
REM 只启动视频 + 云台栈：go2rtc（需补丁版，见接入指南）+ xiaomi-onvif 桥
REM 推荐改用 python main.py serve：同时拉起这两个程序和执事，崩溃自动重启
REM 前置：仓库根目录已有 go2rtc.yaml、go2rtc.exe、xiaomi-onvif.exe
REM 所有端口只监听 127.0.0.1，不对局域网开放
cd /d %~dp0\..\..
start "go2rtc" go2rtc.exe -config go2rtc.yaml
timeout /t 4 /nobreak >nul
start "onvif" xiaomi-onvif.exe -go2rtc=http://127.0.0.1:1984 -rtsp=rtsp://127.0.0.1:8554 -camera=living=127.0.0.1:8891,living
echo 已启动: RTSP=rtsp://127.0.0.1:8554/living  ONVIF=127.0.0.1:8891
