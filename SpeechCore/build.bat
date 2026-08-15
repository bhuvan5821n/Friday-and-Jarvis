@echo off
rem SpeechCore build — portable MinGW-w64 on D:, no MSVC needed.
set GPP=D:\toolchains\mingw64\bin\g++.exe
"%GPP%" -std=c++20 -O2 src\speechcore.cpp -o Build\SpeechCore.exe ^
    -lole32 -lws2_32 -lavrt -static -mwindows
if %errorlevel%==0 (echo Built Build\SpeechCore.exe) else (echo BUILD FAILED & exit /b 1)
