# Windows 脚本调度器

为 Windows 桌面自动化安排任务队列，支持定时执行、进程监控、超时重试和图形化配置。

## 功能

- 队列按时间串行执行，运行期间错过的定时任务结束后补跑。
- 启动并等待、重试组、进程监控、任务清理和可选分辨率检查。
- 区分前台/后台任务；可按真实键鼠活动暂停前台任务，并用热键控制进度。
- Tkinter 配置编辑器及本机浏览器配置工作台。
- 可选 Server 酱失败/汇总通知，运行日志自动清理。

## 安装和配置

Windows 10/11，建议 Python 3.13 x64（包含 Tkinter）。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
.\.venv\Scripts\python.exe config_editor.py
.\.venv\Scripts\python.exe main.py --check
```

示例队列默认不启用，须替换程序路径、设置任务和时间后启用。不同任务的进程匹配要尽量限定路径，避免误结束同名程序。配置保存后重启调度器。

## 运行

```powershell
# 常驻调度
.\.venv\Scripts\python.exe main.py
# 手动执行一次，再进入常驻调度
.\.venv\Scripts\python.exe main.py --run 示例队列
# 可选：浏览器编辑器，需要 Node.js 18+
.\.venv\Scripts\python.exe config_editor_web.py
```

浏览器工作台仅监听本机，Python API 与 Node 前端由启动脚本一并管理。关闭启动脚本即可停止工作台。

默认控制热键：`Ctrl+Alt+=` 暂停/恢复，`Ctrl+Alt+-` 上一个，`Ctrl+Alt+Shift+=` 下一个，`Ctrl+Alt+0` 清零。按配置启用相关功能；后台任务不会因普通键鼠活动自动暂停。

GUI 自动化需要已登录桌面。如果被管理程序以管理员身份运行，调度器也需相应权限。程序会按照配置结束进程和执行清理；先用无关紧要的测试程序验证自己的配置。

## 与其他项目配合

更新器和鸣潮启动器分别发布为独立仓库，可在任务中指定其 Python 解释器和入口路径。调度器不附带这些项目、游戏本体或第三方自动化程序。

## 测试

`.\.venv\Scripts\python.exe -m unittest discover -s tests`

## 隐私、许可与致谢

本项目原创部分采用 [MIT License](LICENSE)。第三方依赖和素材遵守各自许可，详见 [第三方说明](THIRD_PARTY_NOTICES.md)。

感谢 **OpenAI GPT** 与 **DeepSeek** 在开发、排查问题和文档整理中的帮助，详见 [致谢](ACKNOWLEDGEMENTS.md)。本项目为个人工具，与游戏厂商、联想或 AI 模型提供方无官方关联。

真实配置、密钥、日志和截图请留在本机，详见 [隐私说明](PRIVACY.md)。首次发布为源码版本，不包含虚拟环境、游戏文件、驱动或预编译程序。
