!ifndef ARCH
!define ARCH "x64"
!endif
; ===== Istakoza POS - one-click installer (portable Python 3.8 bundled: works on Windows 7 SP1 / 8 / 10 / 11) =====
; Build:  makensis -DARCH=x86 setup.nsi   or   makensis -DARCH=x64 setup.nsi
Unicode true
!include "MUI2.nsh"
Name "Istakoza POS"
!if "${ARCH}" == "x64"
  OutFile "IstakozaPOS_Setup_64bit.exe"
  InstallDir "$PROGRAMFILES64\IstakozaPOS"
  !define PYDIR "py64"
!else
  OutFile "IstakozaPOS_Setup_32bit.exe"
  InstallDir "$PROGRAMFILES\IstakozaPOS"
  !define PYDIR "py32"
!endif
RequestExecutionLevel admin
SetCompressor /SOLID lzma
AutoCloseWindow true
ShowInstDetails nevershow
BrandingText "Istakoza POS"
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "Install"
  SetShellVarContext all          ; $APPDATA = C:\ProgramData (the database lives there)
  nsExec::Exec 'taskkill /F /IM IstakozaPOS.exe'
  Sleep 1500
  RMDir /r "$INSTDIR\py"
  SetOutPath "$INSTDIR\py"
  File /r "${PYDIR}\*.*"          ; portable Python 3.8 (includes the Windows 7 C runtime)
  SetOutPath "$INSTDIR"
  File "server.py"
  File "pos_istakoza.html"
  Delete "$INSTDIR\start_pos.bat"
  Delete "$INSTDIR\pos_client.bat"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$APPDATA\Istakoza"
  CreateDirectory "$APPDATA\Istakoza\backups"
  nsExec::Exec 'icacls "$APPDATA\Istakoza" /grant *S-1-5-32-545:(OI)(CI)M /T'
  ; precompile once (as admin) so it starts fast for normal users
  nsExec::Exec '"$INSTDIR\py\IstakozaPOS.exe" -m compileall -q "$INSTDIR\py\Lib" "$INSTDIR\server.py"'
  ; firewall so other devices (PCs / phones) can open the POS
  nsExec::Exec 'netsh advfirewall firewall delete rule name="IstakozaPOS"'
  nsExec::Exec 'netsh advfirewall firewall add rule name="IstakozaPOS" dir=in action=allow protocol=TCP localport=8080 profile=any'
  ; shortcuts (the program itself opens the POS window with silent printing)
  CreateShortCut "$DESKTOP\Istakoza POS.lnk" "$INSTDIR\py\IstakozaPOS.exe" '"$INSTDIR\server.py"' "$INSTDIR\py\IstakozaPOS.exe" 0 SW_SHOWMINIMIZED
  CreateShortCut "$SMPROGRAMS\Istakoza POS.lnk" "$INSTDIR\py\IstakozaPOS.exe" '"$INSTDIR\server.py"' "$INSTDIR\py\IstakozaPOS.exe" 0 SW_SHOWMINIMIZED
  Delete "$SMPROGRAMS\Istakoza POS Server.lnk"
  CreateShortCut "$SMSTARTUP\Istakoza POS Server.lnk" "$INSTDIR\py\IstakozaPOS.exe" '"$INSTDIR\server.py" --no-browser' "$INSTDIR\py\IstakozaPOS.exe" 0 SW_SHOWMINIMIZED
  WriteRegStr HKLM "Software\IstakozaPOS" "InstallDir" "$INSTDIR"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\IstakozaPOS" "DisplayName" "Istakoza POS"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\IstakozaPOS" "UninstallString" '"$INSTDIR\Uninstall.exe"'
SectionEnd

Function .onInstSuccess
  SetOutPath "$INSTDIR"
  Exec '"$INSTDIR\py\IstakozaPOS.exe" "$INSTDIR\server.py"'
FunctionEnd

Section "Uninstall"
  SetShellVarContext all
  nsExec::Exec 'taskkill /F /IM IstakozaPOS.exe'
  Sleep 1500
  nsExec::Exec 'netsh advfirewall firewall delete rule name="IstakozaPOS"'
  RMDir /r "$INSTDIR"
  Delete "$DESKTOP\Istakoza POS.lnk"
  Delete "$SMPROGRAMS\Istakoza POS.lnk"
  Delete "$SMSTARTUP\Istakoza POS Server.lnk"
  DeleteRegKey HKLM "Software\IstakozaPOS"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\IstakozaPOS"
  ; NOTE: the database (ProgramData\Istakoza) is kept on purpose
SectionEnd
