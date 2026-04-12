; start.ahk — System tray launcher for the Local Speech Dictation System
; AutoHotkey v1 syntax (matches installed version)
;
; This script manages the Python process lifecycle only.
; The Ctrl+` toggle hotkey is handled by Python (keyboard library),
; NOT by this script — to avoid hotkey conflicts.
;
; Usage: Double-click start.ahk (or right-click → Run Script)

#NoEnv
#SingleInstance Force
SendMode Input
SetWorkingDir %A_ScriptDir%\..

; --- Configuration ---
PYTHON_EXE := "python"
SCRIPT_PATH := A_ScriptDir . "\..\src\main.py"
PID := 0

; --- Tray menu ---
Menu, Tray, NoStandard
Menu, Tray, Add, Start Dictation System, StartSystem
Menu, Tray, Add, Stop Dictation System, StopSystem
Menu, Tray, Add, Restart Dictation System, RestartSystem
Menu, Tray, Add
Menu, Tray, Add, Exit, ExitScript
Menu, Tray, Tip, Local Speech Dictation (stopped)
Menu, Tray, Icon

; Auto-start on launch
Gosub, StartSystem
Return

; ============================================================
StartSystem:
    if (PID > 0) {
        MsgBox, 48, Already Running, The dictation system is already running (PID=%PID%).
        Return
    }
    Run, %PYTHON_EXE% "%SCRIPT_PATH%",, , PID
    if (ErrorLevel || PID = 0) {
        MsgBox, 16, Launch Error, Failed to start Python.`n`nMake sure Python is in PATH and the conda environment is activated.
        PID := 0
        Return
    }
    Menu, Tray, Tip, Local Speech Dictation (running)
    ToolTip, Dictation system started.`nPress Ctrl+` to toggle listening.
    SetTimer, ClearTooltip, 3000
    Return

; ============================================================
StopSystem:
    if (PID = 0) {
        MsgBox, 48, Not Running, The dictation system is not currently running.
        Return
    }
    Process, Close, %PID%
    PID := 0
    Menu, Tray, Tip, Local Speech Dictation (stopped)
    ToolTip, Dictation system stopped.
    SetTimer, ClearTooltip, 2000
    Return

; ============================================================
RestartSystem:
    Gosub, StopSystem
    Sleep, 500
    Gosub, StartSystem
    Return

; ============================================================
ExitScript:
    Gosub, StopSystem
    ExitApp

; ============================================================
ClearTooltip:
    SetTimer, ClearTooltip, Off
    ToolTip
    Return
