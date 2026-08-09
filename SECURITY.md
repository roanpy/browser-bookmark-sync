# Security Policy / 安全策略

## Supported versions / 支持版本

Security fixes target the latest `main` branch and the latest published release, currently `v0.1.1`. Reproduce older-release issues against both the affected release and current `main` before reporting them.

安全修复面向最新 `main` 分支和最新公开版本（当前为 `v0.1.1`）。旧版本问题请同时在受影响版本和当前 `main` 上确认后再报告。

## Reporting a vulnerability / 报告漏洞

Do **not** open a public issue. Use [GitHub private vulnerability reporting](https://github.com/roanpy/browser-bookmark-sync/security/advisories/new). If that option is unavailable, contact the maintainer through the public contact method on the maintainer's GitHub profile and request a private channel without including exploit details.

**不要提交公开 Issue。** 请使用 [GitHub 私密漏洞报告](https://github.com/roanpy/browser-bookmark-sync/security/advisories/new)。如果该入口不可用，请通过维护者 GitHub 主页公开的联系方式仅请求建立私密沟通渠道，不要公开漏洞细节。

Include the affected macOS and browser versions, sync direction, cloud-sync state, impact, and minimal reproduction steps. Use synthetic bookmarks and remove account names, local paths, URLs, backup files, screenshots, and logs that contain private data.

请提供受影响的 macOS 与浏览器版本、同步方向、云同步状态、影响和最小复现步骤。请使用合成书签，并删除账号名、本地路径、真实 URL、备份文件以及包含隐私数据的截图和日志。

Bookmark backup files contain complete browsing data. Never attach them to public or private reports unless the maintainer explicitly requests a separately encrypted minimal fixture.

书签备份包含完整浏览数据。除非维护者明确要求单独加密的最小样例，否则不要将备份附加到公开或私密报告。
