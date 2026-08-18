; Inno Setup script for OpenVZ Leads.
;
; Turns PyInstaller's folder build into the thing a Windows user expects: an
; installer that puts the app in Program Files, adds Start Menu and optional
; desktop shortcuts, and registers a proper uninstaller.
;
; Unsigned, so SmartScreen shows "Windows protected your PC" on first run —
; More info, then Run anyway. Same situation as the other OPENVZ builds; we
; have no code-signing certificate.

#define AppName        "OpenVZ Leads"
#define AppVersion     "1.0.0"
#define AppPublisher   "OPENVZ AI"
#define AppURL         "https://www.openvzai.com/leads"
#define AppExeName     "OpenVZ Leads.exe"

[Setup]
AppId={{8F3C1A62-4B7E-4E2D-9C1F-5A6D0E7B2C41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
InfoBeforeFile=windows-readme.txt
OutputBaseFilename=OpenVZ-Leads-{#AppVersion}-windows-x64-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Per-user install by default so no admin prompt is needed; lowest means the
; installer only asks for elevation if the user picks a machine-wide location.
PrivilegesRequiredOverridesAllowed=dialog
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\OpenVZ Leads\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The workspace under %APPDATA% is deliberately left alone: it holds the
; user's prospects, account briefs and edited prompts. Uninstalling the app
; should not throw away their work.
Type: dirifempty; Name: "{app}"
