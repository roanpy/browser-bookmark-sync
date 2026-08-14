on bundledToolPath()
	return (POSIX path of (path to me)) & "Contents/Resources/bin/sync-bookmarks"
end bundledToolPath

on pythonPath()
	repeat with candidate in {"/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/Library/Frameworks/Python.framework/Versions/Current/bin/python3", "/usr/bin/python3"}
		try
			do shell script "test -x " & quoted form of (candidate as text) & " && " & quoted form of (candidate as text) & " -c " & quoted form of "import sys; raise SystemExit(sys.version_info < (3, 11))"
			return candidate as text
		end try
	end repeat
	try
		set candidate to do shell script "/bin/zsh -lc " & quoted form of "command -v python3"
		do shell script quoted form of candidate & " -c " & quoted form of "import sys; raise SystemExit(sys.version_info < (3, 11))"
		return candidate
	on error
		error "Python 3.11 or newer is required. Install Python, then reopen Bookmark Sync."
	end try
end pythonPath

on toolCommand(pythonPath)
	return quoted form of pythonPath & " " & quoted form of my bundledToolPath()
end toolCommand

on backupPath()
	return (POSIX path of (path to downloads folder from user domain)) & "bookmark-sync-backups"
end backupPath

on displayName(storeId)
	if storeId is "safari" then return "Safari"
	set separatorOffset to offset of ":" in storeId
	set browserId to text 1 thru (separatorOffset - 1) of storeId
	set profileName to text (separatorOffset + 1) thru -1 of storeId
	set browserName to browserId
	if browserId is "chrome" then set browserName to "Chrome"
	if browserId is "edge" then set browserName to "Edge"
	if browserId is "brave" then set browserName to "Brave"
	if browserId is "vivaldi" then set browserName to "Vivaldi"
	if browserId is "opera" then set browserName to "Opera"
	return browserName & "（" & profileName & "）"
end displayName

on detectedStores(toolCommand, pythonPath)
	set parserCode to "import json,sys; print('\\n'.join(item['id'] for item in json.load(sys.stdin)['stores']))"
	set storeText to do shell script toolCommand & " --list --json | " & quoted form of pythonPath & " -c " & quoted form of parserCode
	return paragraphs of storeText
end detectedStores

on storeIdForDisplay(chosenName, displayNames, storeIds)
	repeat with itemIndex from 1 to count displayNames
		if item itemIndex of displayNames is chosenName then return item itemIndex of storeIds
	end repeat
	error "Selected browser profile is no longer available."
end storeIdForDisplay

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
	set pythonPath to my pythonPath()
	set toolCommand to my toolCommand(pythonPath)
	set preflight to do shell script toolCommand & " --list"
	set storeIds to my detectedStores(toolCommand, pythonPath)
	if (count storeIds) < 2 then error "At least two browser bookmark profiles are required."
	set displayNames to {}
	repeat with storeId in storeIds
		set end of displayNames to my displayName(storeId as text)
	end repeat
	set sourceChoice to choose from list displayNames with title "书签同步" with prompt "选择书签来源（含浏览器配置）" default items {item 1 of displayNames}
	if sourceChoice is false then return
	set sourceName to item 1 of sourceChoice
	set sourceId to my storeIdForDisplay(sourceName, displayNames, storeIds)

	set targetOptions to {}
	repeat with displayItem in displayNames
		if (displayItem as text) is not sourceName then set end of targetOptions to displayItem as text
	end repeat
	set targetChoices to choose from list targetOptions with title "书签同步" with prompt "选择一个或多个目标浏览器" default items targetOptions with multiple selections allowed
	if targetChoices is false or targetChoices is {} then return

	set targetText to my joinText(targetChoices)
	set confirmationText to "来源：" & sourceName & return & "目标：" & targetText & return & return & ¬
		"未开启云同步时将直接同步；检测到云端回灌风险时自动使用安全模式。" & return & ¬
		"安全模式会临时清空目标云端书签，等待云端稳定后再恢复来源书签。" & return & ¬
		"目标书签会先备份，运行中的相关浏览器会自动退出。" & return & return & preflight
	display dialog confirmationText with title "确认书签同步" buttons {"取消", "开始同步"} default button "开始同步" cancel button "取消"

	set syncCommand to toolCommand & " --auto-close --allow-cloud-purge --from " & quoted form of sourceId & " --to"
	repeat with targetName in targetChoices
		set targetId to my storeIdForDisplay(targetName as text, displayNames, storeIds)
		set syncCommand to syncCommand & " " & quoted form of targetId
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
