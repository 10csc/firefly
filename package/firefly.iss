; 流萤 Firefly — Windows 一键安装包
[Setup]
AppId={{F1E7A3C5-9B2D-4E6A-8F1C-3D5B7A9E0C41}
AppName=流萤 Firefly
AppVersion=0.6.0
AppPublisher=Firefly Project
DefaultDirName={localappdata}\Programs\Firefly
DefaultGroupName=流萤 Firefly
DisableProgramGroupPage=yes
OutputDir=.
OutputBaseFilename=firefly-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
UninstallDisplayName=流萤 Firefly
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "F:\CodeFile\firefly\dist\firefly\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\流萤 Firefly"; Filename: "{app}\firefly.exe"
Name: "{autodesktop}\流萤 Firefly"; Filename: "{app}\firefly.exe"

[Run]
Filename: "{app}\firefly.exe"; Description: "启动流萤"; Flags: nowait postinstall skipifsilent
