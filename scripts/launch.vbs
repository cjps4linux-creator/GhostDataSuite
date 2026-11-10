' Ghost-Data Suite launcher
Dim shell, batPath, vbsPath, scriptDir
Set shell = CreateObject("WScript.Shell")
scriptDir = shell.CurrentDirectory
vbsPath = shell.ScriptFullName
batPath = Replace(vbsPath, "\launch.vbs", "\scripts\launch.bat")
shell.Run "cmd /c """ & batPath & """", 0, False
