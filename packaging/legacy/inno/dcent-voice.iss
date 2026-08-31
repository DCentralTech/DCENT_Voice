; DCENT_Voice — open-source, local-first voice dictation
; Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
; SPDX-License-Identifier: MIT
;
; Unsigned Windows installer. Signing/notarization is an environment
; credential step (Authenticode). This script is the complete product
; installer pipeline: Start Menu, uninstall registry, per-user install.
;
; Build (when Inno Setup is installed):
;   .\scripts\build_inno_installer.ps1

#define MyAppName "DCENT_Voice"
#define MyAppVersion "0.2.0b1"
#define MyAppPublisher "D-Central Technologies"
#define MyAppExeName "dcent-voice.exe"
#ifndef SealedPayload
  #error SealedPayload must name a fresh tree created by model_registry stage-payload
#endif

[Setup]
AppId={{A7C3D1E0-5B92-4F18-9C41-DCENTVOICE0001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\DCENT_Voice
DefaultGroupName=DCENT_Voice
PrivilegesRequired=lowest
Compression=lzma2
SolidCompression=yes
OutputDir=..\..\dist
OutputBaseFilename=DCENT_Voice-Setup
SetupIconFile=..\dcent-voice.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
WizardStyle=modern
DisableProgramGroupPage=yes
; SignTool=authenticode  ; activate when Authenticode credentials exist

[Files]
; Created only by `model_registry stage-payload`; never point Inno at the mutable build tree.
Source: "{#SealedPayload}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "verify-installed.ps1"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{group}\DCENT_Voice"; Filename: "{app}\{#MyAppExeName}"
Name: "{userstartup}\DCENT_Voice"; Filename: "{app}\{#MyAppExeName}"; Tasks: startup

[Tasks]
Name: "startup"; Description: "Launch DCENT_Voice at sign-in"; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch DCENT_Voice"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    if (not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
                 '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
                 ExpandConstant('{tmp}\verify-installed.ps1') + '" -Executable "' +
                 ExpandConstant('{app}\{#MyAppExeName}') + '" -Payload "' +
                 ExpandConstant('{app}') + '" -TimeoutSeconds 300',
                 ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated,
                 ResultCode)) or (ResultCode <> 0) then
      RaiseException('Installed speech-model verification failed. Installation was aborted.');
  end;
end;

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
