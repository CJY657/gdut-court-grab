# 揭阳校区场馆预约脚本

在放场时刻（默认 10:00:00）自动选场并提交，支持多个账号各抢一个场。

- `grab.py`：全速版，放场瞬间提交。
- `grab_gentle.py`：拟人版，放场后延迟 2~4 秒再提交，三档可选：`balanced`（默认）/ `tight` / `relaxed`。

## 使用

1. `cp config.example.json config.json`，填入各账号登录 ID 和表单信息（`config.json` 已在 .gitignore 中，不会提交）。
2. `python3 grab_gentle.py --check` 只读检查登录态和表单。
3. `python3 grab_gentle.py [--profile balanced|tight|relaxed]` 等到放场时刻自动抢。

更多参数见两个脚本开头的说明。

## 注意

- 每个账号每天只提交一次，请勿重复运行。
- 仅供学习交流，请遵守学校场馆预约规定。
