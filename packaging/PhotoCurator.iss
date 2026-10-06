#define MyAppName "PhotoCurator"
#define MyAppVersion "1.4.2"
#define MyAppPublisher "PhotoCurator-CN"
#define MyAppExeName "PhotoCurator.exe"

[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"

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
UsePreviousAppDir=yes
Uninstallable=yes
ShowLanguageDialog=no
AppMutex=Local\PhotoCurator_CN_youzh1701
VersionInfoVersion=1.4.2.0
SetupIconFile=PhotoCurator.ico
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式"; Flags: unchecked

[InstallDelete]
; 覆盖升级前清理旧程序文件，避免 PyInstaller 旧模块残留；data 目录不受影响。
Type: filesandordirs; Name: "{app}\app"

[Files]
Source: "..\dist\PhotoCurator\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs restartreplace
Source: "MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Dirs]
Name: "{app}\data"
Name: "{app}\data\config"
Name: "{app}\data\cache"
Name: "{app}\data\logs"

[Icons]
Name: "{autoprograms}\PhotoCurator"; Filename: "{app}\app\{#MyAppExeName}"; WorkingDir: "{app}\app"
Name: "{autodesktop}\PhotoCurator"; Filename: "{app}\app\{#MyAppExeName}"; WorkingDir: "{app}\app"; Tasks: desktopicon

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "正在准备 Microsoft WebView2 运行组件..."; Flags: waituntilterminated runhidden
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
