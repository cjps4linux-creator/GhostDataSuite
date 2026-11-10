' Ghost-Data Suite dashboard launcher
Dim shell, batPath, vbsPath
Set shell = CreateObject("WScript.Shell")
vbsPath = WScript.ScriptFullName
batPath = Replace(vbsPath, "\launch-dashboard.vbs", "\scripts\launch-dashboard.bat")
shell.Run "cmd /c """ & batPath & """", 0, False
