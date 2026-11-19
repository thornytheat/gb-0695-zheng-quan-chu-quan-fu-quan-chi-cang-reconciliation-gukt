# 接口

`POST /api/run`：JSON中的csv文本，省略使用样例；或multipart字段file。最大5MB。

`POST /api/m1`至`POST /api/m7`：独立运行到指定模块，JSON结构同运行接口。

`GET /api/history`：历史批次。`POST /api/review`接收id、revision和decision（通过或退回）。`GET /api/evidence.csv`提供单一证据导出。

结构错误422，版本变化409，文件过大413；错误返回code与message。所有页面可从左侧七个模块入口访问。

`GET /api/runs/<id>`读取指定批次；页面与证据CSV通过run_id查询参数绑定批次，模块导航保留该参数。

JSON请求体必须是对象，数组、标量、null、损坏JSON及错误上传字段返回400，且不运行默认样例。

## 可转债转股（前缀 `/conv`）

独立的转股复核工作流，规则与接口见 [CONV.md](CONV.md)。页面入口 `/conv/`；
`POST /conv/api/run` 计算，`POST /conv/api/c1`～`/conv/api/c5` 分阶段运行，
`POST /conv/api/review` 复核、`POST /conv/api/publish` 发布（对账相符且复核通过才放行），
`GET /conv/api/evidence.csv` 导出证据。数据存于 `CONV_DB`，与主工具的 SQLite 分离。
