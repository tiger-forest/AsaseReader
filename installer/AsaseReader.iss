; 浅濑阅读器 Asase Reader —— Inno Setup 6 安装脚本
; 方案：.workbuddy/memory/plan-installer-package.md §4
; 版本宏来自 tools/gen_version_info.py 生成的 installer/version.iss，勿手写版本号。
; UTF-8 with BOM：Unicode Inno Setup 需要 BOM 才能正确读中文向导文案。
; 注意（CP3 时点）：本机 ISCC.exe 未安装，本脚本**尚未编译验证**，
; 编译与真机安装/卸载留到 CP8 人工验收。

#include "version.iss"

[Setup]
AppId={{9A4F2C31-7B8E-4D05-A6C3-2E1F8B7D4C90}
AppName={#MyAppProduct}
AppVersion={#MyAppVersion}
AppVerName=浅濑阅读器 {#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion4}
VersionInfoProductVersion={#MyAppVersion4}
VersionInfoCompany={#MyAppPublisher}
VersionInfoCopyright={#MyAppCopyright}
VersionInfoProductName={#MyAppProduct}
VersionInfoDescription=浅濑阅读器 Asase Reader
; A 路线：装在用户目录、免管理员；不弹「为所有用户安装」对话框，
; 避免误选需要管理员的模式。
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
DefaultDirName={localappdata}\Programs\AsaseReader
DefaultGroupName=浅濑阅读器
DisableProgramGroupPage=no
AllowNoIcons=yes
OutputDir=..\dist\installer
OutputBaseFilename=AsaseReaderSetup-{#MyAppVersion}
UninstallDisplayName=浅濑阅读器 {#MyAppVersion}
UninstallDisplayIcon={app}\AsaseReader.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; 用户拍板：向导仅简体中文，不做双语。
ShowLanguageDialog=no
; 不做程序内检查更新、不联网上报；唯一的联网行为是下面缺 WebView2 时
; 去微软官方域名取在线安装器，不带任何凭据、不碰 B 站。

[Languages]
; Inno Setup 官方安装包**不带中文语言文件**（本机 6.7.4 只有 29 种语言）。
; 因此把 .isl 随项目入库，用相对本 .iss 目录的路径引用：编译机无需改 Inno
; 安装目录，CI / 别人 clone 后也能复现。
; 来源：官方翻译分发页收录的简体中文翻译（kira-96 维护，适配 6.5.0+）
; https://github.com/kira-96/Inno-Setup-Chinese-Simplified-Translation
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
; 用户 2026-11-06 拍板：桌面快捷方式**默认勾选**（不加 unchecked）。
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式:"

[Files]
; 只收 onedir 产物，绝不把源码 / 夹具 / 缓存 / 书库带进安装包。
Source: "..\dist\onedir\AsaseReader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\浅濑阅读器"; Filename: "{app}\AsaseReader.exe"; WorkingDir: "{app}"
Name: "{group}\卸载 浅濑阅读器"; Filename: "{uninstallexe}"
Name: "{autodesktop}\浅濑阅读器"; Filename: "{app}\AsaseReader.exe"; Tasks: desktopicon; WorkingDir: "{app}"

[Run]
; 可勾选、默认不勾（AI 默认，可否决）；勾选后正好接上 2.0.0 的首启书库模态。
Filename: "{app}\AsaseReader.exe"; Description: "立即启动 浅濑阅读器"; Flags: nowait postinstall skipifsilent unchecked

[UninstallDelete]
; 【硬约束】此段绝不列出 %LOCALAPPDATA%\AsaseReader 或任何书库路径。
; 覆盖升级 / 卸载只碰 {app}、快捷方式与 HKCU 卸载键；书库与设置一律保留。
; 下面这一行只删卸载后已空的程序目录本身，不触碰任何用户数据。
Type: dirifempty; Name: "{app}"

[Code]
const
  WV2_GUID = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  // 微软官方 WebView2 在线 Bootstrapper（约 1~2 MB）；不内置离线 Runtime。
  WV2_URL  = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703';

function HasWebView2(): Boolean;
var
  V: String;
begin
  Result := False;
  if RegQueryStringValue(HKEY_LOCAL_MACHINE,
       'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WV2_GUID,
       'pv', V) and (V <> '') then Result := True;
  if RegQueryStringValue(HKEY_CURRENT_USER,
       'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WV2_GUID,
       'pv', V) and (V <> '') then Result := True;
end;

{ 「允许改安装路径」与「数据目录固定」会冲突：装到数据目录上，
  卸载时配置和 WebView2 数据会被一起清掉。这里硬拒并给出具体原因。 }
function IsUnderDataDir(Dir: String): Boolean;
var
  DataDir, D: String;
begin
  DataDir := LowerCase(ExpandConstant('{localappdata}\AsaseReader'));
  D := LowerCase(Trim(Dir));
  while (Length(D) > 0) and (D[Length(D)] = '\') do
    D := Copy(D, 1, Length(D) - 1);
  Result := False;
  if D = DataDir then Result := True
  else if Copy(D, 1, Length(DataDir) + 1) = DataDir + '\' then Result := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    if IsUnderDataDir(WizardForm.DirEdit.Text) then
    begin
      MsgBox('不能把程序装到数据目录里：' + #13#10 +
             ExpandConstant('{localappdata}\AsaseReader') + #13#10 +
             '卸载时会把配置、界面偏好和 WebView2 数据一起删掉，请换一个目录。',
             mbError, MB_OK);
      Result := False;
    end;
  end;
end;

{ 缺 WebView2 时下载官方在线安装器；下载/安装失败**不阻断安装**，
  只提示用户手动装。Win10/11 大多自带，不能为少数机器挡住所有人。 }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Tmp: String;
  ResultCode: Integer;
begin
  Result := '';
  if HasWebView2() then Exit;
  try
    DownloadTemporaryFile(WV2_URL, 'MicrosoftEdgeWebview2Setup.exe', '', nil);
    Tmp := ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe');
    if FileExists(Tmp) then
      Exec(Tmp, '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  except
    Log('WebView2 bootstrapper download failed; continue installation.');
  end;
  if not HasWebView2() then
    MsgBox('未检测到 WebView2 运行时，自动安装也未成功。' + #13#10 +
           '程序可能白屏，请到微软官网安装「WebView2 运行时」后重试。' + #13#10 +
           '本次安装不会被中断。', mbInformation, MB_OK);
end;
