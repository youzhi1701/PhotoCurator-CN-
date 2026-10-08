#define MyAppName "PhotoCurator"
#ifndef MyAppVersion
  #define MyAppVersion "1.7.3"
#endif
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
VersionInfoVersion={#MyAppVersion}.0
SetupIconFile=PhotoCurator.ico
; Upgrades terminate a running PhotoCurator instance automatically so mapped
; program files can be replaced without asking the user to close it manually.
CloseApplications=no
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式"; Flags: unchecked

[InstallDelete]
; 覆盖升级只清理可替换程序文件。用户运行数据位于 {localappdata}\PhotoCurator\data，
; 旧版 {app}\data 也不会在覆盖阶段删除，以便应用首次启动完成安全迁移。
Type: filesandordirs; Name: "{app}\app"

[Files]
Source: "..\dist\PhotoCurator\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs restartreplace
Source: "MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Dirs]
Name: "{localappdata}\PhotoCurator\data"
Name: "{localappdata}\PhotoCurator\data\config"
Name: "{localappdata}\PhotoCurator\data\cache"
Name: "{localappdata}\PhotoCurator\data\logs"

[Icons]
Name: "{autoprograms}\PhotoCurator"; Filename: "{app}\app\{#MyAppExeName}"; WorkingDir: "{app}\app"
Name: "{autodesktop}\PhotoCurator"; Filename: "{app}\app\{#MyAppExeName}"; WorkingDir: "{app}\app"; Tasks: desktopicon

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "正在准备 Microsoft WebView2 运行组件..."; Flags: waituntilterminated runhidden
Filename: "{app}\app\{#MyAppExeName}"; Description: "打开 PhotoCurator"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 可重建数据始终清理；用户决策与索引配置仅在卸载确认后删除。
Type: filesandordirs; Name: "{localappdata}\PhotoCurator\data\cache"
Type: filesandordirs; Name: "{localappdata}\PhotoCurator\data\logs"
Type: filesandordirs; Name: "{localappdata}\PhotoCurator\data\内置测试数据"
; 清理旧版安装目录中遗留的可重建数据，但保留 legacy config 作为安全回退。
Type: filesandordirs; Name: "{app}\data\cache"
Type: filesandordirs; Name: "{app}\data\logs"
Type: filesandordirs; Name: "{app}\data\内置测试数据"

[Code]
const
  PhotoCuratorMutex = 'Local\PhotoCurator_CN_youzh1701';

var
  DeleteSettings: Boolean;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  I: Integer;
begin
  Result := '';

  if CheckForMutexes(PhotoCuratorMutex) then
  begin
    Log('PhotoCurator is running; closing the old instance automatically before upgrade.');

    { taskkill is scoped to the product executable and /T also closes a
      transient child picker if one exists. }
    if not Exec(
      ExpandConstant('{sys}\taskkill.exe'),
      '/F /T /IM "{#MyAppExeName}"',
      '',
      SW_HIDE,
      ewWaitUntilTerminated,
      ResultCode
    ) then
    begin
      Result := '无法自动关闭正在运行的 PhotoCurator。请稍后重试安装。';
      Exit;
    end;

    { Wait for Windows to release the named mutex and mapped executable files. }
    for I := 1 to 30 do
    begin
      if not CheckForMutexes(PhotoCuratorMutex) then
        Break;
      Sleep(100);
    end;

    if CheckForMutexes(PhotoCuratorMutex) then
    begin
      Result := '正在运行的 PhotoCurator 未能自动退出。请稍后重试安装。';
      Exit;
    end;

    Sleep(250);
    Log('Previous PhotoCurator instance stopped; continuing in-place upgrade.');
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    DeleteSettings := MsgBox(
      '是否同时删除 PhotoCurator 的设置？' + #13#10 +
      '无论选择什么，都不会删除你的原照片、筛选结果、自定义输出目录或 PhotoCurator 软件回收站。',
      mbConfirmation, MB_YESNO) = IDYES;

  if (CurUninstallStep = usPostUninstall) and DeleteSettings then
  begin
    DelTree(ExpandConstant('{localappdata}\PhotoCurator\data\config'), True, True, True);
    DelTree(ExpandConstant('{app}\data\config'), True, True, True);
  end;
end;
