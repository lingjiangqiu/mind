# 通过 GitHub 网页上传完整源码

仓库地址：https://github.com/lingjiangqiu/mind

本次交付不需要 Git 命令。解压“校园心理助手_GitHub网页上传包_2026-11-14.zip”，只上传其中“01_需要上传”内的两个批次。

## 第一批：app（68 个文件）

1. 在仓库根页面选择 **Add file → Upload files**。
2. 打开“01_需要上传/第1批_打开后上传里面的内容”，把里面的 **app 文件夹**拖进上传框。不要把“第1批”文件夹本身拖进去。
3. 等待上传列表加载完成。路径应以 `app/` 开头，例如 `app/main.py`。
4. 提交说明填写 `Upload application source`，提交到 `main`。

## 第二批：配置、文档、技能和测试（36 个文件）

1. 回到仓库根页面，再选择 **Add file → Upload files**。
2. 打开“01_需要上传/第2批_打开后上传里面的内容”，按 Ctrl+A 全选里面的文件和文件夹，一起拖进上传框。不要把“第2批”文件夹本身拖进去。
3. 上传路径应直接是 `README.md`、`skills/...`、`tests/...`、`docs/...`、`.github/workflows/test.yml` 等。以点开头的 `.env.example`、`.gitignore`、`.dockerignore`、`.github` 也需要上传。
4. 提交说明填写 `Upload project configuration docs skills and tests`，提交到 `main`。

GitHub 网页每次最多上传 100 个文件、每个文件不超过 25 MiB，因此完整源码按 68 + 36 分成两次。官方说明：https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository

## 上传完成后检查

- 仓库根目录直接有 `.github`、`app`、`data`、`docs`、`scripts`、`skills`、`tests`，以及 README、依赖、Docker、示例配置等文件。
- README 自动显示项目说明和截图，截图路径为 `docs/demo-desktop.jpg`。
- 不应出现“第1批”“第2批”“01_需要上传”“02_不要上传_本地说明”这些包装目录。
- 压缩包供本地解压使用；不要把 ZIP 当作源码上传，也不用重新上传被排除的缓存或本地 Git 目录。

`.env.example` 是已检查的配置模板，API Key、邮件密码和种子登录密码为空；令牌密钥是明确的待替换占位符。真实 `.env`、私钥、数据库和对话数据不在上传批次内。

源码来源与许可仍按 `SOURCE_PROVENANCE.md` 填写真实情况；来源未确认时保持 Private。GitHub 保存源码，现有服务器提供在线体验；本次网页上传不会改动服务器部署。
