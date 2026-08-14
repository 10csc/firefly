; 娴佽悿 Firefly 鈥?Windows 涓€閿畨瑁呭寘
[Setup]
AppId={{F1E7A3C5-9B2D-4E6A-8F1C-3D5B7A9E0C41}
AppName=娴佽悿 Firefly
AppVersion=0.7.2
AppPublisher=Firefly Project
DefaultDirName={localappdata}\Programs\Firefly
DefaultGroupName=娴佽悿 Firefly
DisableProgramGroupPage=yes
OutputDir=.
OutputBaseFilename=firefly-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
UninstallDisplayName=娴佽悿 Firefly
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=firefly.ico
; 鑷姩鏇存柊蹇呴』锛氬畨瑁呭墠鍏抽棴杩愯涓殑 firefly.exe锛堝惁鍒欐枃浠堕攣瀵艰嚧瑕嗙洊澶辫触锛?
CloseApplications=yes
CloseApplicationsFilter=firefly.exe

[Files]
Source: "..\dist\firefly\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion; Excludes: "user_data"

[InstallDelete]
; 寮哄埗鍒犻櫎鏃у揩鎹锋柟寮忥紝纭繚鍥炬爣闅忔柊 exe 鍒锋柊
Type: files; Name: "{autodesktop}\娴佽悿 Firefly.lnk"
Type: files; Name: "{autoprograms}\娴佽悿 Firefly.lnk"
Type: files; Name: "{userdesktop}\娴佽悿 Firefly.lnk"

[Icons]
Name: "{autoprograms}\娴佽悿 Firefly"; Filename: "{app}\firefly.exe"
Name: "{autodesktop}\娴佽悿 Firefly"; Filename: "{app}\firefly.exe"

[Run]
Filename: "{app}\firefly.exe"; Description: "鍚姩娴佽悿"; Flags: nowait postinstall skipifsilent
