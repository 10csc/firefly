; Firefly —— Windows 一键安装包
[Setup]
AppId={{F1E7A3C5-9B2D-4E6A-8F1C-3D5B7A9E0C41}
AppName=Firefly
AppVersion=0.9.1
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

[UninstallDelete]
; P3.6 / 02 §G4：卸载时清掉**运行期生成**的插件/缓存目录（它们不在 [Files] 登记范围内）。
;   Inno 只删自己在 [Files] 里装过的文件（下表 Excludes 仅排除 user_data）⇒ 运行期生成的
;   目录若不在此显式声明，卸载后会在 {app} 留下残留。
; ★ 取舍总则：voice / hotupdate / notice / logs = **可再生或运行期产物，卸载即清**；
;   user_data = **用户数据（对话 / API Key / 角色包），绝不在此列。**
;   ① {app}\voice     语音插件根（与 user_data 平级，见 02 §G4）。模型 8 文件 872MB 由插件页
;      运行期从 ModelScope 下载到 {app}\voice\models\ —— 这正是 §G4 记的 872MB 残留缺口。
;   ② {app}\hotupdate 热更新覆盖层（web/py 两层 + state.json）。换入即替换旧层、patch-*.zip
;      用后即删 ⇒ 不会累积；整包升级/回滚也会清它；联网可重下（可再生）。
;   ③ {app}\notice    公告缓存（横幅图 + state.json），联网自动重建（可再生）。
;   ④ {app}\logs      运行日志，事后排查价值归零后即清。
; ★ 只影响**卸载**：覆盖安装/升级时 [Files] 仍照常保留（voice 等**未**计入 [Files] 排除，
;   否则会变成"覆盖安装删模型"，与本条目的正好相反）。
Type: filesandordirs; Name: "{app}\voice"
Type: filesandordirs; Name: "{app}\hotupdate"
Type: filesandordirs; Name: "{app}\notice"
Type: filesandordirs; Name: "{app}\logs"

[Icons]
Name: "{autoprograms}\Firefly"; Filename: "{app}\firefly.exe"
Name: "{autodesktop}\Firefly"; Filename: "{app}\firefly.exe"

[Run]
Filename: "{app}\firefly.exe"; Description: "启动 Firefly"; Flags: nowait postinstall skipifsilent
