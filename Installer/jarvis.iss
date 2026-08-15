; JARVIS installer — Inno Setup 6 script
; Build the exes first:  python build_app.py
; Then compile this with Inno Setup Compiler → Installer/JarvisSetup.exe
; User data (config, memory, logs) stays in the install dir and survives updates.

#define AppName "JARVIS"
#define AppVersion "1.0.0"
#define AppDir "..\dist\Jarvis"

[Setup]
AppId={{8F4B2C6E-JARVIS-DESKTOP-ASSISTANT}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Bhuvan
DefaultDirName={autopf}\Jarvis
DefaultGroupName=JARVIS
DisableProgramGroupPage=yes
OutputDir=.
OutputBaseFilename=JarvisSetup
SetupIconFile=..\assets\jarvis.ico
UninstallDisplayIcon={app}\Jarvis.exe
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
; lowest = installs per-user (no admin), autostart via HKCU works cleanly

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart";   Description: "Start JARVIS automatically with Windows"; GroupDescription: "Startup:"

[Files]
Source: "{#AppDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\JARVIS";                Filename: "{app}\Jarvis.exe"
Name: "{group}\Uninstall JARVIS";      Filename: "{uninstallexe}"
Name: "{autodesktop}\JARVIS";          Filename: "{app}\Jarvis.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "JarvisService"; \
    ValueData: """{app}\JarvisService.exe"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\Jarvis.exe"; Description: "Launch JARVIS now"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; nothing destructive — memory/config are ordinary files removed with the app dir
