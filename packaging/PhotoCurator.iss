#define MyAppName "PhotoCurator"
#ifndef MyAppVersion
  #define MyAppVersion "1.7.19"
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
; Never terminate active photo operations during upgrade. The preflight guard
; refuses to replace program files until the previous app exits normally.
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
; DelTree/filesandordirs can be dangerous if runtime cache directories were
; replaced with junctions pointing to real photographs. Uninstalling the
; PROGRAM must not traverse or delete ANY user-writable runtime directory.
; Runtime cache, offline previews, catalog, history and the recycle bin are
; retained. Users may clear rebuildable cache through the app's guarded
; storage-management controls BEFORE uninstalling.

[Code]
const
  PhotoCuratorMutex = 'Local\PhotoCurator_CN_youzh1701';

var
  ClearRecents: Boolean;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  { Never force-kill the application during file processing. An unclean
    shutdown could leave an unfinished move or a pending catalog update. }
  if CheckForMutexes(PhotoCuratorMutex) then
  begin
    Log('PhotoCurator still running; postponing upgrade safely.');
    Result := 'PhotoCurator 仍在运行。请先从右下角系统托盘选择“退出”，'
      + '等待正在执行的照片任务结束，然后重新运行安装程序。'
      + '安装不会删除已保存的图库和用户数据。';
    Exit;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    { An unattended upgrade/uninstall must never block on a custom dialog.
      Silent uninstall always retains all user data and recent folders. }
    ClearRecents := False;
    if not UninstallSilent then
      ClearRecents := MsgBox(
      '是否清除 PhotoCurator 最近打开的文件夹记录？' + #13#10 +
      '图库索引、人工筛选决策、离线预览、日志和软件回收站将全部保留。' + #13#10 +
      '卸载程序不会递归删除任何照片目录或运行数据。',
      mbConfirmation, MB_YESNO) = IDYES;
  end;

  if (CurUninstallStep = usPostUninstall) and ClearRecents then
  begin
    { Delete only two explicitly named regular configuration entries. Never
      recursively traverse any user-modifiable directory during uninstall. }
    DeleteFile(ExpandConstant('{localappdata}\PhotoCurator\data\config\recents.json'));
    DeleteFile(ExpandConstant('{app}\data\config\recents.json'));
  end;
end;
