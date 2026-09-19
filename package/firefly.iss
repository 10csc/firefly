; Firefly —— Windows 一键安装包
[Setup]
AppId={{F1E7A3C5-9B2D-4E6A-8F1C-3D5B7A9E0C41}
AppName=Firefly
AppVersion=0.9.0
AppPublisher=Firefly Project
DefaultDirName={localappdata}\Programs\Firefly
DefaultGroupName=Firefly
DisableProgramGroupPage=yes
OutputDir=.
OutputBaseFilename=firefly-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
UninstallDisplayName=Firefly
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=firefly.ico
; 自动更新必须：安装前关闭运行中的 firefly.exe（否则文件锁导致覆盖失败）
CloseApplications=yes
CloseApplicationsFilter=firefly.exe

[Files]
Source: "..\dist\firefly\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion; Excludes: "user_data"

[InstallDelete]
; 强制删除旧快捷方式，确保图标随新 exe 刷新
Type: files; Name: "{autodesktop}\Firefly.lnk"
Type: files; Name: "{autoprograms}\Firefly.lnk"
Type: files; Name: "{userdesktop}\Firefly.lnk"

[Icons]
Name: "{autoprograms}\Firefly"; Filename: "{app}\firefly.exe"
Name: "{autodesktop}\Firefly"; Filename: "{app}\firefly.exe"

[Run]
Filename: "{app}\firefly.exe"; Description: "启动 Firefly"; Flags: nowait postinstall skipifsilent
