#define MyAppName "PhotoCurator"
#define MyAppVersion "1.3.0"
#define MyAppPublisher "PhotoCurator-CN"
#define MyAppExeName "PhotoCurator.exe"

[Setup]
AppId={{8B142F6F-E58F-4F7D-97B9-3DA5E82602A4}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\PhotoCurator
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\release
OutputBaseFilename=PhotoCurator-Setup-v{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#MyAppName}
SetupLogging=yes
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式"; Flags: unchecked

[Files]
Source: "..\dist\PhotoCurator\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{app}\data"
Name: "{app}\data\config"
Name: "{app}\data\cache"
Name: "{app}\data\logs"

[Icons]
Name: "{autoprograms}\PhotoCurator"; Filename: "{app}\app\{#MyAppExeName}"; WorkingDir: "{app}\app"
Name: "{autodesktop}\PhotoCurator"; Filename: "{app}\app\{#MyAppExeName}"; WorkingDir: "{app}\app"; Tasks: desktopicon

[Run]
Filename: "{app}\app\{#MyAppExeName}"; Description: "打开 PhotoCurator"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\data\cache"
Type: filesandordirs; Name: "{app}\data\logs"
Type: filesandordirs; Name: "{app}\data\内置测试数据"

[Code]
var
  DeleteSettings: Boolean;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    DeleteSettings := MsgBox(
      '是否同时删除 PhotoCurator 的设置？' + #13#10 +
      '无论选择什么，都不会删除你的原照片、筛选结果或自定义输出目录。',
      mbConfirmation, MB_YESNO) = IDYES;

  if (CurUninstallStep = usPostUninstall) and DeleteSettings then
    DelTree(ExpandConstant('{app}\data\config'), True, True, True);
end;
