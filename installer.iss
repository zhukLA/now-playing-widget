; Inno Setup — установщик NowPlayingWidget.
; Все пути относительно этого файла (dist — рядом с .iss).
;
; Сборка:  iscc installer.iss
;
; PrivilegesRequired=lowest — установка на текущего пользователя без прав администратора.

#define MyAppName "Now Playing Widget"
#define MyAppExeName "NowPlayingWidget.exe"
#define MyAppVersion "1.0.0"

[Setup]
; Фиксированный AppId: обновления поверх старой версии.
AppId={{5E1F3B7A-9C42-4D18-B6E4-2A70D9C51F83}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=NowPlayingWidget
DefaultDirName={localappdata}\Programs\NowPlayingWidget
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=NowPlayingWidgetSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
VersionInfoVersion={#MyAppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
; Русский — только если Languages\Russian.isl доступен у компилятора.
#if fileExists(CompilerPath + "Languages\Russian.isl")
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительно:"; Flags: unchecked
Name: "autostart"; Description: "Запускать вместе с Windows"; GroupDescription: "Дополнительно:"; Flags: unchecked

[Files]
; Путь dist — относительно расположения installer.iss.
Source: "dist\NowPlayingWidget\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

; Автозапуск через реестр HKCU\...\Run.
; ValueName должен совпадать с settings.AUTOSTART_RUN_VALUE_NAME.
[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueName: "NowPlayingWidget"; ValueType: string; \
    ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; \
    Description: "Запустить {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent
