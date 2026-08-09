on bundledToolPath()
	return (POSIX path of (path to me)) & "Contents/Resources/bin/sync-bookmarks"
end bundledToolPath

on backupPath()
	return (POSIX path of (path to downloads folder from user domain)) & "bookmark-sync-backups"
end backupPath

on browserAlias(browserName)
	if browserName is "Chrome" then return "chrome"
	if browserName is "Edge" then return "edge"
	if browserName is "Brave" then return "brave"
	if browserName is "Vivaldi" then return "vivaldi"
	if browserName is "Opera" then return "opera"
	return "safari"
end browserAlias

on compactResult(syncOutput)
	set resultLines to {}
	repeat with outputLine in paragraphs of syncOutput
		set lineText to outputLine as text
		if lineText starts with "Strategy:" or lineText starts with "Result:" or lineText starts with "Verification:" then
			set end of resultLines to lineText
		end if
	end repeat
	if resultLines is {} then return syncOutput
	set AppleScript's text item delimiters to linefeed
	set resultText to resultLines as text
	set AppleScript's text item delimiters to ""
	return resultText
end compactResult

on joinText(itemsToJoin)
	set AppleScript's text item delimiters to "、"
	set joinedText to itemsToJoin as text
	set AppleScript's text item delimiters to ""
	return joinedText
end joinText

on run
	set toolPath to my bundledToolPath()
	set preflight to do shell script quoted form of toolPath & " --list"
	set browserNames to {}
	repeat with browserSpec in {{"Chrome", "chrome:"}, {"Edge", "edge:"}, {"Brave", "brave:"}, {"Vivaldi", "vivaldi:"}, {"Opera", "opera:"}, {"Safari", "safari"}}
		if preflight contains item 2 of browserSpec then set end of browserNames to item 1 of browserSpec
	end repeat
	set sourceChoice to choose from list browserNames with title "书签同步" with prompt "选择书签来源" default items {"Chrome"}
	if sourceChoice is false then return
	set sourceName to item 1 of sourceChoice

	set targetOptions to {}
	repeat with browserName in browserNames
		if (browserName as text) is not sourceName then set end of targetOptions to browserName as text
	end repeat
	set targetChoices to choose from list targetOptions with title "书签同步" with prompt "选择一个或多个目标浏览器" default items targetOptions with multiple selections allowed
	if targetChoices is false or targetChoices is {} then return

	set targetText to my joinText(targetChoices)
	set confirmationText to "来源：" & sourceName & return & "目标：" & targetText & return & return & ¬
		"未开启云同步时将直接同步；检测到云端回灌风险时自动使用安全模式。" & return & ¬
		"安全模式会临时清空目标云端书签，等待云端稳定后再恢复来源书签。" & return & ¬
		"目标书签会先备份，运行中的相关浏览器会自动退出。" & return & return & preflight
	display dialog confirmationText with title "确认书签同步" buttons {"取消", "开始同步"} default button "开始同步" cancel button "取消"

	set syncCommand to quoted form of toolPath & " --auto-close --allow-cloud-purge --from " & my browserAlias(sourceName) & " --to"
	repeat with targetName in targetChoices
		set syncCommand to syncCommand & " " & my browserAlias(targetName as text)
	end repeat

	display notification "正在同步，请等待云端状态稳定。" with title "书签同步"
	try
		with timeout of 900 seconds
			set syncOutput to do shell script syncCommand
		end timeout
	on error errorMessage number errorNumber
		display dialog "同步失败（" & errorNumber & "）" & return & return & errorMessage with title "书签同步" buttons {"关闭"} default button "关闭" with icon stop
		return
	end try

	set resultChoice to display dialog "同步完成" & return & return & my compactResult(syncOutput) with title "书签同步" buttons {"完成", "打开备份目录"} default button "完成"
	if button returned of resultChoice is "打开备份目录" then do shell script "open " & quoted form of my backupPath()
end run
