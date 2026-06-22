; AutoCat Windows NSIS 安装脚本
; 用法: makensis packaging/AutoCat-win.nsi
; 或通过 build_win.py 自动调用
;
; 依赖: PyInstaller onedir 产物 dist/AutoCat/

!include "MUI2.nsh"
!include "FileFunc.nsh"

; ── 版本信息（由 build_win.py 注入）─────────────
!define PRODUCT_NAME "AutoCat"
!define PRODUCT_VERSION "3.0.1"
!define PRODUCT_PUBLISHER "AutoCat Team"
!define PRODUCT_WEB_SITE "https://github.com/your-repo/AutoCat"
!define PRODUCT_DIR_REGKEY "Software\Microsoft\Windows\CurrentVersion\App Paths\AutoCat.exe"
!define PRODUCT_UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}"
!define PRODUCT_UNINST_ROOT_KEY "HKLM"

; ── 安装程序属性 ───────────────────────────────
Name "${PRODUCT_NAME} ${PRODUCT_VERSION}"
OutFile "dist\AutoCat-${PRODUCT_VERSION}-windows-x86_64.exe"
InstallDir "$PROGRAMFILES64\${PRODUCT_NAME}"
InstallDirRegKey HKLM "${PRODUCT_DIR_REGKEY}" ""
ShowInstDetails show
ShowUnInstDetails show
RequestExecutionLevel admin

; ── 界面设置 ───────────────────────────────────
!define MUI_ABORTWARNING
!define MUI_ICON "${NSISDIR}\Contrib\Graphics\Icons\modern-install.ico"
!define MUI_UNICON "${NSISDIR}\Contrib\Graphics\Icons\modern-uninstall.ico"

; ── 安装页面 ───────────────────────────────────
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "LICENSE"  ; 需在项目根目录放置 LICENSE 文件
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\AutoCat.exe"
!define MUI_FINISHPAGE_RUN_TEXT "启动 AutoCat"
!insertmacro MUI_PAGE_FINISH

; ── 卸载页面 ───────────────────────────────────
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

; ── 语言 ───────────────────────────────────────
!insertmacro MUI_LANGUAGE "SimpChinese"

; ── 安装脚本 ───────────────────────────────────
Section "MainSection" SEC01
    SetOutPath "$INSTDIR"
    SetOverwrite on

    ; 复制 PyInstaller onedir 产物
    File /r "dist\AutoCat\*.*"

    ; 创建开始菜单快捷方式
    CreateDirectory "$SMPROGRAMS\${PRODUCT_NAME}"
    CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk" "$INSTDIR\AutoCat.exe"
    CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\卸载 ${PRODUCT_NAME}.lnk" "$INSTDIR\uninst.exe"

    ; 创建桌面快捷方式
    CreateShortCut "$DESKTOP\${PRODUCT_NAME}.lnk" "$INSTDIR\AutoCat.exe"
SectionEnd

Section -Post
    ; 写注册表（App Paths，让系统找到 exe）
    WriteRegStr HKLM "${PRODUCT_DIR_REGKEY}" "" "$INSTDIR\AutoCat.exe"
    WriteRegStr ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "DisplayName" "$(^Name)"
    WriteRegStr ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "UninstallString" "$INSTDIR\uninst.exe"
    WriteRegStr ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "DisplayIcon" "$INSTDIR\AutoCat.exe"
    WriteRegStr ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "DisplayVersion" "${PRODUCT_VERSION}"
    WriteRegStr ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "Publisher" "${PRODUCT_PUBLISHER}"
    WriteRegStr ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "URLInfoAbout" "${PRODUCT_WEB_SITE}"

    ; 估算安装大小
    ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
    IntFmt $0 "0x%08X" $0
    WriteRegDWORD ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}" "EstimatedSize" "$0"

    ; 生成卸载程序
    WriteUninstaller "$INSTDIR\uninst.exe"
SectionEnd

; ── 卸载脚本 ───────────────────────────────────
Section Uninstall
    ; 删文件
    RMDir /r "$INSTDIR"

    ; 删开始菜单
    RMDir /r "$SMPROGRAMS\${PRODUCT_NAME}"

    ; 删桌面快捷方式
    Delete "$DESKTOP\${PRODUCT_NAME}.lnk"

    ; 删注册表
    DeleteRegKey ${PRODUCT_UNINST_ROOT_KEY} "${PRODUCT_UNINST_KEY}"
    DeleteRegKey HKLM "${PRODUCT_DIR_REGKEY}"

    SetAutoClose true
SectionEnd
