# MapleStoryFarm 项目长期记忆

## 用户约定（必须遵守）

- **每次改动完代码就立刻 `git commit` 一次**，不要攒着等用户说"提交"。
  提交信息用中文，前缀沿用 `feat:` / `fix:` / `style:` / `refactor:` / `memory:`。
  分支是 `dev`，只在本地提交，不主动 push（除非用户要求）。
- 运行时数据文件 `webui/bars.json`、`webui/masks.json` 是被跟踪的，改动一起提交。
- 改完 `webui/index.html` 后，页面有热更新；提醒用户 **Ctrl+F5** 强刷。

## 架构要点

- `webui/server.py`：FastAPI + mss 抓屏。`/stream`（MJPEG，本地 30fps）、
  `/api/snapshot`（单张，手机 /m 用）、`/api/stats`、`/api/bars`、`/api/motion`。
- 页面分工：`webui/index.html` 本地（带屏蔽区编辑、标定）、`webui/m.html` 手机省流版；
  公网访问 `/` 会 302 到 `/m`（`?full=1` 强开主页面）。
- 血条量测：`measure_bar` → `slot_bounds` 找整条槽当分母；`%` 由后端给，
  前端只钳位不要再乘 100。
- 已彻底砍掉 H.264 / HLS / MSE 那条链路，只留 MJPEG。别再引回来。
- 抓屏前提：游戏窗口必须在前台且可见，否则读数会接近 0（`/api/window/focus` 可前置）。
- **游戏进程以管理员身份运行**：普通权限的服务无法 `ShowWindow(SW_RESTORE)` 还原它的
  窗口（UIPI 拦），只能手动点任务栏；要做自动还原就得用管理员身份起服务。
  窗口最小化时 `IsIconic=1`、rect(-32000,-32000)、client 0×0，一切叠加层都不画（没帧）。
